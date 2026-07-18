"""OpenAI vision judge: strict-schema change reports for image pairs.

The prompt lives in ``prompts/inspector.md`` (with ``{OLD_DATE}``/``{NEW_DATE}``
placeholders substituted per pair). The response contract is enforced twice:
once server-side via structured outputs (``response_format: json_schema``,
``strict: true``) and once client-side by the :class:`Report` pydantic model.

Two rules the prompt states are ALSO enforced in code, because models
occasionally violate their own instructions:

* field order — the schema asks for ``old_description``/``new_description``
  BEFORE the verdict fields, so the model describes what it sees before it
  judges (order in the schema's ``properties`` is the order the model
  generates);
* the confidence floor — any report with ``confidence < 0.40`` is coerced to
  ``changed=false, category="no_change"`` before it is written. ``magnitude``
  is kept untouched either way so precision can later be split by tier.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from . import config

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

CATEGORIES = (
    "construction", "demolition", "storefront_change", "signage",
    "road_infrastructure", "surface_condition", "street_furniture",
    "vegetation", "other", "no_change",
)
MAGNITUDES = ("major", "moderate", "subtle")

# Below this confidence a positive verdict is not trusted: coerced to no_change.
MIN_CHANGE_CONFIDENCE = 0.40

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 2.0          # 2s, 4s between retries
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class Report(BaseModel):
    """Client-side mirror of the structured-output schema (same field order)."""

    old_description: str
    new_description: str
    changed: bool
    category: Literal[CATEGORIES]  # type: ignore[valid-type]
    magnitude: Literal[MAGNITUDES]  # type: ignore[valid-type]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str


# Property order below is generation order: descriptions first, verdict after.
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "street_change_report",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "old_description": {
                    "type": "string",
                    "description": "One sentence: what is at this spot in IMAGE A (older).",
                },
                "new_description": {
                    "type": "string",
                    "description": "One sentence: what is at this spot in IMAGE B (newer).",
                },
                "changed": {
                    "type": "boolean",
                    "description": "Did a durable physical change occur between A and B?",
                },
                "category": {
                    "type": "string",
                    "enum": list(CATEGORIES),
                    "description": "Kind of change; no_change if changed=false.",
                },
                "magnitude": {
                    "type": "string",
                    "enum": list(MAGNITUDES),
                    "description": "major | moderate | subtle; use subtle when changed=false.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0-1.0 per the calibration anchors in the prompt.",
                },
                "evidence": {
                    "type": "string",
                    "description": "One sentence naming the specific visual detail.",
                },
            },
            "required": [
                "old_description", "new_description", "changed",
                "category", "magnitude", "confidence", "evidence",
            ],
            "additionalProperties": False,
        },
    },
}


class InspectorError(Exception):
    """A pair-level failure that should mark the pair status=error."""


# --- prompt ------------------------------------------------------------------

def load_prompt_template() -> str:
    """Read ``prompts/inspector.md`` and verify its date placeholders exist."""
    text = (config.PROMPTS_DIR / "inspector.md").read_text(encoding="utf-8")
    for placeholder in ("{OLD_DATE}", "{NEW_DATE}"):
        if placeholder not in text:
            raise InspectorError(f"prompt is missing the {placeholder} placeholder")
    return text


def format_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def render_prompt(template: str, old_ms: int, new_ms: int) -> str:
    """Substitute the capture dates; str.replace so no other braces are touched."""
    return template.replace("{OLD_DATE}", format_date(old_ms)).replace(
        "{NEW_DATE}", format_date(new_ms)
    )


# --- request -----------------------------------------------------------------

def image_data_url(path: str | Path) -> str:
    data = Path(path).read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def build_payload(
    model: str, prompt: str, older_path: str | Path, newer_path: str | Path
) -> dict:
    """One chat-completions request: prompt text + labeled A/B images."""
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "text", "text": "IMAGE A (older):"},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url(older_path), "detail": "high"},
                    },
                    {"type": "text", "text": "IMAGE B (newer):"},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url(newer_path), "detail": "high"},
                    },
                ],
            }
        ],
        "response_format": RESPONSE_FORMAT,
        "max_tokens": 500,
    }


# --- calling -----------------------------------------------------------------

def request_judgment(client: httpx.Client, payload: dict) -> tuple[Report, str]:
    """POST with retry/backoff; return the validated report and its raw JSON."""
    last_error = "unknown error"
    for attempt in range(_MAX_ATTEMPTS):
        if attempt:
            time.sleep(_BACKOFF_BASE_S * 2 ** (attempt - 1))
        try:
            resp = client.post(OPENAI_URL, json=payload)
        except httpx.HTTPError as exc:
            last_error = f"network error: {exc}"
            continue
        if resp.status_code in _RETRY_STATUSES:
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            continue
        if resp.status_code != 200:
            raise InspectorError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            raw = resp.json()["choices"][0]["message"]["content"]
            return Report.model_validate_json(raw), raw
        except Exception as exc:  # malformed/refused response — not retryable
            raise InspectorError(f"bad response: {exc}") from exc
    raise InspectorError(f"gave up after {_MAX_ATTEMPTS} attempts; last: {last_error}")


def apply_confidence_floor(report: Report) -> Report:
    """Code-level backstop for the prompt's own rule.

    Below MIN_CHANGE_CONFIDENCE a positive verdict becomes no_change;
    magnitude is preserved so per-tier precision stats stay possible.
    """
    if report.confidence < MIN_CHANGE_CONFIDENCE and (
        report.changed or report.category != "no_change"
    ):
        return report.model_copy(update={"changed": False, "category": "no_change"})
    return report


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        timeout=120.0,
    )


# --- cost estimate (dry run) -------------------------------------------------

# Approximate high-detail image token cost by thumbnail size (OpenAI tiling).
_IMAGE_TOKENS = {1024: 765, 2048: 1105}
_OUTPUT_TOKENS = 220
# (input $/M tokens, output $/M tokens); unknown models fall back to gpt-4o.
_MODEL_PRICES = {"gpt-4o": (2.50, 10.00), "gpt-4o-mini": (0.15, 0.60)}


def estimate_cost(n_pairs: int, image_size: int, model: str) -> tuple[int, float]:
    """Rough (input_tokens_per_pair, total_usd) for judging ``n_pairs``."""
    prompt_tokens = len(load_prompt_template()) // 4
    per_pair_in = prompt_tokens + 2 * _IMAGE_TOKENS.get(image_size, 765)
    price_in, price_out = _MODEL_PRICES.get(model, _MODEL_PRICES["gpt-4o"])
    total = n_pairs * (per_pair_in * price_in + _OUTPUT_TOKENS * price_out) / 1_000_000
    return per_pair_in, total
