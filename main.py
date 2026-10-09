"""Small HTTP API for the SA:MP Messenger MoonLoader client."""
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from uuid import uuid4
import re

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="SA:MP Messenger", version="0.3")
lock = RLock()
clients = {}  # token -> {nick, last_seen, last_message}
messages = deque(maxlen=100)
next_id = 0
SESSION_TIMEOUT = 45  # seconds without a heartbeat
MIN_MESSAGE_INTERVAL = 0.35
NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")


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


@app.get("/")
def root():
    with lock:
        cleanup(monotonic())
        return {"status": "online", "name": "SAMP Messenger", "version": "0.3", "online": len(clients)}


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
    global next_id
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
        next_id += 1
        message = {
            "id": next_id,
            "nick": entry["nick"],
            "text": content,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        messages.append(message)
        return {"ok": True, "id": next_id}


@app.post("/leave")
def leave(data: TokenRequest):
    with lock:
        clients.pop(data.token, None)
    return {"ok": True}
