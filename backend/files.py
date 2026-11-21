"""Temporary file storage and authenticated HTTP transfers for live chat peers."""
import re
import secrets
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
    sender_id: str
    recipient_id: str | None

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

    def reserve(self, peer_id):
        # All accounting runs synchronously on the application event loop.
        if peer_id in self.uploading:
            raise HTTPException(409, "已有文件正在上传")
        if self.used + (len(self.uploading) + 1) * self.max_size > self.limit:
            raise HTTPException(507, "本次服务的文件空间不足，请稍后重试或重启服务清理")
        self.uploading.add(peer_id)

    def commit(self, record):
        self.records[record.id] = record
        self.used += record.size


def authenticated_peer(request: Request):
    authorization = request.headers.get("authorization", "")
    kind, _, token = authorization.partition(" ")
    if kind.lower() == "bearer" and token:
        for peer in request.app.state.room.peers.values():
            if secrets.compare_digest(peer.token.encode(), token.encode()):
                return peer
    raise HTTPException(401, "连接凭证无效，请重新进入聊天室")


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
    async with room.lock:
        sender = authenticated_peer(request)
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
    store.reserve(sender.id)
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
            current = authenticated_peer(request)
            if current is not sender:
                raise HTTPException(401, "连接已改变，请重新发送文件")
            target = target_peer(room, sender, to)
            record = StoredFile(file_id, safe_filename(filename), size, sender.id, to)
            event = {"type": "file", "id": str(uuid4()),
                     "sender": sender.public(), "file": record.public(),
                     "time": datetime.now(timezone.utc).isoformat()}
            if target:
                event["recipient"] = target.public()
            store.commit(record)
            committed = True
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
        store.uploading.discard(sender.id)
        if not committed:
            path.unlink(missing_ok=True)


@router.get("/{file_id}")
async def download_file(request: Request, file_id: str):
    async with request.app.state.room.lock:
        peer = authenticated_peer(request)
        store: FileStore = request.app.state.files
        record = store.records.get(file_id)
        if record is None:
            raise HTTPException(404, "文件不存在或已随服务重启清理")
        if record.recipient_id is not None and peer.id not in (record.sender_id, record.recipient_id):
            raise HTTPException(403, "你无权下载这个私聊文件")
        path = store.root / record.id
        if not path.is_file():
            raise HTTPException(404, "文件已不可用")
    return FileResponse(path, media_type="application/octet-stream", filename=record.name,
                        content_disposition_type="attachment",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
