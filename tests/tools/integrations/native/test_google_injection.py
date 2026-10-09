"""Model- or user-supplied text never changes the structure of a Drive q, a Gmail q or a URL path."""

from __future__ import annotations

import re

import httpx
import pytest

from mavis.domain.errors import FailureKind
from mavis.domain.integrations import UserRef
from mavis.tools.integrations.native.google import GoogleError, GoogleExecutor, search_query

from .gfake import FakeGoogle

USER = UserRef(user_id=3)
FILES = r"/drive/v3/files$"
drive_q = GoogleExecutor._drive_q


def unescape_literals(q: str) -> str:
    """The query with every single-quoted literal blanked, so only its structure is left."""
    return re.sub(r"'(?:[^'\\]|\\.)*'", "''", q)


ADVERSARIAL_DRIVE = [
    "x) or (trashed = true",
    "x' or trashed = true or name contains '",
    "name contains 'x') or (trashed = true",
    "name contains 'x' or trashed = true",
    "name contains 'a\\' or trashed = true or name contains '",
    "name contains 'a\\\\' or trashed = true or name contains '",
    "trashed = true",
    "not trashed = false",
    "'root' in parents) or (trashed = true",
    "x\\",
    "'",
    "(((((((((((((((((((((((name contains 'x')))))))))))))))))))))))",
    "name contains 'x' " + "or name contains 'y' " * 40,
    "name contains 'x'; trashed = true",
    "sharedWithMe or trashed = true",
]


@pytest.mark.parametrize("query", ADVERSARIAL_DRIVE)
@pytest.mark.parametrize("extra", ["", "mimeType = 'application/vnd.google-apps.spreadsheet'"])
def test_no_input_can_turn_trashed_filter_off_or_add_a_clause(query, extra):
    q, _ = drive_q(query, extra=extra)
    shape = unescape_literals(q)
    assert q.endswith(" and trashed = false")
    assert shape.count("trashed") == 1  # the only mention is ours, outside every literal
    assert "true" not in shape.replace("starred", "")
    # the user part is one parenthesised group ANDed with our clauses: it cannot reach outside it
    depth = 0
    for i, ch in enumerate(shape):
        depth += ch == "("
        depth -= ch == ")"
        assert depth >= 0
        if depth == 0 and ch == ")":
            assert shape[i + 1:].startswith(" and ") or i == len(shape) - 1
    assert depth == 0


def test_free_text_is_a_fulltext_term_with_escapes():
    q, ordered = drive_q("it's a \\ test")
    assert q == "(fullText contains 'it\\'s a \\\\ test') and trashed = false" and not ordered


def test_allowlisted_drive_syntax_is_kept_and_rebuilt():
    q, ordered = drive_q("name contains 'budget' and (mimeType = 'application/pdf' or starred = true)")
    assert q == ("(name contains 'budget' and (mimeType = 'application/pdf' or starred = true))"
                 " and trashed = false") and ordered
    q, ordered = drive_q("fullText contains 'Priya' and modifiedTime > '2026-01-01T00:00:00Z'")
    assert not ordered and "modifiedTime > '2026-01-01T00:00:00Z'" in q
    assert drive_q("'abc' in parents")[0].startswith("('abc' in parents)")
    assert drive_q("NAME CONTAINS 'x'")[0].startswith("(name contains 'x')")


@pytest.mark.parametrize("query", ["owners", "name", "modifiedTime > 'not a date'", "starred = maybe",
                                   "name contains", "unknownField = 'x'", "mimeType in parents"])
def test_text_outside_the_grammar_is_only_ever_searched_as_text(query):
    q, _ = drive_q(query)
    assert unescape_literals(q) == "(fullText contains '') and trashed = false"


async def test_drive_and_sheets_send_the_rebuilt_q_to_google():
    fake = FakeGoogle().on("GET", FILES, httpx.Response(200, json={"files": []}))
    ex, _ = fake.executor()
    await ex.execute(USER, "drive.search", {"query": "x) or (trashed = true"})
    await ex.execute(USER, "sheets.find", {"query": "x) or (trashed = true"})
    for req in fake.requests:
        assert unescape_literals(req.url.params["q"]).count("trashed") == 1


