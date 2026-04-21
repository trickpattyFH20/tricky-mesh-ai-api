import logging
from collections.abc import Iterable

import httpx


log = logging.getLogger(__name__)


class LlamaClient:
    """OpenAI-compatible client for a local llama.cpp server.

    Exposes both sync `complete` (used by the summarizer's worker thread)
    and async `acomplete` (used by the async daemon). They share endpoint
    config + resolved system prompt but have independent httpx clients.
    """

    def __init__(
        self,
        endpoint: str,
        model: str | None,
        system_prompt: str,
        timeout: float,
        max_tokens: int | None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.url = self.endpoint + "/v1/chat/completions"
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.system_prompt = self._resolve_prompt(system_prompt)
        self._sync_client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None

    def _resolve_prompt(self, template: str) -> str:
        if "{model}" not in template:
            return template
        name = self.model or self._fetch_model_name() or "an AI model"
        resolved = template.replace("{model}", name)
        log.info("resolved system prompt with model=%r", name)
        return resolved

    def _fetch_model_name(self) -> str | None:
        try:
            r = httpx.get(self.endpoint + "/v1/models", timeout=5.0)
            r.raise_for_status()
            data = r.json()
            items = data.get("data") or data.get("models") or []
            if items:
                return items[0].get("id") or items[0].get("name")
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            log.warning("could not fetch model name from %s: %s", self.endpoint, e)
        return None

    def _build_payload(
        self,
        user_text: str,
        history: Iterable[tuple[str, str]],
        extra_system: str | None,
        system_override: str | None,
    ) -> dict:
        base_system = system_override if system_override is not None else self.system_prompt
        messages: list[dict] = [{"role": "system", "content": base_system}]
        if extra_system:
            messages.append({"role": "system", "content": extra_system})
        for role, content in history:
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        payload: dict = {
            "messages": messages,
            "temperature": 0.7,
            "stream": False,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.model:
            payload["model"] = self.model
        return payload

    @staticmethod
    def _extract_reply(data: dict) -> str:
        # `content` is the user-facing reply. `reasoning_content` on thinking
        # models is internal and intentionally discarded.
        return data["choices"][0]["message"]["content"] or ""

    def complete(
        self,
        user_text: str,
        history: Iterable[tuple[str, str]] = (),
        extra_system: str | None = None,
        system_override: str | None = None,
    ) -> str:
        """Sync version used by the summarizer's background worker thread."""
        payload = self._build_payload(user_text, history, extra_system, system_override)
        if self._sync_client is None:
            self._sync_client = httpx.Client(timeout=self.timeout)

        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                r = self._sync_client.post(self.url, json=payload)
                r.raise_for_status()
                return self._extract_reply(r.json())
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                last_exc = e
                if attempt == 1:
                    log.warning("llm request failed (attempt 1/2): %s", e)
        assert last_exc is not None
        raise last_exc

    async def acomplete(
        self,
        user_text: str,
        history: Iterable[tuple[str, str]] = (),
        extra_system: str | None = None,
        system_override: str | None = None,
    ) -> str:
        """Async version used by the daemon's event handler."""
        payload = self._build_payload(user_text, history, extra_system, system_override)
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=self.timeout)

        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                r = await self._async_client.post(self.url, json=payload)
                r.raise_for_status()
                return self._extract_reply(r.json())
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                last_exc = e
                if attempt == 1:
                    log.warning("llm request failed (attempt 1/2): %s", e)
        assert last_exc is not None
        raise last_exc

    def close(self) -> None:
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
