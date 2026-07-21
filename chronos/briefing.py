"""GPT-5.6 neighborhood briefs grounded in Chronos judgments.

The model receives only the already-stored, evidence-bearing change records for
one map viewport. It never invents a location or an image claim: every finding
must cite one of the supplied pair IDs, which the server validates again before
returning it to the browser.
"""
from __future__ import annotations

import json
import time
from typing import Literal

import httpx
from pydantic import BaseModel

from . import config

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class BriefFinding(BaseModel):
    pair_id: str
    rationale: str
    action: Literal["inspect", "monitor", "record"]


class AreaBrief(BaseModel):
    title: str
    summary: str
    findings: list[BriefFinding]
    coverage_caveat: str


_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "chronos_area_brief",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pair_id": {"type": "string"},
                            "rationale": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["inspect", "monitor", "record"],
                            },
                        },
                        "required": ["pair_id", "rationale", "action"],
                        "additionalProperties": False,
                    },
                },
                "coverage_caveat": {"type": "string"},
            },
            "required": ["title", "summary", "findings", "coverage_caveat"],
            "additionalProperties": False,
        },
    },
}


def build_payload(records: list[dict]) -> dict:
    """Build a compact, evidence-only structured-output request."""
    prompt = """You are Chronos's neighborhood-change analyst. Summarize only the
supplied vision judgments; do not infer facts not in those records. Produce a
brief for a city operations or field team. Every finding must cite an exact
pair_id from the records. Prioritize durable changes and plausible subtle risks.
Use inspect for a field visit, monitor for a change worth watching, and record
for a well-supported change that needs no immediate visit. The coverage caveat
must say that this reflects only places with paired Mapillary imagery.

JUDGED CHANGES:\n""" + json.dumps(records, separators=(",", ":"))
    return {
        "model": config.BRIEF_MODEL,
        "messages": [
            {"role": "system", "content": "Return only the requested JSON schema."},
            {"role": "user", "content": prompt},
        ],
        "response_format": _SCHEMA,
        # GPT-5.x models require max_completion_tokens (max_tokens is rejected).
        "max_completion_tokens": 700,
    }


def request_brief(client: httpx.Client, payload: dict) -> tuple[AreaBrief, str]:
    """Call GPT-5.6 with bounded retries and validate the returned JSON."""
    last_error = "unknown error"
    for attempt in range(3):
        if attempt:
            time.sleep(2 ** (attempt - 1))
        try:
            response = client.post(OPENAI_URL, json=payload)
        except httpx.HTTPError as exc:
            last_error = f"network error: {exc}"
            continue
        if response.status_code in _RETRY_STATUSES:
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            continue
        if response.status_code != 200:
            raise RuntimeError(f"OpenAI returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            raw = response.json()["choices"][0]["message"]["content"]
            return AreaBrief.model_validate_json(raw), raw
        except Exception as exc:
            raise RuntimeError(f"invalid brief response: {exc}") from exc
    raise RuntimeError(f"brief request failed after 3 attempts; last: {last_error}")


def validate_evidence(brief: AreaBrief, pair_ids: set[str]) -> AreaBrief:
    """Reject a model response that cites a pair absent from the input records."""
    unknown = {finding.pair_id for finding in brief.findings} - pair_ids
    if unknown:
        raise RuntimeError("brief cited an unknown evidence pair")
    return brief
