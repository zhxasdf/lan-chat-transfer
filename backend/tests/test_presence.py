"""Account presence and delivery across simultaneous browser connections."""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.main import ChatRoom, Peer, create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as value:
        yield value


def login(client, name, register=True):
    payload = {"username": name, "password": "password123"}
    if register:
        assert client.post("/api/auth/register", json=payload).status_code == 201
    return client.post("/api/auth/login", json=payload).json()["token"]


def enter(stack, client, token, existing=(), duplicate=False):
    ws = stack.enter_context(client.websocket_connect("/ws"))
    ws.send_json({"type": "auth", "token": token})
    user = ws.receive_json()["user"]
    assert ws.receive_json()["type"] == "users"
    if not duplicate:
        assert ws.receive_json()["type"] == "notice"
        for other in existing:
            assert other.receive_json()["type"] == "users"
            assert other.receive_json()["type"] == "notice"
    return ws, user


def test_multiple_tabs_share_identity_and_first_last_presence(client):
    alice_token, bob_token = login(client, "alice"), login(client, "bob")
    with ExitStack() as stack:
        observer, _ = enter(stack, client, bob_token)
        with ExitStack() as original:
            a, user = enter(original, client, alice_token, [observer])
            with ExitStack() as extra:
                sibling, same = enter(extra, client, alice_token, duplicate=True)
                assert same == user
                assert user["id"] == user["user_id"]
                assert len(client.app.state.room.users_event()["users"]) == 2
                # A group message must be next: opening a second tab emits no join notice.
                sibling.send_json({"type": "chat", "text": "both tabs"})
                event = sibling.receive_json()
                assert event == a.receive_json() == observer.receive_json()
            a.send_json({"type": "chat", "text": "still online"})
            assert a.receive_json()["text"] == "still online"
            assert observer.receive_json()["text"] == "still online"
        assert len(observer.receive_json()["users"]) == 1
        assert observer.receive_json()["text"] == "alice 离开了聊天室"


@pytest.mark.parametrize("kind", ["private", "file"])
def test_private_delivery_fans_out_to_both_accounts_only(client, kind):
    ta, tb, tc = [login(client, name) for name in ("alice", "bob", "charlie")]
    with ExitStack() as stack:
        a, alice = enter(stack, client, ta)
        b, bob = enter(stack, client, tb, [a])
        c, _ = enter(stack, client, tc, [a, b])
        a2, _ = enter(stack, client, ta, duplicate=True)
        with ExitStack() as old_target:
            b2, _ = enter(old_target, client, tb, duplicate=True)
            if kind == "private":
                a.send_json({"type": kind, "to": bob["id"], "text": "private sync"})
            else:
                response = client.post("/api/files", params={"filename": "sync.txt", "to": bob["id"]},
                                       headers={"Authorization": f"Bearer {ta}", "Content-Type": "application/octet-stream"},
                                       content=b"synced contents")
                assert response.status_code == 201
            event = a.receive_json()
            assert event["type"] == kind
            assert event == a2.receive_json() == b.receive_json() == b2.receive_json()
            if kind == "file":
                endpoint = f'/api/files/{event["file"]["id"]}'
                assert client.get(endpoint, headers={"Authorization": f"Bearer {tb}"}).content == b"synced contents"
                assert client.get(endpoint, headers={"Authorization": f"Bearer {tc}"}).status_code == 403
        # Closing one target connection must preserve the selected account ID.
        a.send_json({"type": "private", "to": bob["id"], "text": "after tab close"})
        assert a.receive_json() == a2.receive_json() == b.receive_json()
        a2.send_json({"type": "private", "to": alice["id"], "text": "self"})
        assert "自己" in a2.receive_json()["message"]
        a.send_json({"type": "chat", "text": "public marker"})
        for ws in (a, a2, b, c):
            assert ws.receive_json()["text"] == "public marker"


def test_logout_closes_shared_session_but_keeps_independent_login(client):
    shared = login(client, "alice")
    separate = login(client, "alice", register=False)
    with ExitStack() as stack:
        a, _ = enter(stack, client, shared)
        a2, _ = enter(stack, client, shared, duplicate=True)
        independent, _ = enter(stack, client, separate, duplicate=True)
        assert client.post("/api/auth/logout", headers={"Authorization": f"Bearer {shared}"}).status_code == 204
        for ws in (a, a2):
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4001
        independent.send_json({"type": "chat", "text": "independent session"})
        assert independent.receive_json()["text"] == "independent session"
        assert len(client.app.state.room.users_event()["users"]) == 1
        revoked = stack.enter_context(client.websocket_connect("/ws"))
        revoked.send_json({"type": "auth", "token": shared})
        assert revoked.receive_json()["type"] == "error"


def test_failed_tab_preserves_account_presence():
    async def scenario():
        room = ChatRoom()
        broken = Peer(AsyncMock(), "alice", "alice", "Alice")
        sibling = Peer(AsyncMock(), "alice", "alice", "Alice")
        observer = Peer(AsyncMock(), "bob", "bob", "Bob")
        broken.socket.send_json.side_effect = RuntimeError("closed")
        room.peers = {p.id: p for p in (broken, sibling, observer)}
        async with room.lock:
            await room.publish([{"type": "chat", "text": "hello"}])
        assert len(room.users_event()["users"]) == 2
        assert sibling.socket.send_json.call_count == observer.socket.send_json.call_count == 1
        assert room.connections("alice") == [sibling]
    asyncio.run(scenario())
