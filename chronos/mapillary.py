"""Mapillary Graph API client: fetch street-level images for a bounding box.

The Graph ``/images`` endpoint refuses large or dense bounding boxes with an
HTTP 500 ("Please reduce the amount of data you're asking for") — and a small
``limit`` does not help, because the server gathers every image in the box
before trimming. The fix is to *tile*: cover the requested box with small
sub-boxes, page through each, and stitch the results. Boxes that are still too
dense are split into quadrants and retried. Duplicate images from overlapping
tiles collapse on the ``images`` primary key, and pairing runs globally over the
whole DB afterward, so a pair that straddles a tile seam is still found.

Every request is rate-limited and cached in ``api_cache`` so re-running ingest
over the same box is free.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

import httpx

from . import db

GRAPH_URL = "https://graph.mapillary.com/images"

# Fields to request per image. Prefer the SfM-corrected ``computed_*`` variants;
# fall back to the raw sensor values when they are absent.
FIELDS = (
    "id,captured_at,compass_angle,computed_compass_angle,"
    "geometry,computed_geometry,sequence,is_pano,thumb_1024_url,thumb_2048_url"
)

_MIN_REQUEST_INTERVAL_S = 0.2   # ~5 requests/second, well under any rate limit
_START_TILE_DEG = 0.005         # ~500 m at SF latitudes — a box this small is accepted
_MIN_TILE_DEG = 0.0004          # ~40 m — stop subdividing here even if still dense
_PAGE_LIMIT = 500               # images per page when paginating a single tile


@dataclass
class Feature:
    """One image parsed from the Graph API, ready for ``db.upsert_image``."""

    id: str
    lon: float
    lat: float
    heading: float | None
    captured_at: int
    sequence_id: str | None
    is_pano: bool
    thumb_1024_url: str | None
    thumb_2048_url: str | None


class DataTooLarge(Exception):
    """The endpoint refused a tile as too dense; the caller should subdivide."""


class MapillaryClient:
    """Rate-limited, cached client over the Graph API ``/images`` endpoint."""

    def __init__(self, conn: sqlite3.Connection, token: str, *, refresh: bool = False):
        self._conn = conn
        self._refresh = refresh
        self._http = httpx.Client(
            headers={"Authorization": f"OAuth {token}"}, timeout=60.0
        )
        self._last_request = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MapillaryClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low-level request ----------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < _MIN_REQUEST_INTERVAL_S:
            time.sleep(_MIN_REQUEST_INTERVAL_S - elapsed)
        self._last_request = time.monotonic()

    def _get_json(self, url: str, params: dict | None) -> dict:
        """GET a URL (with optional params), caching the JSON body by request.

        The cache key is the fully-resolved URL with the auth token excluded, so
        it is stable across runs and never stores the secret.
        """
        request = self._http.build_request("GET", url, params=params)
        cache_key = "GET " + str(request.url)

        if not self._refresh:
            cached = db.cache_get(self._conn, cache_key)
            if cached is not None:
                return json.loads(cached)

        self._throttle()
        resp = self._http.send(request)
        if resp.status_code == 500:
            # The documented "too much data" signal — distinguish it from a real
            # server fault so the caller knows to subdivide rather than abort.
            raise DataTooLarge(str(request.url))
        resp.raise_for_status()
        body = resp.text
        db.cache_put(self._conn, cache_key, body)
        self._conn.commit()
        return json.loads(body)

    # -- feature parsing ------------------------------------------------------

    @staticmethod
    def _parse_feature(item: dict) -> Feature | None:
        geom = item.get("computed_geometry") or item.get("geometry")
        if not geom or "coordinates" not in geom:
            return None
        lon, lat = geom["coordinates"][0], geom["coordinates"][1]

        heading = item.get("computed_compass_angle")
        if heading is None:
            heading = item.get("compass_angle")

        captured_at = item.get("captured_at")
        if captured_at is None:
            return None

        seq = item.get("sequence")
        return Feature(
            id=str(item["id"]),
            lon=float(lon),
            lat=float(lat),
            heading=float(heading) if heading is not None else None,
            captured_at=int(captured_at),
            sequence_id=str(seq) if seq else None,
            is_pano=bool(item.get("is_pano", False)),
            thumb_1024_url=item.get("thumb_1024_url"),
            thumb_2048_url=item.get("thumb_2048_url"),
        )

    # -- tile fetch -----------------------------------------------------------

    def _fetch_tile(self, west: float, south: float, east: float, north: float) -> list[Feature]:
        """Page through one tile, following ``paging.next`` links to the end."""
        features: list[Feature] = []
        params: dict | None = {
            "fields": FIELDS,
            "bbox": f"{west},{south},{east},{north}",
            "limit": _PAGE_LIMIT,
        }
        url = GRAPH_URL
        while True:
            data = self._get_json(url, params)
            for item in data.get("data", []):
                feat = self._parse_feature(item)
                if feat is not None:
                    features.append(feat)
            nxt = data.get("paging", {}).get("next")
            if not nxt:
                break
            # ``next`` is a fully-formed URL with its own cursor; drop our params.
            url, params = nxt, None
        return features

    # -- public bbox fetch with auto-tiling -----------------------------------

    def fetch_bbox(
        self,
        west: float,
        south: float,
        east: float,
        north: float,
        *,
        limit: int,
        time_budget_s: float | None = None,
    ) -> list[Feature]:
        """Fetch images in a bbox, tiling + subdividing to dodge HTTP 500s.

        Returns up to ``limit`` unique features (by id). A depth-first stack of
        tiles is worked through; any tile the endpoint rejects is split into four
        quadrants and pushed back, down to ``_MIN_TILE_DEG``.

        ``time_budget_s`` caps wall-clock time: in a very dense area the recursive
        subdivision can otherwise run for minutes, so the interactive "search this
        area" flow passes a budget and accepts whatever imagery it gathered.
        """
        seen: dict[str, Feature] = {}
        stack = list(_grid_tiles(west, south, east, north, _START_TILE_DEG))
        start = time.monotonic()

        while stack and len(seen) < limit:
            if time_budget_s is not None and time.monotonic() - start > time_budget_s:
                break
            w, s, e, n = stack.pop()
            try:
                for feat in self._fetch_tile(w, s, e, n):
                    seen.setdefault(feat.id, feat)
                    if len(seen) >= limit:
                        break
            except DataTooLarge:
                if (e - w) <= _MIN_TILE_DEG or (n - s) <= _MIN_TILE_DEG:
                    # As small as we go; skip this sliver rather than loop forever.
                    continue
                stack.extend(_split_quad(w, s, e, n))
        return list(seen.values())

    # -- thumbnail download ---------------------------------------------------

    def download_thumb(self, url: str, dest) -> None:
        """Download a thumbnail image to ``dest`` (a path-like)."""
        self._throttle()
        resp = self._http.get(url)
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            fh.write(resp.content)


# --- tile geometry (pure helpers) --------------------------------------------

def _grid_tiles(west: float, south: float, east: float, north: float, step: float):
    """Yield ``step``-degree tiles covering the box (last row/col may be thinner)."""
    lat = south
    while lat < north:
        top = min(lat + step, north)
        lon = west
        while lon < east:
            right = min(lon + step, east)
            yield (lon, lat, right, top)
            lon = right
        lat = top


def _split_quad(west: float, south: float, east: float, north: float):
    """Split a box into its four quadrants."""
    mid_lon = (west + east) / 2
    mid_lat = (south + north) / 2
    return [
        (west, south, mid_lon, mid_lat),
        (mid_lon, south, east, mid_lat),
        (west, mid_lat, mid_lon, north),
        (mid_lon, mid_lat, east, north),
    ]
