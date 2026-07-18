"""Tests for the inspector's pure logic: schema contract, coercion, retries.

No network: OpenAI calls are exercised through httpx.MockTransport only.
"""
from __future__ import annotations

import json

import httpx
import pytest

from chronos import inspector
from chronos.inspector import Report


def make_report(**overrides):
    base = dict(
        old_description="a shop", new_description="a bank", changed=True,
        category="storefront_change", magnitude="moderate",
        confidence=0.9, evidence="sign changed",
    )
    base.update(overrides)
    return Report(**base)


# --- schema contract ---------------------------------------------------------

def test_schema_field_order_describes_before_judging():
    props = list(inspector.RESPONSE_FORMAT["json_schema"]["schema"]["properties"])
    assert props == [
        "old_description", "new_description", "changed",
        "category", "magnitude", "confidence", "evidence",
    ]


def test_schema_is_strict_and_all_fields_required():
    js = inspector.RESPONSE_FORMAT["json_schema"]
    schema = js["schema"]
    assert js["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_pydantic_rejects_unknown_category():
    with pytest.raises(Exception):
        make_report(category="banana")


def test_pydantic_rejects_out_of_range_confidence():
    with pytest.raises(Exception):
        make_report(confidence=1.5)


# --- confidence floor --------------------------------------------------------

def test_low_confidence_coerced_to_no_change_keeps_magnitude():
    r = inspector.apply_confidence_floor(
        make_report(confidence=0.3, magnitude="subtle")
    )
    assert (r.changed, r.category, r.magnitude) == (False, "no_change", "subtle")


def test_confidence_at_floor_is_not_coerced():
    r = make_report(confidence=inspector.MIN_CHANGE_CONFIDENCE)
    assert inspector.apply_confidence_floor(r) is r


# --- prompt rendering --------------------------------------------------------

def test_render_prompt_substitutes_dates():
    template = "old: {OLD_DATE} new: {NEW_DATE} literal {braces} stay"
    out = inspector.render_prompt(template, 1528675200000, 1725235200000)
    assert out == "old: 2018-06-11 new: 2024-09-02 literal {braces} stay"


def test_real_prompt_file_has_placeholders():
    template = inspector.load_prompt_template()
    assert "{OLD_DATE}" in template and "{NEW_DATE}" in template


# --- retry/backoff -----------------------------------------------------------

def _ok_response() -> dict:
    content = json.dumps(dict(
        old_description="a", new_description="b", changed=False,
        category="no_change", magnitude="subtle", confidence=0.2, evidence="same",
    ))
    return {"choices": [{"message": {"content": content}}]}


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(inspector.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=_ok_response())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report, raw = inspector.request_judgment(client, {"model": "m"})
    assert len(calls) == 3
    assert report.changed is False and report.category == "no_change"


def test_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(inspector.time, "sleep", lambda s: None)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    )
    with pytest.raises(inspector.InspectorError, match="gave up"):
        inspector.request_judgment(client, {"model": "m"})


def test_non_retryable_status_raises_immediately():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="bad key")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(inspector.InspectorError, match="401"):
        inspector.request_judgment(client, {"model": "m"})
    assert len(calls) == 1
