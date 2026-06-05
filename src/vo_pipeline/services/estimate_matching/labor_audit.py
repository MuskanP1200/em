"""
labour_audit.py
───────────────
Labour validation for one estimate (rule-based):
  - match_labor_subtotals  : Body / Mechanical / Frame / Glass labour
  - match_labour_refinish  : Refinish labour (sourced from paint_hrs lines)

Both functions collect their raw inputs (actual_hrs, expected_hrs, rates,
amounts) and delegate all match/direction logic to _validate_labour_type,
which returns the same explicit dict shape in both cases.
"""

from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd

from estimate_matching.config import LABOR_TYPE_RATE_MAP, ROUND_DECIMALS, ROUND_HRS

# CDR rate profile columns included in every subtot_detail labour row
_RATE_COLS = [
    "bdy_lbr_rate", "mchncl_lbr_rate", "frm_lbr_rate", "pnt_mtrl_rate",
    "dmstc_part_disc_amt", "frn_part_disc_amt", "kyls_disc_amt",
    "specl_instruct_txt", "grp_note_txt",
]

logger = logging.getLogger(__name__)


# ── Unit cost lookup ──────────────────────────────────────────────────────────


def get_unit_cost_by_labor_type(df: pd.DataFrame) -> pd.Series:
    """Map each labor type to its expected rate column."""
    result = pd.Series(np.nan, index=df.index, dtype=float)
    for labor_type, rate_col in LABOR_TYPE_RATE_MAP.items():
        mask = df["cieca_lbr_typ_dsc"] == labor_type
        result[mask] = df.loc[mask, rate_col]
    return result


# ── Glass rate extractor ──────────────────────────────────────────────────────

_GLASS_RATE_RE = re.compile(
    r"(?:"
    r"glass\s+lab(?:or|our)[^$\n]{0,40}\$\s*(\d+(?:\.\d+)?)"  # group 1 — "Glass Labor $39/hour"
    r"|"
    r"\$\s*(\d+(?:\.\d+)?)[^$\n]{0,40}glass\s+lab(?:or|our)"  # group 2 — "$39 for Glass Labor"
    r"|"
    r"glass[^$\n]{0,10}\$\s*(\d+(?:\.\d+)?)"  # group 3 — "Glass $39"
    r")",
    re.IGNORECASE,
)


def extract_glass_labor_rate(text: str) -> float | None:
    """Extract a glass labor rate from free-text special instructions."""
    if not text:
        return None
    m = _GLASS_RATE_RE.search(text)
    if not m:
        return None
    return float(m.group(1) or m.group(2) or m.group(3))


# ── Shared validation core ────────────────────────────────────────────────────


