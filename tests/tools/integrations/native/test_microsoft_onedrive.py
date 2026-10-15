import httpx
import pytest

from mavis.domain.errors import FailureKind as K

from .graphfake import FakeGraph, graph_error, graph_item, page, query_of
from .test_microsoft_transport import U


async def test_search_escapes_the_term_in_the_path_and_maps_items():
    g = FakeGraph().on(
        "GET",
        r"/v1.0/me/drive/search\(q='.*'\)$",
        httpx.Response(
            200,
            json=page(
                [
                    graph_item(
                        "01AAA",
                        name="Budget.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                    {"id": "01DIR", "name": "Folder", "folder": {"childCount": 2}},
                    graph_item("01SHR", name="Plan.docx", shared_by="Priya", remote_drive_id="b!XYZ"),
                ]
            ),
        ),
    )
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.search", {"query": "o'brien) /../ x"})
    raw_path = g.requests[0].url.raw_path.decode()
    assert "o%27%27brien" in raw_path and "%29" in raw_path and "/../" not in raw_path
    files = r.data["files"]
    assert [f["id"] for f in files] == ["ms:01AAA", "ms:b!XYZ~remote-01SHR"]  # the folder is dropped
    assert files[1]["shared"] is True and files[1]["sharedBy"] == "Priya"


async def test_empty_query_lists_recent_files():
    g = FakeGraph().on(
        "GET", r"/v1.0/me/drive/recent$", httpx.Response(200, json=page([graph_item("01A")] * 30))
    )
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.search", {"query": "", "max_results": 5})
    assert len(r.data["files"]) == 5


async def test_shared_with_me_unavailable_is_a_fixed_message():
    g = FakeGraph().on(
        "GET", r"/v1.0/me/drive/sharedWithMe$", graph_error(404, "itemNotFound", "secret-detail")
    )
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.list_recent", {"shared_with_me": True})
    assert r.error_kind is K.UNAVAILABLE and "not available right now" in r.error and "secret" not in r.error


async def test_text_file_is_cut_at_one_megabyte():
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T$",
        httpx.Response(200, json=graph_item("01T", name="n.txt", size=900_000, mime="text/plain")),
    )
    g.on("GET", r"/v1.0/me/drive/items/01T/content$", httpx.Response(200, content=b"a" * 1_200_000))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "ms:01T"})
    assert r.ok and r.data["truncated"] is True and len(r.data["text"]) <= 1_000_000


async def test_oversized_file_is_refused_before_download():
    g = FakeGraph().on(
        "GET",
        r"/v1.0/me/drive/items/01BIG$",
        httpx.Response(200, json=graph_item("01BIG", size=50_000_000, mime="text/plain")),
    )
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01BIG"})
    assert r.error_kind is K.INVALID_ARGUMENT and "over 10 MB" in r.error and not g.calls("GET", "/content")


async def test_content_redirect_goes_to_an_allowed_host_without_the_token():
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T$",
        httpx.Response(200, json=graph_item("01T", name="n.txt", size=20, mime="text/plain")),
    )
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T/content$",
        httpx.Response(302, headers={"Location": "https://corp-my.sharepoint.com/dl/abc"}),
    )
    g.on(
        "GET",
        r"/dl/abc$",
        httpx.Response(200, content=b"hello world", headers={"content-type": "text/plain; charset=utf-8"}),
    )
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.ok and r.data["text"] == "hello world" and r.data["truncated"] is False
    dl = g.requests[-1]
    assert dl.url.host == "corp-my.sharepoint.com" and "authorization" not in dl.headers


@pytest.mark.parametrize(
    "location", ["https://evil.test/x", "http://corp.sharepoint.com/x", "https://169.254.169.254/x"]
)
async def test_content_redirect_to_other_hosts_is_refused(location):
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T$",
        httpx.Response(200, json=graph_item("01T", size=20, mime="text/plain")),
    )
    g.on("GET", r"/v1.0/me/drive/items/01T/content$", httpx.Response(302, headers={"Location": location}))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.error_kind is K.UNAVAILABLE and all(q.url.host in ("graph.microsoft.com",) for q in g.requests)


