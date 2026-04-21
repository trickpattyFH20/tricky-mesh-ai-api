import json

from tricky_mesh_ai.deadletter import DeadLetterLog


def test_disabled_when_path_none(tmp_path):
    dl = DeadLetterLog(None)
    assert not dl.enabled
    dl.record("x", "!a", "t")  # should not raise, not write


def test_writes_jsonl(tmp_path):
    p = tmp_path / "d.jsonl"
    dl = DeadLetterLog(p)
    dl.record("authz", "!abc", "hello", {"extra": 1})
    dl.record("rate_limited", "!abc", "again", {"retry_after_s": 12.5})

    lines = p.read_text().splitlines()
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    assert e1["reason"] == "authz"
    assert e1["fromId"] == "!abc"
    assert e1["text"] == "hello"
    assert e1["extra"] == 1
    assert "ts" in e1
    e2 = json.loads(lines[1])
    assert e2["retry_after_s"] == 12.5


def test_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "deeper" / "d.jsonl"
    dl = DeadLetterLog(p)
    dl.record("x", "!a", "t")
    assert p.is_file()
