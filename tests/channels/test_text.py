from mavis.channels.text import split_text


def test_short_text_is_one_chunk() -> None:
    assert split_text("  hello  ") == ["hello"]


def test_empty_text_is_no_chunks() -> None:
    assert split_text("   ") == []


def test_split_long_text_prefers_newlines() -> None:
    para = "a" * 3000
    chunks = split_text(f"{para}\n{para}")
    assert chunks == [para, para]
    assert all(len(c) <= 4096 for c in chunks)


def test_split_without_whitespace_hard_cuts() -> None:
    chunks = split_text("x" * 9000)
    assert [len(c) for c in chunks] == [4096, 4096, 808]
