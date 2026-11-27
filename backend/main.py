"""Single-process chat server with SQLite accounts and in-memory connections."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import anyio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.db import AuthError, Database
from backend.files import FileStore, MAX_FILE_SIZE, STORAGE_LIMIT, router as files_router


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def bearer_token(request: Request):
    authorization = request.headers.get("authorization", "")
    kind, _, token = authorization.partition(" ")
    if kind.lower() != "bearer" or not token:
        return None
    return token


@dataclass
class Peer:
    socket: WebSocket
    user_id: str
    username: str
    display_name: str
    id: str = field(default_factory=lambda: str(uuid4()))

    def public(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "nickname": self.display_name,
        }


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
        return {
            "type": "notice",
            "text": f"{peer.display_name} {action}了聊天室",
            "time": timestamp(),
        }

    async def remove(self, peer):
        async with self.lock:
            if self.peers.pop(peer.id, None):
                await self.publish([self.users_event(), self.notice(peer, "离开")])


def create_app(*, file_limit=MAX_FILE_SIZE, storage_limit=STORAGE_LIMIT, db_path=None):
    if db_path is None:
        db_path = os.environ.get("LAN_CHAT_DB")
    database_path = (
        Path(db_path)
        if db_path
        else Path(__file__).resolve().parent / "data" / "chat.db"
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.db = Database(database_path)
        with TemporaryDirectory(
            prefix="lan-chat-transfer-", dir=os.environ.get("LAN_CHAT_TMP")
        ) as directory:
            app.state.files = FileStore(Path(directory), file_limit, storage_limit)
            yield

    app = FastAPI(title="LAN Chat Transfer", lifespan=lifespan)
    room = ChatRoom()
    app.state.room = room
    app.include_router(files_router)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    async def current_account(request: Request):
        token = bearer_token(request)
        if token is None:
            raise HTTPException(401, "登录凭证无效，请重新登录")
        account = await asyncio.to_thread(app.state.db.session_user, token)
        if account is None:
            raise HTTPException(401, "登录已过期，请重新登录")
        return account

    @app.post("/api/auth/register", status_code=201)
    async def register(payload: dict):
        try:
            user = await asyncio.to_thread(
                app.state.db.create_user,
                payload.get("username"),
                payload.get("password"),
            )
        except AuthError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"user": user}

    @app.post("/api/auth/login")
    async def login(payload: dict):
        try:
            user = await asyncio.to_thread(
                app.state.db.authenticate,
                payload.get("username"),
                payload.get("password"),
            )
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from None
        token = await asyncio.to_thread(app.state.db.create_session, user["id"])
        return {"user": user, "token": token}

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request):
        token = bearer_token(request)
        if token is not None:
            await asyncio.to_thread(app.state.db.delete_session, token)

    @app.get("/api/auth/me")
    async def me(request: Request):
        return {"user": await current_account(request)}

    @app.get("/api/history")
    async def history(request: Request, kind: str = "public", peer: str | None = None,
                      before: str | None = None, limit: int = 50):
        account = await current_account(request)
        limit = max(1, min(limit, 100))
        if kind == "public":
            messages, has_more = await asyncio.to_thread(
                app.state.db.public_history, limit, before
            )
        elif kind == "private":
            if not isinstance(peer, str) or not peer:
                raise HTTPException(400, "请选择私聊对象")
            messages, has_more = await asyncio.to_thread(
                app.state.db.private_history, account["id"], peer, limit, before
            )
        else:
            raise HTTPException(400, "未知的历史类型")
        return {"messages": messages, "has_more": has_more}

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
                    if kind == "auth":
                        if peer:
                            raise ValueError("已经登录")
                        token = event.get("token")
                        if not isinstance(token, str) or not token:
                            raise ValueError("登录凭证无效，请重新登录")
                        account = await asyncio.to_thread(
                            app.state.db.session_user, token
                        )
                        if account is None:
                            raise ValueError("登录已过期，请重新登录")
                        async with room.lock:
                            peer = Peer(
                                socket,
                                account["id"],
                                account["username"],
                                account["display_name"],
                            )
                            room.peers[peer.id] = peer
                            welcome = {"type": "welcome", "user": peer.public()}
                            if not await room.send(socket, welcome):
                                room.peers.pop(peer.id, None)
                                break
                            await room.publish(
                                [room.users_event(), room.notice(peer, "进入")]
                            )
                    elif kind == "chat":
                        if not peer or peer.id not in room.peers:
                            raise ValueError("请先登录")
                        text = event.get("text")
                        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                            raise ValueError("消息须为1～2000个字符")
                        async with room.lock:
                            message_id = str(uuid4())
                            created = timestamp()
                            await room.publish([{
                                "type": "chat",
                                "id": message_id,
                                "sender": peer.public(),
                                "text": text.strip(),
                                "time": created,
                            }])
                            await asyncio.to_thread(
                                app.state.db.add_message,
                                message_id,
                                "public",
                                peer.user_id,
                                None,
                                text.strip(),
                                None,
                                None,
                                None,
                                created,
                            )
                    elif kind == "private":
                        if not peer or peer.id not in room.peers:
                            raise ValueError("请先登录")
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
                            message_id = str(uuid4())
                            created = timestamp()
                            await room.deliver([peer, target], {
                                "type": "private",
                                "id": message_id,
                                "sender": peer.public(),
                                "recipient": target.public(),
                                "text": text.strip(),
                                "time": created,
                            })
                            await asyncio.to_thread(
                                app.state.db.add_message,
                                message_id,
                                "private",
                                peer.user_id,
                                target.user_id,
                                text.strip(),
                                None,
                                None,
                                None,
                                created,
                            )
                    else:
                        raise ValueError("不支持的事件类型")
                except (ValueError, TypeError) as exc:
                    if not await room.send(socket, {"type": "error", "message": str(exc)}):
                        break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if peer:
                # Test clients and server shutdown may cancel the connection task.
                # Finish removing it and notifying other peers even in that scope.
                with anyio.CancelScope(shield=True):
                    await room.remove(peer)

    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    async def index():
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse(
            {"message": "请先构建frontend，或访问Vite开发服务器"}, status_code=503
        )

    return app


app = create_app()
