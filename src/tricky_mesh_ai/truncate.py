ELLIPSIS = "…"
_ELLIPSIS_BYTES = len(ELLIPSIS.encode("utf-8"))  # 3


def truncate(text: str, max_bytes: int = 255) -> str:
    """Return `text` trimmed so its UTF-8 encoding is ≤ `max_bytes`.

    If trimming is needed, cuts on a UTF-8 character boundary, prefers to break
    at the last whitespace within the tail window, and appends an ellipsis.
    The ellipsis itself is counted against the byte budget.
    """
    text = text.strip()
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    budget = max_bytes - _ELLIPSIS_BYTES
    if budget <= 0:
        # Degenerate: budget too small for even the ellipsis. Hard-cut at
        # max_bytes on a char boundary and return without ellipsis.
        return _decode_up_to(encoded, max_bytes).rstrip()

    head = _decode_up_to(encoded, budget)
    # Try to break at the last whitespace within the tail window.
    last_space = head.rfind(" ")
    tail_window = min(40, budget // 2)
    if last_space >= len(head) - tail_window and last_space > 0:
        head = head[:last_space]

    return head.rstrip().rstrip(",;:-") + ELLIPSIS


def _decode_up_to(encoded: bytes, max_bytes: int) -> str:
    """Decode the longest UTF-8 prefix of `encoded` that fits in `max_bytes`."""
    if max_bytes >= len(encoded):
        return encoded.decode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
