"""SQLite schema and connection helpers.

The database is the single source of truth and the cache: images, candidate
pairs, judgments, and raw Mapillary API responses all live here so that no
command ever re-fetches or re-judges work that is already stored.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from . import config, pairing

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
    pair_id          TEXT PRIMARY KEY REFERENCES pairs(id),
    model            TEXT NOT NULL,
    old_description  TEXT,                 -- model's read of the older image
    new_description  TEXT,                 -- model's read of the newer image
    changed          INTEGER NOT NULL,
    category         TEXT,
    magnitude        TEXT,                 -- major|moderate|subtle (always kept)
    confidence       REAL,
    evidence         TEXT,
    raw_json         TEXT,
    created_at       INTEGER
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
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table already exists (idempotent)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(judgments)")}
    for col in ("old_description", "new_description"):
        if col not in cols:
            conn.execute(f"ALTER TABLE judgments ADD COLUMN {col} TEXT")


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- api_cache ---------------------------------------------------------------

def cache_get(conn: sqlite3.Connection, key: str) -> str | None:
    """Return a cached response body for ``key``, or None if not stored."""
    row = conn.execute("SELECT body FROM api_cache WHERE key = ?", (key,)).fetchone()
    return row["body"] if row else None


def cache_put(conn: sqlite3.Connection, key: str, body: str) -> None:
    """Store (or replace) a cached response body under ``key``."""
    conn.execute(
        "INSERT OR REPLACE INTO api_cache (key, body, fetched_at) VALUES (?, ?, ?)",
        (key, body, _now_ms()),
    )


# --- images ------------------------------------------------------------------

def upsert_image(
    conn: sqlite3.Connection,
    *,
    id: str,
    lon: float,
    lat: float,
    heading: float | None,
    captured_at: int,
    sequence_id: str | None,
    is_pano: bool,
    thumb_url: str | None,
) -> None:
    """Insert an image, refreshing the volatile fields if it already exists.

    The primary key collapses duplicates that arrive from overlapping tiles;
    ``thumb_url`` is refreshed because Mapillary's signed URLs expire.
    """
    conn.execute(
        """
        INSERT INTO images (id, lon, lat, heading, captured_at, sequence_id,
                            is_pano, thumb_url, fetched_at)
        VALUES (:id, :lon, :lat, :heading, :captured_at, :sequence_id,
                :is_pano, :thumb_url, :fetched_at)
        ON CONFLICT(id) DO UPDATE SET
            lon=excluded.lon, lat=excluded.lat, heading=excluded.heading,
            captured_at=excluded.captured_at, sequence_id=excluded.sequence_id,
            is_pano=excluded.is_pano, thumb_url=excluded.thumb_url,
            fetched_at=excluded.fetched_at
        """,
        {
            "id": id, "lon": lon, "lat": lat, "heading": heading,
            "captured_at": captured_at, "sequence_id": sequence_id,
            "is_pano": 1 if is_pano else 0, "thumb_url": thumb_url,
            "fetched_at": _now_ms(),
        },
    )


def load_images(conn: sqlite3.Connection) -> list[pairing.Image]:
    """Load every stored image as a pairing.Image for candidate search."""
    rows = conn.execute(
        "SELECT id, lon, lat, heading, captured_at, sequence_id, is_pano FROM images"
    ).fetchall()
    return [
        pairing.Image(
            id=r["id"],
            lon=r["lon"],
            lat=r["lat"],
            captured_at=r["captured_at"],
            # A null sequence must never collide with another null: give each its
            # own synthetic id so same-sequence exclusion can't wrongly fire.
            sequence_id=r["sequence_id"] or f"_noseq_{r['id']}",
            heading=r["heading"],
            is_pano=bool(r["is_pano"]),
        )
        for r in rows
    ]


def set_thumb_path(conn: sqlite3.Connection, image_id: str, path: str) -> None:
    conn.execute("UPDATE images SET thumb_path = ? WHERE id = ?", (path, image_id))


# --- pairs -------------------------------------------------------------------

def insert_pair(conn: sqlite3.Connection, pair: pairing.Pair) -> bool:
    """Insert a candidate pair; return True if it was new (not already stored)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO pairs (id, older_id, newer_id, distance_m,
                                     heading_diff_deg, gap_days, score, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair.id, pair.older_id, pair.newer_id, pair.distance_m,
            pair.heading_diff_deg, pair.gap_days, pair.score, _now_ms(),
        ),
    )
    return cur.rowcount > 0


def pairs_to_judge(
    conn: sqlite3.Connection, *, limit: int, retry_errors: bool = False
) -> list[sqlite3.Row]:
    """Unjudged pairs joined with both images' dates and thumbnails.

    Best-aligned pairs first (lowest score) so a capped run spends its budget
    on the pairs most likely to be genuinely comparable.
    """
    statuses = ("candidate", "error") if retry_errors else ("candidate",)
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"""
        SELECT p.id, p.older_id, p.newer_id,
               io.captured_at AS older_captured_at,
               inw.captured_at AS newer_captured_at,
               io.thumb_path  AS older_thumb,
               inw.thumb_path AS newer_thumb
        FROM pairs p
        JOIN images io  ON io.id  = p.older_id
        JOIN images inw ON inw.id = p.newer_id
        WHERE p.status IN ({marks})
        ORDER BY p.score ASC
        LIMIT ?
        """,
        (*statuses, limit),
    ).fetchall()


def insert_judgment(
    conn: sqlite3.Connection,
    *,
    pair_id: str,
    model: str,
    old_description: str,
    new_description: str,
    changed: bool,
    category: str,
    magnitude: str,
    confidence: float,
    evidence: str,
    raw_json: str,
) -> None:
    """Store (or replace, for --retry-errors) a judgment for a pair."""
    conn.execute(
        """
        INSERT OR REPLACE INTO judgments
            (pair_id, model, old_description, new_description, changed,
             category, magnitude, confidence, evidence, raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair_id, model, old_description, new_description,
            1 if changed else 0, category, magnitude, confidence,
            evidence, raw_json, _now_ms(),
        ),
    )


def set_pair_status(
    conn: sqlite3.Connection, pair_id: str, status: str, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE pairs SET status = ?, error = ? WHERE id = ?",
        (status, error, pair_id),
    )


# --- counts ------------------------------------------------------------------

def _count(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def count_images(conn: sqlite3.Connection) -> int:
    return _count(conn, "SELECT COUNT(*) FROM images")


def count_pairs(conn: sqlite3.Connection) -> int:
    return _count(conn, "SELECT COUNT(*) FROM pairs")


def count_unjudged(conn: sqlite3.Connection) -> int:
    return _count(conn, "SELECT COUNT(*) FROM pairs WHERE status = 'candidate'")
