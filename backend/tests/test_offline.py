"""Offline text delivery, private inbox authorization and durable read cursors."""
from contextlib import ExitStack
from unittest.mock import patch
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as value:
        yield value


def account(client, name):
    payload = {"username": name, "password": "password123"}
    created = client.post("/api/auth/register", json=payload)
    assert created.status_code == 201
    token = client.post("/api/auth/login", json=payload).json()["token"]
    return created.json()["user"]["id"], {"Authorization": f"Bearer {token}"}


def connect(stack, client, headers, duplicate=False, observers=()):
    ws = stack.enter_context(client.websocket_connect("/ws"))
    ws.send_json({"type": "auth", "token": headers["Authorization"].split()[1]})
    assert ws.receive_json()["type"] == "welcome"
    assert ws.receive_json()["type"] == "users"
    if not duplicate:
        assert ws.receive_json()["type"] == "notice"
        for other in observers:
            other.receive_json()
            other.receive_json()
    return ws


def send(ws, target, text="offline hello"):
    ws.send_json({"type": "private", "to": target, "text": text})
    event = ws.receive_json()
    assert event["type"] == "private"
    return event


def inbox(client, headers):
    return client.get("/api/conversations", headers=headers).json()["items"]


def test_lookup_auth_and_exact_match(client):
    alice, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    assert client.get("/api/users/lookup", params={"username": "bob"}).status_code == 401
    result = client.get("/api/users/lookup", params={"username": " BOB "}, headers=ha)
    assert result.json()["user"]["id"] == bob
    assert set(result.json()["user"]) == {"id", "user_id", "username", "nickname"}
    assert client.get("/api/users/lookup", params={"username": "bo"}, headers=ha).status_code == 404
    assert client.get("/api/users/lookup", params={"username": "alice"}, headers=ha).status_code == 400
    assert inbox(client, hb) == []


def test_offline_saved_and_only_participants_see_it(client):
    alice, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    _, hc = account(client, "charlie")
    with ExitStack() as stack:
        a = connect(stack, client, ha)
        event = send(a, bob)
        assert event["queued"] is True
        assert inbox(client, ha)[0]["unread"] == 0
        assert inbox(client, hb)[0]["unread"] == 1
        assert inbox(client, hb)[0]["peer"]["id"] == alice
        assert inbox(client, hc) == []
        for headers, peer in [(ha, bob), (hb, alice)]:
            response = client.get("/api/history", params={"kind": "private", "peer": peer}, headers=headers)
            assert response.json()["messages"][0]["id"] == event["id"]
        assert client.get("/api/history", params={"kind": "private", "peer": alice}, headers=hc).json()["messages"] == []


def test_backlog_pages_do_not_clear_unseen_messages(client):
    alice, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    with ExitStack() as stack:
        a = connect(stack, client, ha)
        ids = [send(a, bob, f"message-{index}")["id"] for index in range(105)]
    params = {"kind": "private", "peer": alice, "unread": "true"}
    page = client.get("/api/history", params=params, headers=hb).json()
    assert [m["id"] for m in page["messages"]] == ids[:50]
    assert page["has_newer"] is True
    assert inbox(client, hb)[0]["unread"] == 105  # Fetching never marks read.
    endpoint = f"/api/conversations/{alice}/read"
    assert client.post(endpoint, json={"through": ids[49]}, headers=hb).status_code == 204
    assert inbox(client, hb)[0]["unread"] == 55
    assert client.post(endpoint, json={"through": ids[0]}, headers=hb).status_code == 204
    assert inbox(client, hb)[0]["unread"] == 55  # Older tabs cannot move the cursor backwards.
    second = client.get("/api/history", params={"kind": "private", "peer": alice, "after": ids[49]}, headers=hb).json()
    assert [m["id"] for m in second["messages"]] == ids[50:100]
    final = client.get("/api/history", params={"kind": "private", "peer": alice, "after": ids[99]}, headers=hb).json()
    assert [m["id"] for m in final["messages"]] == ids[100:]
    assert final["has_newer"] is False
    client.post(endpoint, json={"through": ids[-1]}, headers=hb)
    assert inbox(client, hb)[0]["unread"] == 0


