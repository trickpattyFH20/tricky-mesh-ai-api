"""FastAPI HTTP server: receive inbound DM webhooks from Remote-Terminal.

The webhook payload shape matches Remote-Terminal's ``Message.model_dump()``
output. We filter aggressively at the top: only incoming PRIV messages pass
through to the daemon. Channel messages, outgoing DMs, anything without a
sender_key / text — all rejected with 204 to keep the caller quiet.

HMAC verification is strongly recommended for production. When
``config.ingest_hmac_secret`` is non-empty, the ``X-Webhook-Signature``
header is required and must match ``sha256=<hex(hmac_sha256(body))>``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from .config import Config
from .daemon import Daemon


log = logging.getLogger(__name__)


def _verify_hmac(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification.

    Matches Remote-Terminal's webhook signing format: header value is
    ``sha256=<hexdigest>``.
    """
    if not secret:
        return True  # verification disabled
    if not header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header)


def _extract_path_len(body: dict[str, Any]) -> int | None:
    """Pull path_len from the first entry in ``paths`` if present."""
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        return None
    first = paths[0]
    if not isinstance(first, dict):
        return None
    val = first.get("path_len")
    return int(val) if isinstance(val, (int, float)) else None


def build_app(config: Config) -> FastAPI:
    """Construct the FastAPI app bound to a fresh Daemon instance.

    The caller is responsible for awaiting ``daemon.startup()`` and
    ``daemon.shutdown()`` — we wire those to the lifespan hook below.
    """
    daemon = Daemon(config)
    app = FastAPI(title="tricky-mesh-ai-api")

    @app.on_event("startup")
    async def _startup() -> None:
        await daemon.startup()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await daemon.shutdown()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/ingest-dm")
    async def ingest_dm(request: Request) -> Response:
        body = await request.body()

        # HMAC verification. The comparison is against the raw bytes to
        # avoid JSON re-serialization drift.
        sig_header = request.headers.get("x-webhook-signature")
        if not _verify_hmac(config.ingest_hmac_secret, body, sig_header):
            log.warning("rejecting /ingest-dm: HMAC mismatch")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        try:
            envelope = await request.json()
        except Exception:
            log.debug("/ingest-dm: non-JSON body; dropping")
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if not isinstance(envelope, dict):
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        # Strict filter: incoming PRIV only.
        if envelope.get("type") != "PRIV":
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if envelope.get("outgoing"):
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        text = (envelope.get("text") or "").strip()
        sender_key = (envelope.get("sender_key") or "").lower()
        if not text or not sender_key:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        pubkey_prefix = sender_key[:12]

        event_payload = {
            "text": text,
            "pubkey_prefix": pubkey_prefix,
            "sender_key": sender_key,
            "sender_name": envelope.get("sender_name"),
            "path_len": _extract_path_len(envelope),
            "timestamp": envelope.get("sender_timestamp"),
            # source may be set to 'mqtt:<label>' for MQTT-downlinked DMs —
            # we don't treat it differently (a DM is a DM) but pass it
            # through for observability in logs / metrics.
            "source": envelope.get("source"),
        }

        log.debug(
            "ingest: source=%s sender=%s text=%r",
            event_payload["source"],
            pubkey_prefix,
            text[:80],
        )
        await daemon.handle_inbound_dm(event_payload)
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return app