# ---- Gmail -------------------------------------------------------------------------------------------

EXCLUSIONS = ("-in:spam", "-in:trash")


@pytest.mark.parametrize("query", [
    "x OR", "x OR  ", "newer_than:1d OR -", "a | ", "{a b}", "a OR b", "(a OR b) c", "{from:a from:b}",
    "from:a OR from:b", "x -", "foo AND", "a OR b OR",
])
def test_exclusions_always_apply_to_the_whole_query(query):
    out = search_query(query)
    for ex in EXCLUSIONS:
        assert ex in out.split()
    body = out.split(" -in:spam")[0]
    assert not re.search(r"(?i)\b(or|and)\s*$", body)  # nothing dangling in front of an exclusion
    if re.search(r"(?i)\bor\b|[{|]", body):
        assert body.startswith("(") and body.endswith(")")  # an alternative cannot sit outside them


@pytest.mark.parametrize("query", [
    'x" -in:spam', 'from:"a', "x) OR (in:trash", "x) OR in:trash (", "(a", "a)", "{a", "a}", "(a}",
    "((a) OR b", 'a "b" "',
])
def test_unbalanced_quotes_or_brackets_are_refused(query):
    with pytest.raises(GoogleError) as exc:
        search_query(query)
    assert exc.value.kind is FailureKind.INVALID_ARGUMENT and exc.value.field == "query"


async def test_an_unbalanced_mail_query_is_invalid_argument_and_never_reaches_google():
    fake = FakeGoogle().on("GET", r".*", httpx.Response(200, json={}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.search", {"query": "x) OR (in:trash"})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and res.error_field == "query"
    assert not fake.requests


def test_naming_a_mailbox_is_still_the_way_to_search_it():
    assert "-in:trash" not in search_query("in:trash invoice").split()


# ---- path segments -----------------------------------------------------------------------------------

BAD_IDS = ["", ".", "..", "a/b", "../drafts", "a/../b", "x\\y", " ", "a\nb", "a/"]
ID_CASES = [
    ("mail.read", "message_id", {}), ("mail.thread", "thread_id", {}), ("drive.meta", "file_id", {}),
    ("drive.read", "file_id", {}), ("drive.permissions", "file_id", {}), ("docs.read", "document_id", {}),
    ("sheets.read", "spreadsheet_id", {"range": "A1"}),
    ("calendar.update_event", "event_id", {"summary": "x"}),
    ("drive.download", "file_id", {}),
]


@pytest.mark.parametrize("bad", BAD_IDS)
@pytest.mark.parametrize(("action", "field", "rest"), ID_CASES, ids=[c[0] for c in ID_CASES])
async def test_ids_that_could_address_another_resource_are_invalid_and_never_sent(action, field, rest, bad):
    fake = FakeGoogle().on("GET", r".*", httpx.Response(200, json={}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, action, {field: bad, **rest})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and not fake.requests
    if bad.strip():  # blank ids are already refused by the argument model
        assert res.error_field == field


async def test_reply_and_a_sheet_range_with_a_slash_are_handled():
    fake = FakeGoogle().on("GET", r".*", httpx.Response(200, json={"values": []}))
    ex, _ = fake.executor()
    res = await ex.execute(USER, "mail.reply", {"thread_id": "../x", "to": "a@x.com", "body": "b"})
    assert res.error_kind is FailureKind.INVALID_ARGUMENT and not fake.requests
    res = await ex.execute(USER, "sheets.read", {"spreadsheet_id": "s1", "range": "'A/B'!A1"})
    assert res.ok and "A%2FB" in str(fake.requests[-1].url)


def test_q_itself_refuses_dot_segments_as_defence_in_depth():
    from mavis.tools.integrations.native.google import _q

    for bad in ("", ".", "..", "a/b"):
        with pytest.raises(GoogleError):
            _q(bad)
    assert _q("abc123_-") == "abc123_-" and _q("a b") == "a%20b"
