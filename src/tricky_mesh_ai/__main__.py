import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .config import Config
from .daemon import Daemon


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

    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"config file not found: {cfg_path}", file=sys.stderr)
        return 2

    cfg = Config.load(cfg_path)
    asyncio.run(Daemon(cfg).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
