"""SAMP Messenger FastAPI server + Telegram webhook relay.

Render environment:
  TELEGRAM_BOT_TOKEN  - BotFather token, secret
  PUBLIC_URL          - exact HTTPS URL of this Render service (no trailing slash)
  TELEGRAM_CHAT_ID    - destination Telegram chat ID; obtain by DMing /id to the bot

No additional Telegram libraries are required.
"""
from collections import deque
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from html import escape as html_escape
from hmac import compare_digest
from queue import Empty, Full, Queue
from threading import RLock, Thread
from time import monotonic, sleep, process_time, time as epoch_time
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from uuid import uuid4
import json
import logging
import os
import re
import sqlite3
import struct
import zlib
import math
from contextlib import contextmanager
from io import BytesIO
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("samp_messenger")
app = FastAPI(title="SA:MP Messenger", version="0.9-tg-reports-map")
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

# Sender health is visible at /metrics, without exposing Telegram credentials.
telegram_sender_thread = None
telegram_sender_lock = RLock()
telegram_health_lock = RLock()
telegram_health = {
    "sent": 0,
    "failed": 0,
    "last_error": None,
    "last_sent_at": None,
}

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
    request = UrlRequest(
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


def api_request_photo(chat_id, caption: str, image: bytes):
    """Send a PNG using Telegram multipart without external packages."""
    if not BOT_TOKEN:
        raise RuntimeError("Telegram is not configured")
    boundary = "samp_" + uuid4().hex
    parts = []
    for name, value in (("chat_id", str(chat_id)), ("caption", caption[:1000])):
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8"))
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"samp-report.png\"\r\nContent-Type: image/png\r\n\r\n").encode("utf-8"))
    parts.append(image)
    parts.append((f"\r\n--{boundary}--\r\n").encode("ascii"))
    req = UrlRequest(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urlopen(req, timeout=30) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise ValueError("Telegram sendPhoto returned ok=false")
    return data


def enqueue_tg_photo(chat_id, caption: str, image: bytes):
    if not BOT_TOKEN or not chat_id:
        return False
    try:
        tg_outgoing.put_nowait(("photo", chat_id, caption, image))
        return True
    except Full:
        log.warning("Telegram report queue full")
        return False


def update_delivery(key, status: str, error: str = ""):
    if key is None:
        return
    with lock:
        tg_delivery[key] = {"status": status, "error": error[:120], "updated": monotonic()}


def enqueue_tg(chat_id, text: str, reply_to_message_id=None, delivery_key=None, parse_mode=None):
    if not BOT_TOKEN or not chat_id:
        update_delivery(delivery_key, "unavailable", "Telegram bot or group is not configured")
        return False
    try:
        tg_outgoing.put_nowait((chat_id, text[:3900], reply_to_message_id, delivery_key, parse_mode))
        return True
    except Full:
        update_delivery(delivery_key, "failed", "Telegram queue is full")
        log.warning("Telegram outgoing queue full")
        return False


def _telegram_error_name(exc):
    """Safe diagnostics: never log bot token, URL or Telegram message content."""
    if isinstance(exc, HTTPError):
        return f"Telegram HTTP {exc.code}"
    return type(exc).__name__


def telegram_worker():
    """Keep processing later messages even when one Telegram request fails.

    urllib/http.client can raise raw OSError subclasses (including
    ConnectionResetError and RemoteDisconnected), not only URLError.
    """
    while True:
        job = tg_outgoing.get()
        is_photo = bool(job and job[0] == "photo")
        if is_photo:
            _, chat_id, content, image_bytes = job
            reply_id, delivery_key, parse_mode = None, None, None
        else:
            chat_id, content, reply_id, delivery_key, parse_mode = job
        success = False
        error = None
        try:
            payload = {"chat_id": chat_id, "text": content, "disable_web_page_preview": True}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if isinstance(reply_id, int):
                payload["reply_parameters"] = {"message_id": reply_id, "allow_sending_without_reply": True}
            for attempt in range(4):
                try:
                    if is_photo:
                        api_request_photo(chat_id, content, image_bytes)
                    else:
                        api_request("sendMessage", payload)
                    success = True
                    update_delivery(delivery_key, "delivered")
                    break
                except Exception as exc:
                    # Do not let a connection reset or unexpected API exception
                    # terminate the only Telegram sender thread.
                    error = _telegram_error_name(exc)
                    permanent = isinstance(exc, HTTPError) and exc.code in (400, 401, 403, 404)
                    if attempt == 3 or permanent:
                        update_delivery(delivery_key, "failed", error)
                        break
                    update_delivery(delivery_key, "retrying", "Temporary Telegram connection error")
                    sleep(min(12, 2 ** (attempt + 1)))
        except Exception as exc:
            error = _telegram_error_name(exc)
            update_delivery(delivery_key, "failed", error)
        finally:
            with telegram_health_lock:
                if success:
                    telegram_health["sent"] += 1
                    telegram_health["last_sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    telegram_health["last_error"] = None
                else:
                    telegram_health["failed"] += 1
                    telegram_health["last_error"] = error or "Unknown Telegram error"
                    log.warning("Telegram send failed: %s", telegram_health["last_error"])
            if success and delivery_key is not None:
                record_report_activity("game_to_tg")
            if not success:
                record_report_error("telegram", error or "Telegram send failed")
            tg_outgoing.task_done()


def ensure_telegram_worker():
    """Restart sender if it ever exits unexpectedly. Never spawn duplicates."""
    global telegram_sender_thread
    if not BOT_TOKEN:
        return
    with telegram_sender_lock:
        if telegram_sender_thread is None or not telegram_sender_thread.is_alive():
            telegram_sender_thread = Thread(target=telegram_worker, daemon=True, name="telegram-sender")
            telegram_sender_thread.start()
            log.info("Telegram sender worker started")


def telegram_sender_status():
    with telegram_sender_lock:
        alive = bool(telegram_sender_thread and telegram_sender_thread.is_alive())
    with telegram_health_lock:
        info = telegram_health.copy()
    return {
        "configured": bool(BOT_TOKEN and TG_CHAT_ID),
        "worker_alive": alive,
        "queue_size": tg_outgoing.qsize(),
        **info,
    }


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
telemetry_lock = RLock()
telemetry_bucket = None
telemetry_acc = None

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
    # Persist 1-minute aggregates, independent of /metrics browser polling.
    try:
        report_ingest(point, counts)
    except Exception as exc:
        log.warning("Telemetry persistence failed (%s)", type(exc).__name__)


def _metrics_sampling_loop():
    # Exceptions must never prevent the chat, GPS or Telegram from working.
    while True:
        sleep(DASHBOARD_SAMPLE_SECONDS)
        try:
            ensure_telegram_worker()
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
        record_report_error("http", "Unhandled exception: " + request.url.path[:80])
        raise
    with metrics_lock:
        metrics_window['total'] += 1
        metrics_total_requests += 1
        if request.url.path.startswith('/gps/'):
            metrics_window['gps'] += 1
        if response.status_code >= 500:
            metrics_window['errors'] += 1
        metrics_window['latency_ms'] += (monotonic() - began) * 1000
    if response.status_code >= 500:
        record_report_error("http", f"HTTP {response.status_code}: {request.url.path[:80]}")
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
<section class="panel" style="margin-top:14px"><h2>Telegram: отправка из SA-MP</h2><div id="tg-status" style="line-height:1.8">Проверка отправителя...</div></section>
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
  const tg=data.telegram_sender||{};
  const tgReady=tg.configured&&tg.worker_alive;
  $('tg-status').textContent=(tgReady?'Работает':'Требует проверки')+' · Отправлено: '+(tg.sent??0)+' · Ошибок: '+(tg.failed??0)+' · В очереди: '+(tg.queue_size??0)+(tg.last_error?' · Последняя ошибка: '+tg.last_error:'')+(tg.last_sent_at?' · Успешно: '+tg.last_sent_at:'');
  $('tg-status').className=tgReady?'':'error';
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
        'telegram_sender': telegram_sender_status(),
    }, headers={'Cache-Control': 'no-store'})


