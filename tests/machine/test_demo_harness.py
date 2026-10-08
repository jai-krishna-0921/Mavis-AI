# ruff: noqa: E501
"""The demo runner's pure parts: cases are data, checks are a registry, reports are dash-free."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import machine_demo as md

ROOT = Path(__file__).resolve().parents[2]


def _rec(sink, replies=(), artifacts=(), case=None):
    case = case or md.DemoCase(id="T1", prompt="p", upload=None, actions=[], expect={}, timeout_s=60, quotas={})
    return md.RunRecord(case=case, sink=list(sink), replies=list(replies), tasks=[], artifacts=list(artifacts),
                        started=0.0, finished=42.0, user_id=1)


def test_shipped_cases_parse_and_name_known_checks():
    cases = md.load_cases(ROOT / "scripts" / "machine_demos.toml")
    assert cases and len({c.id for c in cases}) == len(cases)
    for c in cases:
        assert set(c.expect) <= set(md.CHECKS), f"{c.id} uses an unknown check"
        if c.upload:
            assert (ROOT / "scripts" / "fixtures" / "machine" / "files" / c.upload).is_file()


@pytest.mark.parametrize("final,word", [("Done: g", "Done"), ("Partly done: g", "Partly done"),
                                        ("Cancelled: x", "Cancelled"), ("Couldn't finish: y", "Couldn't finish")])
def test_final_word_comes_from_the_last_card_frame(final, word):
    sink = [{"kind": "text", "text": "Working on: g", "at": "t0"}, {"kind": "edit", "text": "Working on: g\n⏳",
            "message_id": -2, "at": "t1"}, {"kind": "edit", "text": final, "message_id": -2, "at": "t2"}]
    rec = _rec(sink)
    assert rec.final_word() == word
    assert len(rec.card_frames()) == 3


def test_card_final_in_check():
    rec = _rec([{"kind": "text", "text": "Working on: g"}, {"kind": "edit", "text": "Done: g", "message_id": -2}])
    assert md.CHECKS["card_final_in"](rec, ["Done"]).ok
    assert not md.CHECKS["card_final_in"](rec, ["Cancelled"]).ok


@pytest.mark.parametrize("n,ok", [(0, False), (1, True), (3, True)])
def test_min_files(n, ok):
    sink = [{"kind": "document", "text": f"f{i}.png", "path": f"/x/f{i}.png"} for i in range(n)]
    assert md.CHECKS["min_files"](_rec(sink), 1).ok is ok


def test_reply_contains_number_and_text():
    rec = _rec([], replies=["There are 1229 primes below 10,000.", "Script attached."])
    assert md.CHECKS["reply_contains"](rec, ["1229"]).ok
    assert not md.CHECKS["reply_contains"](rec, ["1230"]).ok


def test_file_ext_check():
    sink = [{"kind": "document", "text": "deck.pptx", "path": "/a/deck.pptx"}]
    assert md.CHECKS["file_ext"](_rec(sink), [".pptx"]).ok
    assert not md.CHECKS["file_ext"](_rec(sink), [".xlsx", ".csv"]).ok


def test_unknown_check_fails_loudly():
    case = md.DemoCase(id="X", prompt="p", upload=None, actions=[], expect={"nope": 1}, timeout_s=1, quotas={})
    [res] = md.run_checks(_rec([], case=case))
    assert not res.ok and "unknown check" in res.detail


def test_transcript_and_report_have_no_dashes_and_list_frames():
    rec = _rec([{"kind": "text", "text": "Working on: g", "at": "2026-10-08T10:00:00"},
                {"kind": "edit", "text": "Done: g", "message_id": -2, "at": "2026-10-08T10:00:09"}],
               replies=["All set."])
    text = md.render_transcript(rec)
    html, summary = md.render_report([(rec, [md.CheckResult("card_final_in", True, "Done")])])
    for doc in (text, html, summary):
        assert "\u2014" not in doc and "\u2013" not in doc
    assert "Done: g" in text and "PASS" in summary


def test_refuses_without_an_active_test_chat(settings):
    with pytest.raises(md.HarnessError):
        md.target_chat(settings)


def test_photos_count_album_members_and_min_screenshots():
    sink = [{"kind": "photo", "path": "/x/a.png"}, {"kind": "album", "paths": ["/x/b.png", "/x/c.png"]}]
    rec = _rec(sink)
    assert len(rec.photos()) == 3
    assert md.CHECKS["min_screenshots"](rec, 3).ok and not md.CHECKS["min_screenshots"](rec, 4).ok


def test_load_cases_defaults_and_actions(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[[case]]\nid = "Z"\nprompt = "hi"\n[[case.actions]]\nat_s = 5\ntap = "cancel"\n', encoding="utf-8")
    [c] = md.load_cases(f)
    assert c.timeout_s == 300 and c.upload is None and c.expect == {} and c.actions == [{"at_s": 5, "tap": "cancel"}]


def test_report_marks_fail_and_cleans_dashes():
    rec = _rec([])
    _, summary = md.render_report([(rec, [md.CheckResult("min_files", False, "0 files — none")])])
    assert "FAIL" in summary and "—" not in summary
    _, empty = md.render_report([(rec, [])])
    assert "FAIL" in empty  # a case with no checks never passes silently


def test_final_word_is_none_while_card_is_running():
    assert _rec([{"kind": "text", "text": "Working on: g"}]).final_word() is None


def test_target_chat_accepts_a_synthetic_chat(settings, monkeypatch):
    monkeypatch.setenv("LIVE_TEST_ENABLED", "true")
    monkeypatch.setenv("TEST_TELEGRAM_CHAT_ID", "-1000000000000001")
    from mavis.config import get_settings
    get_settings.cache_clear()
    assert md.target_chat(get_settings()) == -1000000000000001
