"""FastAPI app: JSON API over the judgments DB + the static map UI.

Read-only over SQLite — the pipeline (ingest/inspect) writes, the server only
serves. Thumbnails come from the local cache in ``data/images/``, so the demo
works fully offline and never touches Mapillary's expiring signed URLs.
"""
from __future__ import annotations

import threading
import uuid
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, pairing

STATIC_DIR = Path(__file__).parent / "static"

# Guardrails for the live "search this area" pipeline.
MAX_SEARCH_SPAN_DEG = 0.05      # ~5.5 km; reject bigger boxes (too slow to fetch)
SEARCH_IMAGE_CAP = 400          # images fetched per search
SEARCH_TIME_BUDGET_S = 25.0     # wall-clock cap so dense areas stay responsive
JUDGE_LIMIT_CAP = 25            # pairs judged per "judge" click (spend control)

app = FastAPI(title="Chronos", docs_url=None, redoc_url=None)


def _iso_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _nearest_panorama(features, lat: float, lon: float):
    """Return the closest 360° image, preferring the newer one on a tie.

    Mapillary's ordinary images are perspective photos. They can only pan
    inside the original camera frame, so they are unsuitable for the
    Google-Street-View-like mode exposed by the pegman. This selector keeps
    that mode on spherical imagery from the first frame onward.
    """
    panoramas = [feature for feature in features if feature.is_pano]
    if not panoramas:
        return None
    return min(
        panoramas,
        key=lambda feature: (
            (feature.lat - lat) ** 2 + (feature.lon - lon) ** 2,
            -feature.captured_at,
        ),
    )


_CHANGES_SELECT = """
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
"""


