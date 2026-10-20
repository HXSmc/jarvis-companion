"""Chat attachments: POST /central/attachments + `attachments` on /chat and session messages."""

from __future__ import annotations

import os
import stat
import time

import pytest

from gateway import attachments
from gateway.stubs import router_stub
from tests.test_central import IOS, TOKEN, _call, stub_router  # noqa: F401  (autouse fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


@pytest.fixture(autouse=True)
def attach_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_ATTACH_DIR", str(tmp_path / "att"))
    return tmp_path / "att"


def _upload(data=PNG, ctype="image/png", name="shot.png", headers=IOS):
    return _call("POST", f"/central/attachments?name={name}", headers={**headers, "Content-Type": ctype}, content=data)


def test_upload_stores_private_file(attach_tmp):
    resp = _upload()
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "shot.png" and data["type"] == "image/png" and data["size"] == len(PNG)
    path = attach_tmp / data["id"] / "shot.png"
    assert path.read_bytes() == PNG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert router_stub.seen == []


def test_upload_needs_ios_token():
    assert _upload(headers={}).status_code == 401
    assert _upload(headers={"Authorization": f"Bearer {TOKEN}"}).status_code in (401, 403)


@pytest.mark.parametrize("ctype,status", [("application/zip", 415), ("", 415), ("text/html", 415)])
def test_upload_rejects_types(ctype, status):
    assert _upload(ctype=ctype).status_code == status


def test_upload_rejects_empty_and_too_large(monkeypatch):
    assert _upload(data=b"").status_code == 400
    monkeypatch.setattr(attachments, "MAX_BYTES", 50)
    assert _upload().status_code == 413


@pytest.mark.parametrize("name,kind,want", [
    ("../../etc/passwd", "text/plain", "_.._etc_passwd.txt"),
    (".hidden.png", "image/png", "hidden.png"),
    ("", "application/pdf", "file.pdf"),
    ("photo.JPEG", "image/jpeg", "photo.JPEG"),
    ("notes", "text/markdown", "notes.md"),
])
def test_safe_name(name, kind, want):
    got = attachments.safe_name(name, kind)
    assert got == want and "/" not in got


def test_chat_appends_paths(attach_tmp):
    att = _upload().json()
    resp = _call("POST", "/chat", headers=IOS,
                 json={"thread_id": "app-1", "msg_id": "m-1", "text": "what is this", "attachments": [att["id"]]})
    assert resp.status_code == 200
    text = router_stub.seen[-1]["body"]["text"]
    assert text.startswith("what is this\n\n[The owner attached")
    assert str((attach_tmp / att["id"] / "shot.png").resolve()) in text


def test_session_message_photo_only(attach_tmp):
    att = _upload(ctype="application/pdf", name="doc.pdf").json()
    resp = _call("POST", "/central/sessions/s-run/message", headers=IOS,
                 json={"msg_id": "m-2", "attachments": [att["id"]]})
    assert resp.status_code == 200
    text = router_stub.seen[-1]["body"]["text"]
    assert text.startswith("[The owner attached")
    assert text.endswith("doc.pdf (1 KB)")


def test_text_unchanged_without_attachments():
    _call("POST", "/chat", headers=IOS, json={"thread_id": "app-1", "msg_id": "m-1", "text": " hi "})
    assert router_stub.seen[-1]["body"]["text"] == " hi "


@pytest.mark.parametrize("ids", [["0" * 32], ["../x"], ["a" * 31]])
def test_unknown_attachment_is_400_before_router(ids):
    resp = _call("POST", "/chat", headers=IOS, json={"thread_id": "t", "msg_id": "m", "text": "x", "attachments": ids})
    assert resp.status_code == 400
    assert router_stub.seen == []


def test_empty_message_and_too_many_files_422():
    assert _call("POST", "/chat", headers=IOS, json={"thread_id": "t", "msg_id": "m", "text": "  "}).status_code == 422
    six = ["a" * 32] * 6
    assert _call("POST", "/chat", headers=IOS,
                 json={"thread_id": "t", "msg_id": "m", "text": "x", "attachments": six}).status_code == 422
    assert router_stub.seen == []


def test_old_uploads_pruned(attach_tmp):
    old = _upload().json()["id"]
    past = time.time() - attachments.RETENTION_SECONDS - 10
    os.utime(attach_tmp / old, (past, past))
    _upload()
    assert not (attach_tmp / old).exists()
