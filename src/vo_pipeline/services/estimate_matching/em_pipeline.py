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
import time

import numpy as np
import pandas as pd

os.environ["PYDEVD_WARN_EVALUATION_TIMEOUT"] = "30"
os.environ["PYDEVD_UNBLOCK_THREADS_TIMEOUT"] = "30"

from sql_connection import read_table  # noqa: E402
from estimate_matching.db_writer import save_results  # noqa: E402
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
)
from estimate_matching.material_audit import match_paint_subtotals  # noqa: E402
from estimate_matching.config import (  # noqa: E402
    BASE_COLS,
    PARTS_INPUT_COLS,
    LBR_INPUT_COLS,
    OTHER_CHRG_COLS,
    RATE_COLS,
    PARTS_AUDIT_COLS,
    LBR_AUDIT_COLS,
    OTHER_CHRG_AUDIT_COLS,
    PARTS_SUBTOT_AUDIT_COLS,
    PAINT_AUDIT_COLS,
    ROUND_DECIMALS,
    EST_LINE_NUMERIC_COLS,
    SUBTOT_NUMERIC_COLS,
    TABLE_EST_LINE,
    TABLE_SUBTOT,
    DATA_SOURCE_MODE,
    API_CONFIG,
)

# ── Config ────────────────────────────────────────────────────────────────────

LOG_EVERY_N = 500

logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── Data loading ──────────────────────────────────────────────────────────────


