"""S3 workspace store plus the quota-checked put and metadata reads every store shares."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from mavis.config import get_settings
from mavis.machine.errors import QuotaExceeded
from mavis.machine.paths import guard
from mavis.machine.ports import FileClass, Provenance, WorkspaceFile, class_of
from mavis.store.repo import machine as repo_machine

PURGE_ATTEMPTS = 3


def workspace_key(prefix: str, user_id: int, path: str) -> str:
    return f"{prefix}u{int(user_id)}/{guard(path)}"


def quota_text() -> str:
    mb = get_settings().workspace_quota_mb
    return f"Your Mavis AI files are full ({mb} MB). Delete some with /files and try again."


def too_big_text() -> str:
    mb = get_settings().file_max_bytes // (1024 * 1024)
    return (f"That file is over {mb} MB, which is more than I can keep and work on. "
            "Could you send a smaller one?")


def to_model(row) -> WorkspaceFile:
    return WorkspaceFile(user_id=row.user_id, path=row.path, size=row.size, sha256=row.sha256,
                         cls=FileClass(row.cls), provenance=Provenance(row.provenance),
                         task_id=row.task_id)


async def put_with_quota(
    user_id: int, path: str, data: bytes, *, provenance: Provenance, cls: FileClass | None,
    task_id: int | None, write: Callable[[str, FileClass], Awaitable[None]],
) -> WorkspaceFile:
    rel = guard(path)
    cls = cls or class_of(rel)
    cap = get_settings().file_max_bytes
    if len(data) > cap:
        raise QuotaExceeded(too_big_text())
    existing = await repo_machine.get_file(user_id, rel)
    replaced = existing.size if existing is not None and existing.deleted_at is None else 0
    limit = get_settings().workspace_quota_mb * 1024 * 1024
    if await repo_machine.live_bytes(user_id) - replaced + len(data) > limit:
        raise QuotaExceeded(quota_text())
    await write(rel, cls)
    f = WorkspaceFile(user_id=int(user_id), path=rel, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                      cls=cls, provenance=provenance, task_id=task_id)
    await repo_machine.upsert_file(f)
    return f


class MetaReads:
    """list and meta come from the metadata rows in every store (they are the source of truth)."""

    async def list(self, user_id: int, prefix: str = "") -> list[WorkspaceFile]:
        return [to_model(r) for r in await repo_machine.list_files(user_id, prefix)]

    async def meta(self, user_id: int, path: str) -> WorkspaceFile | None:
        row = await repo_machine.get_file(user_id, guard(path))
        return to_model(row) if row is not None and row.deleted_at is None else None

    async def _require(self, user_id: int, path: str) -> str:
        rel = guard(path)
        if await self.meta(user_id, rel) is None:
            raise FileNotFoundError(rel)
        return rel


class S3WorkspaceStore(MetaReads):
    """Objects at s3://{bucket}/{prefix}u{user_id}/{path}, tagged cls=... for lifecycle rules. The bucket
    comes from settings; credentials from the instance role (never a static key)."""

    def __init__(self, bucket: str | None = None, prefix: str | None = None, client: Any = None) -> None:
        s = get_settings()
        self.bucket = bucket or s.workspace_bucket
        self.prefix = prefix if prefix is not None else s.workspace_prefix
        if not self.bucket:
            raise ValueError("WORKSPACE_BUCKET is not set")
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=s.agentcore_region)
        self._s3 = client

    async def put(self, user_id, path, data, *, provenance, cls=None, task_id=None):
        async def write(rel: str, cls_: FileClass) -> None:
            await asyncio.to_thread(self._s3.put_object, Bucket=self.bucket,
                                    Key=workspace_key(self.prefix, user_id, rel), Body=bytes(data),
                                    Tagging=f"cls={cls_.value}", ServerSideEncryption="AES256")
        return await put_with_quota(user_id, path, data, provenance=provenance, cls=cls, task_id=task_id,
                                    write=write)

    async def get(self, user_id, path):
        rel = await self._require(user_id, path)
        try:
            res = await asyncio.to_thread(self._s3.get_object, Bucket=self.bucket,
                                          Key=workspace_key(self.prefix, user_id, rel))
        except Exception as exc:  # noqa: BLE001 - botocore ClientError: expired by the lifecycle rules
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) in ("NoSuchKey", "404"):
                raise FileNotFoundError(rel) from exc
            raise
        return await asyncio.to_thread(res["Body"].read)

    async def delete(self, user_id, path):
        rel = guard(path)
        await asyncio.to_thread(self._s3.delete_object, Bucket=self.bucket,
                                Key=workspace_key(self.prefix, user_id, rel))
        await repo_machine.soft_delete_file(user_id, rel)

    async def purge_user(self, user_id):
        """Delete every object, retrying the ones S3 reports as failed. Rows are removed only for objects
        that are gone; when any object survives the retries this raises so the deletion step is retried."""
        root = f"{self.prefix}u{int(user_id)}/"
        pages = await asyncio.to_thread(
            lambda: list(self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=root)))
        keys = [o["Key"] for page in pages for o in page.get("Contents", [])]
        pending = keys
        for _attempt in range(PURGE_ATTEMPTS):
            failed: list[str] = []
            for i in range(0, len(pending), 1000):
                batch = {"Objects": [{"Key": k} for k in pending[i:i + 1000]]}
                res = await asyncio.to_thread(self._s3.delete_objects, Bucket=self.bucket, Delete=batch)
                failed += [e["Key"] for e in (res or {}).get("Errors", []) if "Key" in e]
            pending = failed
            if not pending:
                break
        if pending:
            gone = set(keys) - set(pending)
            paths = [k[len(root):] for k in gone]
            await repo_machine.delete_file_rows(user_id, paths)
            raise RuntimeError(f"{len(pending)} workspace object(s) could not be deleted")
        await repo_machine.purge_user(user_id)
