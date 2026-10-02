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


def test_split_avoids_cutting_inside_fence() -> None:
    code = "\n".join("x = 1" for _ in range(40))
    text = "intro line\n" * 5 + f"```\n{code}\n```\nafter"
    chunks = split_text(text, 300)
    assert all(c.count("```") % 2 == 0 for c in chunks)
    assert all(len(c) <= 300 for c in chunks)


def test_split_forced_inside_fence_closes_and_reopens() -> None:
    code = "\n".join(f"line {i}" for i in range(200))
    chunks = split_text(f"```\n{code}\n```", 300)
    assert len(chunks) > 2
    for c in chunks:
        assert c.startswith("```") and c.rstrip().endswith("```") and len(c) <= 300