def load_estimate_data(
    data_source_mode: str = DATA_SOURCE_MODE,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load est_line_df and subtot_df from the source configured in config.yaml.

    data_source_mode:
        prefiltered — read from pre-built DB snapshot (fast, for testing)
        live        — run load_estimates() against the live DB
        api         — fetch from VR Services SOAP APIs
    """
    if data_source_mode == "api":
        from api_ingest.estimate_loader import (
            search_and_save_new_estimates,
            fetch_estimate_details,
        )
        from api_ingest.api_auth import get_token
        from settings import get_settings

        logger.info("data_source_mode=api: loading from VR Services APIs")
        creds = get_settings().model_dump()
        token = get_token(
            username=creds["ICE_API_USER_NAME"],
            password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
            auth_url="http://appsecwse-iprod/appsec/enhanced/webservice/rsi",
        )
        est_ids = search_and_save_new_estimates(
            token,
            status_code=API_CONFIG.get("status_code", "WAITONAUTH"),
            group=API_CONFIG.get("group", "DR"),
            max_records=API_CONFIG.get("max_records"),
        )
        est_line_df, subtot_df = fetch_estimate_details(
            token, est_ids, max_workers=API_CONFIG.get("max_workers", 4)
        )
        return (
            cast_numeric(est_line_df, EST_LINE_NUMERIC_COLS),
            cast_numeric(subtot_df, SUBTOT_NUMERIC_COLS),
        )

    if data_source_mode == "live":
        from query_table import load_estimates

        logger.info("data_source_mode=live: loading est_line_df from live DB")
        est_line_df = cast_numeric(load_estimates(), EST_LINE_NUMERIC_COLS)
    else:
        logger.info(
            "data_source_mode=prefiltered: loading est_line_df from DB snapshot"
        )
        est_line_df = cast_numeric(read_table(TABLE_EST_LINE), EST_LINE_NUMERIC_COLS)

    subtot_df = cast_numeric(read_table(TABLE_SUBTOT), SUBTOT_NUMERIC_COLS)
    return est_line_df, subtot_df


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

    line_cols = (
        BASE_COLS
        + RATE_COLS
        + PARTS_INPUT_COLS
        + LBR_INPUT_COLS
        + OTHER_CHRG_COLS
        + PARTS_AUDIT_COLS
        + OTHER_CHRG_AUDIT_COLS
    )
    line_detail = line_detail.reindex(columns=line_cols)
    for col in ("discount_expected", "discount_match"):
        if col in line_detail.columns:
            line_detail[col] = line_detail[col].astype("boolean")

    # ── subtot_detail ─────────────────────────────────────────────────────────
    parts_subtot_cols = ["est_id", "cieca_part_typ_dsc"] + PARTS_SUBTOT_AUDIT_COLS
    df_parts_sub = (
        df_parts_subtot_audit[
            [c for c in parts_subtot_cols if c in df_parts_subtot_audit.columns]
        ].copy()
        if not df_parts_subtot_audit.empty
        else pd.DataFrame()
    )
    df_parts_sub.rename(
        columns={"cieca_part_typ_dsc": "cieca_tot_typ_dsc"}, inplace=True
    )
    df_parts_sub["subtot_type"] = "parts"

    lbr_subtot_cols = ["est_id", "cieca_lbr_typ_dsc"] + LBR_AUDIT_COLS + RATE_COLS
    df_lbr_sub = (
        df_lbr_subtot_audit[
            [c for c in lbr_subtot_cols if c in df_lbr_subtot_audit.columns]
        ].copy()
        if not df_lbr_subtot_audit.empty
        else pd.DataFrame()
    )
    df_lbr_sub.rename(columns={"cieca_lbr_typ_dsc": "cieca_tot_typ_dsc"}, inplace=True)
    df_lbr_sub["subtot_type"] = "labor"

    _df_paint = (
        df_paint_subtot_audit if df_paint_subtot_audit is not None else pd.DataFrame()
    )
    paint_subtot_cols = ["est_id", "cieca_tot_typ_dsc"] + PAINT_AUDIT_COLS
    df_paint_sub = (
        _df_paint[[c for c in paint_subtot_cols if c in _df_paint.columns]].copy()
        if not _df_paint.empty
        else pd.DataFrame()
    )
    if not df_paint_sub.empty:
        df_paint_sub["subtot_type"] = "materials"

    subtot_cols = (
        ["est_id", "claim_number", "cieca_tot_typ_dsc", "subtot_type"]
        + PARTS_SUBTOT_AUDIT_COLS
        + LBR_AUDIT_COLS
        + PAINT_AUDIT_COLS
        + RATE_COLS
    )
    _subtot_frames = [
        df
        for df in [df_parts_sub, df_lbr_sub, df_paint_sub]
        if not df.empty and not df.isna().all(axis=None)
    ]
    subtot_detail = (
        pd.concat(_subtot_frames, ignore_index=True, sort=False)
        if _subtot_frames
        else pd.DataFrame(columns=subtot_cols)
    )
    if not subtot_detail.empty and "claim_number" in est_line_df.columns:
        claim_map = est_line_df[["est_id", "claim_number"]].drop_duplicates("est_id")
        subtot_detail = subtot_detail.merge(claim_map, on="est_id", how="left")
    subtot_detail = subtot_detail.reindex(columns=subtot_cols)

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
        .apply(lambda x: (x == "Match").all())
        .rename("paint_est_pass")
        .reset_index()
        if df_paint_subtot_audit is not None and not df_paint_subtot_audit.empty
        else pd.DataFrame(columns=["est_id", "paint_est_pass"])
    )

    parts_issues = (
        df_parts_audit[df_parts_audit["discount_match"].eq(False)]
        .groupby("est_id")
        .size()
        .rename("parts_line_issues")
        .reset_index()
        if not df_parts_audit.empty
        else pd.DataFrame(columns=["est_id", "parts_line_issues"])
    )

    under_discount_issues = (
        df_parts_audit[df_parts_audit["discount_direction"] == "Under Discount"]
        .groupby("est_id")
        .size()
        .rename("under_discount_lines")
        .reset_index()
        if not df_parts_audit.empty
        else pd.DataFrame(columns=["est_id", "under_discount_lines"])
    )

    lbr_issues = (
        df_lbr_subtot_audit[df_lbr_subtot_audit["overall_lbr_match"] == "No Match"]
        .groupby("est_id")
        .size()
        .rename("lbr_issues")
        .reset_index()
        if not df_lbr_subtot_audit.empty
        else pd.DataFrame(columns=["est_id", "lbr_issues"])
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

    for df in [
        lbr_pass,
        parts_pass,
        paint_pass,
        parts_issues,
        under_discount_issues,
        lbr_issues,
        lbr_card_totals,
    ]:
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

    issue_cols = ["parts_line_issues", "under_discount_lines", "lbr_issues"]
    est_summary[issue_cols] = est_summary[issue_cols].apply(
        lambda s: pd.to_numeric(s, errors="coerce").fillna(0).astype(int)
    )
    for col in ["total_actual_lbr_amt", "total_expected_lbr_amt"]:
        if col in est_summary.columns:
            est_summary[col] = pd.to_numeric(est_summary[col], errors="coerce").round(
                ROUND_DECIMALS
            )

    return est_summary, subtot_detail, line_detail


# ── Batch runner (standalone / direct invocation) ────────────────────────────


def main():
    client = create_llm_client()
    est_line_df, subtot_df = load_estimate_data(DATA_SOURCE_MODE)

    est_ids = est_line_df["est_id"].unique()
    logger.info("Processing %d unique est_ids.", len(est_ids))

    t_total = time.perf_counter()
    succeeded = 0
    all_summary, all_subtot, all_line = [], [], []

    for i, est_id in enumerate(est_ids):
        if (i + 1) % LOG_EVERY_N == 0:
            logger.info("Progress: %d/%d estimates processed.", i + 1, len(est_ids))

        est_rows = est_line_df[est_line_df["est_id"] == est_id]
        subtot_rows = subtot_df[subtot_df["est_id"] == est_id]

        try:
            summary, subtot, line = run_em_pipeline(
                str(est_id), est_rows, subtot_rows, client, save=False
            )
            all_summary.append(summary)
            all_subtot.append(subtot)
            all_line.append(line)
            succeeded += 1
        except Exception as e:
            logger.error("est_id %s: FAILED — %s", est_id, e)

    save_results(
        pd.concat(
            [df for df in all_summary if not df.empty and not df.isna().all(axis=None)],
            ignore_index=True,
        ),
        pd.concat(
            [df for df in all_subtot if not df.empty and not df.isna().all(axis=None)],
            ignore_index=True,
        ),
        pd.concat(
            [df for df in all_line if not df.empty and not df.isna().all(axis=None)],
            ignore_index=True,
        ),
    )

    elapsed = time.perf_counter() - t_total
    logger.info(
        "Finished: %d/%d estimates in %.1fs (avg %.2fs/estimate)",
        succeeded,
        len(est_ids),
        elapsed,
        elapsed / len(est_ids) if len(est_ids) else 0,
    )


if __name__ == "__main__":
    from api_logging.config_logging import configure_logging
    from settings import get_settings

    configure_logging(get_settings())
    main()
