"""Read-only production explorer for Chronos's bundled demo data.

This module intentionally imports neither the ingestion client nor either
OpenAI workflow. It opens SQLite in read-only mode and exposes only the map's
precomputed data and locally bundled thumbnails.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config

STATIC_DIR = Path(__file__).parent / "static"

if not config.DEMO_MODE:
    raise RuntimeError("demo_server requires CHRONOS_DEMO_ONLY=1")
if not config.DB_PATH.is_file():
    raise RuntimeError(f"demo database is missing: {config.DB_PATH}")

app = FastAPI(title="Chronos Explorer", docs_url=None, redoc_url=None)


def _connect() -> sqlite3.Connection:
    """Open the committed bundle without any possibility of SQLite writes."""
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iso_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


_CHANGES_SELECT = """
    SELECT p.id AS pair_id, p.distance_m, p.heading_diff_deg, p.gap_days,
           j.model, j.changed, j.category, j.magnitude, j.confidence,
           j.evidence, j.old_description, j.new_description,
           io.id AS older_id, io.captured_at AS older_at,
           inw.id AS newer_id, inw.captured_at AS newer_at,
           inw.lat, inw.lon
    FROM judgments j
    JOIN pairs p ON p.id = j.pair_id
    JOIN images io ON io.id = p.older_id
    JOIN images inw ON inw.id = p.newer_id
"""


def _row_to_change(row: sqlite3.Row) -> dict:
    return {
        "pair_id": row["pair_id"], "lat": row["lat"], "lon": row["lon"],
        "changed": bool(row["changed"]), "category": row["category"],
        "magnitude": row["magnitude"], "confidence": row["confidence"],
        "evidence": row["evidence"], "old_description": row["old_description"],
        "new_description": row["new_description"], "model": row["model"],
        "distance_m": row["distance_m"], "heading_diff_deg": row["heading_diff_deg"],
        "gap_days": row["gap_days"],
        "older": {"image_id": row["older_id"], "date": _iso_date(row["older_at"])},
        "newer": {"image_id": row["newer_id"], "date": _iso_date(row["newer_at"])},
    }


@app.get("/api/changes")
def changes(include_unchanged: bool = False) -> list[dict]:
    where = "" if include_unchanged else "WHERE j.changed = 1"
    conn = _connect()
    try:
        rows = conn.execute(_CHANGES_SELECT + where + " ORDER BY j.confidence DESC").fetchall()
    finally:
        conn.close()
    return [_row_to_change(row) for row in rows]


@app.get("/api/stats")
def stats() -> dict:
    conn = _connect()
    try:
        return {
            "images": conn.execute("SELECT COUNT(*) FROM images").fetchone()[0],
            "pairs": conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0],
            "judged": conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0],
            "changed": conn.execute("SELECT COUNT(*) FROM judgments WHERE changed = 1").fetchone()[0],
        }
    finally:
        conn.close()


@app.get("/api/config")
def public_config() -> dict:
    """Tell the shared UI to hide every live-data or paid interaction."""
    return {"demo_mode": True, "has_token": False, "mapillary_token": ""}


@app.get("/images/{image_id}.jpg")
def image(image_id: str) -> FileResponse:
    if "/" in image_id or "\\" in image_id or ".." in image_id:
        raise HTTPException(status_code=404)
    for size in (2048, 1024):
        path = config.thumb_path(image_id, size)
        if path.is_file():
            return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="thumbnail not in demo bundle")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
