import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.main import ChatRoom, Peer, create_app

PASSWORD = "password123"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as value:
        yield value


def account(client, username):
    response = client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    token = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    ).json()["token"]
    return token


def join(client, ws, name):
    token = account(client, name)
    ws.send_json({"type": "auth", "token": token})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    users = ws.receive_json()
    assert users["type"] == "users"
    assert ws.receive_json()["type"] == "notice"
    return welcome["user"], users["users"]


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_username_and_login(client):
    created = client.post(
        "/api/auth/register", json={"username": " Alice ", "password": PASSWORD}
    )
    assert created.status_code == 201
    assert created.json()["user"]["username"] == "Alice"
    duplicate = client.post(
        "/api/auth/register", json={"username": "alice", "password": PASSWORD}
    )
    assert duplicate.status_code == 400
    assert client.post(
        "/api/auth/login", json={"username": "Alice", "password": "wrongpass1"}
    ).status_code == 401
    token = client.post(
        "/api/auth/login", json={"username": "Alice", "password": PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).json()["user"]["username"] == "Alice"
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401


@pytest.mark.parametrize("payload", [
    {"username": " ", "password": PASSWORD},
    {"username": "a" * 21, "password": PASSWORD},
    {"username": 12, "password": PASSWORD},
    {"username": "okname", "password": "short"},
    {"username": "okname", "password": 123},
    {"username": "okname"},
])
def test_registration_validation(client, payload):
    assert client.post("/api/auth/register", json=payload).status_code == 400


@pytest.mark.parametrize("event", [
    {"type": "chat", "text": "还没登录"},
    {"type": "unknown"}, [], None,
])
def test_unauthenticated_events(client, event):
    with client.websocket_connect("/ws") as ws:
        ws.send_json(event)
        assert ws.receive_json()["type"] == "error"
        join(client, ws, "正常用户")


def test_three_users_broadcast_and_disconnect(client):
    with client.websocket_connect("/ws") as a:
        alice, _ = join(client, a, "张三三")
        with client.websocket_connect("/ws") as b:
            bob, users = join(client, b, "李四四")
            assert len(users) == 2
            assert a.receive_json()["users"] == users
            a.receive_json()
            with client.websocket_connect("/ws") as c:
                _, users = join(client, c, "王五五")
                assert len(users) == 3
                for ws in (a, b):
                    assert ws.receive_json()["users"] == users
                    ws.receive_json()
                text = "你好🙂\n<script>alert(1)</script>"
                a.send_json({"type": "chat", "text": text, "sender": bob})
                events = [ws.receive_json() for ws in (a, b, c)]
                assert events[0] == events[1] == events[2]
                assert events[0]["sender"] == alice
                assert events[0]["text"] == text
                assert events[0]["id"] and events[0]["time"]
                # The next received event must be the next broadcast, not a duplicate.
                b.send_json({"type": "chat", "text": "收到"})
                assert all(ws.receive_json()["text"] == "收到" for ws in (a, b, c))
            for ws in (a, b):
                assert len(ws.receive_json()["users"]) == 2
                assert "王五五" in ws.receive_json()["text"]
        assert len(a.receive_json()["users"]) == 1
        a.receive_json()
        a.send_json({"type": "chat", "text": "仍能聊天"})
        assert a.receive_json()["text"] == "仍能聊天"


@pytest.mark.parametrize("text", ["", "  ", "x" * 2001, 123, None])
def test_invalid_messages(client, text):
    with client.websocket_connect("/ws") as ws:
        join(client, ws, "测试员")
        ws.send_json({"type": "chat", "text": text})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "chat", "text": "正常"})
        assert ws.receive_json()["text"] == "正常"


