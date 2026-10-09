"""Offline installs: the worker resolves wheels (with dependencies) for the sandbox's platform."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import respx

from mavis.machine.wheels import WheelCache

INDEX = "https://pypi.example/simple"


def _wheel(name: str, version: str, requires: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
            f"Requires-Dist: {r}\n" for r in requires
        )
        z.writestr(f"{name.replace('-', '_')}-{version}.dist-info/METADATA", meta)
    return buf.getvalue()


def _project(name, files):
    return {
        "name": name,
        "files": [{"filename": f, "url": f"https://files.example/{f}", "hashes": {}} for f in files],
    }


@pytest.fixture
def index():
    with respx.mock(assert_all_called=False) as mock:
        projects = {
            "tablekit": [
                "tablekit-1.0.0-py3-none-any.whl",
                "tablekit-2.1.0-py3-none-any.whl",
                "tablekit-3.0.0-cp312-cp312-win_amd64.whl",
            ],
            "fastnum": [
                "fastnum-0.9-cp312-cp312-manylinux2014_x86_64.whl",
                "fastnum-0.9-cp312-cp312-macosx_11_0_arm64.whl",
            ],
            "winonly": ["winonly-1.0-py3-none-any.whl"],
        }
        reqs = {
            "tablekit-2.1.0": ["fastnum>=0.5", "winonly; sys_platform == 'win32'"],
            "fastnum-0.9": [],
            "winonly-1.0": [],
        }
        for name, files in projects.items():
            mock.get(f"{INDEX}/{name}/").mock(return_value=httpx.Response(200, json=_project(name, files)))
            for f in files:
                n, v = f.split("-")[:2]
                mock.get(f"https://files.example/{f}").mock(
                    return_value=httpx.Response(200, content=_wheel(n, v, reqs.get(f"{n}-{v}", [])))
                )
        yield mock


async def test_resolves_newest_compatible_with_deps_and_markers(index, settings):
    cache = WheelCache(index_url=INDEX, platform_tags=["manylinux2014_x86_64"], python_version="3.12")
    got = [name for name, _ in await cache.resolve(["tablekit"])]
    assert got == ["tablekit-2.1.0-py3-none-any.whl", "fastnum-0.9-cp312-cp312-manylinux2014_x86_64.whl"]


async def test_allowlist_limits_top_level_packages(index, settings, monkeypatch):
    monkeypatch.setattr(settings, "machine_package_allow", ["fastnum"])
    cache = WheelCache(index_url=INDEX, platform_tags=["manylinux2014_x86_64"], python_version="3.12")
    with pytest.raises(ValueError):
        await cache.resolve(["tablekit"])
    assert [n for n, _ in await cache.resolve(["fastnum"])]


@pytest.mark.parametrize("bad", ["../etc", "pkg; rm -rf /", "a b", ""])
async def test_bad_names_are_refused(index, settings, bad):
    with pytest.raises(ValueError):
        await WheelCache(index_url=INDEX).resolve([bad])
