"""The structural Gmail exclusions are derived from the parsed query, never from the user's wording."""

from __future__ import annotations

import pytest

from mavis.tools.integrations.native.google import search_query

SPAM_TRASH = ["-in:spam", "-in:trash"]
CATS = ["-category:promotions", "-category:social", "-category:forums"]


def extras(query: str) -> list[str]:
    return [t for t in search_query(query).split() if t.startswith("-") and t not in query.split()]


@pytest.mark.parametrize("query", [
    "", "newer_than:14d", "after:1790000000 -in:sent", "is:unread newer_than:2d", "in:inbox older_than:1y",
    "label:work after:2026/01/01", "has:attachment larger:5M", "(is:unread OR is:starred) newer_than:7d",
])
def test_listings_exclude_spam_trash_and_bulk_categories(query):
    assert extras(query) == SPAM_TRASH + CATS


@pytest.mark.parametrize("query", [
    "from:alice newer_than:30d", "to:bob@x.com", "subject:invoice", "invoice", '"quarterly report"',
    "from:amazon.com", "list:news.example.com", "filename:pdf budget", "{from:a from:b}", "rfc822msgid:<x@y>",
    "(from:alice OR from:bob)",
])
def test_targeted_searches_keep_categories_but_still_skip_spam_and_trash(query):
    assert extras(query) == SPAM_TRASH


@pytest.mark.parametrize(("query", "named"), [
    ("category:promotions newer_than:7d", "promotions"),
    ("newer_than:7d category:social", "social"),
    ("label:forums", "forums"),
    ("-category:promotions newer_than:1d", "promotions"),
])
def test_a_category_named_by_the_query_is_not_excluded_again_but_the_others_are(query, named):
    added = extras(query)
    assert f"-category:{named}" not in added
    assert [c for c in CATS if c != f"-category:{named}"] == [c for c in added if c in CATS]


def test_an_unlisted_category_changes_nothing():
    assert extras("category:updates newer_than:1d") == SPAM_TRASH + CATS


@pytest.mark.parametrize(("query", "missing"), [
    ("in:trash", "-in:trash"), ("in:spam newer_than:3d", "-in:spam"), ("label:spam", "-in:spam"),
])
def test_a_mailbox_named_by_the_query_is_not_excluded(query, missing):
    assert missing not in search_query(query).split()


def test_in_anywhere_disables_mailbox_exclusions_only():
    out = search_query("in:anywhere newer_than:1d").split()
    assert "-in:spam" not in out and "-in:trash" not in out and "-category:promotions" in out


def test_negated_terms_do_not_make_a_query_targeted():
    assert "-category:promotions" in search_query("-from:noreply@x.com newer_than:3d").split()
    assert "-category:promotions" in search_query("newer_than:3d -invoice").split()


def test_original_query_is_preserved_verbatim_first():
    assert search_query("from:alice  subject:\"a b\"").startswith('from:alice  subject:"a b" ')
