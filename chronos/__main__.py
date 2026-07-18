"""Chronos command-line entrypoint: ingest | inspect | serve.

Milestone 1 wires up the full CLI surface; each subcommand's implementation
lands in its own milestone (ingest = 2, inspect = 3, serve = 4).
"""
from __future__ import annotations

import argparse
import sys

from . import pairing


def _normalize_bbox_arg(argv: list[str]) -> list[str]:
    """Rewrite ``--bbox <value>`` to ``--bbox=<value>``.

    A bbox begins with a negative longitude (e.g. ``-122.42,...``), which
    argparse otherwise mistakes for an option flag and rejects. Folding the
    space form into the ``=`` form sidesteps that so both spellings work.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--bbox" and i + 1 < len(argv):
            out.append(f"--bbox={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def cmd_ingest(args: argparse.Namespace) -> int:
    print("ingest: implemented in milestone 2 (Mapillary fetch + candidate pairs).")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    print("inspect: implemented in milestone 3 (OpenAI vision judge).")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    print("serve: implemented in milestone 4 (map UI).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chronos", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser(
        "ingest", help="fetch Mapillary imagery for a bbox and build candidate pairs"
    )
    p_ingest.add_argument(
        "--bbox", required=True, metavar="minLon,minLat,maxLon,maxLat",
        help="bounding box to ingest",
    )
    p_ingest.add_argument(
        "--limit", type=int, default=2000, help="max images to fetch (default: 2000)"
    )
    p_ingest.add_argument(
        "--max-dist", type=float, default=pairing.DEFAULT_MAX_DIST_M,
        help=f"max pair distance in meters (default: {pairing.DEFAULT_MAX_DIST_M:g})",
    )
    p_ingest.add_argument(
        "--max-heading", type=float, default=pairing.DEFAULT_MAX_HEADING_DEG,
        help=f"max heading difference in degrees (default: {pairing.DEFAULT_MAX_HEADING_DEG:g})",
    )
    p_ingest.add_argument(
        "--min-gap-days", type=int, default=pairing.DEFAULT_MIN_GAP_DAYS,
        help=f"min capture gap in days (default: {pairing.DEFAULT_MIN_GAP_DAYS})",
    )
    p_ingest.add_argument(
        "--refresh", action="store_true", help="ignore the API cache and refetch"
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_inspect = sub.add_parser(
        "inspect", help="judge candidate pairs with the OpenAI vision API"
    )
    p_inspect.add_argument(
        "--limit", type=int, default=25, help="max pairs to judge (default: 25)"
    )
    p_inspect.add_argument("--model", default=None, help="override OPENAI_MODEL")
    p_inspect.add_argument(
        "--image-size", type=int, choices=(1024, 2048), default=1024,
        help="thumbnail size sent to the model (default: 1024)",
    )
    p_inspect.add_argument(
        "--dry-run", action="store_true",
        help="count pairs and estimate cost without calling the API",
    )
    p_inspect.add_argument(
        "--retry-errors", action="store_true",
        help="re-judge pairs previously marked as errors",
    )
    p_inspect.set_defaults(func=cmd_inspect)

    p_serve = sub.add_parser("serve", help="serve the map UI")
    p_serve.add_argument(
        "--port", type=int, default=8000, help="port to listen on (default: 8000)"
    )
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(_normalize_bbox_arg(argv))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
