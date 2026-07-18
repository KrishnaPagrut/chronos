"""Chronos command-line entrypoint: ingest | inspect | serve.

Milestone 1 wires up the full CLI surface; each subcommand's implementation
lands in its own milestone (ingest = 2, inspect = 3, serve = 4).
"""
from __future__ import annotations

import argparse
import sys

from . import config, db, pairing


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


def _parse_bbox(text: str) -> tuple[float, float, float, float]:
    """Parse ``minLon,minLat,maxLon,maxLat`` into ordered (w, s, e, n) floats."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "bbox must be minLon,minLat,maxLon,maxLat (4 comma-separated numbers)"
        )
    try:
        lon1, lat1, lon2, lat2 = (float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError("bbox values must be numbers")
    west, east = sorted((lon1, lon2))
    south, north = sorted((lat1, lat2))
    if west == east or south == north:
        raise argparse.ArgumentTypeError("bbox has zero width or height")
    return west, south, east, north


def cmd_ingest(args: argparse.Namespace) -> int:
    # Import here so the CLI (and its --help) load without httpx present.
    from . import mapillary

    if not config.MAPILLARY_TOKEN:
        print("error: MAPILLARY_TOKEN is not set (add it to .env).", file=sys.stderr)
        return 1

    west, south, east, north = args.bbox
    config.ensure_dirs()
    db.init_db()
    conn = db.connect()
    try:
        before_images = db.count_images(conn)
        before_pairs = db.count_pairs(conn)

        print(f"Fetching Mapillary imagery for bbox {west},{south},{east},{north} ...")
        with mapillary.MapillaryClient(
            conn, config.MAPILLARY_TOKEN, refresh=args.refresh
        ) as client:
            features = client.fetch_bbox(west, south, east, north, limit=args.limit)
            for feat in features:
                db.upsert_image(
                    conn,
                    id=feat.id,
                    lon=feat.lon,
                    lat=feat.lat,
                    heading=feat.heading,
                    captured_at=feat.captured_at,
                    sequence_id=feat.sequence_id,
                    is_pano=feat.is_pano,
                    thumb_url=feat.thumb_1024_url,
                )
            conn.commit()
            print(f"  fetched {len(features)} images "
                  f"({db.count_images(conn) - before_images} new).")

            print("Building candidate pairs ...")
            images = db.load_images(conn)
            pairs = pairing.find_pairs(
                images,
                max_dist_m=args.max_dist,
                max_heading_deg=args.max_heading,
                min_gap_days=args.min_gap_days,
            )
            new_pairs = 0
            for p in pairs:
                if db.insert_pair(conn, p):
                    new_pairs += 1
            conn.commit()
            print(f"  {len(pairs)} candidate pairs ({new_pairs} new).")

            # Cache thumbnails only for images that made it into a pair.
            paired_ids = {pid for p in pairs for pid in (p.older_id, p.newer_id)}
            url_by_id = {f.id: f.thumb_1024_url for f in features}
            downloaded = 0
            for image_id in paired_ids:
                path = config.thumb_path(image_id, 1024)
                if path.exists():
                    db.set_thumb_path(conn, image_id, str(path))
                    continue
                url = url_by_id.get(image_id)
                if not url:
                    row = conn.execute(
                        "SELECT thumb_url FROM images WHERE id = ?", (image_id,)
                    ).fetchone()
                    url = row["thumb_url"] if row else None
                if not url:
                    continue
                client.download_thumb(url, path)
                db.set_thumb_path(conn, image_id, str(path))
                downloaded += 1
            conn.commit()
            print(f"  cached {downloaded} new thumbnails.")

        print(
            f"\nDone. images: {db.count_images(conn)} "
            f"(+{db.count_images(conn) - before_images}), "
            f"pairs: {db.count_pairs(conn)} "
            f"(+{db.count_pairs(conn) - before_pairs}), "
            f"unjudged: {db.count_unjudged(conn)}."
        )
    finally:
        conn.close()
    return 0


def _resolve_thumb(image_id: str, stored: str | None, size: int) -> str | None:
    """Best local thumbnail for an image: requested size, then any cached copy."""
    from pathlib import Path

    preferred = config.thumb_path(image_id, size)
    if preferred.exists():
        return str(preferred)
    if stored and Path(stored).exists():
        return stored
    other = config.thumb_path(image_id, 2048 if size == 1024 else 1024)
    return str(other) if other.exists() else None


def cmd_inspect(args: argparse.Namespace) -> int:
    from . import inspector

    db.init_db()
    conn = db.connect()
    try:
        rows = db.pairs_to_judge(conn, limit=args.limit, retry_errors=args.retry_errors)
        model = args.model or config.OPENAI_MODEL

        if args.dry_run:
            per_pair_in, total = inspector.estimate_cost(len(rows), args.image_size, model)
            print(f"{len(rows)} pair(s) would be judged with {model} "
                  f"at {args.image_size}px ({db.count_unjudged(conn)} unjudged total).")
            print(f"~{per_pair_in} input tokens/pair; estimated cost ~${total:.2f}. "
                  "No API calls made.")
            return 0

        if not config.OPENAI_API_KEY:
            print("error: OPENAI_API_KEY is not set (add it to .env).", file=sys.stderr)
            return 1
        if not rows:
            print("Nothing to judge: no unjudged pairs. Run ingest first.")
            return 0

        template = inspector.load_prompt_template()
        judged = changed_count = errors = 0
        with inspector.make_client() as client:
            for row in rows:
                older = _resolve_thumb(row["older_id"], row["older_thumb"], args.image_size)
                newer = _resolve_thumb(row["newer_id"], row["newer_thumb"], args.image_size)
                if not older or not newer:
                    db.set_pair_status(conn, row["id"], "error", "missing thumbnail")
                    conn.commit()
                    errors += 1
                    print(f"  {row['id']}: error (missing thumbnail)")
                    continue

                prompt = inspector.render_prompt(
                    template, row["older_captured_at"], row["newer_captured_at"]
                )
                payload = inspector.build_payload(model, prompt, older, newer)
                try:
                    report, raw = inspector.request_judgment(client, payload)
                except inspector.InspectorError as exc:
                    db.set_pair_status(conn, row["id"], "error", str(exc))
                    conn.commit()
                    errors += 1
                    print(f"  {row['id']}: error ({exc})")
                    continue

                report = inspector.apply_confidence_floor(report)
                db.insert_judgment(
                    conn,
                    pair_id=row["id"],
                    model=model,
                    old_description=report.old_description,
                    new_description=report.new_description,
                    changed=report.changed,
                    category=report.category,
                    magnitude=report.magnitude,
                    confidence=report.confidence,
                    evidence=report.evidence,
                    raw_json=raw,
                )
                db.set_pair_status(conn, row["id"], "judged")
                conn.commit()
                judged += 1
                if report.changed:
                    changed_count += 1
                    print(f"  {row['id']}: {report.category} ({report.magnitude}, "
                          f"{report.confidence:.2f})")
                else:
                    print(f"  {row['id']}: no_change ({report.confidence:.2f})")

        print(f"\nDone. judged: {judged}, changed: {changed_count}, errors: {errors}, "
              f"unjudged remaining: {db.count_unjudged(conn)}.")
    finally:
        conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    db.init_db()
    print(f"Chronos map: http://127.0.0.1:{args.port}")
    uvicorn.run("chronos.server:app", host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chronos", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser(
        "ingest", help="fetch Mapillary imagery for a bbox and build candidate pairs"
    )
    p_ingest.add_argument(
        "--bbox", required=True, type=_parse_bbox,
        metavar="minLon,minLat,maxLon,maxLat", help="bounding box to ingest",
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
