"""drive.upload (Phase 6 hook): only this task's artifacts, staged through Composio file storage."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import respx

from mavis.domain.errors import ActionFailed
from mavis.domain.integrations import ConnectionState, ToolResult, UserRef
from mavis.domain.policy import Capability
from mavis.store.repo import tasks
from mavis.tools.integrations import workspace_guard
from mavis.tools.integrations.actions import DriveUploadArgs
from mavis.tools.integrations.composio import ComposioProvider
from mavis.tools.integrations.workspace_tools import UPLOAD_LIMIT, drive_upload
from mavis.tools.registry import ToolContext

BASE = "https://backend.composio.dev/api/v3"


@respx.mock
async def test_provider_stages_the_file_then_uploads(settings):
    deck = settings.artifacts_dir / "deck.pptx"
    deck.write_bytes(b"PK fake pptx")
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "ca_g", "status": "ACTIVE", "user_id": "mavis-7", "toolkit": {"slug": "googlesuper"}},
    ]}))
    req = respx.post(f"{BASE}/files/upload/request").mock(return_value=httpx.Response(200, json={
        "key": "projects/p/requests/googlesuper/deck.pptx",
        "new_presigned_url": "https://storage.composio.dev/put?sig=1",
    }))
    put = respx.put("https://storage.composio.dev/put?sig=1").mock(return_value=httpx.Response(200))
    run = respx.post(f"{BASE}/tools/execute/GOOGLESUPER_UPLOAD_FILE").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {"id": "drive-file-1"}})
    )
    provider = ComposioProvider(api_key="ck_test", base_url=BASE, workspace=True)
    mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    res = await provider.execute(UserRef(user_id=7), "drive.upload_file",
                                 {"path": str(deck), "name": "Q3 deck.pptx", "mime": mime})
    assert res.ok and res.data["id"] == "drive-file-1"
    body = json.loads(req.calls.last.request.content)
    assert body["tool_slug"] == "GOOGLESUPER_UPLOAD_FILE" and body["toolkit_slug"] == "googlesuper"
    assert body["md5"] == hashlib.md5(b"PK fake pptx").hexdigest()
    assert "x-api-key" not in put.calls.last.request.headers  # the storage host never sees the key
    assert json.loads(run.calls.last.request.content)["arguments"] == {"file_to_upload": {
        "name": "Q3 deck.pptx", "mimetype": mime, "s3key": "projects/p/requests/googlesuper/deck.pptx",
    }}


@pytest.fixture
def drive(provider):
    provider.set_state(1, Capability.DRIVE, ConnectionState.ACTIVE)
    provider.results["drive.upload_file"] = ToolResult(ok=True, data={"id": "drive-file-1"})
    return provider


PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


async def _artifact(user, settings, name="deck.pptx", size=10, mime=PPTX) -> tuple[int, int]:
    tid = await tasks.create(user.id, goal="make a deck")
    path = settings.artifacts_dir / name
    path.write_bytes(b"x" * size)
    aid = await tasks.add_artifact(tid, user.id, "pptx", str(path), mime, title="Q3 deck")
    return tid, aid


async def test_uploads_an_artifact_of_this_task(user, settings, drive, cache):
    tid, aid = await _artifact(user, settings)
    workspace_guard._created.clear()
    out = await drive_upload(ToolContext(user_id=user.id, task_id=tid), DriveUploadArgs(artifact_id=aid),
                             provider=drive, cache=cache)
    assert out.startswith("Done.") and "drive-file-1" in out
    _, action, args = drive.executed[-1]
    assert action == "drive.upload_file" and args["name"] == "Q3 deck.pptx" and args["mime"] == PPTX
    assert args["path"].endswith("deck.pptx")
    assert workspace_guard.created_by(tid) == {"drive-file-1"}


async def test_refuses_another_tasks_artifact(user, settings, drive, cache):
    _, aid = await _artifact(user, settings)
    other = await tasks.create(user.id, goal="something else")
    with pytest.raises(ActionFailed, match="no file"):
        await drive_upload(ToolContext(user_id=user.id, task_id=other), DriveUploadArgs(artifact_id=aid),
                           provider=drive, cache=cache)
    assert drive.executed == []


async def test_refuses_outside_chat_tasks_and_big_files(user, settings, drive, cache):
    with pytest.raises(ActionFailed, match="only a background task"):
        await drive_upload(ToolContext(user_id=user.id), DriveUploadArgs(artifact_id=1), provider=drive,
                           cache=cache)
    tid, aid = await _artifact(user, settings, name="big.bin", size=UPLOAD_LIMIT + 1)
    with pytest.raises(ActionFailed, match="5 MB"):
        await drive_upload(ToolContext(user_id=user.id, task_id=tid), DriveUploadArgs(artifact_id=aid),
                           provider=drive, cache=cache)


@pytest.mark.parametrize("url", [
    "http://storage.composio.dev/put?sig=1",      # not https
    "https://evil.example/put?sig=1",             # not a storage host
    "https://127.0.0.1/put?sig=1",                # IP literal
    "https://localhost/put?sig=1",
    "https://composio.dev.evil.example/put",      # suffix trick
])
@respx.mock
async def test_refuses_an_unexpected_upload_host(url, tmp_path, settings):
    deck = settings.artifacts_dir / "deck.pptx"
    deck.write_bytes(b"PK")
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "ca_g", "status": "ACTIVE", "user_id": "mavis-7", "toolkit": {"slug": "googlesuper"}},
    ]}))
    respx.post(f"{BASE}/files/upload/request").mock(
        return_value=httpx.Response(200, json={"key": "k", "new_presigned_url": url}))
    put = respx.put(url).mock(return_value=httpx.Response(200))
    run = respx.post(f"{BASE}/tools/execute/GOOGLESUPER_UPLOAD_FILE").mock(
        return_value=httpx.Response(200, json={"successful": True, "data": {}}))
    provider = ComposioProvider(api_key="ck_test", base_url=BASE, workspace=True)
    res = await provider.execute(UserRef(user_id=7), "drive.upload_file",
                                 {"path": str(deck), "name": "d.pptx", "mime": PPTX})
    assert not res.ok and res.error == "unexpected upload host" and url not in res.error
    assert not put.called and not run.called


@respx.mock
async def test_provider_refuses_a_path_outside_artifacts_or_too_big(tmp_path, settings):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"s")
    big = settings.artifacts_dir / "big.bin"
    big.write_bytes(b"x" * (UPLOAD_LIMIT + 1))
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "ca_g", "status": "ACTIVE", "user_id": "mavis-7", "toolkit": {"slug": "googlesuper"}},
    ]}))
    req = respx.post(f"{BASE}/files/upload/request")
    provider = ComposioProvider(api_key="ck_test", base_url=BASE, workspace=True)
    for path in (outside, big):
        res = await provider.execute(UserRef(user_id=7), "drive.upload_file",
                                     {"path": str(path), "name": "x", "mime": "text/plain"})
        assert not res.ok
    assert not req.called


async def test_refuses_files_outside_the_artifacts_dir(user, settings, drive, cache, tmp_path):
    tid = await tasks.create(user.id, goal="g")
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    link = settings.artifacts_dir / "link.txt"
    link.symlink_to(outside)
    ids = [
        await tasks.add_artifact(tid, user.id, "txt", "/etc/passwd", "text/plain", title="p"),
        await tasks.add_artifact(tid, user.id, "txt", str(outside), "text/plain", title="o"),
        await tasks.add_artifact(tid, user.id, "txt", str(link), "text/plain", title="l"),
        await tasks.add_artifact(tid, user.id, "txt", str(settings.artifacts_dir / "gone.txt"), "text/plain",
                                 title="g"),
    ]
    for aid in ids:
        with pytest.raises(ActionFailed):
            await drive_upload(ToolContext(user_id=user.id, task_id=tid), DriveUploadArgs(artifact_id=aid),
                               provider=drive, cache=cache)
    assert drive.executed == []


async def test_refuses_another_users_artifact(user, settings, drive, cache):
    tid, aid = await _artifact(user, settings)
    with pytest.raises(ActionFailed, match="no file"):
        await drive_upload(ToolContext(user_id=user.id + 1, task_id=tid), DriveUploadArgs(artifact_id=aid),
                           provider=drive, cache=cache)
    assert drive.executed == []


async def test_title_without_extension_gets_the_files_suffix(user, settings, drive, cache):
    tid = await tasks.create(user.id, goal="g")
    path = settings.artifacts_dir / "a.csv"
    path.write_text("a,b")
    aid = await tasks.add_artifact(tid, user.id, "csv", str(path), "text/csv", title="data report")
    await drive_upload(ToolContext(user_id=user.id, task_id=tid), DriveUploadArgs(artifact_id=aid),
                       provider=drive, cache=cache)
    assert drive.executed[-1][2]["name"] == "data report.csv"