def _validate_labour_type(
    est_id: str,
    label: str,
    actual_hrs: float,
    expected_hrs: float,
    expected_rate: float | None,
    actual_rate: float | None,
    actual_amt: float,
) -> dict:
    """
    Compute all match / direction / amount fields for one labour type.

    Parameters
    ----------
    est_id        : estimate identifier
    label         : cieca_lbr_typ_dsc value (e.g. "Labor - Body")
    actual_hrs    : hours from line aggregation (dtl_lbr_hr_qty or paint_hrs)
    expected_hrs  : hours from the subtotal table (tot_hr)
    expected_rate : CDR contracted rate (None if unknown)
    actual_rate   : tot_amt / tot_hr (None if tot_hr == 0)
    actual_amt    : actual amount charged (tot_amt)
    """
    # ── Hours ─────────────────────────────────────────────────────────────────
    no_hr_data = (not actual_hrs) and (not expected_hrs)
    hrs_match = round(actual_hrs, ROUND_HRS) == round(expected_hrs, ROUND_HRS)

    lbr_typ_hrs_match = "Match" if (no_hr_data or hrs_match) else "No Match"
    lbr_hr_direction = (
        ("Over" if actual_hrs > expected_hrs else "Under")
        if lbr_typ_hrs_match == "No Match"
        else None
    )

    # ── Rate ──────────────────────────────────────────────────────────────────
    if expected_rate is not None and actual_rate is not None and not no_hr_data:
        rate_match = round(expected_rate, 0) == round(actual_rate, 0)
        lbr_typ_rate_match = "Match" if rate_match else "No Match"
    else:
        lbr_typ_rate_match = (
            "Match"  # cannot evaluate — treat as pass #something is wrong here
        )

    lbr_direction = (
        ("Overcharged" if actual_rate > expected_rate else "Undercharged")
        if (
            lbr_typ_rate_match == "No Match"
            and expected_rate is not None
            and actual_rate is not None
        )
        else None
    )

    # ── Amounts ───────────────────────────────────────────────────────────────
    expected_lbr_amt = (
        round(expected_rate * expected_hrs, 1) if expected_rate is not None else None
    )
    lbr_amt_match = (
        abs(actual_amt - expected_lbr_amt) <= 1.0
        if expected_lbr_amt is not None
        else None
    )
    # ── Overall ───────────────────────────────────────────────────────────────
    overall_lbr_match = (
        "Match"
        if lbr_typ_hrs_match == "Match" and lbr_typ_rate_match == "Match"
        else "No Match"
    )

    # ── Reason ───────────────────────────────────────────────────────────────
    if overall_lbr_match == "No Match" or lbr_amt_match is False:
        reasons = []
        if lbr_typ_hrs_match == "No Match":
            reasons.append(
                f"Hour mismatch (expected: {expected_hrs}, actual: {actual_hrs})"
            )
        if lbr_typ_rate_match == "No Match":
            reasons.append(
                f"Rate mismatch (expected: {expected_rate}, actual: {actual_rate})"
            )
        if lbr_amt_match is False:
            reasons.append(
                f"Amount mismatch (expected: ${expected_lbr_amt:.2f}, actual: ${actual_amt:.2f})"
            )
        lbr_mismatch_reason = f"{label}: {', '.join(reasons)}" if reasons else None
    else:
        lbr_mismatch_reason = None

    # ── Amounts ───────────────────────────────────────────────────────────────
    expected_lbr_amt = (
        round(expected_rate * expected_hrs, ROUND_DECIMALS)
        if expected_rate is not None
        else None
    )

    return {
        "est_id": est_id,
        "cieca_lbr_typ_dsc": label,
        "actual_hrs": actual_hrs,
        "expected_hrs": expected_hrs,
        "lbr_typ_hrs_match": lbr_typ_hrs_match,
        "lbr_hr_direction": lbr_hr_direction,
        "actual_lbr_rate": actual_rate,
        "expected_lbr_rate": expected_rate,
        "lbr_typ_rate_match": lbr_typ_rate_match,
        "lbr_direction": lbr_direction,
        "actual_lbr_amt": actual_amt,
        "expected_lbr_amt": expected_lbr_amt,
        "lbr_amt_match": lbr_amt_match,
        "overall_lbr_match": overall_lbr_match,
        "lbr_mismatch_reason": lbr_mismatch_reason,
    }


# ── Body / Mechanical / Frame / Glass labour matching ────────────────────────


