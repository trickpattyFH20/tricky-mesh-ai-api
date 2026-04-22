import pytest

from tricky_mesh_ai.config import Config


def _write(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(body)
    return p


def test_minimal(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://127.0.0.1:8000
llama_endpoint: http://x:1
allowed_pubkey_prefixes: ["a1b2c3d4e5f6"]
""",
    )
    c = Config.load(p)
    assert c.rt_base_url == "http://127.0.0.1:8000"
    assert c.listen_host == "127.0.0.1"
    assert c.listen_port == 8090
    assert c.ingest_hmac_secret == ""
    assert c.allowed_pubkey_prefixes == ["a1b2c3d4e5f6"]
    assert c.max_reply_bytes == 140
    assert c.llm_max_tokens is None


def test_missing_required_key(tmp_path):
    p = _write(
        tmp_path,
        """
llama_endpoint: http://x:1
""",
    )
    with pytest.raises(ValueError, match="missing required keys"):
        Config.load(p)


def test_empty_allowlist_ok(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
allowed_pubkey_prefixes: []
""",
    )
    c = Config.load(p)
    assert c.allowed_pubkey_prefixes == []


def test_allowed_prefixes_omitted_defaults_to_empty(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
""",
    )
    c = Config.load(p)
    assert c.allowed_pubkey_prefixes == []


def test_unknown_key_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
not_a_real_key: true
""",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        Config.load(p)


def test_prefix_normalization(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
allowed_pubkey_prefixes: ["A1B2C3D4E5F6", "0xdeadbeefcafe"]
""",
    )
    c = Config.load(p)
    assert c.allowed_pubkey_prefixes == ["a1b2c3d4e5f6", "deadbeefcafe"]
    assert c.allowed_prefix_set == {"a1b2c3d4e5f6", "deadbeefcafe"}


def test_prefix_wrong_length_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
allowed_pubkey_prefixes: ["a1b2c3"]
""",
    )
    with pytest.raises(ValueError, match="12 hex chars"):
        Config.load(p)


def test_prefix_non_hex_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
allowed_pubkey_prefixes: ["not-hex-chars"]
""",
    )
    with pytest.raises(ValueError, match="12 hex chars"):
        Config.load(p)


def test_dead_letter_path_expands(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
dead_letter_log_path: ~/dead.jsonl
""",
    )
    c = Config.load(p)
    assert c.dead_letter_path is not None
    assert str(c.dead_letter_path).endswith("/dead.jsonl")
    assert not str(c.dead_letter_path).startswith("~")


def test_custom_listen_port(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
listen_port: 9091
listen_host: 0.0.0.0
""",
    )
    c = Config.load(p)
    assert c.listen_port == 9091
    assert c.listen_host == "0.0.0.0"


def test_ingest_hmac_secret_and_rt_auth(tmp_path):
    p = _write(
        tmp_path,
        """
rt_base_url: http://rt:8000
llama_endpoint: http://x:1
ingest_hmac_secret: shh
rt_basic_auth_username: alice
rt_basic_auth_password: hunter2
""",
    )
    c = Config.load(p)
    assert c.ingest_hmac_secret == "shh"
    assert c.rt_basic_auth_username == "alice"
    assert c.rt_basic_auth_password == "hunter2"
