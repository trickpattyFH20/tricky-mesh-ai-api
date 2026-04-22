"""CLI entry point: launch the FastAPI HTTP server.

The daemon no longer owns a meshcore connection; it's a pure HTTP
service receiving webhook deliveries from Remote-Terminal and POSTing
replies back to RT. This entry point loads YAML config, builds the
FastAPI app, and hands off to uvicorn. uvicorn owns the signal
handlers and shutdown flow.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .config import Config
from .http_server import build_app


def main() -> int:
    parser = argparse.ArgumentParser(prog="tricky-mesh-ai")
    parser.add_argument(
        "-c",
        "--config",
        default=os.path.expanduser("~/.config/tricky-mesh-ai-api/config.yaml"),
        help="path to YAML config (default: %(default)s)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    log_level = args.log_level.upper()
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"config file not found: {cfg_path}", file=sys.stderr)
        return 2

    cfg = Config.load(cfg_path)
    app = build_app(cfg)

    # Defer to uvicorn for the event loop + lifespan handling.
    uvicorn.run(
        app,
        host=cfg.listen_host,
        port=cfg.listen_port,
        log_level=log_level.lower(),
        # Single worker: our Daemon holds in-memory conversation state.
        # Scaling beyond this requires a shared store (redis etc.) — out
        # of scope for this refactor.
        workers=1,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
