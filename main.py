"""SAMP Messenger FastAPI server + Telegram webhook relay.

Render environment:
  TELEGRAM_BOT_TOKEN  - BotFather token, secret
  PUBLIC_URL          - exact HTTPS URL of this Render service (no trailing slash)
  TELEGRAM_CHAT_ID    - destination Telegram chat ID; obtain by DMing /id to the bot

No additional Telegram libraries are required.
"""
from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
from queue import Empty, Full, Queue
from threading import RLock, Thread
from time import monotonic, sleep, process_time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import json
import logging
import os
import re
import sqlite3

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("samp_messenger")
app = FastAPI(title="SA:MP Messenger", version="0.8-tg-receipts-gps-1s-cuffs")
lock = RLock()
clients = {}  # token -> {nick, last_seen, last_message}
messages = deque(maxlen=200)
# In-memory deduplication of acknowledged SA:MP sends; protects against
# duplicated delivery when the client retries after a lost HTTP response.
seen_game_messages = {}  # (nick_lower, message_id) -> previous message id
seen_game_order = deque()
MAX_DEDUP = 2500
# Delivery receipt statuses are kept in process memory; unlike a generic /send HTTP
# acknowledgement, 'delivered' means Telegram's sendMessage returned ok=true.
tg_delivery = {}  # (nick_lower, message_id) -> {status, error, updated}


next_id = 0
SESSION_TIMEOUT = 45
GPS_TIMEOUT = 12  # seconds without GPS updates -> remove stale marker
GPS_MAX_PLAYERS = 40  # keep within a conservative GTA marker budget
MIN_MESSAGE_INTERVAL = 0.35
NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Stable secret is never sent to players or exposed through game endpoints.
WEBHOOK_SECRET = sha256(("samp-tg-hook:" + BOT_TOKEN).encode()).hexdigest() if BOT_TOKEN else ""
tg_outgoing = Queue(maxsize=200)
processed_updates = deque(maxlen=300)
processed_set = set()

# Registration is stored in SQLite. Render Free uses an ephemeral filesystem:
# set REGISTRY_DB_PATH to a persistent mounted disk to retain registrations
# across deploys/restarts (Render's free tier does not offer persistent disks).
REGISTRY_DB_PATH = os.getenv("REGISTRY_DB_PATH", "telegram_users.sqlite3")


