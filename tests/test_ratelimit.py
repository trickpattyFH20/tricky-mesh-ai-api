from tricky_mesh_ai.ratelimit import RateLimiter


def test_disabled_always_allows():
    rl = RateLimiter(0)
    for _ in range(5):
        ok, _ = rl.allow("a")
        assert ok


def test_first_allowed_second_denied_within_cooldown():
    rl = RateLimiter(30)
    ok1, _ = rl.allow("a", now=1000.0)
    ok2, retry = rl.allow("a", now=1005.0)
    assert ok1 is True
    assert ok2 is False
    assert 24.0 < retry <= 25.0


def test_allowed_after_cooldown():
    rl = RateLimiter(30)
    assert rl.allow("a", now=1000.0)[0] is True
    assert rl.allow("a", now=1031.0)[0] is True


def test_per_key_isolation():
    rl = RateLimiter(30)
    assert rl.allow("a", now=1000.0)[0] is True
    assert rl.allow("b", now=1000.0)[0] is True
    assert rl.allow("a", now=1000.0)[0] is False
