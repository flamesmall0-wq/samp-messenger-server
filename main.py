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

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

log = logging.getLogger("samp_messenger")
app = FastAPI(title="SA:MP Messenger", version="0.4-tg")
lock = RLock()
clients = {}  # token -> {nick, last_seen, last_message}
messages = deque(maxlen=200)
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


class JoinRequest(BaseModel):
    nick: str


class SendRequest(BaseModel):
    token: str
    text: str = Field(min_length=1, max_length=500)


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


def enqueue_tg(chat_id, text: str):
    if not BOT_TOKEN or not chat_id:
        return
    try:
        tg_outgoing.put_nowait((chat_id, text[:3900]))
    except Full:
        log.warning("Telegram outgoing queue full; dropping one notification")


def telegram_worker():
    while True:
        chat_id, text = tg_outgoing.get()
        try:
            api_request("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            })
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
            "version": "0.4-tg", "online": len(clients),
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
        if now - entry["last_message"] < MIN_MESSAGE_INTERVAL:
            raise HTTPException(status_code=429, detail="Please wait before sending again")
        entry["last_message"] = now
        item = append_message(entry["nick"], content, "game")
    # Do not block the SA:MP HTTP request while sending to Telegram.
    if TG_CHAT_ID:
        enqueue_tg(TG_CHAT_ID, f"🎮 [G] {item['nick']}: {content}")
    return {"ok": True, "id": item["id"]}


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

    if command == "/online":
        with lock:
            cleanup(monotonic())
            names = sorted((entry["nick"] for entry in clients.values()), key=str.lower)
        enqueue_tg(chat_id, "SA:MP online ({}): {}".format(len(names), ", ".join(names) if names else "nobody"))
        return {"ok": True}
    if command == "/help":
        enqueue_tg(chat_id, "Send any text to SA:MP, or /g your message. /online shows active users; /id shows chat ID.")
        return {"ok": True}
    if text.startswith("/"):
        if command != "/g":
            return {"ok": True}  # Ignore unknown commands.
        text = text.partition(" ")[2].strip()
    if not text:
        return {"ok": True}

    with lock:
        append_message(pretty_sender(message.get("from") or {}), text[:500], "telegram")
    return {"ok": True}
