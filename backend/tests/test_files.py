import json
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from backend.db import Database
from backend.files import MAX_FILE_SIZE
from backend.main import Peer, create_app

PASSWORD = "password123"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as value:
        yield value


def login(client, name, register=True):
    if register:
        response = client.post(
            "/api/auth/register", json={"username": name, "password": PASSWORD}
        )
        assert response.status_code == 201, response.text
    data = client.post(
        "/api/auth/login", json={"username": name, "password": PASSWORD}
    ).json()
    return data["user"], data["token"]


def enter(client, ws, name, register=True):
    user, token = login(client, name, register)
    ws.send_json({"type": "auth", "token": token})
    welcome = ws.receive_json()
    users = ws.receive_json()
    ws.receive_json()
    assert "token" not in json.dumps(users)
    assert "token" not in welcome["user"]
    return welcome["user"], {"Authorization": f"Bearer {token}",
                             "Content-Type": "application/octet-stream"}


def drain_join(*sockets):
    for ws in sockets:
        assert ws.receive_json()["type"] == "users"
        assert ws.receive_json()["type"] == "notice"


def upload(client, headers, body=b"hello", name="test.txt", to=None):
    params = {"filename": name}
    if to is not None:
        params["to"] = to
    return client.post("/api/files", params=params, headers=headers, content=body)


def assert_clean(client):
    store = client.app.state.files
    assert not store.records and store.used == 0 and not store.uploading
    assert not list(store.root.iterdir())


def test_public_file_broadcast_download_and_same_names(client):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        alice, ah = enter(client, a, "张三三")
        _, bh = enter(client, b, "李四四")
        drain_join(a)
        ids = []
        for content in (b"first", b"second", b""):
            response = upload(client, ah, content, "资料 文档.txt")
            assert response.status_code == 201
            event = a.receive_json()
            assert event == b.receive_json()
            assert event["type"] == "file" and "recipient" not in event
            assert event["sender"] == alice
            assert event["file"]["size"] == len(content)
            file_id = response.json()["file_id"]
            ids.append(file_id)
            assert event["file"]["id"] == file_id
            result = client.get(f"/api/files/{file_id}", headers=bh)
            assert result.content == content and result.status_code == 200
            assert result.headers["content-disposition"].startswith("attachment;")
            assert result.headers["cache-control"] == "no-store"
            assert result.headers["x-content-type-options"] == "nosniff"
            assert result.headers["content-type"] == "application/octet-stream"
        assert len(set(ids)) == 3
        with client.websocket_connect("/ws") as later:
            _, lh = enter(client, later, "后来加入")
            assert client.get(f"/api/files/{ids[0]}", headers=lh).content == b"first"


def test_private_file_permissions_and_sender_leaves(client):
    with client.websocket_connect("/ws") as b, client.websocket_connect("/ws") as c:
        bob, bh = enter(client, b, "李四四")
        _, ch = enter(client, c, "王五五")
        drain_join(b)
        with client.websocket_connect("/ws") as a:
            _, ah = enter(client, a, "张三三")
            drain_join(b, c)
            response = upload(client, ah, b"private bytes", to=bob["id"])
            assert response.status_code == 201
            file_id = response.json()["file_id"]
            event = a.receive_json()
            assert event == b.receive_json()
            assert event["recipient"] == bob
            assert client.get(f"/api/files/{file_id}", headers=ah).content == b"private bytes"
            assert client.get(f"/api/files/{file_id}", headers=bh).content == b"private bytes"
            assert client.get(f"/api/files/{file_id}", headers=ch).status_code == 403
            a.send_json({"type": "chat", "text": "public marker"})
            # If a private file had leaked, C's next event would be 'file'.
            assert c.receive_json()["text"] == "public marker"
            a.receive_json()
            b.receive_json()
        drain_join(b, c)
        # Account sessions survive a dropped connection, so the sender can still download.
        assert client.get(f"/api/files/{file_id}", headers=ah).content == b"private bytes"
        assert client.get(f"/api/files/{file_id}", headers=bh).content == b"private bytes"
        with client.websocket_connect("/ws") as outsider:
            _, oh = enter(client, outsider, "外人甲")
            assert client.get(f"/api/files/{file_id}", headers=oh).status_code == 403
        with client.websocket_connect("/ws") as again:
            # A fresh connection for the same account keeps its file access.
            _, same = enter(client, again, "张三三", register=False)
            assert client.get(f"/api/files/{file_id}", headers=same).content == b"private bytes"


@pytest.mark.parametrize("authorization", [None, "Bearer unknown", "Bearer", "Basic abc", "Bearer é"])
def test_missing_and_invalid_credentials(client, authorization):
    headers = {"Authorization": authorization.encode("utf-8")} if authorization is not None else {}
    assert client.post("/api/files", headers=headers, content=b"x").status_code == 401
    assert client.get("/api/files/nonexistent", headers=headers).status_code == 401
    assert_clean(client)


def test_public_id_is_not_a_credential(client):
    with client.websocket_connect("/ws") as ws:
        user, _ = enter(client, ws, "测试员")
        assert upload(client, {"Authorization": f"Bearer {user['id']}"}).status_code == 401


def test_invalid_targets_and_missing_file(client):
    with client.websocket_connect("/ws") as ws:
        user, headers = enter(client, ws, "测试员")
        assert upload(client, headers, to=user["id"]).status_code == 400
        assert upload(client, headers, to="not-existing").status_code == 404
        assert client.get("/api/files/not-found", headers=headers).status_code == 404
        assert_clean(client)