def _row_to_change(r) -> dict:
    return {
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


@app.get("/api/changes")
def changes(include_unchanged: bool = False) -> list[dict]:
    """Judged pairs with everything the map and detail panel need."""
    where = "" if include_unchanged else "WHERE j.changed = 1"
    conn = db.connect()
    try:
        rows = conn.execute(
            _CHANGES_SELECT + where + " ORDER BY j.confidence DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_change(r) for r in rows]


@app.get("/api/config")
def config_public() -> dict:
    """Client-side config. The Mapillary token is a client-usable access token
    (like a Maps API key) — mapillary-js needs it in the browser to load tiles."""
    return {"mapillary_token": config.MAPILLARY_TOKEN, "has_token": bool(config.MAPILLARY_TOKEN)}


@app.get("/api/nearest")
def nearest(lat: float, lon: float) -> dict:
    """Nearest 360° Mapillary panorama — the Street View entrypoint.

    Ordinary Mapillary photos have a fixed camera frame, which makes their
    field of view feel restricted when dragged. The Street View pane therefore
    starts only on spherical images. Search a wider radius than the pairing
    flow so a panorama can be found when the drop is between capture points.
    """
    from . import mapillary

    if not config.MAPILLARY_TOKEN:
        raise HTTPException(status_code=503, detail="MAPILLARY_TOKEN not set")

    d = 0.0018  # ~200 m half-box at SF latitudes; panorama coverage is sparser
    conn = db.connect()
    try:
        with mapillary.MapillaryClient(conn, config.MAPILLARY_TOKEN) as client:
            feats = client.fetch_bbox(
                lon - d, lat - d, lon + d, lat + d,
                limit=300, time_budget_s=8.0,
            )
    finally:
        conn.close()
    panorama = _nearest_panorama(feats, lat, lon)
    if panorama is None:
        return {"image_id": None, "reason": "no_panorama"}
    return {
        "image_id": panorama.id,
        "lat": panorama.lat,
        "lon": panorama.lon,
        "date": _iso_date(panorama.captured_at),
        "coverage": len(feats),
        "panorama_coverage": sum(feature.is_pano for feature in feats),
    }


# ============================ "search this area" ============================
# Long-running ingest/judge work runs in a background thread so the HTTP request
# returns immediately with a job id; the client polls /api/job/{id} for progress.

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "running", "phase": "starting",
                         "done": 0, "total": 0, "result": None, "error": None}
    return job_id


def _update(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


_BBOX_FILTER = "inw.lat BETWEEN ? AND ? AND inw.lon BETWEEN ? AND ?"


def _candidate_rows(conn, bbox, limit):
    """Unjudged candidate pairs whose newer image falls in the bbox, best first."""
    w, s, e, n = bbox
    return conn.execute(
        """
        SELECT p.id, p.older_id, p.newer_id,
               io.captured_at AS older_captured_at, io.thumb_url AS older_url,
               inw.captured_at AS newer_captured_at, inw.thumb_url AS newer_url
        FROM pairs p
        JOIN images io  ON io.id  = p.older_id
        JOIN images inw ON inw.id = p.newer_id
        WHERE p.status = 'candidate' AND """ + _BBOX_FILTER + """
        ORDER BY p.score ASC LIMIT ?
        """,
        (s, n, w, e, limit),
    ).fetchall()


def _count_candidates(conn, bbox) -> int:
    w, s, e, n = bbox
    return conn.execute(
        "SELECT COUNT(*) FROM pairs p JOIN images inw ON inw.id = p.newer_id "
        "WHERE p.status = 'candidate' AND " + _BBOX_FILTER,
        (s, n, w, e),
    ).fetchone()[0]


def _changes_for_pairs(conn, pair_ids: list[str]) -> list[dict]:
    if not pair_ids:
        return []
    marks = ",".join("?" * len(pair_ids))
    rows = conn.execute(
        _CHANGES_SELECT + f" WHERE p.id IN ({marks})", pair_ids
    ).fetchall()
    return [_row_to_change(r) for r in rows]


def _brief_records(conn, bbox) -> list[dict]:
    """Evidence-bearing change records in one viewport, highest confidence first."""
    w, s, e, n = bbox
    rows = conn.execute(
        _CHANGES_SELECT + " WHERE j.changed = 1 AND " + _BBOX_FILTER +
        " ORDER BY j.confidence DESC LIMIT 30",
        (s, n, w, e),
    ).fetchall()
    records = []
    for row in rows:
        change = _row_to_change(row)
        records.append({
            "pair_id": change["pair_id"],
            "category": change["category"],
            "magnitude": change["magnitude"],
            "confidence": change["confidence"],
            "evidence": change["evidence"],
            "old_description": change["old_description"],
            "new_description": change["new_description"],
            "older_date": change["older"]["date"],
            "newer_date": change["newer"]["date"],
        })
    return records


def _ensure_thumb(conn, mly_client, image_id: str, url: str | None) -> str | None:
    """Return a local thumbnail path, downloading it from Mapillary if needed."""
    path = config.thumb_path(image_id, 1024)
    if path.exists():
        db.set_thumb_path(conn, image_id, str(path))
        return str(path)
    if not url:
        row = conn.execute(
            "SELECT thumb_url FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        url = row["thumb_url"] if row else None
    if not url:
        return None
    mly_client.download_thumb(url, path)
    db.set_thumb_path(conn, image_id, str(path))
    return str(path)


def _run_search(job_id: str, bbox) -> None:
    """Ingest imagery for a bbox and build candidate pairs (no OpenAI spend)."""
    from . import mapillary

    conn = db.connect()
    try:
        w, s, e, n = bbox
        _update(job_id, phase="fetching imagery")
        with mapillary.MapillaryClient(conn, config.MAPILLARY_TOKEN) as client:
            feats = client.fetch_bbox(
                w, s, e, n, limit=SEARCH_IMAGE_CAP, time_budget_s=SEARCH_TIME_BUDGET_S
            )
            for f in feats:
                db.upsert_image(
                    conn, id=f.id, lon=f.lon, lat=f.lat, heading=f.heading,
                    captured_at=f.captured_at, sequence_id=f.sequence_id,
                    is_pano=f.is_pano, thumb_url=f.thumb_1024_url,
                )
            conn.commit()

        _update(job_id, phase="pairing")
        pairs = pairing.find_pairs(db.load_images(conn))
        new_pairs = sum(1 for p in pairs if db.insert_pair(conn, p))
        conn.commit()

        candidates = _count_candidates(conn, bbox)
        judge_n = min(candidates, JUDGE_LIMIT_CAP)
        _, est_cost = inspector_estimate(judge_n)
        _update(job_id, status="done", phase="done", result={
            "added_images": len(feats), "new_pairs": new_pairs,
            "candidates": candidates, "judge_limit": judge_n,
            "est_cost": round(est_cost, 2),
        })
    except Exception as exc:  # surface to the client rather than 500
        _update(job_id, status="error", error=str(exc))
    finally:
        conn.close()


def inspector_estimate(n: int):
    from . import inspector
    return inspector.estimate_cost(n, 1024, config.OPENAI_MODEL)


def _run_judge(job_id: str, bbox, limit: int) -> None:
    """Judge candidate pairs in a bbox with the vision model (spends credits)."""
    from . import inspector, mapillary

    conn = db.connect()
    try:
        rows = _candidate_rows(conn, bbox, limit)
        _update(job_id, total=len(rows), phase="judging")
        if not rows:
            _update(job_id, status="done", phase="done",
                    result={"judged": 0, "changes": []})
            return

        template = inspector.load_prompt_template()
        model = config.OPENAI_MODEL
        judged_ids: list[str] = []
        with mapillary.MapillaryClient(conn, config.MAPILLARY_TOKEN) as mly, \
                inspector.make_client() as openai_client:
            for i, row in enumerate(rows):
                older = _ensure_thumb(conn, mly, row["older_id"], row["older_url"])
                newer = _ensure_thumb(conn, mly, row["newer_id"], row["newer_url"])
                if not older or not newer:
                    db.set_pair_status(conn, row["id"], "error", "missing thumbnail")
                    conn.commit()
                    _update(job_id, done=i + 1)
                    continue
                prompt = inspector.render_prompt(
                    template, row["older_captured_at"], row["newer_captured_at"]
                )
                payload = inspector.build_payload(model, prompt, older, newer)
                try:
                    report, raw = inspector.request_judgment(openai_client, payload)
                except inspector.InspectorError as exc:
                    db.set_pair_status(conn, row["id"], "error", str(exc))
                    conn.commit()
                    _update(job_id, done=i + 1)
                    continue
                report = inspector.apply_confidence_floor(report)
                db.insert_judgment(
                    conn, pair_id=row["id"], model=model,
                    old_description=report.old_description,
                    new_description=report.new_description,
                    changed=report.changed, category=report.category,
                    magnitude=report.magnitude, confidence=report.confidence,
                    evidence=report.evidence, raw_json=raw,
                )
                db.set_pair_status(conn, row["id"], "judged")
                conn.commit()
                judged_ids.append(row["id"])
                _update(job_id, done=i + 1)

        _update(job_id, status="done", phase="done", result={
            "judged": len(judged_ids),
            "changes": _changes_for_pairs(conn, judged_ids),
        })
    except Exception as exc:
        _update(job_id, status="error", error=str(exc))
    finally:
        conn.close()


def _validate_bbox(west, south, east, north):
    if east <= west or north <= south:
        raise HTTPException(status_code=400, detail="Invalid bounding box.")
    if (east - west) > MAX_SEARCH_SPAN_DEG or (north - south) > MAX_SEARCH_SPAN_DEG:
        raise HTTPException(status_code=400, detail="Area too large — zoom in to search.")


@app.post("/api/search_area")
def search_area(west: float, south: float, east: float, north: float) -> dict:
    if not config.MAPILLARY_TOKEN:
        raise HTTPException(status_code=503, detail="MAPILLARY_TOKEN not set")
    _validate_bbox(west, south, east, north)
    job_id = _new_job()
    threading.Thread(
        target=_run_search, args=(job_id, (west, south, east, north)), daemon=True
    ).start()
    return {"job_id": job_id}


@app.post("/api/judge_area")
def judge_area(
    west: float, south: float, east: float, north: float, limit: int = JUDGE_LIMIT_CAP
) -> dict:
    if not config.OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not set")
    _validate_bbox(west, south, east, north)
    limit = max(1, min(limit, JUDGE_LIMIT_CAP))
    job_id = _new_job()
    threading.Thread(
        target=_run_judge, args=(job_id, (west, south, east, north), limit), daemon=True
    ).start()
    return {"job_id": job_id}


@app.post("/api/brief_area")
def brief_area(west: float, south: float, east: float, north: float) -> dict:
    """Generate a cached, evidence-linked GPT-5.6 brief for a map viewport."""
    from . import briefing

    if not config.OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not set")
    _validate_bbox(west, south, east, north)
    bbox = (west, south, east, north)
    conn = db.connect()
    try:
        records = _brief_records(conn, bbox)
        if not records:
            raise HTTPException(status_code=404, detail="No judged changes in this area yet.")
        cache_material = json.dumps(
            {"model": config.BRIEF_MODEL, "records": records}, sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = "brief " + hashlib.sha256(cache_material.encode()).hexdigest()
        cached = db.cache_get(conn, cache_key)
        if cached is not None:
            return json.loads(cached)

        payload = briefing.build_payload(records)
        with briefing.httpx.Client(
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"}, timeout=120.0
        ) as client:
            brief, _ = briefing.request_brief(client, payload)
        briefing.validate_evidence(brief, {record["pair_id"] for record in records})
        result = {"brief": brief.model_dump(), "model": config.BRIEF_MODEL, "cached": False}
        db.cache_put(conn, cache_key, json.dumps(result))
        conn.commit()
        return result
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        conn.close()


@app.get("/api/job/{job_id}")
def job_status(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return dict(job)


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
