"""Persistence, atomic metadata, crash recovery and owner deletion."""
import hashlib
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.files import DownloadResponse
from backend.tests.test_files import enter, login, upload, drain_join, assert_clean, asgi_upload


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=tmp_path / "chat.db")) as value:
        yield value


def test_offline_download_permissions_after_restart(tmp_path):
    database = tmp_path / "chat.db"
    contents = b"persistent private bytes\x00\xff"
    with TestClient(create_app(db_path=database)) as client:
        bob, bt = login(client, "recipient")
        _, ct = login(client, "outsider")
        bh, ch = {"Authorization": f"Bearer {bt}"}, {"Authorization": f"Bearer {ct}"}
        with client.websocket_connect("/ws") as ws:
            alice, ah = enter(client, ws, "sender")
            file_id = upload(client, ah, contents, to=bob["id"]).json()["file_id"]
            event = ws.receive_json()
            assert event["queued"] is True
            assert event["file"]["sha256"] == hashlib.sha256(contents).hexdigest()
    with TestClient(create_app(db_path=database)) as client:
        endpoint = f"/api/files/{file_id}"
        assert client.get(endpoint).status_code == 401
        assert client.get(endpoint, headers=ch).status_code == 403
        assert client.get(endpoint, headers=bh).content == contents
        assert client.get(endpoint, headers=ah).content == contents
        assert client.app.state.files.used == len(contents)
        history = client.get("/api/history", params={"kind": "private", "peer": alice["id"]}, headers=bh).json()
        assert history["messages"][0]["file"] == event["file"]


