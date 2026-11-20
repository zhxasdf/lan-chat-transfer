import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.main import ChatRoom, Peer, create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as value:
        yield value


def join(ws, name):
    ws.send_json({"type": "join", "nickname": name})
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    users = ws.receive_json()
    assert users["type"] == "users"
    assert ws.receive_json()["type"] == "notice"
    return welcome["user"], users["users"]


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_three_users_broadcast_and_disconnect(client):
    with client.websocket_connect("/ws") as a:
        alice, _ = join(a, "张三")
        with client.websocket_connect("/ws") as b:
            bob, users = join(b, "李四")
            assert len(users) == 2
            assert a.receive_json()["users"] == users
            a.receive_json()
            with client.websocket_connect("/ws") as c:
                _, users = join(c, "王五")
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
                assert "王五" in ws.receive_json()["text"]
        assert len(a.receive_json()["users"]) == 1
        a.receive_json()
        a.send_json({"type": "chat", "text": "仍能聊天"})
        assert a.receive_json()["text"] == "仍能聊天"


def test_nicknames_and_reuse(client):
    with client.websocket_connect("/ws") as a:
        join(a, " Alice ")
        with client.websocket_connect("/ws") as b:
            b.send_json({"type": "join", "nickname": "Alice"})
            assert b.receive_json()["type"] == "error"
            _, users = join(b, "Bob")
            assert {p["nickname"] for p in users} == {"Alice", "Bob"}
    with client.websocket_connect("/ws") as c:
        _, users = join(c, "Alice")
        assert len(users) == 1


@pytest.mark.parametrize("event", [
    {"type": "join", "nickname": " "},
    {"type": "join", "nickname": "a" * 21},
    {"type": "join", "nickname": 12},
    {"type": "chat", "text": "还没进入"},
    {"type": "unknown"}, [], None,
])
def test_invalid_join_and_events(client, event):
    with client.websocket_connect("/ws") as ws:
        ws.send_json(event)
        assert ws.receive_json()["type"] == "error"
        join(ws, "正常用户")


@pytest.mark.parametrize("text", ["", "  ", "x" * 2001, 123, None])
def test_invalid_messages(client, text):
    with client.websocket_connect("/ws") as ws:
        join(ws, "测试")
        ws.send_json({"type": "chat", "text": text})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "chat", "text": "正常"})
        assert ws.receive_json()["text"] == "正常"


def test_invalid_json_and_duplicate_join(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        join(ws, "用户")
        ws.send_json({"type": "join", "nickname": "另一个"})
        assert ws.receive_json()["type"] == "error"


def test_boundaries_and_binary(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(b"binary")
        assert ws.receive_json()["type"] == "error"
        join(ws, "名" * 20)
        ws.send_json({"type": "chat", "text": "🙂" * 2000})
        assert ws.receive_json()["text"] == "🙂" * 2000


def test_failed_recipient_does_not_block_others():
    async def scenario():
        room = ChatRoom()
        good = Peer(AsyncMock(), "正常")
        bad = Peer(AsyncMock(), "失效")
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
