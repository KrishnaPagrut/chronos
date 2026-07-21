"""FastAPI app: JSON API over the judgments DB + the static map UI.

Read-only over SQLite — the pipeline (ingest/inspect) writes, the server only
serves. Thumbnails come from the local cache in ``data/images/``, so the demo
works fully offline and never touches Mapillary's expiring signed URLs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Chronos", docs_url=None, redoc_url=None)


def _iso_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@app.get("/api/changes")
def changes(include_unchanged: bool = False) -> list[dict]:
    """Judged pairs with everything the map and detail panel need."""
    where = "" if include_unchanged else "WHERE j.changed = 1"
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""
            SELECT p.id AS pair_id, p.distance_m, p.heading_diff_deg, p.gap_days,
                   j.model, j.changed, j.category, j.magnitude, j.confidence,
                   j.evidence, j.old_description, j.new_description,
                   io.id  AS older_id, io.captured_at AS older_at,
                   inw.id AS newer_id, inw.captured_at AS newer_at,
                   inw.lat, inw.lon
            FROM judgments j
            JOIN pairs  p   ON p.id   = j.pair_id
            JOIN images io  ON io.id  = p.older_id
            JOIN images inw ON inw.id = p.newer_id
            {where}
            ORDER BY j.confidence DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "pair_id": r["pair_id"],
            "lat": r["lat"],
            "lon": r["lon"],
            "changed": bool(r["changed"]),
            "category": r["category"],
            "magnitude": r["magnitude"],
            "confidence": r["confidence"],
            "evidence": r["evidence"],
            "old_description": r["old_description"],
            "new_description": r["new_description"],
            "model": r["model"],
            "distance_m": r["distance_m"],
            "heading_diff_deg": r["heading_diff_deg"],
            "gap_days": r["gap_days"],
            "older": {"image_id": r["older_id"], "date": _iso_date(r["older_at"])},
            "newer": {"image_id": r["newer_id"], "date": _iso_date(r["newer_at"])},
        }
        for r in rows
    ]


@app.get("/api/config")
def config_public() -> dict:
    """Client-side config. The Mapillary token is a client-usable access token
    (like a Maps API key) — mapillary-js needs it in the browser to load tiles."""
    return {"mapillary_token": config.MAPILLARY_TOKEN, "has_token": bool(config.MAPILLARY_TOKEN)}


@app.get("/api/nearest")
def nearest(lat: float, lon: float) -> dict:
    """Newest Mapillary image near a point — the entrypoint for Street View mode.

    Live-queries a small bbox around the drop point (cached in api_cache), then
    returns the most recently captured image so the pegman lands on current
    imagery.
    """
    from . import mapillary

    if not config.MAPILLARY_TOKEN:
        raise HTTPException(status_code=503, detail="MAPILLARY_TOKEN not set")

    d = 0.00045  # ~50 m half-box at SF latitudes
    conn = db.connect()
    try:
        with mapillary.MapillaryClient(conn, config.MAPILLARY_TOKEN) as client:
            feats = client.fetch_bbox(lon - d, lat - d, lon + d, lat + d, limit=60)
    finally:
        conn.close()
    if not feats:
        return {"image_id": None}
    newest = max(feats, key=lambda f: f.captured_at)
    return {
        "image_id": newest.id,
        "lat": newest.lat,
        "lon": newest.lon,
        "date": _iso_date(newest.captured_at),
        "coverage": len(feats),
    }


@app.get("/api/stats")
def stats() -> dict:
    conn = db.connect()
    try:
        judged = conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0]
        changed = conn.execute(
            "SELECT COUNT(*) FROM judgments WHERE changed = 1"
        ).fetchone()[0]
        return {
            "images": db.count_images(conn),
            "pairs": db.count_pairs(conn),
            "judged": judged,
            "changed": changed,
        }
    finally:
        conn.close()


@app.get("/images/{image_id}.jpg")
def image(image_id: str) -> FileResponse:
    """Serve a cached thumbnail, largest size available."""
    if "/" in image_id or "\\" in image_id or ".." in image_id:
        raise HTTPException(status_code=404)
    for size in (2048, 1024):
        path = config.thumb_path(image_id, size)
        if path.exists():
            return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="thumbnail not cached")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
