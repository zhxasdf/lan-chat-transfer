"""Persistent file storage and account-authorized online/offline transfers."""
import asyncio
import hashlib
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import ClientDisconnect

MAX_FILE_SIZE = 20 * 1024 * 1024
STORAGE_LIMIT = 256 * 1024 * 1024
router = APIRouter(prefix="/api/files", tags=["files"])


def safe_filename(value: str) -> str:
    name = re.split(r"[/\\]", value)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", name).strip().strip(".")
    return name[:180] or "file"


def managed_id(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class StoredFile:
    id: str
    name: str
    size: int
    owner_user_id: str
    recipient_user_id: str | None
    sha256: str
    status: str = "available"

    def public(self):
        return {"id": self.id, "name": self.name, "size": self.size,
                "sha256": self.sha256, "status": self.status}

    @classmethod
    def from_row(cls, row):
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


class FileStore:
    def __init__(self, root: Path, max_size=MAX_FILE_SIZE, limit=STORAGE_LIMIT):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_size = max_size
        self.limit = limit
        self.records: dict[str, StoredFile] = {}
        self.used = 0
        self.uploading: set[str] = set()
        self.downloading: dict[str, int] = {}

    def recover(self, database):
        """Recover committed files and interrupted uploads for the current schema."""
        for row in database.file_records():
            record = StoredFile.from_row(row)
            if not managed_id(record.id):
                raise ValueError("文件记录包含非法存储ID")
            path = self.root / record.id
            if record.status != "available":
                continue
            valid = path.is_file() and not path.is_symlink() and path.stat().st_size == record.size
            if valid:
                with path.open("rb") as source:
                    valid = hashlib.file_digest(source, "sha256").hexdigest() == record.sha256
            if valid:
                self.commit(record)
            else:
                database.set_file_status(record.id, "missing")
        for path in self.root.iterdir():
            is_partial = path.name.startswith(".part-") and managed_id(path.name[6:])
            is_orphan = managed_id(path.name) and path.name not in self.records
            if (is_partial or is_orphan) and (path.is_file() or path.is_symlink()):
                path.unlink()

    def reserve(self, user_id):
        if user_id in self.uploading:
            raise HTTPException(409, "已有文件正在上传")
        if self.used + (len(self.uploading) + 1) * self.max_size > self.limit:
            raise HTTPException(507, "文件存储空间不足，请先删除自己上传的文件")
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
    peers = room.connections(user_id)
    return peers[0] if peers else None


async def target_account(database, sender_id, target_id):
    if target_id is None:
        return None
    if target_id == sender_id:
        raise HTTPException(400, "不能给自己发送私聊文件")
    target = await asyncio.to_thread(database.get_user, target_id)
    if target is None:
        raise HTTPException(404, "接收账号不存在")
    return {"id": target["id"], "user_id": target["id"],
            "username": target["username"], "nickname": target["display_name"]}


@router.post("", status_code=201)
async def upload_file(request: Request, filename: str = "file", to: str | None = None):
    room, database = request.app.state.room, request.app.state.db
    store: FileStore = request.app.state.files
    user = await authenticated_user(request)
    async with room.lock:
        if online_peer(room, user["id"]) is None:
            raise HTTPException(401, "请先在浏览器中登录聊天室")
        await target_account(database, user["id"], to)
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
    partial, path = store.root / f".part-{file_id}", store.root / file_id
    committed = False
    try:
        size, digest = 0, hashlib.sha256()
        async with await anyio.open_file(partial, "xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > store.max_size:
                    raise HTTPException(413, "文件超过20 MiB限制")
                digest.update(chunk)
                await output.write(chunk)
            await output.flush()
        async with room.lock:
            current = await authenticated_user(request)
            sender = online_peer(room, current["id"])
            if current["id"] != user["id"] or sender is None:
                raise HTTPException(401, "聊天连接已断开，请重新发送文件")
            target = await target_account(database, current["id"], to)
            record = StoredFile(file_id, safe_filename(filename), size, current["id"], to, digest.hexdigest())
            event = {"type": "file", "id": str(uuid4()), "sender": sender.public(),
                     "file": record.public(), "time": datetime.now(timezone.utc).isoformat()}
            if target:
                event["recipient"] = target
                event["queued"] = not bool(room.connections(to))
            async def finalize():
                nonlocal committed
                await asyncio.to_thread(partial.replace, path)
                seq = await asyncio.to_thread(database.add_file_message, record, event["id"], event["time"])
                store.commit(record)
                committed = True
                return seq
            with anyio.CancelScope(shield=True):
                # Native asyncio cancellation also must wait for the DB thread before cleanup.
                finalizing = asyncio.create_task(finalize())
                cancelled = False
                while True:
                    try:
                        event["seq"] = await asyncio.shield(finalizing)
                        break
                    except asyncio.CancelledError:
                        if finalizing.cancelled():
                            raise
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
            if target:
                await room.deliver([sender, *room.connections(to)], event)
                await room.refresh_inbox([sender.user_id, to])
            else:
                await room.publish([event])
        return {"file_id": file_id}
    except ClientDisconnect:
        raise HTTPException(400, "上传连接已中断") from None
    except sqlite3.Error:
        raise HTTPException(507, "文件信息保存失败，请重试") from None
    except OSError:
        raise HTTPException(507, "文件读写失败，请检查磁盘空间和权限") from None
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.to_thread(partial.unlink, missing_ok=True)
                if not committed:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
            finally:
                store.uploading.discard(user["id"])


async def file_record(request, file_id):
    row = await asyncio.to_thread(request.app.state.db.get_file, file_id)
    if row is None or not managed_id(file_id):
        raise HTTPException(404, "文件不存在")
    return StoredFile.from_row(row)


def check_access(record, user_id):
    if record.recipient_user_id is not None and user_id not in (record.owner_user_id, record.recipient_user_id):
        raise HTTPException(403, "你无权下载这个私聊文件")


async def notify_file_change(room, record):
    event = {"type": "file_state", "file": record.public()}
    if record.recipient_user_id:
        await room.deliver(room.connections(record.owner_user_id) + room.connections(record.recipient_user_id), event)
    else:
        await room.publish([event])


class DownloadResponse(FileResponse):
    """Protect files from owner deletion while a response streams."""
    def __init__(self, path, room, store, file_id, **kwargs):
        super().__init__(path, **kwargs)
        self.room, self.store, self.file_id = room, store, file_id

    async def __call__(self, scope, receive, send):
        async with self.room.lock:
            row = await asyncio.to_thread(self.room.db.get_file, self.file_id)
            if not row or row["status"] != "available" or self.file_id not in self.store.records or not Path(self.path).is_file():
                return await JSONResponse({"detail": "文件已不可用"}, status_code=404)(scope, receive, send)
            self.store.downloading[self.file_id] = self.store.downloading.get(self.file_id, 0) + 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.store.downloading[self.file_id] -= 1
            if not self.store.downloading[self.file_id]:
                del self.store.downloading[self.file_id]


@router.get("/{file_id}")
async def download_file(request: Request, file_id: str):
    user = await authenticated_user(request)
    store, room = request.app.state.files, request.app.state.room
    async with room.lock:
        record = await file_record(request, file_id)
        check_access(record, user["id"])
        if record.status != "available":
            raise HTTPException(404, "文件已删除" if record.status == "deleted" else "文件已不可用")
        path = store.root / file_id
        if not path.is_file() or path.is_symlink() or path.stat().st_size != record.size:
            await asyncio.to_thread(request.app.state.db.set_file_status, file_id, "missing")
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                removed = True
            except OSError:
                removed = False
            if removed and file_id in store.records:
                store.used -= store.records.pop(file_id).size
            await notify_file_change(room, replace(record, status="missing"))
            raise HTTPException(404, "文件已不可用")
    return DownloadResponse(path, room, store, file_id, media_type="application/octet-stream",
                            filename=record.name, content_disposition_type="attachment",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.delete("/{file_id}", status_code=204)
async def delete_file(request: Request, file_id: str):
    user = await authenticated_user(request)
    store, room = request.app.state.files, request.app.state.room
    async with room.lock:
        record = await file_record(request, file_id)
        if record.owner_user_id != user["id"]:
            raise HTTPException(403, "只有发送者可以删除文件")
        if store.downloading.get(file_id):
            raise HTTPException(409, "文件正在下载，请稍后再删除")
        await asyncio.to_thread(request.app.state.db.set_file_status, file_id, "deleted")
        await notify_file_change(room, replace(record, status="deleted"))
        try:
            await asyncio.to_thread((store.root / file_id).unlink, missing_ok=True)
        except OSError:
            raise HTTPException(507, "文件已标记删除，但磁盘清理失败，请稍后重试或重启服务") from None
        if file_id in store.records:
            store.used -= store.records.pop(file_id).size
