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
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
import json
import logging
import os
import re
import sqlite3

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

log = logging.getLogger("samp_messenger")
app = FastAPI(title="SA:MP Messenger", version="0.7-tg-reg")
lock = RLock()
clients = {}  # token -> {nick, last_seen, last_message}
messages = deque(maxlen=200)
# In-memory deduplication of acknowledged SA:MP sends; protects against
# duplicated delivery when the client retries after a lost HTTP response.
seen_game_messages = {}  # (nick_lower, message_id) -> previous message id
seen_game_order = deque()
MAX_DEDUP = 2500

next_id = 0
SESSION_TIMEOUT = 45
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


def enqueue_tg(chat_id, text: str, reply_to_message_id=None):
    if not BOT_TOKEN or not chat_id:
        return
    try:
        tg_outgoing.put_nowait((chat_id, text[:3900], reply_to_message_id))
    except Full:
        log.warning("Telegram outgoing queue full; dropping one notification")


def telegram_worker():
    while True:
        chat_id, text, reply_id = tg_outgoing.get()
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if isinstance(reply_id, int):
                # Telegram ignores the reply if the target message was deleted.
                payload["reply_parameters"] = {"message_id": reply_id, "allow_sending_without_reply": True}
            api_request("sendMessage", payload)
        except (HTTPError, URLError, ValueError, TimeoutError) as exc:
            # Never print the token or Telegram request URL.
            log.warning("Telegram send failed (%s)", type(exc).__name__)
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


@app.on_event("startup")
def setup_telegram():
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
            "version": "0.7-tg-reg", "online": len(clients),
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
            return {"ok": True, "id": seen_game_messages[key], "duplicate": True}
        if now - entry["last_message"] < MIN_MESSAGE_INTERVAL:
            raise HTTPException(status_code=429, detail="Please wait before sending again")
        entry["last_message"] = now
        item = append_message(entry["nick"], content, "game")
        if key:
            seen_game_messages[key] = item["id"]
            seen_game_order.append(key)
            if len(seen_game_order) > MAX_DEDUP:
                old = seen_game_order.popleft()
                seen_game_messages.pop(old, None)
    # Avoid duplicate posts to Telegram on a retry.
    if TG_CHAT_ID:
        enqueue_tg(TG_CHAT_ID, f"🎮 [G] {item['nick']}: {content}")
    return {"ok": True, "id": item["id"], "duplicate": False}


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
