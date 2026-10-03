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
async def test_provider_stages_the_file_then_uploads(tmp_path):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"PK fake pptx")
    respx.get(f"{BASE}/connected_accounts").mock(return_value=httpx.Response(200, json={"items": [
        {"id": "ca_g", "status": "ACTIVE", "user_id": "mavis-7", "toolkit": {"slug": "googlesuper"}},
    ]}))
    req = respx.post(f"{BASE}/files/upload/request").mock(return_value=httpx.Response(200, json={
        "key": "projects/p/requests/googlesuper/deck.pptx",
        "new_presigned_url": "https://storage.example/put?sig=1",
    }))
    put = respx.put("https://storage.example/put?sig=1").mock(return_value=httpx.Response(200))
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


async def _artifact(user, settings, name="deck.pptx", size=10) -> tuple[int, int]:
    tid = await tasks.create(user.id, goal="make a deck")
    path = settings.artifacts_dir / name
    path.write_bytes(b"x" * size)
    aid = await tasks.add_artifact(tid, user.id, "pptx", str(path), "application/pdf", title="Q3 deck")
    return tid, aid


async def test_uploads_an_artifact_of_this_task(user, settings, drive, cache):
    tid, aid = await _artifact(user, settings)
    workspace_guard._created.clear()
    out = await drive_upload(ToolContext(user_id=user.id, task_id=tid), DriveUploadArgs(artifact_id=aid),
                             provider=drive, cache=cache)
    assert out.startswith("Done.") and "drive-file-1" in out
    _, action, args = drive.executed[-1]
    assert action == "drive.upload_file" and args["name"] == "Q3 deck" and args["path"].endswith("deck.pptx")
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
