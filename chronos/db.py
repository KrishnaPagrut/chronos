"""SQLite schema and connection helpers.

The database is the single source of truth and the cache: images, candidate
pairs, judgments, and raw Mapillary API responses all live here so that no
command ever re-fetches or re-judges work that is already stored.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id           TEXT PRIMARY KEY,
    lon          REAL NOT NULL,
    lat          REAL NOT NULL,
    heading      REAL,
    captured_at  INTEGER NOT NULL,        -- epoch milliseconds
    sequence_id  TEXT,
    is_pano      INTEGER NOT NULL DEFAULT 0,
    thumb_path   TEXT,                     -- local cached thumbnail
    thumb_url    TEXT,                     -- signed Mapillary URL (expires)
    fetched_at   INTEGER
);

CREATE TABLE IF NOT EXISTS pairs (
    id               TEXT PRIMARY KEY,     -- "<older_id>_<newer_id>"
    older_id         TEXT NOT NULL REFERENCES images(id),
    newer_id         TEXT NOT NULL REFERENCES images(id),
    distance_m       REAL NOT NULL,
    heading_diff_deg REAL NOT NULL,
    gap_days         INTEGER NOT NULL,
    score            REAL NOT NULL,        -- lower = better aligned
    status           TEXT NOT NULL DEFAULT 'candidate',  -- candidate|judged|error
    error            TEXT,
    created_at       INTEGER
);

CREATE TABLE IF NOT EXISTS judgments (
    pair_id     TEXT PRIMARY KEY REFERENCES pairs(id),
    model       TEXT NOT NULL,
    changed     INTEGER NOT NULL,
    category    TEXT,
    magnitude   TEXT,                      -- major|moderate|subtle
    confidence  REAL,
    evidence    TEXT,
    raw_json    TEXT,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS api_cache (
    key        TEXT PRIMARY KEY,           -- hash of request URL + params
    body       TEXT NOT NULL,
    fetched_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pairs_status ON pairs(status);
CREATE INDEX IF NOT EXISTS idx_images_captured ON images(captured_at);
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name and foreign keys enforced."""
    conn = sqlite3.connect(str(db_path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    """Create the schema if it does not yet exist (idempotent)."""
    if db_path is None:
        config.ensure_dirs()
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