def match_labor_subtotals(
    est_df: pd.DataFrame,
    est_subtot_df: pd.DataFrame,
) -> list[dict]:
    """
    Validate labour lines for one estimate against the subtotal table.
    Returns list of dicts (one per labour type).
    """

    labor_df = est_df[
        est_df["cieca_lbr_typ_dsc"].str.contains("labor", case=False, na=False)
    ].copy()
    if labor_df.empty:
        return []

    subtot_df = est_subtot_df[
        est_subtot_df["cieca_tot_typ_dsc"].str.contains("labor", case=False, na=False)
    ].copy()
    subtot_df = subtot_df.rename(
        columns={
            "cieca_tot_typ_dsc": "cieca_lbr_typ_dsc",
            "lbr_rate": "actual_lbr_rate",
        }
    )

    glass_rate = extract_glass_labor_rate(
        labor_df["specl_instruct_txt"].dropna().iloc[0]
        if "specl_instruct_txt" in labor_df.columns
        and not labor_df["specl_instruct_txt"].dropna().empty
        else None
    )

    # Aggregate line-level data
    grouped = (
        labor_df.groupby(["est_id", "cieca_lbr_typ_dsc"])
        .agg(
            lbr_hr_qty=("lbr_hr_qty", "first"),
            dtl_lbr_hr_qty=("dtl_lbr_hr_qty", "sum"),
            dtl_lbr_tot_amt=("dtl_lbr_tot_amt", "sum"),
            bdy_lbr_rate=("bdy_lbr_rate", "first"),
            mchncl_lbr_rate=("mchncl_lbr_rate", "first"),
            frm_lbr_rate=("frm_lbr_rate", "first"),
            pnt_mtrl_rate=("pnt_mtrl_rate", "first"),
            specl_instruct_txt=("specl_instruct_txt", "first"),
            grp_note_txt=("grp_note_txt", "first"),
        )
        .reset_index()
    )
    # Round hours and dollar amounts separately — different precision needed
    grouped["dtl_lbr_hr_qty"] = grouped["dtl_lbr_hr_qty"].round(ROUND_HRS)
    grouped["dtl_lbr_tot_amt"] = grouped["dtl_lbr_tot_amt"].round(ROUND_DECIMALS)

    grouped = grouped.merge(
        subtot_df[
            [
                "est_id",
                "claim_number",
                "cieca_lbr_typ_dsc",
                "tot_amt",
                "tot_hr",
                "actual_lbr_rate",
            ]
        ],
        on=["est_id", "cieca_lbr_typ_dsc"],
        how="left",
    )
    grouped["tot_hr"] = grouped["tot_hr"].fillna(0)

    # Compute expected and actual rates on the full DataFrame (vectorized)
    grouped["expected_lbr_rate"] = get_unit_cost_by_labor_type(grouped)
    if glass_rate is not None:
        glass_mask = (
            grouped["cieca_lbr_typ_dsc"].str.contains("glass", case=False, na=False)
            & grouped["expected_lbr_rate"].isna()
        )
        grouped.loc[glass_mask, "expected_lbr_rate"] = glass_rate

    results = []
    for _, row in grouped.iterrows():
        result = _validate_labour_type(
            est_id=row["est_id"],
            label=row["cieca_lbr_typ_dsc"],
            actual_hrs=float(row.get("tot_hr") or 0),
            expected_hrs=float(row.get("dtl_lbr_hr_qty") or 0),
            expected_rate=(
                row.get("expected_lbr_rate")
                if pd.notna(row.get("expected_lbr_rate"))
                else None
            ),
            actual_rate=(
                row.get("actual_lbr_rate")
                if pd.notna(row.get("actual_lbr_rate"))
                else None
            ),
            actual_amt=float(row.get("tot_amt") or 0),
        )
        # Include CDR rate profile columns for subtot_detail
        for col in _RATE_COLS:
            result[col] = row.get(col)
        results.append(result)

    return results


# ── Refinish labour matching ──────────────────────────────────────────────────


