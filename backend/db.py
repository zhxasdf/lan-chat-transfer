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
            conn.execute(
                "INSERT INTO messages (id, kind, sender_user_id, recipient_user_id,"
                " text, file_id, file_name, file_size, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, kind, sender_user_id, recipient_user_id, text,
                 file_id, file_name, file_size, created_at),
            )

    _MESSAGE_SELECT = (
        "SELECT m.*,"
        " s.username AS sender_username, s.display_name AS sender_display_name,"
        " r.username AS recipient_username, r.display_name AS recipient_display_name"
        " FROM messages m JOIN users s ON s.id = m.sender_user_id"
        " LEFT JOIN users r ON r.id = m.recipient_user_id"
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
                },
                "time": row["created_at"],
            }
            if recipient:
                event["recipient"] = recipient
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
