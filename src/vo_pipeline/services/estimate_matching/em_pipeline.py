"""
em_pipeline.py
──────────────
Estimate matching pipeline orchestrator.

Runs parts, labour, and materials validation for each estimate and
assembles the three output tables (line_detail, subtot_detail, est_summary).

Domain modules
--------------
  parts_audit     — LLM discount audit + parts subtotal matching
  labour_audit    — Body/Mechanical/Frame/Glass/Refinish labour matching
  materials_audit — Paint-and-materials rate matching
  llm             — LLM client factory
  helpers         — Shared utilities (cast_numeric)
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np
import pandas as pd

os.environ["PYDEVD_WARN_EVALUATION_TIMEOUT"] = "30"
os.environ["PYDEVD_UNBLOCK_THREADS_TIMEOUT"] = "30"

from estimate_matching.db_writer import save_results, LINE_DETAIL_COLS, SUBTOT_DETAIL_COLS  # noqa: E402
from estimate_matching.helpers import cast_numeric  # noqa: E402
from estimate_matching.llm import create_llm_client  # noqa: E402
from estimate_matching.parts_audit import (  # noqa: E402
    filter_parts_lines,
    build_estimate_json,
    audit_estimate_with_llm,
    _compute_parts_derived_cols,
    match_parts_subtotals,
)
from estimate_matching.labor_audit import (  # noqa: E402
    match_labor_subtotals,
    match_labour_refinish,
    add_line_labour_rate_match,
)
from estimate_matching.material_audit import match_paint_subtotals  # noqa: E402
from estimate_matching.config import (  # noqa: E402
    ROUND_DECIMALS,
    EST_LINE_NUMERIC_COLS,
    SUBTOT_NUMERIC_COLS,

)

# ── Config ────────────────────────────────────────────────────────────────────


logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)




# ── Single-estimate pipeline ──────────────────────────────────────────────────


def run_em_pipeline(
    est_id: str,
    est_rows: pd.DataFrame,
    subtot_rows: pd.DataFrame,
    client,
    save: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Validate a single estimate through all three audit domains.

    If save=True (default), persists results to Postgres immediately.
    Pass save=False to accumulate in the caller and write in bulk.

    Returns (est_summary, subtot_detail, line_detail).
    """
    est_rows = est_rows.copy()
    est_rows["cieca_discount_pct"] = (
        est_rows["cieca_line_adj_amt"]
        / est_rows["dtl_tot_part_price_amt"].replace(0, pd.NA)
    ) * 100
    try:
        est_rows = add_line_labour_rate_match(est_rows)
    except Exception as e:
        logger.error("est_id %s: LINE LABOUR RATE MATCH ERROR — %s", est_id, e)

    parts_results: list[dict] = []
    parts_subtot_results: list[dict] = []
    lbr_results: list[dict] = []
    paint_results: list[dict] = []

    # ── Parts (LLM) ───────────────────────────────────────────────────────────
    parts_lines = filter_parts_lines(est_rows)
    if parts_lines.empty:
        logger.info("est_id %s: no parts lines — skipping LLM.", est_id)
    else:
        try:
            estimate_json = build_estimate_json(parts_lines)
            audit_lines = audit_estimate_with_llm(client, estimate_json)
            for line in audit_lines:
                line["est_id"] = est_id
                _compute_parts_derived_cols(line, parts_lines)
            parts_results.extend(audit_lines)
        except json.JSONDecodeError as e:
            logger.error("est_id %s: JSON PARSE ERROR — %s", est_id, e)
        except Exception as e:
            logger.error("est_id %s: PARTS LLM ERROR — %s", est_id, e)

    # ── Parts subtotals (rule-based) ──────────────────────────────────────────
    try:
        result = match_parts_subtotals(est_rows, subtot_rows)
        if result:
            parts_subtot_results.extend(result)
    except Exception as e:
        logger.error("est_id %s: PARTS SUBTOTAL ERROR — %s", est_id, e)

    # ── Labour — Body / Mechanical / Frame / Glass ────────────────────────────
    try:
        result = match_labor_subtotals(est_rows, subtot_rows)
        if result:
            lbr_results.extend(result)
    except Exception as e:
        logger.error("est_id %s: LABOUR ERROR — %s", est_id, e)

    # ── Labour — Refinish ─────────────────────────────────────────────────────
    try:
        result = match_labour_refinish(est_rows, subtot_rows)
        if result:
            lbr_results.extend(result)
    except Exception as e:
        logger.error("est_id %s: REFINISH LABOUR ERROR — %s", est_id, e)

    # ── Materials / Paint ─────────────────────────────────────────────────────
    try:
        result = match_paint_subtotals(est_rows, subtot_rows)
        if result:
            paint_results.extend(result)
    except Exception as e:
        logger.error("est_id %s: MATERIALS ERROR — %s", est_id, e)

    # ── Assemble output ───────────────────────────────────────────────────────
    df_parts_audit = pd.DataFrame(parts_results)
    df_parts_subtot_audit = pd.DataFrame(parts_subtot_results)
    df_lbr_subtot_audit = pd.DataFrame(lbr_results)
    df_paint_subtot_audit = pd.DataFrame(paint_results)

    est_summary, subtot_detail, line_detail = _build_output_tables(
        est_rows,
        df_parts_audit,
        df_parts_subtot_audit,
        df_lbr_subtot_audit,
        df_paint_subtot_audit,
    )

    if save:
        save_results(est_summary, subtot_detail, line_detail)

    return est_summary, subtot_detail, line_detail