async def test_docx_uses_office_text_and_legacy_doc_converts_to_pdf(monkeypatch):
    from mavis.tools.integrations.native import graph_files

    monkeypatch.setattr(
        graph_files, "office_text", lambda data, mime: f"parsed:{mime.rsplit('/', 1)[-1]}:{len(data)}"
    )
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01D$",
        httpx.Response(
            200,
            json=graph_item(
                "01D",
                name="a.docx",
                size=10,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
    )
    g.on("GET", r"/v1.0/me/drive/items/01D/content$", httpx.Response(200, content=b"PK-docx"))
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01L$",
        httpx.Response(200, json=graph_item("01L", name="a.doc", size=10, mime="application/msword")),
    )
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01L/content$",
        httpx.Response(200, content=b"%PDF-1.4 converted", headers={"content-type": "application/pdf"}),
    )
    ex, _ = g.executor()
    assert (await ex.execute(U, "onedrive.read", {"item_id": "01D"})).data["text"].startswith("parsed:")
    legacy = await ex.execute(U, "onedrive.read", {"item_id": "01L"})
    assert (
        legacy.data["text"] == "parsed:pdf:18"
        and g.calls("GET", "01L/content")[0].url.params["format"] == "pdf"
    )


async def test_failed_conversion_returns_metadata_only():
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01L$",
        httpx.Response(200, json=graph_item("01L", name="a.xls", size=10, mime="application/vnd.ms-excel")),
    )
    g.on("GET", r"/v1.0/me/drive/items/01L/content$", graph_error(400, "invalidRequest"))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01L"})
    assert r.ok and r.data["text"] == "" and "could not be converted" in r.data["note"]


async def test_compound_ids_address_the_other_drive_and_hostile_halves_are_refused():
    g = FakeGraph().on(
        "GET",
        r"/v1.0/drives/b!XYZ/items/01SHR$",
        httpx.Response(200, json=graph_item("01SHR", size=5, mime="text/plain")),
    )
    g.on("GET", r"/v1.0/drives/b!XYZ/items/01SHR/content$", httpx.Response(200, content=b"hi"))
    ex, _ = g.executor()
    assert (await ex.execute(U, "onedrive.read", {"item_id": "ms:b!XYZ~01SHR"})).ok
    bad = await ex.execute(U, "onedrive.read", {"item_id": "ms:b!XYZ~../x"})
    assert bad.error_kind is K.INVALID_ARGUMENT


async def test_search_view_uses_the_remote_item_and_hides_download_urls():
    item = graph_item("01SHR", name="Plan.docx", shared_by="Priya", remote_drive_id="b!XYZ")
    item["@microsoft.graph.downloadUrl"] = "https://corp-my.sharepoint.com/secret-download"
    g = FakeGraph().on("GET", r"/v1.0/me/drive/search\(q='.*'\)$", httpx.Response(200, json=page([item])))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.search", {"query": "plan"})
    assert "secret-download" not in str(r.data) and "downloadUrl" not in str(r.data)
    assert query_of(g.requests[0])["$top"] == "10"
    g.assert_all_routed()


async def test_shared_item_is_read_from_its_own_drive_with_the_item_name():
    g = FakeGraph().on(
        "GET",
        r"/v1.0/drives/b!XYZ/items/remote-01SHR$",
        httpx.Response(200, json=graph_item("remote-01SHR", name="Plan.txt", size=5, mime="text/plain")),
    )
    g.on("GET", r"/v1.0/drives/b!XYZ/items/remote-01SHR/content$", httpx.Response(200, content=b"plan!"))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "ms:b!XYZ~remote-01SHR"})
    assert r.ok and r.data["text"] == "plan!" and r.data["id"] == "ms:b!XYZ~remote-01SHR"
    g.assert_all_routed()


@pytest.mark.parametrize(
    "location",
    [
        "https://user:pw@corp.sharepoint.com/x",
        "https://corp.sharepoint.com:8443/x",
        "/relative/x",
        "https://evilsharepoint.com/x",
        "https://sharepoint.com.evil.test/x",
        "ftp://corp.sharepoint.com/x",
        "",
    ],
)
async def test_redirect_target_is_checked_before_any_fetch(location):
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T$",
        httpx.Response(200, json=graph_item("01T", size=20, mime="text/plain")),
    )
    g.on("GET", r"/v1.0/me/drive/items/01T/content$", httpx.Response(302, headers={"Location": location}))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.error_kind is K.UNAVAILABLE and "not trusted" in r.error
    assert len(g.requests) == 2 and (not location or location not in r.error)


