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
    PARTS_MAP = {
        "Glass":             "Parts - Glass",
        "Parts - Aftermarket": "Parts - Aftermarket (QRP)",
    }
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

    # Store values rounded to 2 decimal places — matching rounds further inline below.
    # expected_* = bottom-up values summed from line items (the audit's source of truth).
    grouped = (
        parts_df.groupby(["est_id", "cieca_part_typ_dsc"])
        .agg(
            veh_make=("veh_make", "first"),
            frn_part_disc_amt=("frn_part_disc_amt", "first"),
            dmstc_part_disc_amt=("dmstc_part_disc_amt", "first"),
            kyls_disc_amt=("kyls_disc_amt", "first"),
            expected_gross_amt=("dtl_tot_part_price_amt", "sum"),
            expected_adj_amt=("cieca_line_adj_amt", "sum"),
        )
        .reset_index()
        .round(2)
    )
    grouped["expected_net_amt"] = (grouped["expected_gross_amt"] + grouped["expected_adj_amt"]).round(2)

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
        how="outer",
    )

    # Fill est_id for subtotal-only rows (outer join leaves it NaN from grouped side)
    merged["est_id"] = merged["est_id"].fillna(parts_df["est_id"].iloc[0])

    no_subtot = merged["gross_amt"].isna()    # lines exist, no subtotal row
    no_lines  = merged["expected_gross_amt"].isna()  # subtotal exists, no line items

    def _expected_adj_pct(row) -> float | None:
        discount_type = get_discount_type(
            str(row.get("cieca_part_typ_dsc", "")),
            str(row.get("veh_make", "")),
        )
        rate_col = _RATE_COL_MAP.get(discount_type)
        val = row.get(rate_col)
        # Negate to match adj_pct sign convention — CDR rates are positive (13)
        # but adj_pct in the subtotal is negative (-13, a deduction)
        # Treat 0 as "no info" — a CDR rate of 0 means no discount is configured,
        # semantically identical to a missing rate. Only a positive value is meaningful.
        return -val if val else None

    merged["expected_adj_pct"] = merged.apply(_expected_adj_pct, axis=1)

    # Calculated expected adjustment $ = real parts total (bottom-up) × expected discount %.
    # Distinct from expected_adj_amt (the line-summed actual adjustments).
    merged["expected_adj_amt_calc"] = (
        merged["expected_gross_amt"]
        * pd.to_numeric(merged["expected_adj_pct"], errors="coerce")
        / 100
    ).round(2)

    merged = cast_numeric(
        merged,
        [
            "expected_gross_amt",
            "expected_adj_amt",
            "expected_net_amt",
            "gross_amt",
            "adj_tot_amt",
            "tot_amt",
        ],
    ).fillna(0)

    # Calculated expected net = gross + calculated discount (falls back to no discount
    # when there's no CDR rate). Parallels expected_adj_amt_calc for card-total display.
    merged["expected_net_amt_calc"] = (
        merged["expected_gross_amt"] + merged["expected_adj_amt_calc"].fillna(0)
    ).round(2)

    gross_match = merged["expected_gross_amt"].round(ROUND_DECIMALS) == merged[
        "gross_amt"
    ].round(ROUND_DECIMALS)
    adj_pct_match = merged["adj_pct"].round(ROUND_DECIMALS) == merged[
        "expected_adj_pct"
    ].round(ROUND_DECIMALS)
    adj_match = merged["expected_adj_amt"].round(ROUND_DECIMALS) == merged[
        "adj_tot_amt"
    ].round(ROUND_DECIMALS)
    # Contract compliance: subtotal adjustment (what insurer pays) vs calculated-from-CDR.
    adj_compliance = merged["adj_tot_amt"].round(ROUND_DECIMALS) == merged[
        "expected_adj_amt_calc"
    ].round(ROUND_DECIMALS)
    net_match = merged["expected_net_amt"].round(ROUND_DECIMALS) == merged["tot_amt"].round(
        ROUND_DECIMALS
    )

    merged["parts_gross_match"] = np.where(
        no_subtot | no_lines, "No Match", np.where(gross_match, "Match", "No Match")
    )
    merged["adj_pct_match"] = np.where(
        no_subtot | no_lines, "No Match", np.where(adj_pct_match, "Match", "No Match")
    )
    merged["adj_match"] = np.where(
        no_subtot | no_lines, "No Match", np.where(adj_match, "Match", "No Match")
    )
    # Compliance is only meaningful when a CDR discount % exists to calculate against.
    no_expected = merged["expected_adj_pct"].isna()
    # No CDR rate = CDR profile is missing → cannot confirm discount is correct → flag as No Match.
    merged["adj_compliance_match"] = np.where(
        no_subtot | no_lines, "No Match",
        np.where(no_expected, "No Match", np.where(adj_compliance, "Match", "No Match")),
    )
    merged["parts_net_match"] = np.where(
        no_subtot | no_lines, "No Match", np.where(net_match, "Match", "No Match")
    )
    # Overall = no individual check is a hard "No Match".
    # "Cannot Validate" (no CDR rate to check against) is treated as neutral, not a fail.
    _overall_checks = [
        "parts_gross_match",
        "parts_net_match",
        "adj_pct_match",
        "adj_compliance_match",
    ]
    _no_fail = np.logical_and.reduce(
        [merged[c].ne("No Match") for c in _overall_checks]
    )
    merged["overall_parts_subtot_match"] = np.where(
        no_subtot | no_lines,
        "No Match",
        np.where(_no_fail, "Match", "No Match"),
    )

    def _parts_subtot_mismatch_reason(row) -> str | None:
        if row["overall_parts_subtot_match"] != "No Match":
            return None
        label = row["cieca_part_typ_dsc"]

        # No subtotal row found — lines exist but no corresponding subtotal.
        if pd.isna(row.get("gross_amt")):
            return f"{label}: Subtotal missing — no parts subtotal row found"

        # No line items found — subtotal charged but no corresponding line items.
        if pd.isna(row.get("expected_gross_amt")):
            return f"{label}: Subtotal charged (${row['gross_amt']:.2f}) but no line items found"

        # When there is no CDR discount rate, that alone is the finding — skip
        # individual field comparisons which would produce confusing "nan%" messages.
        if pd.isna(row.get("expected_adj_pct")):
            return f"{label}: CDR profile is missing — discount rate not configured"

        reasons = []
        if row["parts_gross_match"] == "No Match":
            reasons.append(
                f"Gross mismatch — line sum ${row['expected_gross_amt']:.2f} "
                f"vs subtotal ${row['gross_amt']:.2f}"
            )
        if row["adj_match"] == "No Match":
            reasons.append(
                f"Adjustment inconsistent — line sum ${row['expected_adj_amt']:.2f} "
                f"vs subtotal ${row['adj_tot_amt']:.2f}"
            )
        if row["adj_pct_match"] == "No Match":
            reasons.append(
                f"Discount % off — CDR expects {row['expected_adj_pct']}% "
                f"but {row['adj_pct']}% was applied"
            )
        if row["adj_compliance_match"] == "No Match":
            reasons.append(
                f"Discount $ off contract — should be ${row['expected_adj_amt_calc']:.2f} "
                f"(gross x CDR %), but ${row['adj_tot_amt']:.2f} was applied"
            )
        if row["parts_net_match"] == "No Match":
            reasons.append(
                f"Net mismatch — line sum ${row['expected_net_amt']:.2f} "
                f"vs subtotal ${row['tot_amt']:.2f}"
            )
        return f"{label}: " + "; ".join(reasons) if reasons else None

    merged["parts_subtot_mismatch_reason"] = merged.apply(_parts_subtot_mismatch_reason, axis=1)

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
