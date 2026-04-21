from tricky_mesh_ai.llm import LlamaClient


def test_no_placeholder_prompt_unchanged():
    c = LlamaClient(
        endpoint="http://nope.invalid",
        model=None,
        system_prompt="plain prompt",
        timeout=1.0,
        max_tokens=None,
    )
    assert c.system_prompt == "plain prompt"


def test_explicit_model_substituted_without_http():
    c = LlamaClient(
        endpoint="http://nope.invalid",
        model="my-model",
        system_prompt="You are {model}, hi.",
        timeout=1.0,
        max_tokens=None,
    )
    assert c.system_prompt == "You are my-model, hi."


def test_fallback_when_fetch_fails(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    c = LlamaClient(
        endpoint="http://nope.invalid",
        model=None,
        system_prompt="You are {model}.",
        timeout=1.0,
        max_tokens=None,
    )
    assert c.system_prompt == "You are an AI model."


def test_fetch_from_models_endpoint(monkeypatch):
    import httpx

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "the-model-name"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResp())
    c = LlamaClient(
        endpoint="http://x",
        model=None,
        system_prompt="You are {model}.",
        timeout=1.0,
        max_tokens=None,
    )
    assert c.system_prompt == "You are the-model-name."
