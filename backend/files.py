"""Temporary file storage and account-authenticated HTTP transfers."""
import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

MAX_FILE_SIZE = 20 * 1024 * 1024
STORAGE_LIMIT = 256 * 1024 * 1024
router = APIRouter(prefix="/api/files", tags=["files"])


def safe_filename(value: str) -> str:
    """Names are display-only; never use one as a storage path."""
    name = re.split(r"[/\\]", value)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", name).strip().strip(".")
    return name[:180] or "file"


@dataclass(frozen=True)
class StoredFile:
    id: str
    name: str
    size: int
    owner_user_id: str
    recipient_user_id: str | None

    def public(self):
        return {"id": self.id, "name": self.name, "size": self.size}


class FileStore:
    def __init__(self, root: Path, max_size=MAX_FILE_SIZE, limit=STORAGE_LIMIT):
        self.root = root
        self.max_size = max_size
        self.limit = limit
        self.records: dict[str, StoredFile] = {}
        self.used = 0
        self.uploading: set[str] = set()

    def reserve(self, user_id):
        # All accounting runs synchronously on the application event loop.
        if user_id in self.uploading:
            raise HTTPException(409, "已有文件正在上传")
        if self.used + (len(self.uploading) + 1) * self.max_size > self.limit:
            raise HTTPException(507, "本次服务的文件空间不足，请稍后重试或重启服务清理")
        self.uploading.add(user_id)

    def commit(self, record):
        self.records[record.id] = record
        self.used += record.size


async def authenticated_user(request: Request):
    authorization = request.headers.get("authorization", "")
    kind, _, token = authorization.partition(" ")
    if kind.lower() != "bearer" or not token:
        raise HTTPException(401, "登录凭证无效，请重新登录")
    user = await asyncio.to_thread(request.app.state.db.session_user, token)
    if user is None:
        raise HTTPException(401, "登录已过期，请重新登录")
    return user


def online_peer(room, user_id):
    return next((peer for peer in room.peers.values() if peer.user_id == user_id), None)


def target_peer(room, sender, target_id):
    if target_id is None:
        return None
    if target_id == sender.id:
        raise HTTPException(400, "不能给自己发送私聊文件")
    target = room.peers.get(target_id)
    if target is None:
        raise HTTPException(409, "对方不在线，无法发送文件")
    return target


@router.post("", status_code=201)
async def upload_file(request: Request, filename: str = "file", to: str | None = None):
    room = request.app.state.room
    store: FileStore = request.app.state.files
    user = await authenticated_user(request)
    async with room.lock:
        sender = online_peer(room, user["id"])
        if sender is None:
            raise HTTPException(401, "请先在浏览器中登录聊天室")
        target_peer(room, sender, to)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/octet-stream":
        raise HTTPException(415, "请使用二进制文件请求体")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise HTTPException(400, "文件长度无效") from None
        if length < 0:
            raise HTTPException(400, "文件长度无效")
        if length > store.max_size:
            raise HTTPException(413, "文件超过20 MiB限制")
    store.reserve(user["id"])
    file_id = str(uuid4())
    path = store.root / file_id
    committed = False
    try:
        size = 0
        async with await anyio.open_file(path, "xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > store.max_size:
                    raise HTTPException(413, "文件超过20 MiB限制")
                await output.write(chunk)
        # Do not retain the room lock while receiving or writing file contents.
        async with room.lock:
            current = await authenticated_user(request)
            if current["id"] != user["id"]:
                raise HTTPException(401, "登录账号已变化，请重新发送文件")
            sender = online_peer(room, current["id"])
            if sender is None:
                raise HTTPException(401, "聊天连接已断开，请重新发送文件")
            target = target_peer(room, sender, to)
            record = StoredFile(
                file_id,
                safe_filename(filename),
                size,
                current["id"],
                target.user_id if target else None,
            )
            event = {
                "type": "file",
                "id": str(uuid4()),
                "sender": sender.public(),
                "file": record.public(),
                "time": datetime.now(timezone.utc).isoformat(),
            }
            if target:
                event["recipient"] = target.public()
            store.commit(record)
            committed = True
            await asyncio.to_thread(
                request.app.state.db.add_message,
                event["id"],
                "file",
                current["id"],
                target.user_id if target else None,
                None,
                file_id,
                record.name,
                size,
                event["time"],
            )
            if target:
                await room.deliver([sender, target], event)
            else:
                await room.publish([event])
        return {"file_id": file_id}
    except ClientDisconnect:
        raise HTTPException(400, "上传连接已中断") from None
    except OSError:
        raise HTTPException(507, "文件读写失败，请检查临时磁盘空间") from None
    finally:
        store.uploading.discard(user["id"])
        if not committed:
            path.unlink(missing_ok=True)


@router.get("/{file_id}")
async def download_file(request: Request, file_id: str):
    user = await authenticated_user(request)
    store: FileStore = request.app.state.files
    async with request.app.state.room.lock:
        record = store.records.get(file_id)
        if record is None:
            raise HTTPException(404, "文件不存在或已随服务重启清理")
        if record.recipient_user_id is not None and user["id"] not in (
            record.owner_user_id,
            record.recipient_user_id,
        ):
            raise HTTPException(403, "你无权下载这个私聊文件")
        path = store.root / record.id
        if not path.is_file():
            raise HTTPException(404, "文件已不可用")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=record.name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
