import json

import httpx
import pytest

from chronos import briefing


def _brief_json(pair_id="pair-1"):
    return json.dumps({
        "title": "One street change needs attention",
        "summary": "A durable road change is the highest-priority finding.",
        "findings": [{
            "pair_id": pair_id,
            "rationale": "The evidence describes a changed crosswalk.",
            "action": "inspect",
        }],
        "coverage_caveat": "Only places with paired Mapillary imagery are represented.",
    })


def test_build_payload_is_grounded_in_supplied_records():
    payload = briefing.build_payload([{"pair_id": "pair-1", "evidence": "new crosswalk"}])

    assert payload["model"] == "gpt-5.6-terra"
    assert "pair-1" in payload["messages"][1]["content"]
    assert payload["response_format"]["json_schema"]["strict"] is True


def test_request_brief_parses_structured_response():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": _brief_json()}}]
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        brief, raw = briefing.request_brief(client, {"model": "test"})

    assert brief.findings[0].pair_id == "pair-1"
    assert raw == _brief_json()


def test_validate_evidence_rejects_unknown_pair():
    brief = briefing.AreaBrief.model_validate_json(_brief_json("not-in-records"))

    with pytest.raises(RuntimeError, match="unknown evidence"):
        briefing.validate_evidence(brief, {"pair-1"})
