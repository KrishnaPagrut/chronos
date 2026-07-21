from types import SimpleNamespace

from chronos.server import _nearest_panorama


def feature(*, lat, lon, captured_at, is_pano):
    return SimpleNamespace(
        lat=lat, lon=lon, captured_at=captured_at, is_pano=is_pano
    )


def test_nearest_panorama_ignores_closer_fixed_view_image():
    nearby_photo = feature(lat=37.0, lon=-122.0, captured_at=20, is_pano=False)
    panorama = feature(lat=37.0005, lon=-122.0005, captured_at=10, is_pano=True)

    assert _nearest_panorama([nearby_photo, panorama], 37.0, -122.0) is panorama


def test_nearest_panorama_prefers_newer_capture_when_distance_ties():
    older = feature(lat=37.001, lon=-122.001, captured_at=10, is_pano=True)
    newer = feature(lat=36.999, lon=-121.999, captured_at=20, is_pano=True)

    assert _nearest_panorama([older, newer], 37.0, -122.0) is newer


def test_nearest_panorama_returns_none_when_no_panorama_is_available():
    photo = feature(lat=37.0, lon=-122.0, captured_at=20, is_pano=False)

    assert _nearest_panorama([photo], 37.0, -122.0) is None
