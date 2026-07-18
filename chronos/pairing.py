"""Pure pairing logic: find comparable street-photo pairs.

This is the most error-prone part of Chronos, so it is deliberately isolated:
no I/O, no third-party imports, only the stdlib. Everything is a pure function
over an in-memory list of :class:`Image`, which makes the rules that decide
"same place, years apart" fully unit-testable.

A candidate pair must satisfy *all* of:
  * great-circle distance <= ``max_dist_m``
  * circular heading difference <= ``max_heading_deg``
  * capture gap >= ``min_gap_days``
  * different Mapillary sequences (a single camera pass never pairs with itself)

Panoramas and images without a compass heading are dropped up front, since
their headings cannot be compared.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

# Default thresholds. These live here (not in config) because this module is the
# single source of truth for what counts as a pair; the ``ingest`` CLI exposes
# them as flags.
DEFAULT_MAX_DIST_M = 15.0
DEFAULT_MAX_HEADING_DEG = 30.0
DEFAULT_MIN_GAP_DAYS = 730  # ~2 years

_EARTH_RADIUS_M = 6_371_000.0
_MS_PER_DAY = 86_400_000.0
_DEG_M = _EARTH_RADIUS_M * math.pi / 180.0  # meters per degree of latitude


@dataclass(frozen=True)
class Image:
    """A single street-level photo, reduced to what pairing needs."""

    id: str
    lon: float
    lat: float
    captured_at: int              # epoch milliseconds
    sequence_id: str
    heading: float | None = None  # compass degrees [0, 360); None if unknown
    is_pano: bool = False


@dataclass(frozen=True)
class Pair:
    """Two images that pass the pairing predicate, older image first."""

    older_id: str
    newer_id: str
    distance_m: float
    heading_diff_deg: float
    gap_days: int
    score: float

    @property
    def id(self) -> str:
        return f"{self.older_id}_{self.newer_id}"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def heading_delta_deg(a: float, b: float) -> float:
    """Smallest absolute angle between two compass headings, in [0, 180]."""
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def _eligible(images: Iterable[Image]) -> list[Image]:
    """Drop images that cannot be aligned: panoramas and missing headings."""
    return [im for im in images if not im.is_pano and im.heading is not None]


def _grid(images: list[Image], cell_m: float, ref_lat: float) -> dict[tuple[int, int], list[int]]:
    """Bucket image indices into a lat/lon grid of ~``cell_m`` cells.

    Cell size equals the distance threshold, so any two images within
    ``cell_m`` fall in the same or an adjacent cell; a 3x3 neighborhood search
    is therefore complete. Longitude is scaled by ``ref_lat`` so cells stay
    roughly square across the (small) bounding box.
    """
    dlat = cell_m / _DEG_M
    dlon = cell_m / (_DEG_M * math.cos(math.radians(ref_lat)))
    cells: dict[tuple[int, int], list[int]] = {}
    for idx, im in enumerate(images):
        key = (int(math.floor(im.lat / dlat)), int(math.floor(im.lon / dlon)))
        cells.setdefault(key, []).append(idx)
    return cells


def _make_pair(a: Image, b: Image, dist: float, hdiff: float, score: float) -> Pair:
    older, newer = (a, b) if a.captured_at <= b.captured_at else (b, a)
    gap_days = int((newer.captured_at - older.captured_at) / _MS_PER_DAY)
    return Pair(older.id, newer.id, dist, hdiff, gap_days, score)


def candidate_pairs(
    images: Iterable[Image],
    *,
    max_dist_m: float = DEFAULT_MAX_DIST_M,
    max_heading_deg: float = DEFAULT_MAX_HEADING_DEG,
    min_gap_days: int = DEFAULT_MIN_GAP_DAYS,
) -> list[Pair]:
    """Every image pair satisfying the pairing predicate (before 1:1 selection).

    Score is ``distance/max_dist + heading_diff/max_heading`` — lower is better
    aligned. Boundaries are inclusive (``<=`` on distance/heading, ``>=`` on gap).
    """
    imgs = _eligible(list(images))
    if len(imgs) < 2:
        return []
    ref_lat = sum(im.lat for im in imgs) / len(imgs)
    cells = _grid(imgs, max_dist_m, ref_lat)

    out: list[Pair] = []
    for (ci, cj), members in cells.items():
        neighbors: list[int] = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                neighbors.extend(cells.get((ci + di, cj + dj), ()))
        for i in members:
            a = imgs[i]
            for j in neighbors:
                if j <= i:  # visit each unordered pair exactly once
                    continue
                b = imgs[j]
                if a.sequence_id == b.sequence_id:
                    continue
                if abs(a.captured_at - b.captured_at) / _MS_PER_DAY < min_gap_days:
                    continue
                hdiff = heading_delta_deg(a.heading, b.heading)
                if hdiff > max_heading_deg:
                    continue
                dist = haversine_m(a.lat, a.lon, b.lat, b.lon)
                if dist > max_dist_m:
                    continue
                score = dist / max_dist_m + hdiff / max_heading_deg
                out.append(_make_pair(a, b, dist, hdiff, score))
    return out


def find_pairs(
    images: Iterable[Image],
    *,
    max_dist_m: float = DEFAULT_MAX_DIST_M,
    max_heading_deg: float = DEFAULT_MAX_HEADING_DEG,
    min_gap_days: int = DEFAULT_MIN_GAP_DAYS,
) -> list[Pair]:
    """Select a 1:1 set of pairs, greedily, best-aligned first.

    Each image is used at most once, so one physical location yields a single
    pair instead of many near-duplicates. Ties in score prefer the longer time
    gap. This is the cost lever: every returned pair becomes one OpenAI call.
    """
    candidates = candidate_pairs(
        images,
        max_dist_m=max_dist_m,
        max_heading_deg=max_heading_deg,
        min_gap_days=min_gap_days,
    )
    candidates.sort(key=lambda p: (p.score, -p.gap_days))
    used: set[str] = set()
    chosen: list[Pair] = []
    for p in candidates:
        if p.older_id in used or p.newer_id in used:
            continue
        used.add(p.older_id)
        used.add(p.newer_id)
        chosen.append(p)
    return chosen
