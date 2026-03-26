from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime

from .archive import candidate_archive_months
from .config import Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="erofs-cibot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bridge", help="run one bridge cycle")
    sub.add_parser("show-config", help="print effective configuration")
    sub.add_parser("show-months", help="print archive months in lookback window")
    sub.add_parser("list-series", help="discover complete recent series")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.from_env()

    if args.command == "bridge":
        from .bridge import run_once

        return run_once(config)

    if args.command == "show-config":
        print(config)
        return 0

    if args.command == "show-months":
        now = datetime.now(tz=UTC)
        for month in candidate_archive_months(now, config.lookback_hours):
            print(month)
        return 0

    if args.command == "list-series":
        from .archive import discover_recent_series

        now = datetime.now(tz=UTC)
        series_list = discover_recent_series(
            config.archive_root,
            lookback_hours=config.lookback_hours,
            now=now,
        )
        for series in series_list:
            print(
                f"v{series.version} patches={series.total} "
                f"root={series.root_message_id} title={series.title}"
            )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
