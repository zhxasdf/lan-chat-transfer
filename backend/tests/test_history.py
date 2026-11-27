import pytest
from fastapi.testclient import TestClient

from backend.main import create_app

PASSWORD = "password123"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as value:
        yield value


def account(client, name):
    response = client.post(
        "/api/auth/register", json={"username": name, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    data = client.post(
        "/api/auth/login", json={"username": name, "password": PASSWORD}
    ).json()
    return data["user"], data["token"], {"Authorization": f"Bearer {data['token']}"}


def enter(ws, token):
    ws.send_json({"type": "auth", "token": token})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    users = ws.receive_json()
    assert users["type"] == "users"
    assert ws.receive_json()["type"] == "notice"
    return welcome["user"]


def drain(ws):
    assert ws.receive_json()["type"] == "users"
    assert ws.receive_json()["type"] == "notice"


def test_public_history_and_pagination(client):
    alice, at, ah = account(client, "alice")
    bob, bt, bh = account(client, "bob")
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        enter(a, at)
        enter(b, bt)
        drain(a)
        for index in range(5):
            a.send_json({"type": "chat", "text": f"消息{index}"})
            a.receive_json()
            b.receive_json()
        body = client.get(
            "/api/history", params={"kind": "public", "limit": 2}, headers=ah
        ).json()
        assert [m["text"] for m in body["messages"]] == ["消息3", "消息4"]
        assert body["has_more"] is True
        oldest = body["messages"][0]["id"]
        older = client.get(
            "/api/history",
            params={"kind": "public", "limit": 10, "before": oldest},
            headers=bh,
        ).json()
        assert [m["text"] for m in older["messages"]] == ["消息0", "消息1", "消息2"]
        assert older["has_more"] is False
        assert all(m["type"] == "chat" for m in older["messages"])
        assert older["messages"][0]["sender"]["username"] == "alice"


def test_private_history_permissions(client):
    alice, at, ah = account(client, "alice")
    bob, bt, bh = account(client, "bob")
    carol, ct, ch = account(client, "carol")
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        enter(a, at)
        bob_connection = enter(b, bt)
        drain(a)
        a.send_json({"type": "private", "to": bob_connection["id"], "text": "只给bob"})
        a.receive_json()
        b.receive_json()
        history = client.get(
            "/api/history", params={"kind": "private", "peer": bob["id"]}, headers=ah
        ).json()
        assert [m["text"] for m in history["messages"]] == ["只给bob"]
        message = history["messages"][0]
        assert message["type"] == "private"
        assert message["sender"]["username"] == "alice"
        assert message["recipient"]["username"] == "bob"
        reverse = client.get(
            "/api/history", params={"kind": "private", "peer": alice["id"]}, headers=bh
        ).json()
        assert [m["text"] for m in reverse["messages"]] == ["只给bob"]
        # A third account can only ever see its own conversation with the peer.
        assert client.get(
            "/api/history", params={"kind": "private", "peer": bob["id"]}, headers=ch
        ).json()["messages"] == []
        assert client.get(
            "/api/history", params={"kind": "private", "peer": alice["id"]}, headers=ch
        ).json()["messages"] == []
        assert client.get(
            "/api/history", params={"kind": "private"}, headers=ah
        ).status_code == 400


def test_file_message_history(client):
    alice, at, ah = account(client, "alice")
    with client.websocket_connect("/ws") as a:
        enter(a, at)
        response = client.post(
            "/api/files",
            params={"filename": "资料.txt"},
            headers={**ah, "Content-Type": "application/octet-stream"},
            content=b"hello",
        )
        assert response.status_code == 201
        file_id = response.json()["file_id"]
        event = a.receive_json()
        history = client.get(
            "/api/history", params={"kind": "public"}, headers=ah
        ).json()
        assert len(history["messages"]) == 1
        message = history["messages"][0]
        assert message["type"] == "file"
        assert message["id"] == event["id"]
        assert message["file"] == event["file"]
        assert message["file"]["status"] == "available"


def test_history_survives_restart(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as client:
        _, token, _ = account(client, "alice")
        with client.websocket_connect("/ws") as ws:
            enter(ws, token)
            ws.send_json({"type": "chat", "text": "重启前的消息"})
            ws.receive_json()
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as client:
        data = client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        ).json()
        headers = {"Authorization": f"Bearer {data['token']}"}
        history = client.get(
            "/api/history", params={"kind": "public"}, headers=headers
        ).json()
        assert [m["text"] for m in history["messages"]] == ["重启前的消息"]


def test_history_requires_auth(client):
    assert client.get("/api/history", params={"kind": "public"}).status_code == 401
    _, _, headers = account(client, "alice")
    assert client.get(
        "/api/history", params={"kind": "nope"}, headers=headers
    ).status_code == 400
