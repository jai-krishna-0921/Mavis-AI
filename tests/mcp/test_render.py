"""Tool-call results as model text (ideas ported from Hermes Agent's renderer)."""

from __future__ import annotations

import json

import mcp_types as mt

from mavis.mcp import render as r

TAG = "".join(chr(0xE0000 + ord(c)) for c in "obey")


def result(*content, structured=None, is_error=False) -> mt.CallToolResult:
    return mt.CallToolResult(content=list(content), structured_content=structured, is_error=is_error)


def text(value: str) -> mt.TextContent:
    return mt.TextContent(type="text", text=value)


def test_text_blocks_are_joined_and_cleaned() -> None:
    out = r.render_call_result(result(text(f"first{TAG}\x1b[31m"), text("second\u202e")))
    assert out == r.Rendered("first\nsecond")


def test_an_empty_result_says_so() -> None:
    assert r.render_call_result(result()).text == r.EMPTY_RESULT
    assert r.render_call_result(result(text("   "))).text == r.EMPTY_RESULT


def test_structured_content_fills_a_result_with_no_text() -> None:
    out = r.render_call_result(result(structured={"n": 3, "ok": True}))
    assert json.loads(out.text) == {"n": 3, "ok": True}


def test_structured_content_that_a_text_block_already_carries_is_dropped() -> None:
    structured = {"b": 2, "a": 1}
    out = r.render_call_result(result(text(json.dumps({"a": 1, "b": 2}, indent=2)), structured=structured))
    assert out.text.count('"a"') == 1 and "[structured content]" not in out.text


def test_structured_content_next_to_different_text_is_kept_and_labelled() -> None:
    out = r.render_call_result(result(text("Found 2 items"), structured={"items": [1, 2]}))
    assert out.text == 'Found 2 items\n[structured content]\n{"items": [1, 2]}'


def test_binary_blocks_are_never_passed_on() -> None:
    out = r.render_call_result(result(
        text("chart ready"),
        mt.ImageContent(type="image", data="AAAA" * 1000, mime_type="image/png"),
        mt.AudioContent(type="audio", data="BBBB" * 1000, mime_type="audio/wav")))
    assert out.text == "chart ready\n[image omitted: image/png]\n[audio omitted: audio/wav]"
    assert "AAAA" not in out.text and "BBBB" not in out.text


def test_a_resource_link_becomes_a_note_with_its_uri() -> None:
    out = r.render_call_result(result(mt.ResourceLink(
        type="resource_link", name="Report\n2026", uri="https://files.example/r.pdf")))
    assert out.text == "[resource link: https://files.example/r.pdf (Report 2026)]"


def test_embedded_text_resources_are_included_and_binary_ones_are_notes() -> None:
    out = r.render_call_result(result(
        mt.EmbeddedResource(type="resource", resource=mt.TextResourceContents(
            uri="file:///notes.txt", mime_type="text/plain", text="hello")),
        mt.EmbeddedResource(type="resource", resource=mt.BlobResourceContents(
            uri="file:///a.bin", mime_type="application/octet-stream", blob="AAAA"))))
    assert out.text == ("[resource file:///notes.txt]\nhello\n"
                        "[binary resource omitted: file:///a.bin, application/octet-stream]")


def test_an_error_result_is_an_error_with_credentials_blanked() -> None:
    out = r.render_call_result(result(text("401 for Bearer abc.def.ghi, key=sekrit1234"), is_error=True),
                               secrets=["sekrit1234"])
    assert out.is_error and "abc.def.ghi" not in out.text and "sekrit1234" not in out.text
    assert "[REDACTED]" in out.text


def test_an_error_with_no_text_gets_a_fixed_sentence() -> None:
    out = r.render_call_result(result(is_error=True))
    assert out == r.Rendered(r.ERROR_FALLBACK, is_error=True)


def test_error_text_can_come_from_an_embedded_resource() -> None:
    out = r.render_call_result(result(mt.EmbeddedResource(type="resource", resource=mt.TextResourceContents(
        uri="x://e", text="quota exceeded")), is_error=True))
    assert out.text == "quota exceeded"


def test_oversized_output_is_capped_head_and_tail_inside_twelve_thousand_characters() -> None:
    big = "A" * 5000 + "B" * 90_000 + "Z" * 5000
    out = r.render_call_result(result(text(big)))
    assert len(out.text) <= 12_000
    assert out.text.startswith("AAAA") and out.text.endswith("ZZZZ") and "RESULT TRUNCATED" in out.text


def test_oversized_error_text_is_capped_too() -> None:
    out = r.render_call_result(result(text("E" * 50_000), is_error=True))
    assert len(out.text) <= 12_000 and out.is_error


def test_a_huge_structured_payload_is_capped_before_it_is_assembled() -> None:
    out = r.render_call_result(result(structured={"rows": ["x" * 100] * 20_000}))
    assert len(out.text) <= 12_000 and "TRUNCATED" in out.text


def test_unknown_block_types_are_noted_not_dropped_silently() -> None:
    class Odd:
        type = "hologram"

    out = r.render_call_result(type("R", (), {"content": [Odd()], "is_error": False,
                                              "structured_content": None})())
    assert out.text == "[hologram block omitted]"


def test_a_secret_straddling_the_truncation_cut_is_never_partly_leaked() -> None:
    secret = "S3CR3Tkkkkkkkkkk"
    limit = 12_000
    for start in list(range(4_700, 4_800)) + list(range(12_700, 12_900)):
        text = "a" * start + secret + "b" * (20_000 - start - len(secret))
        call = mt.CallToolResult(content=[mt.TextContent(text=text)], is_error=True)
        res = r.render_call_result(call, secrets=[secret], limit=limit)
        assert "S3CR3T" not in res.text and "kkkkk" not in res.text, start