def test_file_and_message_metadata_rollback_together(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        with client.app.state.db._write() as conn:
            conn.execute("CREATE TRIGGER reject_card BEFORE INSERT ON messages "
                         "WHEN NEW.kind = 'file' BEGIN SELECT RAISE(ABORT,'disk error'); END")
        assert upload(client, headers).status_code == 507
        assert client.app.state.db.file_records() == []
        assert client.app.state.db.public_history()[0] == []
        assert_clean(client)
        with client.app.state.db._write() as conn:
            conn.execute("DROP TRIGGER reject_card")
        assert upload(client, headers).status_code == 201


def test_rename_failure_cleans_partial_without_card(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        with patch.object(Path, "replace", side_effect=OSError("rename failed")):
            assert upload(client, headers).status_code == 507
        assert_clean(client)
        assert client.app.state.db.file_records() == []


def test_owner_delete_frees_capacity_and_retains_history(client):
    store = client.app.state.files
    store.max_size, store.limit = 8, 8
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        _, ah = enter(client, a, "sender")
        bob, bh = enter(client, b, "recipient")
        drain_join(a)
        file_id = upload(client, ah, b"12345678", to=bob["id"]).json()["file_id"]
        a.receive_json()
        b.receive_json()
        endpoint = f"/api/files/{file_id}"
        assert client.delete(endpoint).status_code == 401
        assert client.delete(endpoint, headers=bh).status_code == 403
        assert upload(client, ah).status_code == 507
        assert client.delete(endpoint, headers=ah).status_code == 204
        assert a.receive_json()["file"]["status"] == b.receive_json()["file"]["status"] == "deleted"
        assert store.used == 0 and not (store.root / file_id).exists()
        assert client.get(endpoint, headers=bh).status_code == 404
        history = client.get("/api/history", params={"kind": "private", "peer": bob["id"]}, headers=ah).json()
        assert history["messages"][0]["file"]["status"] == "deleted"
        assert client.delete(endpoint, headers=ah).status_code == 204
        a.receive_json()
        b.receive_json()
        assert upload(client, ah).status_code == 201


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_restart_marks_damaged_content_unavailable(tmp_path, damage):
    db_path = tmp_path / "chat.db"
    with TestClient(create_app(db_path=db_path)) as client, client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        file_id = upload(client, headers, b"original").json()["file_id"]
        path = client.app.state.files.root / file_id
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"tampered")  # Same size: recovery must also check SHA-256.
    with TestClient(create_app(db_path=db_path)) as client:
        assert client.get(f"/api/files/{file_id}", headers=headers).status_code == 404
        assert client.app.state.files.used == 0
        assert client.app.state.db.public_history()[0][0]["file"]["status"] == "missing"


def test_restart_cleans_only_managed_residuals(tmp_path):
    db_path, root = tmp_path / "chat.db", tmp_path / "files"
    with TestClient(create_app(db_path=db_path)) as client, client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        keep_id = upload(client, headers, b"keep").json()["file_id"]
        ws.receive_json()
        deleted_id = upload(client, headers, b"delete").json()["file_id"]
        client.app.state.db.set_file_status(deleted_id, "deleted")
    partial, orphan = root / f".part-{uuid4()}", root / str(uuid4())
    partial.write_bytes(b"partial")
    orphan.write_bytes(b"uncommitted")
    unrelated = root / "notes.txt"
    unrelated.write_text("do not touch", encoding="utf-8")
    with TestClient(create_app(db_path=db_path)) as client:
        assert (root / keep_id).read_bytes() == b"keep"
        assert not partial.exists() and not orphan.exists() and not (root / deleted_id).exists()
        assert unrelated.read_text(encoding="utf-8") == "do not touch"
        assert client.app.state.files.used == 4


def test_failed_delete_remains_inaccessible_and_retries(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        file_id = upload(client, headers).json()["file_id"]
        ws.receive_json()
        endpoint = f"/api/files/{file_id}"
        with patch.object(Path, "unlink", side_effect=PermissionError("busy")):
            assert client.delete(endpoint, headers=headers).status_code == 507
        assert client.get(endpoint, headers=headers).status_code == 404
        assert client.app.state.files.used == 5
        assert client.delete(endpoint, headers=headers).status_code == 204
        assert client.app.state.files.used == 0


def test_actual_download_blocks_delete_until_stream_finishes(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        file_id = upload(client, headers, b"streaming content").json()["file_id"]
        ws.receive_json()
        endpoint = f"/api/files/{file_id}"
        started, release = threading.Event(), threading.Event()
        original = DownloadResponse.__call__
        async def held_response(response, scope, receive, send):
            async def held_send(message):
                if message["type"] == "http.response.body":
                    started.set()
                    await asyncio.to_thread(release.wait, 10)
                await send(message)
            await original(response, scope, receive, held_send)
        with patch.object(DownloadResponse, "__call__", held_response), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.get, endpoint, headers=headers)
            try:
                assert started.wait(5)
                assert client.delete(endpoint, headers=headers).status_code == 409
            finally:
                release.set()
            result = future.result(timeout=5)
            assert result.status_code == 200 and result.content == b"streaming content"
        assert client.app.state.files.downloading == {}
        assert client.delete(endpoint, headers=headers).status_code == 204


def test_restart_restores_quota_and_delete_releases_it(tmp_path):
    db_path = tmp_path / "chat.db"
    with TestClient(create_app(db_path=db_path, file_limit=8, storage_limit=8)) as client, client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        file_id = upload(client, headers, b"12345678").json()["file_id"]
    with TestClient(create_app(db_path=db_path, file_limit=8, storage_limit=8)) as client, client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender", register=False)
        assert upload(client, headers, b"x").status_code == 507
        assert client.delete(f"/api/files/{file_id}", headers=headers).status_code == 204
        ws.receive_json()
        assert upload(client, headers, b"x").status_code == 201


def test_native_request_cancellation_during_commit_keeps_committed_content(client):
    with client.websocket_connect("/ws") as ws:
        _, headers = enter(client, ws, "sender")
        entered, release = threading.Event(), threading.Event()
        original = client.app.state.db.add_file_message
        def delayed_commit(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)
        async def scenario():
            async def receive():
                return {"type": "http.request", "body": b"cancelled response, committed content", "more_body": False}
            task = asyncio.create_task(asgi_upload(client.app, headers, receive))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        with patch.object(client.app.state.db, "add_file_message", delayed_commit):
            client.portal.call(scenario)
        records = client.app.state.db.file_records()
        assert len(records) == 1
        result = client.get(f'/api/files/{records[0]["id"]}', headers=headers)
        assert result.status_code == 200 and result.content == b"cancelled response, committed content"
        assert not client.app.state.files.uploading
