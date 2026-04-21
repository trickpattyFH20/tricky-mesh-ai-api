from tricky_mesh_ai.truncate import truncate


def _blen(s: str) -> int:
    return len(s.encode("utf-8"))


def test_under_limit_unchanged():
    assert truncate("hello") == "hello"
    assert truncate("  spaced  ") == "spaced"


def test_exactly_at_limit_ascii():
    s = "a" * 255
    assert truncate(s, 255) == s
    assert _blen(truncate(s, 255)) == 255


def test_over_limit_appends_ellipsis():
    out = truncate("a" * 300, 255)
    assert _blen(out) <= 255
    assert out.endswith("…")


def test_breaks_at_word_boundary_when_close():
    words = ("word " * 200).strip()
    out = truncate(words, 50)
    assert _blen(out) <= 50
    assert out.endswith("…")
    assert out[:-1].rstrip().endswith("word")


def test_multibyte_counted_as_bytes():
    # Each "é" is 2 bytes in UTF-8.
    s = "é" * 200  # 400 bytes
    out = truncate(s, 50)
    assert _blen(out) <= 50
    assert out.endswith("…")


def test_emoji_boundary_safe():
    # Each emoji is 4 bytes. Trim in middle of emoji must not produce garbage.
    s = "😀" * 40  # 160 bytes
    out = truncate(s, 30)  # should fit 6 emoji (24b) + ellipsis (3b) = 27b
    assert _blen(out) <= 30
    assert out.endswith("…")
    # Everything before ellipsis must be valid complete characters.
    assert all(ord(c) for c in out[:-1])


def test_ellipsis_counts_in_budget():
    out = truncate("x" * 10, 5)
    assert _blen(out) <= 5