@app.on_event("startup")
def setup_telegram():
    # Start read-only metrics sampler in the background.
    Thread(target=_metrics_sampling_loop, daemon=True, name="performance-metrics").start()
    try:
        initialize_report_storage()
    except Exception as exc:
        log.warning("Report database unavailable: %s", type(exc).__name__)
    # Create the registration DB before webhooks start arriving.
    try:
        with registration_connection():
            pass
    except sqlite3.Error as exc:
        log.error("Telegram registration database unavailable: %s", type(exc).__name__)
    # Webhook registration runs outside the HTTP request loop.
    if BOT_TOKEN:
        ensure_telegram_worker()
        Thread(target=configure_webhook, daemon=True, name="telegram-webhook-setup").start()


@app.get("/")
def root():
    with lock:
        cleanup(monotonic())
        return {
            "status": "online", "name": "SAMP Messenger",
            "version": "0.9-tg-reports-map", "online": len(clients),
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


def telegram_game_text(nick: str, content: str) -> str:
    """Only presentation for Telegram; never alters stored /g messages."""
    nick_safe = html_escape(nick, quote=True)
    content_safe = html_escape(content, quote=True)
    # Already-existing cuff alert formats from other client versions.
    cuff_alert = re.fullmatch(
        r'[\"«»]?\s*' + re.escape(nick) +
        r'[\"«»]?\s+(?:кафают\s+помогите\s+ему|в\s+наручниках!\s*помогите\s+ему(?:\s*[—-]\s*его\s+метка\s+находится\s+на\s+карте\.?)?)',
        content, flags=re.IGNORECASE,
    )
    if cuff_alert:
        return f"🎮 [G] <b>{nick_safe}</b> в наручниках! Помогите ему — его метка находится на карте."
    return f"🎮 [G] <b>{nick_safe}</b>: {content_safe}"


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
                enqueue_tg(TG_CHAT_ID, telegram_game_text(entry["nick"], content), delivery_key=key, parse_mode="HTML")
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
    enqueue_tg(TG_CHAT_ID, telegram_game_text(item["nick"], content), delivery_key=key, parse_mode="HTML")
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
        # Cuff notification: one Telegram message per False->True transition.
        # The client already sends the real cuff signal; GPS itself is unchanged.
        cuff_just_started = bool(data.cuffed) and not entry.get("cuff_alert_state", False)
        entry["cuff_alert_state"] = bool(data.cuffed)
        if not data.share:
            entry.pop("gps", None)
            result = {"ok": True, "sharing": False}
        else:
            if data.x is None or data.y is None or data.z is None:
                raise HTTPException(status_code=400, detail="Missing GPS coordinates")
            entry["gps"] = {"x": data.x, "y": data.y, "z": data.z, "cuffed": data.cuffed, "last_update": now}
            result = {"ok": True, "sharing": True}
        if cuff_just_started:
            # Deliver the same alert through /state to every SA:MP client,
            # including the cuffed player. No repeated alerts while cuffed.
            cuff_text = f"{entry['nick']} в наручниках! Помогите ему!"
            if data.share:
                cuff_text = f"{entry['nick']} в наручниках! Помогите ему — его метка находится на карте."
            append_message(entry["nick"], cuff_text, "cuff")
    if cuff_just_started:
        nick = entry["nick"]
        if data.share:
            alert = f"🎮 [G] <b>{html_escape(nick, quote=True)}</b> в наручниках! Помогите ему — его метка находится на карте."
        else:
            alert = f"🎮 [G] <b>{html_escape(nick, quote=True)}</b> в наручниках! Помогите ему!"
        enqueue_tg(TG_CHAT_ID, alert, parse_mode="HTML")
    return result


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



# ===============================================================
# Telegram operational reports (day and calendar month).
# Aggregates are stored in SQLite, not sent to any third-party chart API.
# On Render Free these files are EPHEMERAL across deploy/restart: captions
# explicitly show how much of the requested period is actually recorded.
# ===============================================================
REPORT_DB_PATH = os.getenv("REPORT_DB_PATH", "samp_metrics.sqlite3")
REPORT_TZ_NAME = os.getenv("REPORT_TZ", "Europe/Kyiv")
REPORT_TZ = timezone.utc
if ZoneInfo is None:
    REPORT_TZ_NAME = "UTC"
if ZoneInfo is not None:
    try:
        REPORT_TZ = ZoneInfo(REPORT_TZ_NAME)
    except (KeyError, ValueError):
        REPORT_TZ_NAME = "UTC"
REPORT_COMMAND_COOLDOWN = 12
_report_last_command = {}


@contextmanager
def _db():
    conn = sqlite3.connect(REPORT_DB_PATH, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            yield conn
    finally:
        conn.close()


def initialize_report_storage():
    with _db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS telemetry (
            minute INTEGER PRIMARY KEY, cpu REAL, ram REAL, players REAL,
            requests INTEGER NOT NULL, gps INTEGER NOT NULL,
            http_errors INTEGER NOT NULL, latency_sum REAL NOT NULL,
            samples INTEGER NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS telemetry_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            origin TEXT NOT NULL, description TEXT NOT NULL)''')
        conn.execute("CREATE INDEX IF NOT EXISTS ix_telemetry_errors_ts ON telemetry_errors(ts)")
        conn.execute('''CREATE TABLE IF NOT EXISTS telemetry_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL)''')
        conn.execute("CREATE INDEX IF NOT EXISTS ix_telemetry_activity_ts ON telemetry_activity(ts)")
        # Keep at most 45 days of minute aggregates and event records.
        cutoff = int(epoch_time()) - 45 * 86400
        conn.execute("DELETE FROM telemetry WHERE minute < ?", (cutoff - cutoff % 60,))
        conn.execute("DELETE FROM telemetry_errors WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM telemetry_activity WHERE ts < ?", (cutoff,))


def record_report_error(origin, description):
    # Diagnostics must never include URLs with bot tokens or user messages.
    try:
        with _db() as conn:
            conn.execute("INSERT INTO telemetry_errors(ts,origin,description) VALUES(?,?,?)",
                         (int(epoch_time()), str(origin)[:30], str(description)[:160]))
    except sqlite3.Error:
        pass  # reporting must never break chat or GPS


def record_report_activity(kind):
    try:
        with _db() as conn:
            conn.execute("INSERT INTO telemetry_activity(ts,kind) VALUES (?,?)",
                         (int(epoch_time()),str(kind)[:30]))
    except sqlite3.Error:
        pass


def _save_acc(bucket, acc):
    if bucket is None or not acc or acc['samples'] == 0:
        return
    try:
        with _db() as conn:
            conn.execute('''INSERT INTO telemetry (
                minute,cpu,ram,players,requests,gps,http_errors,latency_sum,samples
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(minute) DO UPDATE SET
                cpu=excluded.cpu,ram=excluded.ram,players=excluded.players,
                requests=excluded.requests,gps=excluded.gps,http_errors=excluded.http_errors,
                latency_sum=excluded.latency_sum,samples=excluded.samples''',
                (bucket,
                 acc['cpu_sum'] / acc['cpu_n'] if acc['cpu_n'] else None,
                 acc['ram_sum'] / acc['ram_n'] if acc['ram_n'] else None,
                 acc['players_sum'] / acc['samples'],
                 acc['requests'], acc['gps'], acc['http_errors'],
                 acc['latency_sum'], acc['samples']))
    except sqlite3.Error as exc:
        log.warning("Report SQLite write failed: %s", type(exc).__name__)


def report_ingest(point, counts):
    global telemetry_bucket, telemetry_acc
    minute = int(epoch_time()) // 60 * 60
    with telemetry_lock:
        if telemetry_bucket != minute:
            if telemetry_bucket is not None:
                _save_acc(telemetry_bucket, telemetry_acc)
            telemetry_bucket = minute
            telemetry_acc = dict(samples=0, cpu_sum=0.0, cpu_n=0, ram_sum=0.0,
                                 ram_n=0, players_sum=0.0, requests=0, gps=0,
                                 http_errors=0, latency_sum=0.0)
        a = telemetry_acc
        a['samples'] += 1
        a['players_sum'] += point['players_online']
        for name, dest in (('cpu_percent', 'cpu'), ('memory_mb', 'ram')):
            val = point.get(name)
            if isinstance(val, (int, float)) and math.isfinite(val):
                a[dest + '_sum'] += val
                a[dest + '_n'] += 1
        a['requests'] += counts['total']
        a['gps'] += counts['gps']
        a['http_errors'] += counts['errors']
        a['latency_sum'] += counts['latency_ms']
        # Keep the current minute visible and durable every ~30 seconds.
        if int(epoch_time()) % 60 in range(25, 32) or int(epoch_time()) % 60 in range(55, 60):
            _save_acc(telemetry_bucket, a)


def report_flush():
    with telemetry_lock:
        _save_acc(telemetry_bucket, telemetry_acc)


def _bounds(mode):
    now = datetime.now(REPORT_TZ)
    if mode == 'day':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, now


def load_report(mode):
    report_flush()
    start, now = _bounds(mode)
    with _db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM telemetry WHERE minute >= ? AND minute <= ? ORDER BY minute",
                            (int(start.timestamp()), int(now.timestamp()))).fetchall()
        counts = conn.execute("SELECT origin, COUNT(*) n FROM telemetry_errors WHERE ts >= ? GROUP BY origin",
                              (int(start.timestamp()),)).fetchall()
        activity = conn.execute("SELECT kind,COUNT(*) n FROM telemetry_activity WHERE ts >= ? GROUP BY kind",
                                (int(start.timestamp()),)).fetchall()
        errors = conn.execute("SELECT ts,origin,description FROM telemetry_errors WHERE ts >= ? ORDER BY ts DESC LIMIT 8",
                              (int(start.timestamp()),)).fetchall()
        error_per_minute = conn.execute("SELECT (ts / 60) * 60 m,COUNT(*) n FROM telemetry_errors WHERE ts >= ? GROUP BY m",
                                        (int(start.timestamp()),)).fetchall()
    event_counts = {row['m']: row['n'] for row in error_per_minute}
    points = [dict(x) for x in rows]
    for point in points:
        point['failures'] = event_counts.get(point['minute'], 0)
    summary = {x['origin']: x['n'] for x in counts}
    summary.update({x['kind']: x['n'] for x in activity})
    return start, now, points, summary, [dict(x) for x in errors]


# Compact built-in 5x7 bitmap font to render PNG with zero pip dependencies.
_FONT = {
 'A':['01110','10001','10001','11111','10001','10001','10001'],
 'B':['11110','10001','10001','11110','10001','10001','11110'],
 'C':['01111','10000','10000','10000','10000','10000','01111'],
 'D':['11110','10001','10001','10001','10001','10001','11110'],
 'E':['11111','10000','10000','11110','10000','10000','11111'],
 'F':['11111','10000','10000','11110','10000','10000','10000'],
 'G':['01111','10000','10000','10111','10001','10001','01110'],
 'H':['10001','10001','10001','11111','10001','10001','10001'],
 'I':['11111','00100','00100','00100','00100','00100','11111'],
 'J':['00111','00010','00010','00010','10010','10010','01100'],
 'K':['10001','10010','10100','11000','10100','10010','10001'],
 'L':['10000','10000','10000','10000','10000','10000','11111'],
 'M':['10001','11011','10101','10101','10001','10001','10001'],
 'N':['10001','11001','10101','10011','10001','10001','10001'],
 'O':['01110','10001','10001','10001','10001','10001','01110'],
 'P':['11110','10001','10001','11110','10000','10000','10000'],
 'Q':['01110','10001','10001','10001','10101','10010','01101'],
 'R':['11110','10001','10001','11110','10100','10010','10001'],
 'S':['01111','10000','10000','01110','00001','00001','11110'],
 'T':['11111','00100','00100','00100','00100','00100','00100'],
 'U':['10001','10001','10001','10001','10001','10001','01110'],
 'V':['10001','10001','10001','10001','10001','01010','00100'],
 'W':['10001','10001','10001','10101','10101','10101','01010'],
 'X':['10001','10001','01010','00100','01010','10001','10001'],
 'Y':['10001','10001','01010','00100','00100','00100','00100'],
 'Z':['11111','00001','00010','00100','01000','10000','11111'],
 '0':['01110','10011','10101','10101','11001','10001','01110'],
 '1':['00100','01100','00100','00100','00100','00100','01110'],
 '2':['01110','10001','00001','00010','00100','01000','11111'],
 '3':['11110','00001','00001','01110','00001','00001','11110'],
 '4':['00010','00110','01010','10010','11111','00010','00010'],
 '5':['11111','10000','10000','11110','00001','00001','11110'],
 '6':['01111','10000','10000','11110','10001','10001','01110'],
 '7':['11111','00001','00010','00100','01000','01000','01000'],
 '8':['01110','10001','10001','01110','10001','10001','01110'],
 '9':['01110','10001','10001','01111','00001','00001','11110'],
 ' ':['00000']*7,
 '-':['00000','00000','00000','11111','00000','00000','00000'],
 ':':['00000','00100','00100','00000','00100','00100','00000'],
 '.':['00000','00000','00000','00000','00000','00110','00110'],
 '/':['00001','00001','00010','00100','01000','10000','10000'],
 '%':['10001','10001','00010','00100','01000','10001','10001'],
 '(':['00010','00100','01000','01000','01000','00100','00010'],
 ')':['01000','00100','00010','00010','00010','00100','01000'],
}


def render_png_report(mode, rows, start, now):
    # 4 independent charts on a single 980x1010 RGB image.
    width, height = 980, 1180
    bg=(14, 22, 35); fg=(228,237,250); muted=(141,157,181); grid=(44,56,75)
    blue=(82,169,247); green=(91,222,166); gold=(246,191,83); red=(244,94,118)
    image = bytearray(bytes(bg) * (width * height))
    def px(x,y,c):
        if 0 <= x < width and 0 <= y < height:
            i=(y*width+x)*3
            image[i:i+3] = bytes(c)
    def rect(x0,y0,x1,y1,c):
        x0=max(0,int(x0)); x1=min(width,int(x1)); y0=max(0,int(y0)); y1=min(height,int(y1))
        triple=bytes(c)
        row=triple * max(0,x1-x0)
        for yy in range(y0,y1):
            off=(yy*width+x0)*3
            image[off:off+len(row)]=row
    def line(x0,y0,x1,y1,c,thick=1):
        x0=int(x0); y0=int(y0); x1=int(x1); y1=int(y1)
        dx=abs(x1-x0); sx=1 if x0<x1 else -1
        dy=-abs(y1-y0); sy=1 if y0<y1 else -1; err=dx+dy
        while True:
            for o in range(-(thick//2),thick//2+1):
                px(x0+o,y0,c); px(x0,y0+o,c)
            if x0==x1 and y0==y1:break
            e2=2*err
            if e2 >= dy: err+=dy; x0+=sx
            if e2 <= dx: err+=dx; y0+=sy
    def write(x,y,txt,c=fg,scale=2):
        x=int(x); y=int(y)
        for ch in str(txt).upper():
            glyph=_FONT.get(ch,_FONT[' '])
            for gy,row in enumerate(glyph):
                for gx,p in enumerate(row):
                    if p=='1':rect(x+gx*scale,y+gy*scale,x+(gx+1)*scale,y+(gy+1)*scale,c)
            x+=6*scale
    write(38,25,'SAMP MESSENGER',fg,4)
    write(40,66,('DAY' if mode == 'day' else 'MONTH')+' REPORT '+now.strftime('%Y-%m-%d'),muted,2)
    write(40,90,'TIME ZONE '+REPORT_TZ_NAME.replace('_',' '),muted,2)
    timeline_start=int(start.timestamp()); timeline_end=max(timeline_start+1,int(now.timestamp()))
    # Distribute bucket values across day/month on calendar timeline.
    datasets=[
        ('CPU %', 'cpu', blue, lambda a: a['cpu'] if a['cpu'] is not None else None),
        ('RAM MB', 'ram', green, lambda a: a['ram'] if a['ram'] is not None else None),
        ('PLAYERS ONLINE', 'players', gold, lambda a: a['players']),
        ('REQUESTS / MIN', 'requests', blue, lambda a: a['requests']),
        ('GPS REQUESTS / MIN', 'gps', green, lambda a: a['gps']),
        ('ERRORS / MIN', 'failures', red, lambda a: a['failures']),
    ]
    # Six independent charts; exact per-minute observations with gaps left blank.
    plot_left=92; plot_right=944; first_y=145
    for k,(label,key,color,getval) in enumerate(datasets):
        top=first_y+k*168; bottom=top+110
        write(40,top-21,label,color,2)
        for n in range(5):
            yy=top+int(n*110/4)
            line(plot_left,yy,plot_right,yy,grid)
        values=[getval(r) for r in rows]
        numeric=[v for v in values if isinstance(v,(float,int)) and math.isfinite(v)]
        high=max(numeric) if numeric else 0
        if high<=0:high=1
        elif key in ('players','requests','gps','http_errors'): high=max(1,math.ceil(high))
        write(14,top,str(round(high,1)),muted,1)
        write(23,bottom-10,'0',muted,1)
        prior=None
        for r,v in zip(rows,values):
            if v is None or not math.isfinite(v):
                prior=None;continue
            x=plot_left + int((r['minute']-timeline_start)/(timeline_end-timeline_start) * (plot_right-plot_left))
            y=bottom-int(min(1,max(0,v/high))*110)
            if prior is not None and r['minute']-prior[0]<=90:
                line(prior[1],prior[2],x,y,color,2)
            else:
                rect(x-1,y-1,x+2,y+2,color)
            prior=(r['minute'],x,y)
        write(plot_left,bottom+10,start.strftime('%d %b %H:%M'),muted,1)
        write(plot_right-132,bottom+10,now.strftime('%d %b %H:%M'),muted,1)
    # Valid PNG RGB with one deflate-compressed IDAT chunk.
    raw=bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(image[y*width*3:(y+1)*width*3])
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress(raw,6))+chunk(b'IEND',b''))


def _fmt_report_caption(mode, start, now, rows, count_by_source):
    label = 'сегодня' if mode == 'day' else 'текущий месяц'
    if not rows:
        return ('📊 SAMP Messenger — отчёт за '+label+'\n'
                'Нет собранных измерений. Сбор начинается после запуска новой версии.\n'
                f'Часовой пояс: {REPORT_TZ_NAME}.')
    first=datetime.fromtimestamp(rows[0]['minute'],REPORT_TZ).strftime('%d.%m %H:%M')
    last=datetime.fromtimestamp(rows[-1]['minute'],REPORT_TZ).strftime('%d.%m %H:%M')
    req=sum(r['requests'] for r in rows)
    gps=sum(r['gps'] for r in rows)
    errors=sum(r['http_errors'] for r in rows)
    peak=max((r['players'] for r in rows),default=0)
    cpu=[r['cpu'] for r in rows if r['cpu'] is not None]
    ram=[r['ram'] for r in rows if r['ram'] is not None]
    tg_err=count_by_source.get('telegram',0)
    # The report must never claim full coverage if the Render worker rebooted.
    minute_span = max(1,int((now-start).total_seconds()/60)+1)
    coverage = min(100, round(100*len(rows)/minute_span,1))
    coverage_text = '<1' if rows and coverage < 1 else f'{coverage:g}'
    gaps = sum(1 for a,b in zip(rows,rows[1:]) if b['minute']-a['minute'] > 180)
    avg_latency = (sum(r['latency_sum'] for r in rows)/req) if req else 0
    return (f'📊 SAMP Messenger — за {label}\n'
            f'📅 Измерения: {first} — {last} ({REPORT_TZ_NAME})\n'
            f'🕒 Покрытие периода: {coverage_text}% (пробелы не считаются работой)\n'
            f'👥 Пик игроков онлайн: {peak:.0f}\n'
            f'🖥 CPU среднее: {(sum(cpu)/len(cpu)) if cpu else 0:.1f}%'+('' if cpu else ' (нет данных)')+'\n'
            f'💾 RAM среднее: {(sum(ram)/len(ram)) if ram else 0:.1f} MB'+('' if ram else ' (нет данных)')+'\n'
            f'🌐 HTTP запросы: {req}, GPS: {gps}\n'
            f'💬 SA-MP → Telegram: {count_by_source.get("game_to_tg",0)}\n'
            f'⏱ Средний ответ HTTP: {avg_latency:.0f} мс\n'
            f'⚠ HTTP 5xx: {errors}, ошибки Telegram: {tg_err}\n'
            f'⏸ Пробелы мониторинга: {gaps} (не обязательно сбои)\n'
            '⚠ Render Free: при перезапуске история может сбрасываться.')



# ===============================================================
# Telegram /map - self-contained geographic SCHEMATIC of GTA SA.
# No Pillow, additional files or changes to MoonLoader are needed.
# GPS coordinates are real and use SA-MP's -3000..3000 world axes;
# background is schematic, NOT a pixel-perfect game texture.


def _map_nick_color(nick: str):
    """Exactly the same hash/HSV-to-RGB procedure as Lua gpsNickColors."""
    h = 2166136
    for byt in nick.lower().encode('utf-8'):
        h = (h * 131 + byt) % 2147483647
    hue = (h % 3600) / 600.0
    sat = .73 + (h // 3600 % 4) * .055
    val = .88 + (h // 14400 % 3) * .055
    c = val * sat
    section = int(hue)
    x = c * (1 - abs(hue % 2 - 1))
    if section == 0:
        rgb = (c, x, 0)
    elif section == 1:
        rgb = (x, c, 0)
    elif section == 2:
        rgb = (0, c, x)
    elif section == 3:
        rgb = (0, x, c)
    elif section == 4:
        rgb = (x, 0, c)
    else:
        rgb = (c, 0, x)
    m = val - c
    return tuple(min(255, int((v + m) * 255 + .5)) for v in rgb)


def _map_snapshot():
    """Only currently shared, fresh coordinates; never show /gshare off."""
    with lock:
        now = monotonic()
        cleanup(now)
        result = []
        for player in clients.values():
            gps = player.get('gps')
            if gps is None or (now - gps.get('last_update', 0)) > GPS_TIMEOUT:
                continue
            result.append({'nick': player['nick'], 'x': gps['x'],
                           'y': gps['y'], 'z': gps['z'],
                           'cuffed': bool(gps.get('cuffed', False))})
    return sorted(result, key=lambda p: p['nick'].lower())[:GPS_MAX_PLAYERS]


def render_gps_map_png(players):
    """Render the user-provided real GTA SA map as a Telegram PNG.

    Positions use the standard GTA SA world rectangle (-3000..3000 X/Y).
    The background is a user-supplied annotated GTA SA map at sa_map.png.
    Only fresh, sharing-enabled GPS clients are included by _map_snapshot().
    """
    from pathlib import Path
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required: add Pillow to requirements.txt") from exc

    source = Path(__file__).resolve().parent / "sa_map.png"
    if not source.is_file():
        raise FileNotFoundError("sa_map.png is missing. Upload it beside main.py on GitHub")

    with Image.open(source) as supplied_map:
        base = supplied_map.convert('RGB')
    width, height = base.size
    # Preserve the entire supplied map, and add a separate legend to the right.
    legend_width = 350
    line_height = 32
    content_height = max(height, 118 + len(players) * line_height + 70)
    canvas = Image.new('RGB', (width + legend_width, content_height), (22, 29, 39))
    canvas.paste(base, (0, 0))
    draw = ImageDraw.Draw(canvas)

    def font(size):
        for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                     '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
                     'C:/Windows/Fonts/arial.ttf'):
            try:
                return ImageFont.truetype(path, size)
            except (OSError, ValueError):
                pass
        return ImageFont.load_default()

    heading_font = font(23)
    nick_font = font(17)
    detail_font = font(13)
    draw.text((width + 23, 23), 'GPS PLAYERS', fill=(246, 249, 253), font=heading_font)
    draw.text((width + 23, 58), f'ON MAP: {len(players)}', fill=(170, 186, 206), font=detail_font)
    draw.line((width + 19, 90, width + legend_width - 20, 90), fill=(64, 79, 98), width=2)

    def pos(x, y):
        # In GTA SA, positive Y is north. Image pixels grow downward.
        # The original supplied map covers the standard +/-3000 world bounds.
        x = float(x)
        y = float(y)
        return (round((x + 3000) * (width - 1) / 6000),
                round((3000 - y) * (height - 1) / 6000))

    def triangle(cx, cy, color):
        outer = [(cx, cy - 15), (cx - 13, cy + 12), (cx + 13, cy + 12)]
        draw.polygon(outer, fill=(0, 0, 0))
        inner = [(cx, cy - 11), (cx - 9, cy + 9), (cx + 9, cy + 9)]
        draw.polygon(inner, fill=color)

    for idx, player in enumerate(players):
        nick = str(player['nick'])[:24]
        color = _map_nick_color(nick)
        x, y = float(player['x']), float(player['y'])
        if -3000 <= x <= 3000 and -3000 <= y <= 3000:
            px, py = pos(x, y)
            triangle(px, py, color)
            # Text outline makes the nickname readable on complex map textures.
            tx = max(3, min(width - 175, px + 15))
            ty = max(2, min(height - 24, py - 11))
            draw.text((tx, ty), nick, font=nick_font, fill=color,
                      stroke_width=2, stroke_fill=(0, 0, 0))
        ly = 108 + idx * line_height
        draw.polygon([(width + 32, ly + 4),
                      (width + 21, ly + 27),
                      (width + 43, ly + 27)], fill=(0, 0, 0))
        draw.polygon([(width + 32, ly + 9),
                      (width + 26, ly + 24),
                      (width + 38, ly + 24)], fill=color)
        draw.text((width + 55, ly + 5), nick, fill=color, font=nick_font)
        if player.get('cuffed'):
            draw.text((width + 55, ly + 25), 'CUFFED', fill=(250, 88, 88), font=detail_font)

    if not players:
        draw.text((width + 23, 122), 'No shared GPS players',
                  fill=(180, 195, 214), font=nick_font)

    draw.text((width + 23, content_height - 48), 'Only /gshare on players',
              fill=(171, 187, 204), font=detail_font)
    draw.text((width + 23, content_height - 29),
              datetime.now(timezone.utc).strftime('%d.%m.%Y  %H:%M UTC'),
              fill=(171, 187, 204), font=detail_font)

    buffer = BytesIO()
    canvas.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def handle_map_command(chat_id,user_id,reply_id):
    if not _tg_command_allowed(chat_id,user_id):
        enqueue_tg(chat_id,'Подожди 12 секунд перед повторным запросом карты.',reply_id)
        return
    try:
        players = _map_snapshot()
        png = render_gps_map_png(players)
        caption = ('🗺 SAMP Messenger — GPS PLAYERS: '+str(len(players))+'\n'
                   'Карта San Andreas; позиции по текущим GPS-координатам.\n'
                   'Видны только игроки с включённым /gshare on и свежими координатами.')
        if not enqueue_tg_photo(chat_id,caption,png):
            enqueue_tg(chat_id,'Не удалось поставить карту в очередь Telegram.',reply_id)
    except Exception as exc:
        log.exception('GPS map rendering failed')
        record_report_error('gps_map',type(exc).__name__)
        enqueue_tg(chat_id,'Не удалось сформировать GPS-карту. Проверь логи Render.',reply_id)


def _tg_command_allowed(chat_id, user_id):
    # One report per account per 12 seconds prevents large Telegram photo spam.
    key = (str(chat_id), user_id)
    now=monotonic()
    with telemetry_lock:
        if now-_report_last_command.get(key,-1e9) < REPORT_COMMAND_COOLDOWN:
            return False
        _report_last_command[key]=now
        return True


def handle_report_command(command, chat_id, user_id, reply_id):
    if not _tg_command_allowed(chat_id,user_id):
        enqueue_tg(chat_id,'Подожди 12 секунд перед следующей командой отчёта.',reply_id)
        return
    try:
        if command in ('/dayreport','/monthreport'):
            mode = 'day' if command=='/dayreport' else 'month'
            start, now, rows, counts, errors = load_report(mode)
            caption = _fmt_report_caption(mode,start,now,rows,counts)
            photo = render_png_report(mode,rows,start,now)
            if not enqueue_tg_photo(chat_id,caption,photo):
                enqueue_tg(chat_id,'Не удалось добавить график в очередь Telegram.',reply_id)
        elif command == '/errors':
            start,now,rows,counts,errors=load_report('day')
            if errors:
                result=['⚠ Ошибки сервера за сегодня:']
                for e in errors:
                    when=datetime.fromtimestamp(e['ts'],REPORT_TZ).strftime('%H:%M:%S')
                    result.append(f"{when} · {e['origin']}: {e['description']}")
                result.append('Время: '+REPORT_TZ_NAME)
                enqueue_tg(chat_id,'\n'.join(result)[:3900],reply_id)
            else:
                enqueue_tg(chat_id,'За сегодня записанных ошибок нет (либо журнал был сброшен перезапуском).',reply_id)
        elif command == '/status':
            with lock:
                cleanup(monotonic()); n=len(clients)
            with metrics_lock:
                m=dict(metrics_latest)
            t=telegram_sender_status()
            enqueue_tg(chat_id,(f'🟢 SAMP Messenger\n👥 Онлайн: {n}\n'
                f'CPU: {m.get("cpu_percent") if m.get("cpu_percent") is not None else "нет данных"}%\n'
                f'RAM: {m.get("memory_mb") if m.get("memory_mb") is not None else "нет данных"} MB\n'
                f'HTTP: {m.get("requests_per_sec")} / сек, GPS: {m.get("gps_per_sec")} / сек\n'
                f'Telegram: {"работает" if t["worker_alive"] else "неактивен"}\n'
                f'Отправлено: {t["sent"]}, ошибок: {t["failed"]}, очередь: {t["queue_size"]}'),reply_id)
    except Exception as exc:
        record_report_error('report',type(exc).__name__)
        log.exception('Telegram report command failed')
        enqueue_tg(chat_id,'Не удалось сформировать отчёт. Проверь логи Render.',reply_id)


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
    if command == "/map":
        Thread(target=handle_map_command, args=(chat_id, user_id, reply_id),
               daemon=True, name="telegram-gps-map").start()
        return {"ok": True}

    if command in ("/dayreport", "/monthreport", "/errors", "/status"):
        # Avoid blocking Telegram webhook during PNG rendering.
        Thread(target=handle_report_command, args=(command, chat_id, user_id, reply_id),
               daemon=True, name="telegram-report").start()
        return {"ok": True}

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
        enqueue_tg(chat_id, "Команды:\n/reg Nick_Name — зарегистрировать свой ник\nкто я или /whoami — узнать свой ник\nкто ты (ответом на сообщение) или /who — узнать чужой ник\n/g текст — отправить в чат SA:MP\n/online — список игроков со скриптом\n/id — ID Telegram-чата\n/dayreport — график нагрузки за сегодня\n/monthreport — график нагрузки за текущий месяц\n/errors — последние ошибки за сегодня\n/status — текущая нагрузка сервера\n/map — GPS-карта с цветными метками и GPS PLAYERS\nОбычные сообщения группы не пересылаются.")
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
