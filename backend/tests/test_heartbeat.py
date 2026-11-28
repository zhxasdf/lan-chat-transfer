import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as value:
        yield value


def login(client, name):
    payload = {"username": name, "password": "password123"}
    assert client.post("/api/auth/register", json=payload).status_code == 201
    return client.post("/api/auth/login", json=payload).json()["token"]


def enter(ws, token):
    ws.send_json({"type": "auth", "token": token})
    assert ws.receive_json()["type"] == "welcome"
    assert ws.receive_json()["type"] == "users"
    assert ws.receive_json()["type"] == "notice"


def test_ping_returns_pong(client):
    with client.websocket_connect("/ws") as ws:
        enter(ws, login(client, "alice"))
        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["type"] == "pong" and pong["time"]


def test_heartbeat_closes_silent_connection_and_allows_reconnect(tmp_path):
    app = create_app(db_path=tmp_path / "chat.db", heartbeat_interval=0.05, heartbeat_timeout=0.15)
    with TestClient(app) as client:
        token = login(client, "alice")
        with client.websocket_connect("/ws") as ws:
            enter(ws, token)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4002
        # The session itself stays valid: a reconnect authenticates again.
        with client.websocket_connect("/ws") as again:
            enter(again, token)
            again.send_json({"type": "ping"})
            assert again.receive_json()["type"] == "pong"


def test_active_connection_survives_heartbeat_window(tmp_path):
    app = create_app(db_path=tmp_path / "chat.db", heartbeat_interval=0.05, heartbeat_timeout=0.2)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        enter(ws, login(client, "alice"))
        for _ in range(4):
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"
            time.sleep(0.08)
        ws.send_json({"type": "chat", "text": "still alive"})
        assert ws.receive_json()["text"] == "still alive"


def test_public_catch_up_after_cursor(client):
    token = login(client, "alice")
    headers = {"Authorization": f"Bearer {token}"}
    with client.websocket_connect("/ws") as ws:
        enter(ws, token)
        ids = []
        for index in range(3):
            ws.send_json({"type": "chat", "text": f"消息{index}"})
            event = ws.receive_json()
            ids.append(event["id"])
        latest = client.get("/api/history", params={"kind": "public"}, headers=headers).json()
        assert latest["next_after"] == ids[-1]
        catch_up = client.get(
            "/api/history", params={"kind": "public", "after": ids[0]}, headers=headers
        ).json()
        assert [m["text"] for m in catch_up["messages"]] == ["消息1", "消息2"]
        assert catch_up["has_newer"] is False
        assert catch_up["next_after"] == ids[-1]
        assert client.get(
            "/api/history", params={"kind": "public"}, headers={"Authorization": "Bearer nope"}
        ).status_code == 401


def test_offline_message_is_caught_up_after_reconnect(client):
    alice_token, bob_token = login(client, "alice"), login(client, "bob")
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}
    bob_id = client.get("/api/auth/me", headers=bob_headers).json()["user"]["id"]
    with client.websocket_connect("/ws") as a:
        enter(a, alice_token)
        a.send_json({"type": "private", "to": bob_id, "text": "while away"})
        event = a.receive_json()
        assert event["queued"] is True
    with client.websocket_connect("/ws") as b:
        enter(b, bob_token)
        page = client.get(
            "/api/history",
            params={"kind": "private", "peer": client.get("/api/auth/me", headers=alice_headers).json()["user"]["id"], "unread": "true"},
            headers=bob_headers,
        ).json()
        assert [m["text"] for m in page["messages"]] == ["while away"]
        assert page["next_after"] == event["id"]
