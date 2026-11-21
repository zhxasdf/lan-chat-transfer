"""Single-process, in-memory chat server."""
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


def timestamp():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Peer:
    socket: WebSocket
    nickname: str
    id: str = field(default_factory=lambda: str(uuid4()))

    def public(self):
        return {"id": self.id, "nickname": self.nickname}


class ChatRoom:
    def __init__(self):
        self.peers: dict[str, Peer] = {}
        # Serialize room changes and broadcasts to preserve event order.
        self.lock = asyncio.Lock()

    async def send(self, socket, event):
        try:
            await asyncio.wait_for(socket.send_json(event), timeout=3)
            return True
        except Exception:
            return False

    async def publish(self, events):
        """Called under lock. Remove failed recipients without recursive broadcasts."""
        while events:
            failed = set()
            for event in events:
                for peer in list(self.peers.values()):
                    if peer.id not in failed and not await self.send(peer.socket, event):
                        failed.add(peer.id)
            removed = [self.peers.pop(uid) for uid in failed if uid in self.peers]
            events = []
            if removed:
                for peer in removed:
                    try:
                        await asyncio.wait_for(peer.socket.close(), timeout=3)
                    except Exception:
                        pass
                events = [self.users_event(), *[self.notice(p, "离开") for p in removed]]

    async def deliver(self, recipients, event):
        """Called under lock. Send one event to selected peers, cleaning up failures."""
        failed = [
            peer for peer in recipients if not await self.send(peer.socket, event)
        ]
        removed = [self.peers.pop(peer.id) for peer in failed if peer.id in self.peers]
        if not removed:
            return
        for peer in removed:
            try:
                await asyncio.wait_for(peer.socket.close(), timeout=3)
            except Exception:
                pass
        await self.publish(
            [self.users_event(), *[self.notice(peer, "离开") for peer in removed]]
        )

    def users_event(self):
        return {"type": "users", "users": [p.public() for p in self.peers.values()]}

    def notice(self, peer, action):
        return {"type": "notice", "text": f"{peer.nickname} {action}了聊天室", "time": timestamp()}

    async def remove(self, peer):
        async with self.lock:
            if self.peers.pop(peer.id, None):
                await self.publish([self.users_event(), self.notice(peer, "离开")])


def create_app():
    app = FastAPI(title="LAN Chat Transfer")
    room = ChatRoom()
    app.state.room = room

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        await socket.accept()
        peer = None
        try:
            while True:
                incoming = await socket.receive()
                if incoming["type"] == "websocket.disconnect":
                    break
                raw = incoming.get("text")
                try:
                    if raw is None:
                        raise ValueError("仅支持文本JSON消息")
                    if len(raw) > 65536:
                        raise ValueError("消息数据过大")
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError("事件必须是JSON对象")
                    kind = event.get("type")
                    if kind == "join":
                        if peer:
                            raise ValueError("已经进入聊天室")
                        name = event.get("nickname")
                        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 20:
                            raise ValueError("昵称须为1～20个字符")
                        name = name.strip()
                        async with room.lock:
                            if any(p.nickname == name for p in room.peers.values()):
                                raise ValueError("昵称已被使用，请换一个")
                            peer = Peer(socket, name)
                            room.peers[peer.id] = peer
                            if not await room.send(socket, {"type": "welcome", "user": peer.public()}):
                                room.peers.pop(peer.id, None)
                                break
                            await room.publish([room.users_event(), room.notice(peer, "进入")])
                    elif kind == "chat":
                        if not peer or peer.id not in room.peers:
                            raise ValueError("请先进入聊天室")
                        text = event.get("text")
                        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                            raise ValueError("消息须为1～2000个字符")
                        async with room.lock:
                            await room.publish([{"type": "chat", "id": str(uuid4()),
                                                 "sender": peer.public(), "text": text.strip(),
                                                 "time": timestamp()}])
                    elif kind == "private":
                        if not peer or peer.id not in room.peers:
                            raise ValueError("请先进入聊天室")
                        text = event.get("text")
                        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                            raise ValueError("消息须为1～2000个字符")
                        target_id = event.get("to")
                        if not isinstance(target_id, str) or not target_id:
                            raise ValueError("请选择私聊对象")
                        async with room.lock:
                            target = room.peers.get(target_id)
                            if target is None:
                                raise ValueError("对方不在线，无法发送私聊")
                            if target.id == peer.id:
                                raise ValueError("不能给自己发送私聊")
                            await room.deliver([peer, target], {
                                "type": "private",
                                "id": str(uuid4()),
                                "sender": peer.public(),
                                "recipient": target.public(),
                                "text": text.strip(),
                                "time": timestamp(),
                            })
                    else:
                        raise ValueError("不支持的事件类型")
                except (ValueError, TypeError) as exc:
                    if not await room.send(socket, {"type": "error", "message": str(exc)}):
                        break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if peer:
                await room.remove(peer)

    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    async def index():
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse({"message": "请先构建frontend，或访问Vite开发服务器"}, status_code=503)

    return app


app = create_app()
