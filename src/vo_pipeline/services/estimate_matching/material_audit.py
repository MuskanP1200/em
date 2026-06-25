"""
materials_audit.py
──────────────────
Materials / paint validation for one estimate (rule-based).

Validates that the Materials-Paint subtotal is consistent with the
contracted paint-and-material rate:

    actual_paint_rate   = Materials-Paint tot_amt / Labour-Refinish tot_hr
    expected_paint_rate = pnt_mtrl_rate (CDR contracted rate)

Aggregates all subtotal rows whose description contains both "paint" and
"material" (covers "Materials - Paint", "Materials - 2 Stage Paint
Materials", "Materials - 3 Stage Paint Materials", etc.).
"""

from __future__ import annotations

import logging

import pandas as pd

from estimate_matching.config import ROUND_DECIMALS, ROUND_HRS

logger = logging.getLogger(__name__)


def match_paint_subtotals(
    est_df: pd.DataFrame, est_subtot_df: pd.DataFrame
) -> list[dict]:
    """
    Validate the Materials-Paint subtotal for one estimate.
    Returns a list with one dict, or empty list if no paint subtotal exists.
    """
    REFINISH_LABEL = "Labor - Refinish"

    # ── Paint subtotal ────────────────────────────────────────────────────────
    dsc_col = est_subtot_df["cieca_tot_typ_dsc"].str.strip().str.lower()
    paint_mask = dsc_col.str.contains("paint", na=False) & dsc_col.str.contains(
        "material", na=False
    )
    paint_subtot = est_subtot_df[paint_mask].copy()

    if paint_subtot.empty:
        return []

    actual_paint_amt = round(float(paint_subtot["tot_amt"].sum() or 0), 2)

    # ── Refinish hours ────────────────────────────────────────────────────────
    ref_subtot = est_subtot_df[
        est_subtot_df["cieca_tot_typ_dsc"].str.strip() == REFINISH_LABEL
    ].copy()

    if not ref_subtot.empty:
        paint_hrs = float(ref_subtot.iloc[0].get("tot_hr") or 0)
        paint_note = "Paint hrs picked from Labour-Refinish subtot section"
    else:
        ref_lines = (
            est_df[est_df["paint_type_code"].str.strip().str.upper() == "R"]
            if "paint_type_code" in est_df.columns
            else pd.DataFrame()
        )
        paint_hrs = (
            float(pd.to_numeric(ref_lines["paint_hrs"], errors="coerce").sum())
            if not ref_lines.empty
            else 0.0
        )
        paint_note = "Paint hrs calculated from paint_hours in detailed line section"

    paint_hrs = round(paint_hrs, ROUND_HRS)
    est_id = est_df["est_id"].iloc[0]

    # ── Expected rate (CDR) and Actual rate match───────────────────────────────────────────────────

    _raw_paint_rate = est_df["pnt_mtrl_rate"].iloc[0]
    # Treat 0 as "no info" — a CDR paint rate of 0 means no rate is configured,
    # semantically identical to a missing rate.
    expected_paint_rate = (
        float(_raw_paint_rate)
        if pd.notna(_raw_paint_rate) and float(_raw_paint_rate) != 0
        else None
    )
    actual_paint_rate = (
        round(actual_paint_amt / paint_hrs, ROUND_DECIMALS) if paint_hrs != 0 else None
    )

    if expected_paint_rate is not None and actual_paint_rate is not None:
        paint_rate_match = (
            "Match"
            if abs(actual_paint_rate - expected_paint_rate) < 1.0
            else "No Match"
        )
    elif expected_paint_rate is None and actual_paint_rate is not None:
        # CDR has no paint rate configured — flag it and override the paint_note
        paint_rate_match = "No Match"
        paint_note = "CDR profile missing — paint material rate not configured"
    elif actual_paint_amt > 0 and actual_paint_rate is None:
        # paint_hrs = 0 but shop charged for paint materials — flag it
        paint_rate_match = "No Match"
        paint_note = "Paint amount charged but no refinish hours found"
    else:
        # paint_hrs = 0 and actual_paint_amt = 0 — nothing charged, nothing expected
        paint_rate_match = "Match"

    # ── Direction ─────────────────────────────────────────────────────────────
    if (
        paint_rate_match == "No Match"
        and expected_paint_rate is not None
        and actual_paint_rate is not None
    ):
        paint_rate_direction = (
            "Over" if actual_paint_rate > expected_paint_rate else "Under"
        )
    else:
        paint_rate_direction = None
    # ── Amount ────────────────────────────────────────────────────────────────
    expected_paint_amt = (
        round(expected_paint_rate * paint_hrs, ROUND_DECIMALS)
        if expected_paint_rate is not None and paint_hrs
        else None
    )
    paint_amt_match = (
        abs(actual_paint_amt - expected_paint_amt) < 1.0
        if expected_paint_amt is not None
        else None
    )

    logger.debug(
        "est_id %s: paint tot=%.2f | paint_hrs=%.1f | expected=%s actual=%s | %s",
        est_id,
        actual_paint_amt,
        paint_hrs,
        expected_paint_rate,
        actual_paint_rate,
        paint_rate_match,
    )

    # Return one row per original paint subtotal type so each staging row
    # joins correctly in em_subtot_agg. The aggregate rate/match result is
    # the same for all rows — all pass or all fail together.
    # expected_paint_amt is prorated by each row's share so card totals sum correctly.
    results = []
    for _, row in paint_subtot.iterrows():
        row_actual = round(float(row["tot_amt"] or 0), 2)
        row_expected = (
            round(expected_paint_amt * (row_actual / actual_paint_amt), 2)
            if expected_paint_amt is not None and actual_paint_amt > 0
            else expected_paint_amt
        )
        results.append(
            {
                "est_id": est_id,
                "cieca_tot_typ_dsc": row["cieca_tot_typ_dsc"].strip(),
                "actual_paint_amt": row_actual,
                "expected_paint_amt": row_expected,
                "paint_amt_match": paint_amt_match,
                "paint_hrs": paint_hrs,
                "expected_paint_rate": expected_paint_rate,
                "actual_paint_rate": actual_paint_rate,
                "paint_rate_match": paint_rate_match,
                "paint_rate_direction": paint_rate_direction,
                "paint_note": paint_note,
            }
        )
    return results