def test_size_limit_and_false_length(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "测试员")
        assert upload(client, headers, b"x" * (MAX_FILE_SIZE + 1)).status_code == 413
        # Unknown-length/chunked data must also be counted by the server.
        client.app.state.files.max_size = 8
        response = upload(client, headers, iter([b"12345", b"6789"]))
        assert response.status_code == 413
        assert_clean(client)
        client.app.state.files.max_size = MAX_FILE_SIZE
        response = upload(client, headers, b"x" * MAX_FILE_SIZE)
        assert response.status_code == 201
        event = ws.receive_json()
        assert event["file"]["size"] == MAX_FILE_SIZE


@pytest.mark.parametrize("name,expected", [
    ("../../outside.txt", "outside.txt"),
    ("C:\\temp\\资料.txt", "资料.txt"),
    ("\r\n\x00", "file"), ("...", "file"),
    ("<script>.html", "script.html"), ("", "file"),
])
def test_safe_names(client, name, expected):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "测试员")
        assert upload(client, headers, name=name).status_code == 201
        event = ws.receive_json()
        assert event["file"]["name"] == expected
        assert [p.name for p in client.app.state.files.root.iterdir()] == [event["file"]["id"]]


def test_quota_and_reservation_cleanup(client):
    store = client.app.state.files
    store.max_size = 8
    store.limit = 10
    with client.websocket_connect("/ws") as ws:
        user, headers = enter(client, ws, "测试员")
        store.reserve("another upload")
        assert upload(client, headers, b"x").status_code == 507
        store.uploading.clear()
        assert upload(client, headers, b"12345678").status_code == 201
        ws.receive_json()
        assert upload(client, headers, b"x").status_code == 507
        assert store.used == 8 and not store.uploading
        store.used = 0
        store.reserve(user["user_id"])
        assert upload(client, headers, b"x").status_code == 409
        store.uploading.clear()


def test_write_failure_cleans_partial_file(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "测试员")
        with patch("backend.files.anyio.open_file", side_effect=OSError("disk full")):
            assert upload(client, headers).status_code == 507
        assert_clean(client)
        assert upload(client, headers).status_code == 201


def test_file_content_and_capacity_survive_restart(tmp_path):
    app = create_app(db_path=tmp_path / "chat.db")
    with TestClient(app) as client:
        root = app.state.files.root
        with client.websocket_connect("/ws") as ws:
            _, headers = enter(client, ws, "测试员")
            file_id = upload(client, headers).json()["file_id"]
            assert (root / file_id).is_file()
    assert (root / file_id).is_file()
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as client, \
            client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "测试员", register=False)
        result = client.get(f"/api/files/{file_id}", headers=headers)
        assert result.status_code == 200 and result.content == b"hello"
        assert client.app.state.files.used == 5


async def asgi_upload(app, headers, receive):
    """Drive HTTP disconnect and interleaving deterministically, without buffering a generator."""
    sent = []
    async def send(message):
        sent.append(message)
    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": "POST", "scheme": "http", "path": "/api/files", "raw_path": b"/api/files",
               "query_string": b"filename=partial.txt", "root_path": "",
               "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
               "server": ("test", 80), "client": ("test", 123)}, receive, send)
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


def test_interrupted_upload_cleans_file(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "测试员")
        events = iter([{"type": "http.request", "body": b"partial", "more_body": True},
                       {"type": "http.disconnect"}])
        async def receive():
            return next(events)
        assert client.portal.call(asgi_upload, client.app, headers, receive) == 400
        assert_clean(client)


def test_sender_leaves_during_upload(client):
    with client.websocket_connect("/ws") as ws:
        user, headers = enter(client, ws, "测试员")
        async def receive():
            await client.app.state.room.remove(client.app.state.room.connections(user["user_id"])[0])
            return {"type": "http.request", "body": b"complete", "more_body": False}
        assert client.portal.call(asgi_upload, client.app, headers, receive) == 401
        assert_clean(client)


def test_target_leaves_during_upload(client):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        _, headers = enter(client, a, "发送者")
        target, _ = enter(client, b, "目标者")
        drain_join(a)
        # Simulate the target disappearing while the HTTP body is being consumed.
        from starlette.requests import Request
        original = Request.stream
        async def interleaved(request):
            await request.app.state.room.remove(request.app.state.room.connections(target["user_id"])[0])
            async for chunk in original(request):
                yield chunk
        with patch.object(Request, "stream", interleaved):
            assert upload(client, headers, to=target["id"]).status_code == 201
        assert len(client.app.state.files.records) == 1


def test_cancelled_connection_still_notifies_other_users(tmp_path):
    async def scenario():
        app = create_app(db_path=tmp_path / "chat.db")
        database = Database(tmp_path / "chat.db")
        app.state.db = database
        account = database.create_user("离开者", "password123")
        token = database.create_session(account["id"])
        observer = Peer(AsyncMock(), "user-1", "observer", "观察者")
        app.state.room.peers[observer.id] = observer
        incoming = iter([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": json.dumps({"type": "auth", "token": token})},
        ])
        async def receive():
            try:
                return next(incoming)
            except StopIteration:
                cancellation.cancel()
                return {"type": "websocket.disconnect", "code": 1000}
        async def send(message):
            pass
        with anyio.CancelScope() as cancellation:
            await app({"type": "websocket", "asgi": {"version": "3.0"},
                       "scheme": "ws", "path": "/ws", "raw_path": b"/ws",
                       "root_path": "", "query_string": b"", "headers": [],
                       "client": ("test", 1), "server": ("test", 80),
                       "subprotocols": []}, receive, send)
        updates = [call.args[0] for call in observer.socket.send_json.call_args_list if call.args[0]["type"] == "users"]
        assert len(updates) == 2
        assert len(updates[0]["users"]) == 2
        assert updates[1]["users"] == [observer.public()]
        assert list(app.state.room.peers) == [observer.id]
    anyio.run(scenario)
