"""Every store keeps bytes per user under keys built from the integer id; metadata rows are the truth."""

from __future__ import annotations

import pytest

from mavis.machine.errors import QuotaExceeded, SandboxPathError
from mavis.machine.fake import MemoryWorkspaceStore
from mavis.machine.local import LocalWorkspaceStore
from mavis.machine.ports import FileClass, Provenance
from mavis.machine.s3store import S3WorkspaceStore, workspace_key
from mavis.store.repo import machine as repo


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put_object(self, *, Bucket, Key, Body, Tagging, ServerSideEncryption):  # noqa: N803 - boto3 names
        assert ServerSideEncryption == "AES256"
        self.objects[Key] = (Body, Tagging)

    def get_object(self, *, Bucket, Key):  # noqa: N803
        body = self.objects[Key][0]

        class B:
            def read(self_inner):
                return body
        return {"Body": B()}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        objs = self.objects

        class P:
            def paginate(self_inner, *, Bucket, Prefix):  # noqa: N803
                yield {"Contents": [{"Key": k} for k in list(objs) if k.startswith(Prefix)]}
        return P()

    def delete_objects(self, *, Bucket, Delete):  # noqa: N803
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)


@pytest.fixture(params=["memory", "local", "s3"])
async def store(request, db, tmp_path):
    if request.param == "memory":
        return MemoryWorkspaceStore()
    if request.param == "local":
        return LocalWorkspaceStore(root=tmp_path / "ws")
    s3 = FakeS3()
    st = S3WorkspaceStore(bucket="mavis-machine-test", prefix="ws/", client=s3)
    st.fake = s3
    return st


async def _users(n):
    from mavis.store.repo import users

    return [(await users.get_or_create_by_chat(80_000 + i, f"U{i}"))[0].id for i in range(n)]


@pytest.mark.parametrize("path,prov", [("inbox/a.csv", Provenance.USER_UPLOAD),
                                       ("out/b.png", Provenance.GENERATED_CLEAN),
                                       ("work/c.json", Provenance.FETCHED)])
async def test_put_get_meta_list_delete(store, path, prov):
    [uid] = await _users(1)
    f = await store.put(uid, path, b"bytes-" + path.encode(), provenance=prov, cls=None)
    body = b"bytes-" + path.encode()
    assert f.cls.value == path.split("/")[0] and f.provenance is prov and f.size == len(body)
    assert await store.get(uid, path) == b"bytes-" + path.encode()
    assert [x.path for x in await store.list(uid)] == [path]
    await store.delete(uid, path)
    assert await store.list(uid) == [] and await store.meta(uid, path) is None


async def test_users_never_see_each_other(store):
    a, b = await _users(2)
    await store.put(a, "work/secret.txt", b"only a", provenance=Provenance.GENERATED_CLEAN, cls=None)
    assert await store.list(b) == []
    with pytest.raises((FileNotFoundError, KeyError)):
        await store.get(b, "work/secret.txt")


async def test_quota_counts_replacements_once(store, settings, monkeypatch):
    monkeypatch.setattr(settings, "workspace_quota_mb", 1)
    [uid] = await _users(1)
    big = b"x" * (700 * 1024)
    await store.put(uid, "work/a.bin", big, provenance=Provenance.GENERATED_CLEAN, cls=None)
    await store.put(uid, "work/a.bin", big, provenance=Provenance.GENERATED_CLEAN, cls=None)  # replace: fine
    with pytest.raises(QuotaExceeded) as info:
        await store.put(uid, "work/b.bin", big, provenance=Provenance.GENERATED_CLEAN, cls=None)
    assert "full" in info.value.user_text and "\u2014" not in info.value.user_text


async def test_purge_user_removes_bytes_and_rows(store):
    a, b = await _users(2)
    for uid in (a, b):
        await store.put(uid, "out/x.txt", b"x", provenance=Provenance.GENERATED_CLEAN, cls=None)
    await store.purge_user(a)
    assert await store.list(a) == [] and len(await store.list(b)) == 1


@pytest.mark.parametrize("uid,path,key", [(7, "out/a.png", "ws/u7/out/a.png"),
                                          (12, "./work//b", "ws/u12/work/b"),
                                          (3, "inbox/x y.csv", "ws/u3/inbox/x y.csv")])
def test_keys_are_built_from_the_user_id_only(uid, path, key):
    assert workspace_key("ws/", uid, path) == key


@pytest.mark.parametrize("bad", ["../u8/out/a", "/ws/u8/a", "out/../../u9/x"])
def test_keys_refuse_escapes(bad):
    with pytest.raises(SandboxPathError):
        workspace_key("ws/", 1, bad)


async def test_s3_objects_are_tagged_by_class(db):
    [uid] = await _users(1)
    s3 = FakeS3()
    st = S3WorkspaceStore(bucket="b", prefix="ws/", client=s3)
    await st.put(uid, "out/r.pdf", b"%PDF", provenance=Provenance.GENERATED_CLEAN, cls=FileClass.OUT)
    assert s3.objects[f"ws/u{uid}/out/r.pdf"][1] == "cls=out"


async def test_quota_override_and_usage_rows(db):
    from datetime import date

    [uid] = await _users(1)
    assert await repo.quota_override(uid, "daily_minutes") is None
    await repo.set_quota(uid, "daily_minutes", 0)
    assert await repo.quota_override(uid, "daily_minutes") == 0
    await repo.record_usage(uid, date(2026, 10, 8), "agentcore", "code", task_id=None, session_id="s-1",
                            wall_s=90, est_cost_usd=0.01)
    await repo.record_usage(uid, date(2026, 10, 8), "agentcore", "code", task_id=None, session_id="s-1",
                            wall_s=90, est_cost_usd=0.01)  # same session: counted once
    assert await repo.minutes_on(uid, date(2026, 10, 8)) == 1.5


@pytest.mark.parametrize("bad", ["../x", "/abs/path", "out/../../y"])
async def test_stores_refuse_escaping_paths(store, bad):
    [uid] = await _users(1)
    with pytest.raises(SandboxPathError):
        await store.put(uid, bad, b"x", provenance=Provenance.GENERATED_CLEAN, cls=None)
    with pytest.raises(SandboxPathError):
        await store.get(uid, bad)


async def test_deleted_files_cannot_be_read_back(store):
    [uid] = await _users(1)
    await store.put(uid, "work/gone.txt", b"x", provenance=Provenance.GENERATED_CLEAN, cls=None)
    await store.delete(uid, "work/gone.txt")
    with pytest.raises(FileNotFoundError):
        await store.get(uid, "work/gone.txt")


async def test_s3_purge_only_touches_the_users_prefix(db):
    a, b = await _users(2)
    s3 = FakeS3()
    st = S3WorkspaceStore(bucket="b", prefix="ws/", client=s3)
    for uid in (a, b):
        await st.put(uid, "out/x.txt", b"x", provenance=Provenance.GENERATED_CLEAN, cls=None)
    await st.purge_user(a)
    assert list(s3.objects) == [f"ws/u{b}/out/x.txt"]
