"""
Séance CLI entry point.

Usage:
    python -m seance capture <url>
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="seance",
        description="Séance — capture and certify web sources.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser("capture", help="Capture a URL snapshot.")
    capture_parser.add_argument("url", help="URL to capture.")

    args = parser.parse_args(argv)

    if args.command == "capture":
        _cmd_capture(args.url)


def _cmd_capture(url: str) -> None:
    """Wire dependencies and run a single capture. Implemented in TASK-10."""
    # Stub — full implementation added in TASK-10 once all dependencies exist.
    print(f"[stub] Would capture: {url}", file=sys.stderr)
    raise NotImplementedError("_cmd_capture is implemented in TASK-10")
