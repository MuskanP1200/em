"""
llm.py
──────
LLM infrastructure for the estimate matching pipeline:
  - SYSTEM_PROMPT   : instruction prompt sent to the model
  - _NumpyEncoder   : JSON encoder that handles numpy scalar types
  - create_llm_client : instantiate the AzureOpenAI client
"""

from __future__ import annotations

import json
import logging

import numpy as np
from openai import AzureOpenAI

from estimate_matching.config import (
    LLM_API_VERSION,
    LLM_ENDPOINT,
)
from settings import get_settings

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an auto-estimate audit assistant.
Given a parts estimate in JSON, analyze each eligible parts line and return a JSON array.

STEP 1 — Determine expected_discount_pct for each line using this priority order:

  PRIORITY 1 (highest): special_notes line-level override
    Parse special_notes for any rule that targets this specific line by:
      - Part/damage type (e.g. "glass gets 15%", "bumper 10%")
      - Line description match (line_desc)
    If a matching rule is found → use that rate. Stop here.
    Note, this is helpful in detecting flat rates.

  PRIORITY 2: special_notes vehicle-make override
    Parse special_notes for any rule that targets the vehicle make (veh_make).
    Match make names case-insensitively (e.g. "TOYO" matches "Toyota", "NISS" matches "Nissan").
    If a matching rule is found → use that rate. Stop here.

  PRIORITY 3 (lowest): structured discount_rates
    Use discount_rates[rule_derived_discount_type] if non-zero.

  If no rate is found at any level → expected_discount_pct = null.

  discount_source should reflect which priority level was used:
    "special_notes_line"  — matched a line/damage/part-type rule in notes
    "special_notes_make"  — matched a vehicle make rule in notes
    "structured_rate"     — used discount_rates column
    "none"                — no rate found anywhere

  IMPORTANT: When using "special_notes_make" as your source, you MUST copy the exact sentence or phrase from special_notes that supports your rate.
  This is mandatory. If you cannot find a direct quote, you cannot use special_notes as the source.

STEP 2 — For each line return:
1. expected_discount_pct   — the discount % from Step 1 (null if none applies).
2. discount_source  — which priority level was used (from Step 1).
3. evidence         — exact quote from special_notes used to derive rate; null if discount_source is "structured_rate" or "none".
4. finding          — one-sentence summary: state the rate used, its source, and whether the applied amount was correct, under, or over the expected amount.

Return ONLY a JSON array, no markdown, no explanation outside JSON.
Schema per element:
{
  "cieca_dtl_hdr_id":         <str>,
  "parts_num":                <str>,
  "expected_discount_pct":  <float|null>,
  "discount_source":          <str>,
  "evidence":                 <str|null>,
  "finding":                  <str>
}"""

# ── JSON encoder ──────────────────────────────────────────────────────────────


class _NumpyEncoder(json.JSONEncoder):
    """Make numpy scalar types JSON-serializable."""

    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


# ── Client factory ────────────────────────────────────────────────────────────


def create_llm_client() -> AzureOpenAI:
    s = get_settings()
    api_key = None

    try:
        api_key = s.AZURE_OPENAI_API_KEY
    except Exception as e:
        logger.warning("Key vault not available, falling back to env: %s", e)

    if not api_key:
        raise ValueError("Key Vault secret 'AZURE-OPENAI-API-KEY' is empty or missing.")

    return AzureOpenAI(
        api_version=LLM_API_VERSION,
        azure_endpoint=LLM_ENDPOINT,
        api_key=api_key,
    )