def registration_connection():
    conn = sqlite3.connect(REGISTRY_DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS telegram_users (
        user_id INTEGER PRIMARY KEY,
        nick TEXT NOT NULL COLLATE NOCASE UNIQUE,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    return conn


def registered_nick(user_id):
    with registration_connection() as conn:
        row = conn.execute(
            "SELECT nick FROM telegram_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else None


def register_nick(user_id, nick):
    # A nick can belong to only one Telegram account. A user can change their nick.
    with registration_connection() as conn:
        existing = conn.execute(
            "SELECT user_id FROM telegram_users WHERE nick = ? COLLATE NOCASE", (nick,)
        ).fetchone()
        if existing and existing[0] != user_id:
            return False
        conn.execute("""INSERT INTO telegram_users(user_id, nick)
            VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET
            nick = excluded.nick, updated_at = CURRENT_TIMESTAMP""", (user_id, nick))
        return True


class JoinRequest(BaseModel):
    nick: str


class SendRequest(BaseModel):
    token: str
    text: str = Field(min_length=1, max_length=500)
    # Optional for compatibility with existing 1.0.4 clients.
    message_id: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class TokenRequest(BaseModel):
    token: str


class GpsUpdateRequest(BaseModel):
    token: str
    # Users may disable location broadcasting entirely without affecting chat.
    share: bool = True
    cuffed: bool = False  # received from server-side SA-MP action/handcuff attachment
    x: float | None = Field(default=None, ge=-5000, le=5000, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=-5000, le=5000, allow_inf_nan=False)
    z: float | None = Field(default=None, ge=-2500, le=5000, allow_inf_nan=False)


def cleanup(now):
    expired = [key for key, entry in clients.items() if now - entry["last_seen"] > SESSION_TIMEOUT]
    for key in expired:
        del clients[key]


def require_client(token, now):
    entry = clients.get(token)
    if entry is None:
        raise HTTPException(status_code=401, detail="Session expired, reconnect")
    entry["last_seen"] = now
    return entry


def append_message(nick: str, text: str, source: str):
    """Call while lock is held. Telegram messages carry source='telegram'."""
    global next_id
    next_id += 1
    item = {
        "id": next_id,
        "nick": nick,
        "text": text,
        "time": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    messages.append(item)
    return item


def api_request(method: str, payload: dict):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise ValueError("Telegram API returned ok=false")
    return data


def update_delivery(key, status: str, error: str = ""):
    if key is None:
        return
    with lock:
        tg_delivery[key] = {"status": status, "error": error[:120], "updated": monotonic()}


def enqueue_tg(chat_id, text: str, reply_to_message_id=None, delivery_key=None):
    if not BOT_TOKEN or not chat_id:
        update_delivery(delivery_key, "unavailable", "Telegram bot or group is not configured")
        return False
    try:
        tg_outgoing.put_nowait((chat_id, text[:3900], reply_to_message_id, delivery_key))
        return True
    except Full:
        update_delivery(delivery_key, "failed", "Telegram queue is full")
        log.warning("Telegram outgoing queue full")
        return False


def telegram_worker():
    while True:
        chat_id, text, reply_id, delivery_key = tg_outgoing.get()
        try:
            payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            if isinstance(reply_id, int):
                payload["reply_parameters"] = {"message_id": reply_id, "allow_sending_without_reply": True}
            # Retry transient API outages. The local Lua queue is kept until Telegram
            # confirms acceptance; a failure here must NOT count as delivered.
            for attempt in range(4):
                try:
                    api_request("sendMessage", payload)
                    update_delivery(delivery_key, "delivered")
                    break
                except (HTTPError, URLError, ValueError, TimeoutError) as exc:
                    if attempt == 3 or (isinstance(exc, HTTPError) and exc.code in (400, 401, 403, 404)):
                        update_delivery(delivery_key, "failed", "Telegram API error: " + type(exc).__name__)
                        log.warning("Telegram send failed (%s)", type(exc).__name__)
                        break
                    update_delivery(delivery_key, "retrying", "Telegram temporarily unavailable")
                    sleep(min(12, 2 ** (attempt + 1)))
        finally:
            tg_outgoing.task_done()


def configure_webhook():
    if not BOT_TOKEN:
        log.info("Telegram disabled: TELEGRAM_BOT_TOKEN is missing")
        return
    if not PUBLIC_URL.startswith("https://"):
        log.warning("Telegram webhook not configured: set PUBLIC_URL to your Render HTTPS URL")
        return
    try:
        api_request("setWebhook", {
            "url": PUBLIC_URL + "/telegram/webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        })
        log.info("Telegram webhook registered")
    except (HTTPError, URLError, ValueError, TimeoutError) as exc:
        log.warning("Telegram webhook registration failed (%s)", type(exc).__name__)




# ===============================================================
# Read-only performance dashboard. This does NOT modify chat/GPS
# handlers or their response formats. Samples are kept in memory.
# No extra Python packages or Render environment variables needed.
# ===============================================================
DASHBOARD_SAMPLE_SECONDS = 5
DASHBOARD_HISTORY_LIMIT = 720  # 60 minutes at 5-second intervals
DASHBOARD_STARTED = monotonic()
metrics_lock = RLock()
metrics_history = deque(maxlen=DASHBOARD_HISTORY_LIMIT)
metrics_window = {"total": 0, "gps": 0, "errors": 0, "latency_ms": 0.0}
metrics_total_requests = 0
metrics_sample_time = monotonic()
metrics_last_cpu_at = monotonic()
metrics_last_cpu_value = None
metrics_last_cpu_source = None
metrics_latest = {
    "at": datetime.now(timezone.utc).isoformat(),
    "cpu_percent": None, "cpu_reference": None,
    "memory_mb": None, "memory_limit_mb": None,
    "players_online": 0, "requests_per_sec": 0.0,
    "gps_per_sec": 0.0, "avg_response_ms": 0.0,
    "errors_per_sec": 0.0,
}


def _read_one_line(path):
    try:
        with open(path, "r", encoding="ascii") as fh:
            return fh.readline().strip()
    except (OSError, ValueError):
        return None


def _cgroup_cpu():
    """Return (CPU seconds used, allowed CPU cores, description)."""
    # cgroup v2: cpu.stat / cpu.max
    try:
        with open('/sys/fs/cgroup/cpu.stat', encoding='ascii') as fh:
            for line in fh:
                if line.startswith('usage_usec '):
                    used = float(line.split()[1]) / 1_000_000
                    limit = _read_one_line('/sys/fs/cgroup/cpu.max')
                    cores = None
                    if limit:
                        quota, period = limit.split()[:2]
                        if quota != 'max':
                            cores = float(quota) / float(period)
                    return used, cores, 'container'
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        pass
    # Fallback: process CPU time. Render may not expose CPU cgroup files.
    return process_time(), None, 'process'


def _memory_info():
    """Return used and limit in MB. Prefer whole-container memory."""
    used = _read_one_line('/sys/fs/cgroup/memory.current')
    limit = _read_one_line('/sys/fs/cgroup/memory.max')
    if used is None:
        used = _read_one_line('/sys/fs/cgroup/memory/memory.usage_in_bytes')
        limit = _read_one_line('/sys/fs/cgroup/memory/memory.limit_in_bytes')
    if used is not None:
        try:
            value = int(used) / (1024 * 1024)
            maximum = int(limit) / (1024 * 1024) if limit and limit != 'max' else None
            # Ignore fake unlimited cgroup v1 limits.
            if maximum and maximum > 1_000_000:
                maximum = None
            return round(value, 2), round(maximum, 2) if maximum else None
        except (ValueError, OverflowError):
            pass
    # Last resort: RSS of this Python process.
    try:
        with open('/proc/self/status', encoding='ascii') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    return round(int(line.split()[1]) / 1024, 2), None
    except (OSError, ValueError, IndexError):
        pass
    return None, None


def _sample_metrics():
    global metrics_sample_time, metrics_last_cpu_at
    global metrics_last_cpu_value, metrics_last_cpu_source
    now = monotonic()
    cpu_value, cpu_cores, cpu_source = _cgroup_cpu()
    if (metrics_last_cpu_value is not None and
            cpu_source == metrics_last_cpu_source and
            now > metrics_last_cpu_at):
        used_cores = max(0.0, cpu_value - metrics_last_cpu_value) / (now - metrics_last_cpu_at)
        cpu_percent = round(100.0 * used_cores / (cpu_cores or 1.0), 2)
        cpu_reference = ('CPU limit' if cpu_cores else '1 CPU core (estimated)')
    else:
        cpu_percent = None
        cpu_reference = ('CPU limit' if cpu_cores else '1 CPU core (estimated)')
    metrics_last_cpu_at = now
    metrics_last_cpu_value = cpu_value
    metrics_last_cpu_source = cpu_source
    memory_mb, memory_limit_mb = _memory_info()
    with lock:
        cleanup(now)
        players_online = len(clients)
    with metrics_lock:
        elapsed = max(0.1, now - metrics_sample_time)
        counts = metrics_window.copy()
        metrics_window.update(total=0, gps=0, errors=0, latency_ms=0.0)
        metrics_sample_time = now
        point = {
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'cpu_percent': cpu_percent,
            'memory_mb': memory_mb,
            'requests_per_sec': round(counts['total'] / elapsed, 2),
            'gps_per_sec': round(counts['gps'] / elapsed, 2),
            'avg_response_ms': round(counts['latency_ms'] / counts['total'], 1) if counts['total'] else 0.0,
            'errors_per_sec': round(counts['errors'] / elapsed, 2),
            'players_online': players_online,
        }
        metrics_history.append(point)
        metrics_latest.update(point)
        metrics_latest['cpu_reference'] = cpu_reference
        metrics_latest['memory_limit_mb'] = memory_limit_mb


def _metrics_sampling_loop():
    # Exceptions must never prevent the chat, GPS or Telegram from working.
    while True:
        sleep(DASHBOARD_SAMPLE_SECONDS)
        try:
            _sample_metrics()
        except Exception as exc:
            log.warning('Performance metrics sampling failed: %s', type(exc).__name__)


@app.middleware('http')
async def measure_requests(request: Request, call_next):
    global metrics_total_requests
    # Do not count the dashboard polling itself as application traffic.
    if request.url.path.startswith('/metrics'):
        return await call_next(request)
    began = monotonic()
    try:
        response = await call_next(request)
    except Exception:
        with metrics_lock:
            metrics_window['total'] += 1
            metrics_total_requests += 1
            metrics_window['errors'] += 1
            metrics_window['latency_ms'] += (monotonic() - began) * 1000
        raise
    with metrics_lock:
        metrics_window['total'] += 1
        metrics_total_requests += 1
        if request.url.path.startswith('/gps/'):
            metrics_window['gps'] += 1
        if response.status_code >= 500:
            metrics_window['errors'] += 1
        metrics_window['latency_ms'] += (monotonic() - began) * 1000
    return response


METRICS_PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SAMP Messenger — Нагрузка сервера</title>
<style>
:root{font-family:Inter,Segoe UI,Arial,sans-serif;color:#ecf3ff;background:#0d1422;color-scheme:dark}
*{box-sizing:border-box}body{margin:0}main{max-width:1250px;margin:auto;padding:27px 22px 54px}
header{display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}
h1{font-size:26px;margin:0;font-weight:720}p{color:#93a7c3;line-height:1.5}
.status{display:flex;align-items:center;gap:8px;color:#84e4b3;font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:#53dc9d;box-shadow:0 0 12px #53dc9d80}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:23px 0}
.card,.panel{background:#151f30;border:1px solid #2b394c;border-radius:14px;padding:17px}
.card .label{font-size:13px;color:#a4b2c8}.card .value{font-size:28px;font-weight:760;margin-top:10px}
.card .sub{font-size:12px;color:#8094af;margin-top:5px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.panel h2{font-size:15px;font-weight:650;margin:0 0 15px}.plot{height:205px;width:100%;display:block}
footer{font-size:13px;color:#8e9eb7;margin-top:22px;line-height:1.6}
.error{color:#ffbb86}code{font-family:ui-monospace,Consolas,monospace}
@media(max-width:840px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.charts{grid-template-columns:1fr}}
@media(max-width:470px){.cards{grid-template-columns:1fr 1fr;gap:8px}.card{padding:12px}.card .value{font-size:22px}main{padding:18px 12px}}
</style></head><body><main>
<header><div><h1>SAMP Messenger — нагрузка сервера</h1><p>Статистика сервера Render · обновляется раз в 5 секунд</p></div>
<div class="status"><span class="dot"></span><span id="online-status">Подключение...</span></div></header>
<section class="cards">
<div class="card"><div class="label">Игроков онлайн</div><div id="players" class="value">—</div><div class="sub">Активных клиентов Messenger</div></div>
<div class="card"><div class="label">CPU</div><div id="cpu" class="value">—</div><div id="cpu-sub" class="sub">Измерение CPU</div></div>
<div class="card"><div class="label">RAM</div><div id="memory" class="value">—</div><div id="memory-sub" class="sub">Оперативная память</div></div>
<div class="card"><div class="label">Запросы/сек</div><div id="rps" class="value">—</div><div id="gps-rps" class="sub">GPS: — /сек</div></div>
</section>
<section class="charts">
<div class="panel"><h2>Загрузка CPU (%)</h2><canvas id="chart-cpu" class="plot"></canvas></div>
<div class="panel"><h2>Память (МБ)</h2><canvas id="chart-memory" class="plot"></canvas></div>
<div class="panel"><h2>Запросы в секунду · GPS отдельно</h2><canvas id="chart-rps" class="plot"></canvas></div>
<div class="panel"><h2>Среднее время ответа (мс)</h2><canvas id="chart-latency" class="plot"></canvas></div>
</section>
<footer><span id="meta">Собираем данные...</span><p>Графики хранят последние 60 минут работы процесса. После перезапуска или обновления Render данные обнуляются. CPU может оцениваться относительно одного ядра, если контейнер не сообщает свой лимит. Панель открыта по ссылке всем, кому известен адрес.</p></footer>
</main>
<script>
'use strict';
const $=id=>document.getElementById(id);
const n=(v,d=1)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(d):'—';
const fmt=(s)=>{const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+' ч '+m+' мин'};
function linePlot(id,history,fields,unit,colors,limit){
 const canvas=$(id),ratio=window.devicePixelRatio||1,rect=canvas.getBoundingClientRect();
 const w=Math.max(180,rect.width),h=Math.max(140,rect.height);
 canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio);
 const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);
 const margin={l:48,r:15,t:12,b:26},pw=w-margin.l-margin.r,ph=h-margin.t-margin.b;
 ctx.clearRect(0,0,w,h);
 const valid=history.flatMap(p=>fields.map(f=>p[f]).filter(v=>typeof v==='number'&&isFinite(v)));
 const ymax=Math.max(limit||0,valid.length?Math.max(...valid)*1.15:1,0.01);
 ctx.lineWidth=1;ctx.font='11px Segoe UI,Arial,sans-serif';ctx.textAlign='right';
 for(let i=0;i<=4;i++){let value=ymax*(1-i/4),y=margin.t+i*ph;
   ctx.strokeStyle='#2b3b53';ctx.beginPath();ctx.moveTo(margin.l,y);ctx.lineTo(w-margin.r,y);ctx.stroke();
   ctx.fillStyle='#8497b2';ctx.fillText(value>=100?value.toFixed(0):value.toFixed(1),margin.l-9,y+4)}
 if(history.length>0){ctx.textAlign='left';ctx.fillStyle='#8497b2';
   const first=new Date(history[0].at),last=new Date(history[history.length-1].at);
   const stamp=d=>d.toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
   ctx.fillText(stamp(first),margin.l,h-6);ctx.textAlign='right';ctx.fillText(stamp(last),w-margin.r,h-6)}
 for(let f=0;f<fields.length;f++){
   const key=fields[f];ctx.beginPath();let began=false;
   const points=history.length;
   for(let i=0;i<points;i++){
     const val=history[i][key];if(typeof val!=='number'||!isFinite(val)){began=false;continue}
     const x=margin.l+(points<=1?pw:(i/(points-1))*pw);
     const y=margin.t+ph-Math.max(0,Math.min(val,ymax))/ymax*ph;
     if(!began){ctx.moveTo(x,y);began=true}else ctx.lineTo(x,y)
   }
   ctx.strokeStyle=colors[f];ctx.lineWidth=2.3;ctx.stroke()
 }
 ctx.textAlign='right';ctx.fillStyle='#8d9eb8';ctx.font='11px Segoe UI,Arial,sans-serif';ctx.fillText(unit,w-margin.r,margin.t+11);
}
async function refresh(){
 try{
  const response=await fetch('/metrics/data',{cache:'no-store'});
  if(!response.ok)throw new Error('HTTP '+response.status);
  const data=await response.json(),cur=data.current||{},hist=data.history||[];
  $('online-status').textContent='Сервер онлайн';$('online-status').className='';
  $('players').textContent=cur.players_online??'—';
  $('cpu').textContent=n(cur.cpu_percent)+'%';$('cpu-sub').textContent=cur.cpu_reference||'Нет данных';
  $('memory').textContent=n(cur.memory_mb)+' МБ';
  $('memory-sub').textContent=cur.memory_limit_mb?'Лимит: '+n(cur.memory_limit_mb,0)+' МБ':'Лимит памяти не определён';
  $('rps').textContent=n(cur.requests_per_sec,1);$('gps-rps').textContent='GPS: '+n(cur.gps_per_sec,1)+' /сек';
  $('meta').textContent='Работает: '+fmt(data.uptime_seconds||0)+' · Всего запросов: '+(data.total_requests||0)+' · Время ответа: '+n(cur.avg_response_ms,1)+' мс · Последнее обновление: '+(cur.at||'—');
  linePlot('chart-cpu',hist,['cpu_percent'],'%', ['#55b9ff'],100);
  linePlot('chart-memory',hist,['memory_mb'],'МБ',['#c38bff'],cur.memory_limit_mb||0);
  linePlot('chart-rps',hist,['requests_per_sec','gps_per_sec'],'/сек',['#51d6af','#f5b962'],0);
  linePlot('chart-latency',hist,['avg_response_ms'],'мс',['#ff8f91'],0);
 }catch(err){$('online-status').textContent='Ошибка загрузки: '+err.message;$('online-status').className='error'}
}
refresh();setInterval(refresh,5000);window.addEventListener('resize',()=>{refresh()});
</script></body></html>"""


@app.get('/metrics', response_class=HTMLResponse)
def metrics_page():
    return HTMLResponse(METRICS_PAGE, headers={'Cache-Control': 'no-store'})


@app.get('/metrics/data')
def metrics_data():
    with metrics_lock:
        snapshot = dict(metrics_latest)
        samples = list(metrics_history)
        # Accounted for even if the sampler has not yet ticked.
        total = metrics_total_requests
    return JSONResponse({
        'ok': True, 'uptime_seconds': round(monotonic() - DASHBOARD_STARTED),
        'total_requests': total, 'current': snapshot, 'history': samples,
    }, headers={'Cache-Control': 'no-store'})


@app.on_event("startup")
def setup_telegram():
    # Start read-only metrics sampler in the background.
    Thread(target=_metrics_sampling_loop, daemon=True, name="performance-metrics").start()
    # Create the registration DB before webhooks start arriving.
    try:
        with registration_connection():
            pass
    except sqlite3.Error as exc:
        log.error("Telegram registration database unavailable: %s", type(exc).__name__)
    # Webhook registration runs outside the HTTP request loop.
    if BOT_TOKEN:
        Thread(target=telegram_worker, daemon=True, name="telegram-sender").start()
        Thread(target=configure_webhook, daemon=True, name="telegram-webhook-setup").start()


@app.get("/")
def root():
    with lock:
        cleanup(monotonic())
        return {
            "status": "online", "name": "SAMP Messenger",
            "version": "0.8-tg-receipts-gps-1s-cuffs", "online": len(clients),
            "telegram_enabled": bool(BOT_TOKEN and TG_CHAT_ID),
        }


@app.post("/join")
def join(data: JoinRequest):
    nick = data.nick.strip()
    if not NICK_RE.fullmatch(nick):
        raise HTTPException(status_code=400, detail="Nick must be 3-24 letters, digits or underscores")
    with lock:
        now = monotonic()
        cleanup(now)
        if any(entry["nick"].lower() == nick.lower() for entry in clients.values()):
            raise HTTPException(status_code=409, detail="Nickname already online")
        token = uuid4().hex
        clients[token] = {"nick": nick, "last_seen": now, "last_message": 0.0}
        return {"ok": True, "token": token, "nick": nick, "interval": 0.25, "last_id": next_id}


@app.get("/state")
def state(token: str, after: int = Query(default=0, ge=0)):
    with lock:
        now = monotonic()
        cleanup(now)
        require_client(token, now)
        return {
            "online": sorted((entry["nick"] for entry in clients.values()), key=str.lower),
            "messages": [msg for msg in messages if msg["id"] > after],
            "last_id": next_id,
        }


@app.post("/send")
def send(data: SendRequest):
    content = data.text.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Empty message")
    with lock:
        now = monotonic()
        cleanup(now)
        entry = require_client(data.token, now)
        key = (entry["nick"].lower(), data.message_id) if data.message_id else None
        if key and key in seen_game_messages:
            previous_id = seen_game_messages[key]
            receipt = tg_delivery.get(key)
            # A previous Telegram failure can be retried without creating a second
            # game message. Do not requeue while another attempt is in flight.
            if receipt and receipt["status"] == "failed" and now - receipt["updated"] >= 10:
                tg_delivery[key] = {"status": "queued", "error": "", "updated": now}
                enqueue_tg(TG_CHAT_ID, f"🎮 [G] {entry['nick']}: {content}", delivery_key=key)
            return {"ok": True, "id": previous_id, "duplicate": True}
        if now - entry["last_message"] < MIN_MESSAGE_INTERVAL:
            raise HTTPException(status_code=429, detail="Please wait before sending again")
        entry["last_message"] = now
        item = append_message(entry["nick"], content, "game")
        if key:
            seen_game_messages[key] = item["id"]
            seen_game_order.append(key)
            tg_delivery[key] = {"status": "queued", "error": "", "updated": now}
            if len(seen_game_order) > MAX_DEDUP:
                old = seen_game_order.popleft()
                seen_game_messages.pop(old, None)
                old_receipt = tg_delivery.get(old)
                if old_receipt and old_receipt["status"] == "delivered":
                    tg_delivery.pop(old, None)
    enqueue_tg(TG_CHAT_ID, f"🎮 [G] {item['nick']}: {content}", delivery_key=key)
    return {"ok": True, "id": item["id"], "duplicate": False}


@app.get("/delivery")
def delivery(token: str, message_id: str = Query(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")):
    with lock:
        now = monotonic()
        cleanup(now)
        entry = require_client(token, now)
        receipt = tg_delivery.get((entry["nick"].lower(), message_id))
        if receipt is None:
            return {"ok": True, "status": "unknown"}
        return {"ok": True, "status": receipt["status"], "error": receipt["error"]}


# Optional GPS extension. Chat and Telegram routes above remain unmodified.
# A valid session token is needed to publish/receive positions. No public GPS list.
@app.post("/gps/update")
def gps_update(data: GpsUpdateRequest):
    with lock:
        now = monotonic()
        cleanup(now)
        entry = require_client(data.token, now)
        if not data.share:
            entry.pop("gps", None)
            return {"ok": True, "sharing": False}
        if data.x is None or data.y is None or data.z is None:
            raise HTTPException(status_code=400, detail="Missing GPS coordinates")
        entry["gps"] = {"x": data.x, "y": data.y, "z": data.z, "cuffed": data.cuffed, "last_update": now}
        return {"ok": True, "sharing": True}


@app.get("/gps/list")
def gps_list(token: str):
    with lock:
        now = monotonic()
        cleanup(now)
        own = require_client(token, now)
        players = []
        for candidate in clients.values():
            if candidate is own:
                continue
            point = candidate.get("gps")
            if point is None or now - point["last_update"] > GPS_TIMEOUT:
                continue
            players.append({"nick": candidate["nick"], "x": point["x"],
                            "y": point["y"], "z": point["z"], "cuffed": point.get("cuffed", False)})
        players.sort(key=lambda item: item["nick"].lower())
        return {"ok": True, "players": players[:GPS_MAX_PLAYERS]}


@app.post("/leave")
def leave(data: TokenRequest):
    with lock:
        clients.pop(data.token, None)
    return {"ok": True}


def seen_update(update_id):
    """Telegram retries webhook requests. Avoid posting the same chat message twice."""
    with lock:
        if update_id in processed_set:
            return True
        if len(processed_updates) == processed_updates.maxlen:
            old = processed_updates.popleft()
            processed_set.discard(old)
        processed_updates.append(update_id)
        processed_set.add(update_id)
        return False


def pretty_sender(user: dict) -> str:
    name = user.get("first_name") or user.get("username") or "Telegram"
    # Strip line breaks / special formatting and bound game chat length.
    name = re.sub(r"[\r\n\t]+", " ", str(name)).strip()
    return name[:40] or "Telegram"


@app.post("/telegram/webhook")
def telegram_webhook(update: dict, x_telegram_bot_api_secret_token: str = Header(default="")):
    if not BOT_TOKEN or not compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return {"ok": True}
    if seen_update(update_id):
        return {"ok": True}

    message = update.get("message") or {}
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return {"ok": True}
    if message.get("from", {}).get("is_bot"):
        return {"ok": True}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = message["text"].strip()
    if not text:
        return {"ok": True}

    # /id works before authorizing a Telegram chat, to simplify setup.
    command = text.split(" ", 1)[0].split("@", 1)[0].lower()
    if command in ("/start", "/id"):
        enqueue_tg(chat_id, f"Telegram chat ID: {chat_id}\nSet TELEGRAM_CHAT_ID on Render to this ID.")
        return {"ok": True}

    # Only one explicitly configured chat can relay to/from the game.
    if not TG_CHAT_ID or chat_id != TG_CHAT_ID:
        enqueue_tg(chat_id, "This chat is not connected to SA:MP. Ask the server owner to configure its chat ID.")
        return {"ok": True}

    # A user can register only their own Telegram account; usernames are not
    # used as identifiers because Telegram usernames can change.
    sender = message.get("from") or {}
    user_id = sender.get("id")
    msg_id = message.get("message_id")
    reply_id = msg_id if isinstance(msg_id, int) else None

    if command == "/reg":
        nick = text[len(text.split(None, 1)[0]):].strip()
        if not isinstance(user_id, int):
            enqueue_tg(chat_id, "Не удалось определить Telegram ID отправителя.", reply_id)
        elif not NICK_RE.fullmatch(nick):
            enqueue_tg(chat_id, "Регистрация: /reg Nick_Name\nНик: 3–24 символа (латинские буквы, цифры, _).", reply_id)
        else:
            try:
                success = register_nick(user_id, nick)
                if success:
                    enqueue_tg(chat_id, f"✅ Зарегистрирован! Твой ник в SA:MP: {nick}", reply_id)
                else:
                    enqueue_tg(chat_id, f"Ник {nick} уже привязан к другому участнику Telegram.", reply_id)
            except sqlite3.Error:
                log.exception("Registration database error")
                enqueue_tg(chat_id, "Ошибка сохранения регистрации. Попробуй позже.", reply_id)
        return {"ok": True}

    is_whoami = command == "/whoami" or bool(re.fullmatch(r"кто\s+я[?!.\s]*", text, flags=re.I))
    is_who = command == "/who" or bool(re.fullmatch(r"кто\s+ты[?!.\s]*", text, flags=re.I))
    if is_whoami or is_who:
        if is_whoami:
            target_id = user_id
            prefix = "Твой"
        else:
            replied = message.get("reply_to_message") or {}
            target_id = (replied.get("from") or {}).get("id")
            prefix = "Его"
            if not isinstance(target_id, int):
                enqueue_tg(chat_id, "Ответь командой «кто ты» (или /who) на сообщение нужного человека.", reply_id)
                return {"ok": True}
        if not isinstance(target_id, int):
            enqueue_tg(chat_id, "Невозможно определить Telegram ID пользователя.", reply_id)
            return {"ok": True}
        try:
            nick = registered_nick(target_id)
        except sqlite3.Error:
            log.exception("Registration lookup error")
            enqueue_tg(chat_id, "Ошибка чтения регистраций. Попробуй позже.", reply_id)
            return {"ok": True}
        if nick:
            answer = f"{prefix} ник в SA:MP: {nick}"
        else:
            answer = ("Ты ещё не зарегистрирован. Используй /reg Nick_Name." if is_whoami
                      else "Этот пользователь ещё не зарегистрировал ник в SA:MP.")
        enqueue_tg(chat_id, answer, reply_id)
        return {"ok": True}

    if command == "/online":
        # Count ONLY active MoonLoader clients (last heartbeat within 45 seconds).
        with lock:
            cleanup(monotonic())
            names = sorted((entry["nick"] for entry in clients.values()), key=str.lower)
        if names:
            reply = "SA:MP Messenger — онлайн: {}\n".format(len(names))
            reply += "\n".join("{}. {}".format(i, nick) for i, nick in enumerate(names, 1))
        else:
            reply = "SA:MP Messenger — онлайн: 0\nНет подключённых игроков."
        enqueue_tg(chat_id, reply)
        return {"ok": True}

    if command == "/help":
        enqueue_tg(chat_id, "Команды:\n/reg Nick_Name — зарегистрировать свой ник\nкто я или /whoami — узнать свой ник\nкто ты (ответом на сообщение) или /who — узнать чужой ник\n/g текст — отправить в чат SA:MP\n/online — список игроков со скриптом\n/id — ID Telegram-чата\nОбычные сообщения группы не пересылаются.")
        return {"ok": True}

    # Forward to SA:MP ONLY explicit /g commands from the configured group.
    # Also accepts Telegram's /g@YourBotName syntax.
    if command != "/g":
        return {"ok": True}
    text = text[len(text.split(None, 1)[0]):].strip()
    if not text:
        enqueue_tg(chat_id, "Чтобы написать в SA:MP, используй: /g текст")
        return {"ok": True}

    try:
        game_nick = registered_nick(user_id) if isinstance(user_id, int) else None
    except sqlite3.Error:
        game_nick = None
    with lock:
        append_message(game_nick or pretty_sender(sender), text[:500], "telegram")
    return {"ok": True}