# ── Output table assembly ─────────────────────────────────────────────────────


def _build_output_tables(
    est_line_df: pd.DataFrame,
    df_parts_audit: pd.DataFrame,
    df_parts_subtot_audit: pd.DataFrame,
    df_lbr_subtot_audit: pd.DataFrame,
    df_paint_subtot_audit: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Assemble the three output DataFrames from raw audit results.

    Returns
    -------
    est_summary   : one row per est_id  (estimate-level pass/fail + issue counts)
    subtot_detail : one row per (est_id, type)  (parts + labour + materials subtotals)
    line_detail   : one row per cieca_dtl_hdr_id  (LLM parts audit at line level)
    """
    # ── line_detail ───────────────────────────────────────────────────────────
    line_detail = est_line_df.copy()
    line_detail["other_charges_match"] = np.where(
        line_detail["cieca_othr_chrg_dtl_line_id"].notna(), "Not validated", None
    )
    if not df_parts_audit.empty:
        line_detail = line_detail.merge(
            df_parts_audit, on="cieca_dtl_hdr_id", how="left", suffixes=("", "_audit")
        )
        line_detail.drop(columns=["est_id_audit"], errors="ignore", inplace=True)

    for col in ("discount_expected", "discount_match"):
        if col in line_detail.columns:
            line_detail[col] = line_detail[col].astype("boolean")

    # Keep only columns defined in the DDL — DDL is the single source of truth
    line_detail = line_detail.reindex(columns=[c for c in LINE_DETAIL_COLS if c in line_detail.columns])

    # ── subtot_detail ─────────────────────────────────────────────────────────
    df_parts_sub = df_parts_subtot_audit.copy() if not df_parts_subtot_audit.empty else pd.DataFrame()
    if not df_parts_sub.empty:
        df_parts_sub.rename(columns={"cieca_part_typ_dsc": "cieca_tot_typ_dsc"}, inplace=True)
        df_parts_sub["subtot_type"] = "parts"

    df_lbr_sub = df_lbr_subtot_audit.copy() if not df_lbr_subtot_audit.empty else pd.DataFrame()
    if not df_lbr_sub.empty:
        df_lbr_sub.rename(columns={"cieca_lbr_typ_dsc": "cieca_tot_typ_dsc"}, inplace=True)
        df_lbr_sub["subtot_type"] = "labor"

    df_paint_sub = df_paint_subtot_audit.copy() if df_paint_subtot_audit is not None and not df_paint_subtot_audit.empty else pd.DataFrame()
    if not df_paint_sub.empty:
        df_paint_sub["subtot_type"] = "materials"

    _subtot_frames = [
        df for df in [df_parts_sub, df_lbr_sub, df_paint_sub]
        if not df.empty and not df.isna().all(axis=None)
    ]
    subtot_detail = (
        pd.concat(_subtot_frames, ignore_index=True, sort=False)
        if _subtot_frames
        else pd.DataFrame()
    )
    if not subtot_detail.empty and "claim_number" in est_line_df.columns:
        claim_map = est_line_df[["est_id", "claim_number"]].drop_duplicates("est_id")
        subtot_detail = subtot_detail.merge(claim_map, on="est_id", how="left")

    # Keep only columns defined in the DDL — DDL is the single source of truth
    subtot_detail = subtot_detail.reindex(columns=[c for c in SUBTOT_DETAIL_COLS if c in subtot_detail.columns])

    # ── est_summary ───────────────────────────────────────────────────────────
    est_meta_cols = [
        "est_id",
        "claim_number",
        "est_tot_amt",
        "lbr_hr_qty",
        "grp_nbr",
        "veh_make",
    ]
    est_summary = (
        est_line_df[[c for c in est_meta_cols if c in est_line_df.columns]]
        .drop_duplicates("est_id")
        .copy()
    )

    lbr_pass = (
        df_lbr_subtot_audit.groupby("est_id")["overall_lbr_match"]
        .apply(lambda x: (x == "Match").all())
        .rename("lbr_est_pass")
        .reset_index()
        if not df_lbr_subtot_audit.empty
        else pd.DataFrame(columns=["est_id", "lbr_est_pass"])
    )

    parts_pass = (
        df_parts_subtot_audit.groupby("est_id")["overall_parts_subtot_match"]
        .apply(lambda x: (x == "Match").all())
        .rename("parts_est_pass")
        .reset_index()
        if not df_parts_subtot_audit.empty
        else pd.DataFrame(columns=["est_id", "parts_est_pass"])
    )

    paint_pass = (
        df_paint_subtot_audit.groupby("est_id")["paint_rate_match"]
        .apply(lambda x: not (x == "No Match").any())  # "Cannot Validate" treated as neutral
        .rename("paint_est_pass")
        .reset_index()
        if df_paint_subtot_audit is not None and not df_paint_subtot_audit.empty
        else pd.DataFrame(columns=["est_id", "paint_est_pass"])
    )

    lbr_card_totals = (
        df_lbr_subtot_audit.groupby("est_id")
        .agg(
            total_actual_lbr_amt=("actual_lbr_amt", "sum"),
            total_expected_lbr_amt=("expected_lbr_amt", "sum"),
        )
        .reset_index()
        if not df_lbr_subtot_audit.empty
        and "actual_lbr_amt" in df_lbr_subtot_audit.columns
        else pd.DataFrame(
            columns=["est_id", "total_actual_lbr_amt", "total_expected_lbr_amt"]
        )
    )

    for df in [lbr_pass, parts_pass, paint_pass, lbr_card_totals]:
        est_summary["est_id"] = est_summary["est_id"].astype(str)
        df["est_id"] = df["est_id"].astype(str)
        est_summary = est_summary.merge(df, on="est_id", how="left")

    lbr_ok = est_summary["lbr_est_pass"].isna() | est_summary["lbr_est_pass"].eq(True)
    parts_ok = est_summary["parts_est_pass"].isna() | est_summary["parts_est_pass"].eq(
        True
    )
    paint_ok = est_summary["paint_est_pass"].isna() | est_summary["paint_est_pass"].eq(
        True
    )
    est_summary["estimate_match"] = np.where(
        lbr_ok & parts_ok & paint_ok, "Match", "No Match"
    )

    for col in ["total_actual_lbr_amt", "total_expected_lbr_amt"]:
        if col in est_summary.columns:
            est_summary[col] = pd.to_numeric(est_summary[col], errors="coerce").round(
                ROUND_DECIMALS
            )

    return est_summary, subtot_detail, line_detail


