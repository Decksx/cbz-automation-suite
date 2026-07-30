from __future__ import annotations

import argparse
import logging
from pathlib import Path

from comic_automation.service import ComicAutomationService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Comic Automation background service."
        )
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            "config/comic-automation.example.toml"
        ),
        help="Path to the TOML configuration file.",
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Initialize the workspace and database, then exit "
            "without starting workers."
        ),
    )

    parser.add_argument(
        "--log-level",
        choices=[
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        ],
        default="INFO",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=(
            "%(asctime)s %(levelname)-8s "
            "%(name)s: %(message)s"
        ),
    )

    service = ComicAutomationService(args.config)

    if args.check:
        service.initialize()
        logging.getLogger(__name__).info(
            "Service configuration and database check passed."
        )
        return 0

    service.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
