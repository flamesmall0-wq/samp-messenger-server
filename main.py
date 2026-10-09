
import asyncio
import json
import re
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI(title="SAMP Messenger")

# Подключённые игроки
players = {}
lock = asyncio.Lock()


@app.get("/")
async def home():
    return {
        "status": "online",
        "name": "SAMP Messenger",
        "online": len(players)
    }


@app.get("/online")
async def online():
    return {
        "count": len(players),
        "players": list(players.values())
    }


async def broadcast(data):
    message = json.dumps(data, ensure_ascii=False)
    async with lock:
        connections = list(players.keys())

    for ws in connections:
        try:
            await ws.send_text(message)
        except Exception:
            pass


async def update_online():
    await broadcast({
        "type": "online",
        "players": list(players.values())
    })


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    username = None

    try:
        # Первый пакет — регистрация ника
        raw = await ws.receive_text()
        data = json.loads(raw)

        if data.get("type") != "join":
            await ws.close(code=1008)
            return

        username = str(data.get("nick", ""))

        if not re.fullmatch(r"[A-Za-z0-9_]{3,24}", username):
            await ws.close(code=1008)
            return

        async with lock:
            if username in players.values():
                await ws.close(code=1008)
                return

            players[ws] = username

        await broadcast({
            "type": "system",
            "text": username + " подключился"
        })

        await update_online()

        while True:
            raw = await ws.receive_text()

            if len(raw) > 4096:
                continue

            data = json.loads(raw)

            if data.get("type") == "message":
                text = str(data.get("text", "")).strip()

                if not text or len(text) > 500:
                    continue

                await broadcast({
                    "type": "message",
                    "nick": username,
                    "text": text
                })

    except (WebSocketDisconnect, ValueError, TypeError):
        pass

    finally:
        async with lock:
            players.pop(ws, None)

        if username:
            await broadcast({
                "type": "system",
                "text": username + " отключился"
            })
            await update_online()