def match_labour_refinish(
    est_df: pd.DataFrame, est_subtot_df: pd.DataFrame
) -> list[dict]:
    """
    Validate Labour-Refinish hours and rate for one estimate.

    In API-sourced data, refinish labour is NOT carried as a cieca_lbr_typ_dsc
    row. Instead each paint/refinish operation line carries:
        paint_hrs       — refinish hours for that line
        paint_type_code — 'R' identifies refinish

    Checks
    ------
    lbr_typ_hrs_match       : sum(paint_hrs where code='R') == subtotal.tot_hr
    lbr_typ_rate_match : bdy_lbr_rate (contracted)     == tot_amt / tot_hr
    """
    _REFINISH_LABEL = "Labor - Refinish"
    refinish_df = (
        est_df[est_df["paint_type_code"].str.strip().str.upper() == "R"].copy()
        if "paint_type_code" in est_df.columns
        else pd.DataFrame()
    )
    if refinish_df.empty:
        return []

    ref_subtot = est_subtot_df[
        est_subtot_df["cieca_tot_typ_dsc"].str.strip() == _REFINISH_LABEL
    ].copy()

    subtot_row = ref_subtot.iloc[0]

    est_id = refinish_df["est_id"].iloc[0]
    actual_lbr_amt = subtot_row.get("tot_amt") or 0

    actual_hrs = subtot_row.get("tot_hr") or 0
    expected_hrs = round(
        (
            float(pd.to_numeric(refinish_df["paint_hrs"], errors="coerce").sum())
            if not refinish_df.empty
            else 0.0
        ),
        ROUND_HRS,
    )

    expected_rate = (
        refinish_df["bdy_lbr_rate"].iloc[0]
        if pd.notna(refinish_df["bdy_lbr_rate"].iloc[0])
        else None
    )
    actual_rate = (
        round(actual_lbr_amt / actual_hrs, ROUND_DECIMALS) if actual_hrs != 0 else None
    )

    result = _validate_labour_type(
        est_id=est_id,
        label=_REFINISH_LABEL,
        actual_hrs=actual_hrs,
        expected_hrs=expected_hrs,
        expected_rate=expected_rate,
        actual_rate=actual_rate,
        actual_amt=actual_lbr_amt,
    )

    for col in _RATE_COLS:
        result[col] = refinish_df[col].iloc[0] if col in est_df.columns else None

    logger.debug(
        "est_id %s: refinish hrs line=%.1f subtot=%.1f | rate expected=%s actual=%s | hrs=%s rate=%s",
        est_id,
        expected_hrs,
        actual_hrs,
        expected_rate,
        actual_rate,
        result["lbr_typ_hrs_match"],
        result["lbr_typ_rate_match"],
    )

    return [result]


# ── Line-level labour rate match ──────────────────────────────────────────────


def add_line_labour_rate_match(est_line_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add line-level labour rate columns to est_line_df.

    For each line where both dtl_lbr_tot_amt and dtl_lbr_hr_qty are present
    and non-zero, compute:
      actual_line_lbr_rate  : dtl_lbr_tot_amt / dtl_lbr_hr_qty
      expected_line_lbr_rate: CDR contracted rate for this labour type
      line_lbr_rate_match   : True / False / None if cannot evaluate
    """
    df = est_line_df.copy()

    # Actual rate = amount / hours where both are non-zero
    has_data = df["dtl_lbr_hr_qty"].notna() & df["dtl_lbr_hr_qty"].gt(0) & \
               df["dtl_lbr_tot_amt"].notna()
    df["actual_line_lbr_rate"] = None
    df.loc[has_data, "actual_line_lbr_rate"] = (
        df.loc[has_data, "dtl_lbr_tot_amt"] / df.loc[has_data, "dtl_lbr_hr_qty"]
    ).round(ROUND_DECIMALS)

    # Expected rate = CDR rate mapped by labour type
    df["expected_line_lbr_rate"] = get_unit_cost_by_labor_type(df)

    # Match — only where both rates are available
    can_compare = df["actual_line_lbr_rate"].notna() & df["expected_line_lbr_rate"].notna()
    df["line_lbr_rate_match"] = None
    df.loc[can_compare, "line_lbr_rate_match"] = (
        df.loc[can_compare, "actual_line_lbr_rate"].round(ROUND_DECIMALS)
        == df.loc[can_compare, "expected_line_lbr_rate"].round(ROUND_DECIMALS)
    )

    return df
