"""Worker-side package resolution for offline installs inside the sandbox (no sandbox egress).

PEP 691 JSON simple index at PIP_INDEX_URL; wheels only; the newest version whose tags fit the sandbox
(python version, platform tags from settings, verified against the live interpreter in Task 15);
dependencies from each wheel's METADATA with markers evaluated for that platform; bounded."""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
import zipfile
from pathlib import Path

import httpx
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from mavis.config import Settings, get_settings

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DEFAULT_ALLOW: list[str] = list(Settings.model_fields["machine_package_allow"].default)
ACCEPT = "application/vnd.pypi.simple.v1+json"


class WheelCache:
    def __init__(
        self,
        index_url: str | None = None,
        platform_tags: list[str] | None = None,
        python_version: str | None = None,
        client: httpx.AsyncClient | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        s = get_settings()
        self.index = (index_url or s.pip_index_url).rstrip("/")
        self.platforms = set(platform_tags or s.machine_platform_tags)
        self.py = python_version or s.machine_python_version
        self._client = client
        self.cache_dir = cache_dir or (s.data_dir / "wheels")

    def _compatible(self, filename: str) -> bool:
        if filename != Path(filename).name or filename.startswith("."):
            return False
        try:
            _n, _v, _b, tags = parse_wheel_filename(filename)
        except Exception:  # noqa: BLE001
            return False
        cp = "cp" + self.py.replace(".", "")
        return any(
            t.interpreter in {cp, "py3", f"py{self.py[0]}"}
            and t.abi in {"none", "abi3", cp}
            and (t.platform == "any" or t.platform in self.platforms)
            for t in tags
        )

    def _env(self) -> dict[str, str]:
        env = default_environment()
        machine = "aarch64" if any("aarch64" in p for p in self.platforms) else "x86_64"
        env.update(
            {
                "python_version": self.py,
                "python_full_version": f"{self.py}.0",
                "sys_platform": "linux",
                "platform_system": "Linux",
                "platform_machine": machine,
                "os_name": "posix",
                "implementation_name": "cpython",
                "platform_python_implementation": "CPython",
            }
        )
        return env

    async def _get(self, client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
        r = await client.get(url, **kw)
        r.raise_for_status()
        return r

    async def resolve(self, packages: list[str], max_total: int = 40) -> list[tuple[str, bytes]]:
        listed = get_settings().machine_package_allow or DEFAULT_ALLOW
        allow = set() if listed == ["*"] else {canonicalize_name(p) for p in listed}
        for p in packages:
            if not _NAME.match(str(p or "")):
                raise ValueError(f"not a package name: {str(p)[:40]!r}")
            if allow and canonicalize_name(p) not in allow:
                raise ValueError(f"{p} is not on the package allowlist")
        out: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        queue = [Requirement(p) for p in packages]
        env = self._env()
        async with (
            contextlib.nullcontext(self._client)
            if self._client is not None
            else httpx.AsyncClient(timeout=60, follow_redirects=True)
        ) as client:
            while queue:
                req = queue.pop(0)
                name = canonicalize_name(req.name)
                if name in seen:
                    continue
                if len(seen) >= max_total:
                    raise ValueError(f"more than {max_total} packages needed")
                seen.add(name)
                data = (await self._get(client, f"{self.index}/{name}/", headers={"Accept": ACCEPT})).json()
                files = [
                    f
                    for f in data.get("files", [])
                    if f["filename"].endswith(".whl")
                    and not f.get("yanked")
                    and self._compatible(f["filename"])
                    and (not req.specifier or parse_wheel_filename(f["filename"])[1] in req.specifier)
                ]
                if not files:
                    raise ValueError(f"no wheel of {req.name} fits the machine")
                best = max(files, key=lambda f: parse_wheel_filename(f["filename"])[1])
                blob = await self._fetch(
                    client, best["filename"], best["url"], (best.get("hashes") or {}).get("sha256")
                )
                out.append((best["filename"], blob))
                for dep in self._requires(blob):
                    if dep.marker is None or dep.marker.evaluate({**env, "extra": ""}):
                        queue.append(dep)
        return out

    async def _fetch(
        self, client: httpx.AsyncClient, filename: str, url: str, sha256: str | None = None
    ) -> bytes:
        cached = self.cache_dir / filename
        seal = self.cache_dir / f"{filename}.sha256"
        if cached.is_file():
            blob = cached.read_bytes()
            digest = hashlib.sha256(blob).hexdigest()
            trusted = sha256 or (seal.read_text().strip() if seal.is_file() else None)
            if trusted == digest:  # re-hashed on every use: a tampered cache entry is refetched
                return blob
        blob = (await self._get(client, url)).content
        digest = hashlib.sha256(blob).hexdigest()
        if sha256 and digest != sha256:
            raise ValueError(f"{filename} does not match the hash the index published")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(blob)
        seal.write_text(digest)
        return blob

    @staticmethod
    def _requires(blob: bytes) -> list[Requirement]:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            meta = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
            if meta is None:
                return []
            text = z.read(meta).decode("utf-8", "replace")
        return [
            Requirement(line.split(":", 1)[1].strip())
            for line in text.splitlines()
            if line.startswith("Requires-Dist:")
        ]
