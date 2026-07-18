"""Unit tests for the pure pairing logic — the error-prone core of Chronos."""
from __future__ import annotations

import math
import random

import pytest

from chronos import pairing
from chronos.pairing import (
    Image,
    candidate_pairs,
    find_pairs,
    haversine_m,
    heading_delta_deg,
)

DAY_MS = 86_400_000
YEAR_MS = 365 * DAY_MS


def img(id, lat, lon, *, days=0, seq="s", heading=0.0, pano=False):
    """Terse Image builder; ``days`` is an epoch-day offset for capture time."""
    return Image(
        id=id, lon=lon, lat=lat, captured_at=days * DAY_MS,
        sequence_id=seq, heading=heading, is_pano=pano,
    )


# --- haversine ---------------------------------------------------------------

def test_haversine_zero_distance():
    assert haversine_m(37.0, -122.0, 37.0, -122.0) == 0.0


def test_haversine_one_degree_at_equator():
    # 1 degree of longitude at the equator ~ 111.195 km (R = 6,371,000 m).
    assert math.isclose(haversine_m(0.0, 0.0, 0.0, 1.0), 111194.9, rel_tol=1e-4)


def test_haversine_is_symmetric():
    assert haversine_m(37.76, -122.42, 37.761, -122.419) == haversine_m(
        37.761, -122.419, 37.76, -122.42
    )


# --- heading -----------------------------------------------------------------

@pytest.mark.parametrize(
    "a,b,expected",
    [(0, 0, 0), (10, 350, 20), (355, 5, 10), (90, 270, 180),
     (0, 180, 180), (359, 1, 2), (45, 135, 90)],
)
def test_heading_delta_wraparound(a, b, expected):
    assert heading_delta_deg(a, b) == pytest.approx(expected)


# --- predicate boundaries are inclusive --------------------------------------

def test_distance_boundary_is_inclusive():
    a = img("a", 37.76, -122.42, days=0, seq="s1")
    b = img("b", 37.7601, -122.42, days=1000, seq="s2")
    d = haversine_m(a.lat, a.lon, b.lat, b.lon)
    assert len(candidate_pairs([a, b], max_dist_m=d)) == 1
    assert len(candidate_pairs([a, b], max_dist_m=d - 0.001)) == 0


def test_heading_boundary_is_inclusive():
    a = img("a", 37.76, -122.42, days=0, seq="s1", heading=100.0)
    b = img("b", 37.76001, -122.42, days=1000, seq="s2", heading=130.0)
    hd = heading_delta_deg(a.heading, b.heading)  # exactly 30
    assert len(candidate_pairs([a, b], max_heading_deg=hd)) == 1
    assert len(candidate_pairs([a, b], max_heading_deg=hd - 0.001)) == 0


def test_gap_boundary_is_inclusive():
    a = img("a", 37.76, -122.42, days=0, seq="s1")
    b = img("b", 37.76001, -122.42, days=730, seq="s2")
    assert len(candidate_pairs([a, b], min_gap_days=730)) == 1
    assert len(candidate_pairs([a, b], min_gap_days=731)) == 0


# --- exclusions --------------------------------------------------------------

def test_same_sequence_excluded():
    a = img("a", 37.76, -122.42, days=0, seq="same")
    b = img("b", 37.76001, -122.42, days=1000, seq="same")
    assert candidate_pairs([a, b]) == []


def test_panorama_excluded():
    a = img("a", 37.76, -122.42, days=0, seq="s1", pano=True)
    b = img("b", 37.76001, -122.42, days=1000, seq="s2")
    assert candidate_pairs([a, b]) == []


def test_missing_heading_excluded():
    a = Image("a", -122.42, 37.76, 0, "s1", heading=None)
    b = img("b", 37.76001, -122.42, days=1000, seq="s2")
    assert candidate_pairs([a, b]) == []


# --- pair fields -------------------------------------------------------------

def test_pair_orders_older_first_and_builds_id():
    newer = img("a", 37.76, -122.42, days=1000, seq="s1")
    older = img("b", 37.76001, -122.42, days=0, seq="s2")
    (p,) = candidate_pairs([newer, older])
    assert (p.older_id, p.newer_id) == ("b", "a")
    assert p.id == "b_a"
    assert p.gap_days == 1000


# --- greedy 1:1 selection ----------------------------------------------------

def test_greedy_uses_each_image_at_most_once():
    old = img("old", 37.76, -122.42, days=0, seq="old")
    n1 = img("n1", 37.760005, -122.42, days=1000, seq="a", heading=1)
    n2 = img("n2", 37.76001, -122.42, days=1000, seq="b", heading=2)
    n3 = img("n3", 37.76002, -122.42, days=1000, seq="c", heading=3)
    pairs = find_pairs([old, n1, n2, n3])
    used = [pid for p in pairs for pid in (p.older_id, p.newer_id)]
    assert len(used) == len(set(used))  # no image reused
    assert any("old" in (p.older_id, p.newer_id) for p in pairs)


def test_greedy_prefers_best_aligned_pair():
    old = img("old", 37.76, -122.42, days=0, seq="old")
    near = img("near", 37.760003, -122.42, days=1000, seq="a", heading=0)
    far = img("far", 37.76001, -122.42, days=1000, seq="b", heading=10)
    (p,) = find_pairs([old, near, far])
    assert "near" in (p.older_id, p.newer_id)


# --- grid completeness vs brute force ----------------------------------------

def _brute_force(images, max_dist, max_heading, min_gap):
    imgs = [im for im in images if not im.is_pano and im.heading is not None]
    res = set()
    for i in range(len(imgs)):
        for j in range(i + 1, len(imgs)):
            a, b = imgs[i], imgs[j]
            if a.sequence_id == b.sequence_id:
                continue
            if abs(a.captured_at - b.captured_at) / DAY_MS < min_gap:
                continue
            if heading_delta_deg(a.heading, b.heading) > max_heading:
                continue
            if haversine_m(a.lat, a.lon, b.lat, b.lon) > max_dist:
                continue
            res.add(frozenset((a.id, b.id)))
    return res


def test_grid_matches_brute_force():
    rng = random.Random(1234)
    images = [
        Image(
            id=f"i{k}",
            lat=37.758 + rng.random() * 0.011,     # ~1.2 km north-south
            lon=-122.420 + rng.random() * 0.014,   # ~1.2 km east-west
            captured_at=int(rng.random() * 6 * YEAR_MS),
            sequence_id=f"seq{rng.randint(0, 40)}",
            heading=rng.random() * 360.0,
            is_pano=(rng.random() < 0.05),
        )
        for k in range(250)
    ]
    got = {frozenset((p.older_id, p.newer_id)) for p in candidate_pairs(images)}
    expected = _brute_force(
        images,
        pairing.DEFAULT_MAX_DIST_M,
        pairing.DEFAULT_MAX_HEADING_DEG,
        pairing.DEFAULT_MIN_GAP_DAYS,
    )
    assert got == expected


def test_empty_and_single_inputs():
    assert candidate_pairs([]) == []
    assert candidate_pairs([img("only", 37.76, -122.42)]) == []
    assert find_pairs([]) == []