def test_invalid_json_and_duplicate_auth(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        token = account(client, "普通用户")
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "welcome"
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "error"


def test_invalid_auth_tokens(client):
    with client.websocket_connect("/ws") as ws:
        for token in ["", "not-a-real-token"]:
            ws.send_json({"type": "auth", "token": token})
            assert ws.receive_json()["type"] == "error"
        join(client, ws, "重试用户")


def test_boundaries_and_binary(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"binary")
        assert ws.receive_json()["type"] == "error"
        join(client, ws, "名" * 20)
        ws.send_json({"type": "chat", "text": "🙂" * 2000})
        assert ws.receive_json()["text"] == "🙂" * 2000


def test_failed_recipient_does_not_block_others():
    async def scenario():
        room = ChatRoom()
        good = Peer(AsyncMock(), "user-1", "normal", "正常者")
        bad = Peer(AsyncMock(), "user-2", "broken", "失效者")
        bad.socket.send_json.side_effect = RuntimeError("closed")
        room.peers = {p.id: p for p in (good, bad)}
        async with room.lock:
            await room.publish([{"type": "chat", "text": "hello"}])
        assert list(room.peers) == [good.id]
        assert good.socket.send_json.call_args_list[0].args[0]["text"] == "hello"
        assert good.socket.send_json.call_args_list[1].args[0]["users"] == [good.public()]
        await room.remove(bad)
        assert len(room.peers) == 1
    asyncio.run(scenario())


def test_private_chat_reaches_only_sender_and_recipient(client):
    with client.websocket_connect("/ws") as a:
        alice, _ = join(client, a, "张三三")
        with client.websocket_connect("/ws") as b:
            bob, users = join(client, b, "李四四")
            assert len(users) == 2
            a.receive_json()
            a.receive_json()
            with client.websocket_connect("/ws") as c:
                _, users = join(client, c, "王五五")
                assert len(users) == 3
                for ws in (a, b):
                    ws.receive_json()
                    ws.receive_json()
                a.send_json({"type": "private", "to": bob["id"], "text": "只给李四"})
                event_a = a.receive_json()
                assert event_a == b.receive_json()
                assert event_a["type"] == "private"
                assert event_a["sender"] == alice
                assert event_a["recipient"] == bob
                assert event_a["text"] == "只给李四"
                assert event_a["id"] and event_a["time"]
                # 王五没有收到这条私聊：它读到的下一条事件必须是随后的群聊。
                a.send_json({"type": "chat", "text": "公开消息"})
                for ws in (a, b, c):
                    assert ws.receive_json()["type"] == "chat"


def test_private_chat_rejects_bad_targets(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "private", "to": "someone", "text": "还没进入"})
        assert "请先登录" in ws.receive_json()["message"]
        me, _ = join(client, ws, "张三三")
        for target in ["", None, "missing-user", me["id"]]:
            ws.send_json({"type": "private", "to": target, "text": "你好"})
            assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "private", "text": "缺少目标"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "chat", "text": "仍能聊天"})
        assert ws.receive_json()["text"] == "仍能聊天"


@pytest.mark.parametrize("text", ["", "  ", "x" * 2001, 123, None])
def test_private_chat_rejects_invalid_text(client, text):
    with client.websocket_connect("/ws") as a:
        alice, _ = join(client, a, "张三三")
        with client.websocket_connect("/ws") as b:
            bob, _ = join(client, b, "李四四")
            a.receive_json()
            a.receive_json()
            a.send_json({"type": "private", "to": bob["id"], "text": text})
            assert a.receive_json()["type"] == "error"
            b.send_json({"type": "private", "to": alice["id"], "text": "正常私聊"})
            assert b.receive_json()["type"] == "private"
            assert a.receive_json()["text"] == "正常私聊"


def test_private_chat_boundary_text(client):
    with client.websocket_connect("/ws") as a:
        _, _ = join(client, a, "张三三")
        with client.websocket_connect("/ws") as b:
            bob, _ = join(client, b, "李四四")
            a.receive_json()
            a.receive_json()
            text = "🙂" * 2000
            a.send_json({"type": "private", "to": bob["id"], "text": text})
            assert a.receive_json()["text"] == text
            assert b.receive_json()["text"] == text


def test_private_chat_to_offline_user(client):
    with client.websocket_connect("/ws") as a:
        _, _ = join(client, a, "张三三")
        with client.websocket_connect("/ws") as b:
            bob, _ = join(client, b, "李四四")
            a.receive_json()
            a.receive_json()
        assert len(a.receive_json()["users"]) == 1
        a.receive_json()
        a.send_json({"type": "private", "to": bob["id"], "text": "还在吗"})
        assert "对方不在线" in a.receive_json()["message"]
        a.send_json({"type": "chat", "text": "仍在群聊"})
        assert a.receive_json()["text"] == "仍在群聊"


def test_private_delivery_cleans_failed_recipient():
    async def scenario():
        room = ChatRoom()
        sender = Peer(AsyncMock(), "user-1", "sender", "发送者")
        target = Peer(AsyncMock(), "user-2", "target", "失效目标")
        target.socket.send_json.side_effect = RuntimeError("closed")
        room.peers = {p.id: p for p in (sender, target)}
        async with room.lock:
            await room.deliver([sender, target], {"type": "private", "text": "你好"})
        assert list(room.peers) == [sender.id]
        events = [call.args[0] for call in sender.socket.send_json.call_args_list]
        assert events[0] == {"type": "private", "text": "你好"}
        assert events[1]["users"] == [sender.public()]
        assert "失效目标" in events[2]["text"]
    asyncio.run(scenario())
