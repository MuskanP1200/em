"""
parts_audit.py
──────────────
End-to-end parts validation for one estimate:
  1. Filter eligible parts lines
  2. Derive discount type per line
  3. Build LLM input JSON
  4. Call LLM and compute derived discount columns
  5. Validate parts subtotals against line aggregates (rule-based)
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from estimate_matching.config import (
    BAD_PART_NUMBERS,
    DOMESTIC_MAKES,
    FOREIGN_MAKES,
    LLM_DEPLOYMENT,
    LLM_MAX_TOKENS,
    ROUND_DECIMALS,
)
from estimate_matching.helpers import cast_numeric
from estimate_matching.llm import SYSTEM_PROMPT, _NumpyEncoder

logger = logging.getLogger(__name__)


# ── Filtering ─────────────────────────────────────────────────────────────────


def filter_parts_lines(est_df: pd.DataFrame) -> pd.DataFrame:
    """
    Keep only genuine, discount-eligible parts lines.
    Drops: no parts detail, existing parts, misclassified part numbers.
    """
    df = est_df.copy()
    df = df[df["cieca_part_dtl_line_id"].notna()]
    df = df[
        ~df["cieca_part_typ_dsc"]
        .str.strip()
        .str.lower()
        .str.contains("existing", na=False)
    ]
    df = df[~df["dtl_part_nbr"].str.strip().str.lower().isin(BAD_PART_NUMBERS)]
    return df


# ── Discount type derivation ──────────────────────────────────────────────────


def get_discount_type(parts_type: str, veh_make: str) -> str | None:
    """Derive discount type from parts classification and vehicle make."""
    if pd.isna(parts_type):
        return None
    pt = parts_type.lower()
    if "aftermarket" in pt:
        return "keyless"
    if "new" in pt:
        make = str(veh_make).upper().strip() if pd.notna(veh_make) else ""
        if make in FOREIGN_MAKES:
            return "foreign"
        if make in DOMESTIC_MAKES:
            return "domestic"
        return "unknown_make"
    return None


# ── LLM input builder ─────────────────────────────────────────────────────────


def build_estimate_json(est_df: pd.DataFrame) -> dict:
    """Convert filtered parts lines for one estimate into LLM input JSON."""
    first = est_df.iloc[0]

    parts_lines = []
    for _, row in est_df.iterrows():
        parts_lines.append(
            {
                "cieca_dtl_hdr_id": row.get("cieca_dtl_hdr_id"),
                "line_desc": row.get("line_dsc"),
                "parts_type": str(row.get("cieca_part_typ_dsc", "")).strip(),
                "parts_num": str(row.get("dtl_part_nbr", "")).strip(),
                "prts_amt": float(row.get("dtl_tot_part_price_amt", 0) or 0),
                "rule_derived_discount_type": get_discount_type(
                    str(row.get("cieca_part_typ_dsc", "")),
                    str(row.get("veh_make", "")),
                ),
                "cieca_line_adj_actual": float(row.get("cieca_line_adj_amt", 0) or 0),
            }
        )

    return {
        "est_id": first["est_id"],
        "veh_make": str(first.get("veh_make", "")),
        "discount_rates": {
            "foreign": float(first.get("frn_part_disc_amt", 0) or 0),
            "domestic": float(first.get("dmstc_part_disc_amt", 0) or 0),
            "aftermarket": float(first.get("kyls_disc_amt", 0) or 0),
        },
        "special_notes": str(first.get("specl_instruct_txt", "") or ""),
        "sublet_markup": str(first.get("sublet_mrkup", "")),
        "postscn_flag": str(first.get("postscn", "")),
        "eligible_parts_lines": parts_lines,
    }


# ── Derived columns (from LLM output) ────────────────────────────────────────


def _compute_parts_derived_cols(line: dict, parts_df: pd.DataFrame) -> None:
    """
    Mutates the LLM result dict in-place, adding fields that can be calculated
    from applicable_pct and the actual adjustment amount.
    """
    hdr_id = line.get("cieca_dtl_hdr_id")

    actual_adj = 0.0
    prts_amt = 0.0
    actual_discount_pct = None
    if hdr_id is not None:
        row = parts_df[parts_df["cieca_dtl_hdr_id"] == hdr_id]
        if not row.empty:
            actual_adj = float(row.iloc[0].get("cieca_line_adj_amt") or 0)
            prts_amt = float(row.iloc[0].get("dtl_tot_part_price_amt") or 0)
            actual_discount_pct = round(
                abs(float(row.iloc[0].get("cieca_discount_pct") or 0)), 0
            )

    try:
        expected_discount_pct = float(line.get("expected_discount_pct") or 0)
    except (TypeError, ValueError):
        expected_discount_pct = None

    discount_expected = expected_discount_pct > 0
    expected_adj = (
        round(prts_amt * (-expected_discount_pct / 100), ROUND_DECIMALS)
        if discount_expected
        else None
    )

    if expected_adj is not None:
        discount_match = abs(actual_adj - expected_adj) <= 1.0
        discount_variance = round(actual_adj - expected_adj, ROUND_DECIMALS)
    else:
        discount_match = None
        discount_variance = None

    if discount_variance is not None and discount_match is False:
        discount_direction = (
            "Over Discount" if discount_variance < 0 else "Under Discount"
        )  # discount_variance > 0
    else:
        discount_direction = None

    discount_pct_match = (
        round(expected_discount_pct, 0) == round(actual_discount_pct, 0)
        if expected_discount_pct and actual_discount_pct is not None
        else None
    )

    discount_pass = (
        bool(discount_match) and bool(discount_pct_match) if discount_expected else None
    )

    line["discount_expected"] = discount_expected
    line["actual_discount_amt"] = actual_adj
    line["expected_discount_amt"] = expected_adj
    line["discount_match"] = discount_match
    line["discount_variance"] = discount_variance
    line["discount_direction"] = discount_direction
    line["actual_discount_pct"] = actual_discount_pct
    line["discount_pct_match"] = discount_pct_match
    line["discount_pass"] = discount_pass


# ── LLM audit ─────────────────────────────────────────────────────────────────


@retry(
    retry=retry_if_exception_type(json.JSONDecodeError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def audit_estimate_with_llm(client, estimate_json: dict) -> list[dict]:
    """Send one estimate to the LLM, return parsed audit results."""
    response = client.chat.completions.create(
        model=LLM_DEPLOYMENT,
        max_tokens=LLM_MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(estimate_json, indent=2, cls=_NumpyEncoder),
            },
        ],
    )
    return json.loads(response.choices[0].message.content)


# ── Parts subtotal matching ───────────────────────────────────────────────────


def match_parts_subtotals(
    est_df: pd.DataFrame, est_subtot_df: pd.DataFrame
) -> list[dict]:
    """
    Validate parts lines for one estimate against the subtotal table.
    Groups line-level data by [est_id, cieca_part_typ_dsc], merges against
    subtotal rows (cieca_tot_typ_dsc containing "part"), then checks:
      - parts_gross_match: sum of line part amounts    == subtotal gross_amt
      - adj_match:         sum of line adjustments     == subtotal adj_tot_amt
      - parts_net_match:   sum of (part + adjustment)  == subtotal tot_amt
    Returns list of dicts (one per part type).
    """
    PARTS_MAP = {"Glass": "Parts - Glass"}
    _RATE_COL_MAP = {
        "foreign": "frn_part_disc_amt",
        "domestic": "dmstc_part_disc_amt",
        "keyless": "kyls_disc_amt",
    }

    parts_df = est_df[est_df["cieca_part_dtl_line_id"].notna()].copy()
    parts_df["cieca_part_typ_dsc"] = parts_df["cieca_part_typ_dsc"].replace(PARTS_MAP)

    if parts_df.empty:
        return []

    subtot_df = est_subtot_df[
        est_subtot_df["cieca_tot_typ_dsc"].str.contains("part", case=False, na=False)
    ].copy()
    subtot_df = subtot_df.rename(columns={"cieca_tot_typ_dsc": "cieca_part_typ_dsc"})

    subtot_df[["gross_amt", "adj_pct", "adj_tot_amt", "tot_amt"]] = subtot_df[
        ["gross_amt", "adj_pct", "adj_tot_amt", "tot_amt"]
    ].fillna(0)

    grouped = (
        parts_df.groupby(["est_id", "cieca_part_typ_dsc"])
        .agg(
            veh_make=("veh_make", "first"),
            frn_part_disc_amt=("frn_part_disc_amt", "first"),
            dmstc_part_disc_amt=("dmstc_part_disc_amt", "first"),
            kyls_disc_amt=("kyls_disc_amt", "first"),
            line_tot_part_amt=("dtl_tot_part_price_amt", "sum"),
            line_adj_amt=("cieca_line_adj_amt", "sum"),
        )
        .reset_index()
        .round(ROUND_DECIMALS)
    )
    grouped["line_net_amt"] = (
        grouped["line_tot_part_amt"] + grouped["line_adj_amt"]
    ).round(ROUND_DECIMALS)

    merged = grouped.merge(
        subtot_df[
            [
                "est_id",
                "cieca_part_typ_dsc",
                "gross_amt",
                "adj_pct",
                "adj_tot_amt",
                "tot_amt",
            ]
        ],
        on=["est_id", "cieca_part_typ_dsc"],
        how="left",
    )

    no_subtot = merged["gross_amt"].isna()

    def _expected_adj_pct(row) -> float | None:
        discount_type = get_discount_type(
            str(row.get("cieca_part_typ_dsc", "")),
            str(row.get("veh_make", "")),
        )
        rate_col = _RATE_COL_MAP.get(discount_type)
        return row.get(rate_col) if rate_col else None

    merged["expected_adj_pct"] = merged.apply(_expected_adj_pct, axis=1)

    merged = cast_numeric(
        merged,
        [
            "line_tot_part_amt",
            "line_adj_amt",
            "line_net_amt",
            "gross_amt",
            "adj_tot_amt",
            "tot_amt",
        ],
    ).fillna(0)

    gross_match = merged["line_tot_part_amt"].round(ROUND_DECIMALS) == merged[
        "gross_amt"
    ].round(ROUND_DECIMALS)
    adj_pct_match = merged["adj_pct"].round(ROUND_DECIMALS) == merged[
        "expected_adj_pct"
    ].round(ROUND_DECIMALS)
    adj_match = merged["line_adj_amt"].round(ROUND_DECIMALS) == merged[
        "adj_tot_amt"
    ].round(ROUND_DECIMALS)
    net_match = merged["line_net_amt"].round(ROUND_DECIMALS) == merged["tot_amt"].round(
        ROUND_DECIMALS
    )

    merged["parts_gross_match"] = np.where(
        no_subtot, "No subtotal found", np.where(gross_match, "Match", "No Match")
    )
    merged["adj_pct_match"] = np.where(
        no_subtot, "No subtotal found", np.where(adj_pct_match, "Match", "No Match")
    )
    merged["adj_match"] = np.where(
        no_subtot, "No subtotal found", np.where(adj_match, "Match", "No Match")
    )
    merged["parts_net_match"] = np.where(
        no_subtot, "No subtotal found", np.where(net_match, "Match", "No Match")
    )
    merged["overall_parts_subtot_match"] = np.where(
        no_subtot,
        "No subtotal found",
        np.where(
            (merged["parts_gross_match"] == "Match")
            & (merged["parts_net_match"] == "Match"),
            "Match",
            "No Match",
        ),
    )

    merged.drop(
        columns=[
            "veh_make",
            "frn_part_disc_amt",
            "dmstc_part_disc_amt",
            "kyls_disc_amt",
        ],
        inplace=True,
    )

    return merged.to_dict(orient="records")