def test_read_and_forward_cursor_cannot_cross_conversations(client):
    alice, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    _, hc = account(client, "charlie")
    with ExitStack() as stack:
        event = send(connect(stack, client, ha), bob)
    path = f"/api/conversations/{alice}/read"
    assert client.post(path, json={"through": event["id"]}).status_code == 401
    for payload in [{}, {"through": []}, {"through": "missing"}]:
        assert client.post(path, json=payload, headers=hb).status_code == 400
    assert client.post(path, json={"through": event["id"]}, headers=hc).status_code == 400
    assert client.post(f"/api/conversations/{bob}/read", json={"through": event["id"]}, headers=hb).status_code == 400
    assert client.get("/api/history", params={"kind": "private", "peer": alice, "after": event["id"]}, headers=hc).status_code == 400
    assert inbox(client, hb)[0]["unread"] == 1


def test_unread_and_read_marker_survive_restart(tmp_path):
    path = tmp_path / "chat.db"
    with TestClient(create_app(db_path=path)) as client, ExitStack() as stack:
        alice, ha = account(client, "alice")
        bob, hb = account(client, "bob")
        a = connect(stack, client, ha)
        first, second = send(a, bob), send(a, bob)
        client.post(f"/api/conversations/{alice}/read", json={"through": first["id"]}, headers=hb)
    with TestClient(create_app(db_path=path)) as client:
        assert inbox(client, hb)[0]["unread"] == 1
        page = client.get("/api/history", params={"kind": "private", "peer": alice, "unread": "true"}, headers=hb).json()
        assert [m["id"] for m in page["messages"]] == [second["id"]]


def test_storage_failure_never_acknowledges_success(client):
    _, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    with ExitStack() as stack:
        a = connect(stack, client, ha)
        with patch.object(client.app.state.db, "add_message", side_effect=sqlite3.OperationalError("disk full")):
            a.send_json({"type": "private", "to": bob, "text": "unsaved"})
            assert a.receive_json() == {"type": "error", "message": "消息保存失败，请重试"}
        assert inbox(client, hb) == []
        send(a, bob, "retry succeeds")


def test_subscribed_tabs_receive_shared_read_state(client):
    alice, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    with ExitStack() as stack:
        a = connect(stack, client, ha)
        event = send(a, bob)
        b = connect(stack, client, hb, observers=[a])
        sibling = connect(stack, client, hb, duplicate=True)
        for ws in (b, sibling):
            ws.send_json({"type": "subscribe_inbox"})
            # A subscription refreshes all tabs that have already subscribed.
            assert ws.receive_json()["items"][0]["unread"] == 1
        assert b.receive_json()["items"][0]["unread"] == 1
        client.post(f"/api/conversations/{alice}/read", json={"through": event["id"]}, headers=hb)
        for ws in (b, sibling):
            assert ws.receive_json()["items"][0]["unread"] == 0


def test_offline_file_saved_and_counted_as_unread(client):
    _, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    with ExitStack() as stack:
        connect(stack, client, ha)
        result = client.post("/api/files", params={"to": bob}, headers={**ha, "Content-Type": "application/octet-stream"}, content=b"no offline file")
        assert result.status_code == 201
        assert inbox(client, hb)[0]["unread"] == 1
        file_id = result.json()["file_id"]
        assert client.get(f"/api/files/{file_id}", headers=hb).content == b"no offline file"


def test_first_unread_page_skips_older_outgoing_messages(client):
    alice, ha = account(client, "alice")
    bob, hb = account(client, "bob")
    for index in range(60):
        client.app.state.db.add_message(f"sent-{index}", "private", alice, bob,
                                       "older outgoing", None, None, None, "2026-11-27T00:00:00+00:00")
    with ExitStack() as stack:
        event = send(connect(stack, client, hb), alice, "first incoming")
    page = client.get("/api/history", params={"kind": "private", "peer": bob, "unread": "true"}, headers=ha).json()
    assert [m["id"] for m in page["messages"]] == [event["id"]]
    assert page["has_more"] is True and page["has_newer"] is False
