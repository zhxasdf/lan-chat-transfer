"""SQLite storage for accounts and login sessions.

The chat server is single-process, so a short-lived connection per operation
behind a thread lock is simpler and safer than sharing one handle. Callers in
async code wrap every method in ``asyncio.to_thread`` to keep the event loop
free.
"""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

USERNAME_PATTERN = re.compile(r"^[\w-]{3,20}$")
PASSWORD_MIN, PASSWORD_MAX = 8, 64
SESSION_DAYS = 7
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    sender_user_id TEXT NOT NULL,
    recipient_user_id TEXT,
    text TEXT,
    file_id TEXT,
    file_name TEXT,
    file_size INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_public ON messages(kind, seq);
CREATE INDEX IF NOT EXISTS idx_messages_pair
    ON messages(sender_user_id, recipient_user_id, seq);
CREATE TABLE IF NOT EXISTS private_reads (
    user_id TEXT NOT NULL REFERENCES users(id),
    peer_user_id TEXT NOT NULL REFERENCES users(id),
    through_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, peer_user_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(recipient_user_id, sender_user_id, seq);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    recipient_user_id TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','deleted','missing'))
);
"""


class AuthError(Exception):
    """A registration or login failure that is safe to show to the client."""


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join([
        "scrypt",
        str(SCRYPT_N),
        str(SCRYPT_R),
        str(SCRYPT_P),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_username(value):
    name = value.strip() if isinstance(value, str) else ""
    if not USERNAME_PATTERN.fullmatch(name):
        raise AuthError("用户名须为3～20个字符，可用中文、字母、数字、下划线或连字符")
    return name


def validate_password(value):
    if not isinstance(value, str) or not PASSWORD_MIN <= len(value) <= PASSWORD_MAX:
        raise AuthError("密码须为8～64个字符")
    return value


def public_user(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
    }


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _read(self):
        with self._lock:
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    @contextmanager
    def _write(self):
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def create_user(self, username, password):
        name = validate_username(username)
        secret = validate_password(password)
        user_id = str(uuid4())
        created = iso(now())
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, display_name, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (user_id, name, hash_password(secret), name, created),
                )
        except sqlite3.IntegrityError:
            raise AuthError("用户名已被使用") from None
        return {"id": user_id, "username": name, "display_name": name}

    def find_user(self, username):
        name = username.strip() if isinstance(username, str) else ""
        with self._read() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ?", (name,)
            ).fetchone()

    def authenticate(self, username, password):
        row = self.find_user(username)
        if row is None or not isinstance(password, str):
            raise AuthError("用户名或密码错误")
        if not verify_password(password, row["password_hash"]):
            raise AuthError("用户名或密码错误")
        return public_user(row)

    def get_user(self, user_id):
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return public_user(row) if row else None

    def create_session(self, user_id):
        token = secrets.token_urlsafe(32)
        created = now()
        expires = created + timedelta(days=SESSION_DAYS)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token_hash(token), user_id, iso(created), iso(expires)),
            )
        return token

    def session_user(self, token):
        if not isinstance(token, str) or not token:
            return None
        digest = token_hash(token)
        with self._write() as conn:
            row = conn.execute(
                "SELECT u.*, s.expires_at AS session_expires"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["session_expires"]) <= now():
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                return None
        return public_user(row)

    def delete_session(self, token):
        if not isinstance(token, str) or not token:
            return
        with self._write() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),)
            )

    # ---------------------------- messages ----------------------------

    def add_message(self, message_id, kind, sender_user_id, recipient_user_id,
                    text, file_id, file_name, file_size, created_at):
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (id, kind, sender_user_id, recipient_user_id,"
                " text, file_id, file_name, file_size, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, kind, sender_user_id, recipient_user_id, text,
                 file_id, file_name, file_size, created_at),
            )
            return cursor.lastrowid

    _MESSAGE_SELECT = (
        "SELECT m.*,"
        " s.username AS sender_username, s.display_name AS sender_display_name,"
        " r.username AS recipient_username, r.display_name AS recipient_display_name,"
        " f.status AS file_status, f.sha256 AS file_sha256"
        " FROM messages m JOIN users s ON s.id = m.sender_user_id"
        " LEFT JOIN users r ON r.id = m.recipient_user_id"
        " LEFT JOIN files f ON f.id = m.file_id"
    )

    def _message_event(self, row):
        sender = {
            "id": row["sender_user_id"],
            "user_id": row["sender_user_id"],
            "username": row["sender_username"],
            "nickname": row["sender_display_name"],
        }
        recipient = None
        if row["recipient_user_id"]:
            recipient = {
                "id": row["recipient_user_id"],
                "user_id": row["recipient_user_id"],
                "username": row["recipient_username"],
                "nickname": row["recipient_display_name"],
            }
        if row["kind"] == "file":
            event = {
                "type": "file",
                "id": row["id"],
                "sender": sender,
                "file": {
                    "id": row["file_id"],
                    "name": row["file_name"],
                    "size": row["file_size"],
                    "sha256": row["file_sha256"],
                    "status": row["file_status"] or "missing",
                },
                "time": row["created_at"],
            }
            if recipient:
                event["recipient"] = recipient
            event["seq"] = row["seq"]
            return event
        event = {
            "type": "private" if row["kind"] == "private" else "chat",
            "id": row["id"],
            "sender": sender,
            "text": row["text"],
            "time": row["created_at"],
        }
        if recipient:
            event["recipient"] = recipient
        event["seq"] = row["seq"]
        return event

    def _history(self, where, params, limit, before_id):
        with self._read() as conn:
            if before_id:
                cursor = conn.execute(
                    "SELECT seq FROM messages WHERE id = ?", (before_id,)
                ).fetchone()
                if cursor is None:
                    return [], False
                where += " AND m.seq < ?"
                params = params + (cursor["seq"],)
            rows = conn.execute(
                f"{self._MESSAGE_SELECT} WHERE {where}"
                " ORDER BY m.seq DESC LIMIT ?",
                params + (limit + 1,),
            ).fetchall()
        has_more = len(rows) > limit
        events = [self._message_event(row) for row in reversed(rows[:limit])]
        return events, has_more

    def public_history(self, limit=50, before_id=None):
        where = (
            "((m.kind = 'public') OR (m.kind = 'file' AND m.recipient_user_id IS NULL))"
        )
        return self._history(where, (), limit, before_id)

    def public_after(self, after_id, limit=50):
        """Catch-up page for reconnecting clients: public messages after a cursor."""
        where = (
            "((m.kind = 'public') OR (m.kind = 'file' AND m.recipient_user_id IS NULL))"
            " AND m.seq > ?"
        )
        with self._read() as conn:
            start = 0
            if after_id:
                cursor = conn.execute(
                    "SELECT seq FROM messages WHERE id = ?", (after_id,)
                ).fetchone()
                if cursor is None:
                    return [], False
                start = cursor["seq"]
            rows = conn.execute(
                f"{self._MESSAGE_SELECT} WHERE {where} ORDER BY m.seq LIMIT ?",
                (start, limit + 1),
            ).fetchall()
        has_newer = len(rows) > limit
        events = [self._message_event(row) for row in rows[:limit]]
        return events, has_newer

    def private_history(self, user_id, peer_user_id, limit=50, before_id=None):
        if not peer_user_id or peer_user_id == user_id:
            return [], False
        where = (
            "m.kind IN ('private', 'file') AND m.recipient_user_id IS NOT NULL AND"
            " ((m.sender_user_id = ? AND m.recipient_user_id = ?)"
            " OR (m.sender_user_id = ? AND m.recipient_user_id = ?))"
        )
        params = (user_id, peer_user_id, peer_user_id, user_id)
        return self._history(where, params, limit, before_id)

    @staticmethod
    def _pair(user_id, peer_id):
        return (
            "m.recipient_user_id IS NOT NULL AND m.kind IN ('private', 'file') AND "
            "((m.sender_user_id = ? AND m.recipient_user_id = ?) OR "
            "(m.sender_user_id = ? AND m.recipient_user_id = ?))",
            (user_id, peer_id, peer_id, user_id),
        )

    def conversations(self, user_id):
        """Only conversation participants can see summaries and unread counts."""
        with self._read() as conn:
            rows = conn.execute(
                "WITH pairs AS (SELECT CASE WHEN sender_user_id = ? THEN recipient_user_id "
                "ELSE sender_user_id END AS peer_id, MAX(seq) AS latest "
                "FROM messages WHERE recipient_user_id IS NOT NULL AND kind IN ('private','file') "
                "AND (sender_user_id = ? OR recipient_user_id = ?) GROUP BY peer_id) "
                "SELECT u.*, m.seq, m.text, m.kind, m.created_at, COALESCE(r.through_seq,0) AS read_seq, "
                "(SELECT COUNT(*) FROM messages incoming WHERE incoming.recipient_user_id = ? "
                "AND incoming.sender_user_id = u.id AND incoming.kind IN ('private','file') "
                "AND incoming.seq > COALESCE(r.through_seq,0)) AS unread "
                "FROM pairs p JOIN users u ON u.id = p.peer_id JOIN messages m ON m.seq = p.latest "
                "LEFT JOIN private_reads r ON r.user_id = ? AND r.peer_user_id = u.id "
                "ORDER BY m.seq DESC",
                (user_id, user_id, user_id, user_id, user_id),
            ).fetchall()
        return [{"peer": {"id": row["id"], "user_id": row["id"],
                           "username": row["username"], "nickname": row["display_name"]},
                 "unread": row["unread"], "read_seq": row["read_seq"],
                 "last_seq": row["seq"], "last_text": row["text"] if row["kind"] == "private" else "[文件]",
                 "time": row["created_at"]} for row in rows]

    def private_page(self, user_id, peer_id, limit=50, after_id=None):
        """Start at the oldest unread, then page forward without skipping a backlog."""
        where, params = self._pair(user_id, peer_id)
        with self._read() as conn:
            if after_id:
                row = conn.execute(
                    f"SELECT m.seq FROM messages m WHERE {where} AND m.id = ?",
                    params + (after_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("消息游标不属于当前私聊")
                start = row["seq"]
            else:
                row = conn.execute(
                    "SELECT through_seq FROM private_reads WHERE user_id = ? AND peer_user_id = ?",
                    (user_id, peer_id),
                ).fetchone()
                start = row["through_seq"] if row else 0
                unread = conn.execute(
                    "SELECT MIN(seq) AS first_seq FROM messages WHERE sender_user_id = ? AND recipient_user_id = ? "
                    "AND kind IN ('private','file') AND seq > ?", (peer_id, user_id, start),
                ).fetchone()
                start = unread["first_seq"] - 1 if unread["first_seq"] is not None else None
            if start is None:
                rows = conn.execute(
                    f"{self._MESSAGE_SELECT} WHERE {where} ORDER BY m.seq DESC LIMIT ?",
                    params + (limit + 1,),
                ).fetchall()
                events = [self._message_event(row) for row in reversed(rows[:limit])]
                return {"messages": events, "has_more": len(rows) > limit, "has_newer": False,
                        "next_after": events[-1]["id"] if events else None}
            if start is not None:
                rows = conn.execute(
                    f"{self._MESSAGE_SELECT} WHERE {where} AND m.seq > ? ORDER BY m.seq LIMIT ?",
                    params + (start, limit + 1),
                ).fetchall()
                page = rows[:limit]
                older = bool(page and conn.execute(
                    f"SELECT 1 FROM messages m WHERE {where} AND m.seq < ? LIMIT 1",
                    params + (page[0]["seq"],),
                ).fetchone())
                events = [self._message_event(row) for row in page]
                return {"messages": events, "has_more": older, "has_newer": len(rows) > limit,
                        "next_after": events[-1]["id"] if events else after_id}

    def mark_private_read(self, user_id, peer_id, message_id):
        where, params = self._pair(user_id, peer_id)
        with self._write() as conn:
            row = conn.execute(
                f"SELECT m.seq FROM messages m WHERE {where} AND m.id = ?",
                params + (message_id,),
            ).fetchone()
            if row is None:
                raise ValueError("消息不属于当前私聊")
            conn.execute(
                "INSERT INTO private_reads(user_id,peer_user_id,through_seq) VALUES (?,?,?) "
                "ON CONFLICT(user_id,peer_user_id) DO UPDATE SET "
                "through_seq = MAX(private_reads.through_seq, excluded.through_seq)",
                (user_id, peer_id, row["seq"]),
            )

    def file_records(self):
        with self._read() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM files").fetchall()]

    def get_file(self, file_id):
        with self._read() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            return dict(row) if row else None

    def set_file_status(self, file_id, status):
        with self._write() as conn:
            conn.execute("UPDATE files SET status = ? WHERE id = ?", (status, file_id))

    def add_file_message(self, record, message_id, created_at):
        """The file metadata and its chat card either both commit, or neither does."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO files(id,name,size,sha256,owner_user_id,recipient_user_id,created_at,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (record.id, record.name, record.size, record.sha256, record.owner_user_id,
                 record.recipient_user_id, created_at, "available"),
            )
            cursor = conn.execute(
                "INSERT INTO messages(id,kind,sender_user_id,recipient_user_id,file_id,file_name,file_size,created_at) "
                "VALUES (?,'file',?,?,?,?,?,?)",
                (message_id, record.owner_user_id, record.recipient_user_id,
                 record.id, record.name, record.size, created_at),
            )
            return cursor.lastrowid