async def test_a_second_redirect_is_refused_and_urls_never_leak():
    first = "https://corp-my.sharepoint.com/dl/abc?tempauth=SECRET"
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T$",
        httpx.Response(200, json=graph_item("01T", size=20, mime="text/plain")),
    )
    g.on("GET", r"/v1.0/me/drive/items/01T/content$", httpx.Response(302, headers={"Location": first}))
    g.on("GET", r"/dl/abc$", httpx.Response(302, headers={"Location": "https://evil.test/y"}))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.error_kind is K.UNAVAILABLE and "SECRET" not in r.error and "evil" not in r.error
    assert all(q.url.host != "evil.test" for q in g.requests)


async def test_download_is_capped_while_streaming():
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T$",
        httpx.Response(200, json=graph_item("01T", size=20, mime="text/plain")),
    )
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T/content$",
        httpx.Response(302, headers={"Location": "https://x.sharepoint.com/dl"}),
    )
    g.on("GET", r"/dl$", httpx.Response(200, content=b"b" * 3_000_000))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert r.ok and r.data["truncated"] is True and len(r.data["text"]) == 1_000_000


async def test_download_failure_has_no_url_in_the_error():
    g = FakeGraph()
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T$",
        httpx.Response(200, json=graph_item("01T", size=20, mime="text/plain")),
    )
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01T/content$",
        httpx.Response(302, headers={"Location": "https://x.sharepoint.com/dl?sig=SECRET"}),
    )
    g.on("GET", r"/dl$", httpx.Response(500))
    ex, _ = g.executor()
    r = await ex.execute(U, "onedrive.read", {"item_id": "01T"})
    assert not r.ok and "SECRET" not in r.error and "sharepoint" not in r.error


async def test_folder_is_refused_and_unknown_type_named():
    g = FakeGraph()
    g.on("GET", r"/v1.0/me/drive/items/01F$", httpx.Response(200, json=graph_item("01F", folder=True)))
    g.on(
        "GET",
        r"/v1.0/me/drive/items/01I$",
        httpx.Response(200, json=graph_item("01I", name="a.png", mime="image/png")),
    )
    ex, _ = g.executor()
    assert (await ex.execute(U, "onedrive.read", {"item_id": "01F"})).error_kind is K.INVALID_ARGUMENT
    img = await ex.execute(U, "onedrive.read", {"item_id": "01I"})
    assert img.error_kind is K.INVALID_ARGUMENT and "image/png is not a text file" in img.error
    g.assert_all_routed()


def test_renderers_flatten_names_and_clip_text():
    from mavis.tools.integrations.microsoft_render import MICROSOFT_RENDERERS as R

    files = {
        "files": [
            {
                "id": "ms:1",
                "name": "a\nb https://x.test/y",
                "mimeType": "text/plain",
                "size": 5,
                "modifiedTime": "2026-10-05T10:00:00Z",
                "modifiedBy": "Alice",
                "shared": True,
                "sharedBy": "Priya",
            }
        ]
    }
    out = R["onedrive.search"](files)
    assert out.count("\n") == 0 and "id=ms:1" in out and "shared by Priya" in out and "x.test" not in out
    read = R["onedrive.read"](
        {
            "id": "ms:1",
            "name": "n",
            "mimeType": "text/plain",
            "size": 5,
            "text": "z" * 9000,
            "truncated": True,
        }
    )
    assert len(read) < 6200 and "truncated" in read


def test_item_view_exposes_the_sharer_address_and_share_time():
    from mavis.tools.integrations.native.graph_files import item_view

    remote = {"id": "r", "name": "Plan.docx", "parentReference": {"driveId": "d"},
              "shared": {"sharedBy": {"user": {"displayName": "Priya", "email": "priya@corp.test"}},
                         "sharedDateTime": "2026-10-15T12:00:00Z"}}
    v = item_view({"id": "x", "remoteItem": remote})
    assert v["sharedByEmail"] == "priya@corp.test" and v["sharedAt"] == "2026-10-15T12:00:00Z"
