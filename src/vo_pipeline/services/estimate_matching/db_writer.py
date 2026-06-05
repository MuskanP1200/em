from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd
from sqlalchemy import text

from sql_connection import get_engine, write_table
from estimate_matching.config import (
    OUTPUT_SCHEMA,
    TABLE_EST_SUMMARY,
    TABLE_SUBTOT_DETAIL,
    TABLE_LINE_DETAIL,
    TABLE_OVERALL_SUMMARY,
)

logger = logging.getLogger("estimate_matching.db_writer")

# ── Null-like strings to replace with NaN before writing ─────────────────────
_NULL_STRINGS = {"none", "null", "na", "n/a", "nan", ""}

# ── Boolean columns per table ─────────────────────────────────────────────────
_BOOL_COLS_SUMMARY = ["lbr_est_pass", "parts_est_pass", "paint_est_pass"]
_BOOL_COLS_LINE = ["discount_match", "discount_expected"]
_BOOL_COLS_SUBTOT = []

# ── Explicit DDL ──────────────────────────────────────────────────────────────
_DDL_EST_SUMMARY = f"""
CREATE TABLE IF NOT EXISTS {OUTPUT_SCHEMA}.{TABLE_EST_SUMMARY} (
    est_id                TEXT UNIQUE,
    claim_number          TEXT,
    est_tot_amt           DOUBLE PRECISION,
    lbr_hr_qty            DOUBLE PRECISION,
    grp_nbr               TEXT,
    veh_make              TEXT,
    lbr_est_pass               BOOLEAN,
    parts_est_pass             BOOLEAN,
    paint_est_pass             BOOLEAN,
    total_actual_lbr_amt       DOUBLE PRECISION,
    total_expected_lbr_amt     DOUBLE PRECISION,
    estimate_match             TEXT,
    update_timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_DDL_LINE_DETAIL = f"""
CREATE TABLE IF NOT EXISTS {OUTPUT_SCHEMA}.{TABLE_LINE_DETAIL} (
    est_id                      TEXT,
    claim_number                TEXT,
    cieca_dtl_hdr_id            TEXT,
    line_item_count             INTEGER,
    line_nbr                    TEXT,
    lbr_hr_qty                  DOUBLE PRECISION,
    est_tot_amt                 DOUBLE PRECISION,
    grp_nbr                     TEXT,
    veh_make                    TEXT,
    dmg_dsc                     TEXT,
    line_dsc                    TEXT,
    bdy_lbr_rate                DOUBLE PRECISION,
    mchncl_lbr_rate             DOUBLE PRECISION,
    frm_lbr_rate                DOUBLE PRECISION,
    pnt_mtrl_rate               DOUBLE PRECISION,
    dmstc_part_disc_amt         DOUBLE PRECISION,
    frn_part_disc_amt           DOUBLE PRECISION,
    kyls_disc_amt               DOUBLE PRECISION,
    specl_instruct_txt          TEXT,
    grp_note_txt                TEXT,
    cieca_part_dtl_line_id      TEXT,
    cieca_line_adj_id           TEXT,
    cieca_part_typ_dsc          TEXT,
    dtl_part_nbr                TEXT,
    dtl_part_nbr_qty            DOUBLE PRECISION,
    dtl_act_part_price_amt      DOUBLE PRECISION,
    dtl_tot_part_price_amt      DOUBLE PRECISION,
    cieca_line_adj_amt          DOUBLE PRECISION,
    cieca_lbr_dtl_line_id       TEXT,
    cieca_lbr_typ_dsc           TEXT,
    dtl_lbr_tot_amt             DOUBLE PRECISION,
    dtl_lbr_hr_qty              DOUBLE PRECISION,
    actual_line_lbr_rate        DOUBLE PRECISION,
    expected_line_lbr_rate      DOUBLE PRECISION,
    line_lbr_rate_match         BOOLEAN,
    cieca_othr_chrg_dtl_line_id TEXT,
    cieca_othr_chrg_typ_dsc     TEXT,
    dtl_othr_chrg_price_amt     DOUBLE PRECISION,
    discount_expected           BOOLEAN,
    discount_source             TEXT,
    evidence                    TEXT,
    expected_discount_pct       DOUBLE PRECISION,
    actual_discount_pct         DOUBLE PRECISION,
    discount_pct_match          BOOLEAN,
    actual_discount_amt         DOUBLE PRECISION,
    expected_discount_amt       DOUBLE PRECISION,
    discount_match              BOOLEAN,
    discount_variance           DOUBLE PRECISION,
    discount_direction          TEXT,
    finding                     TEXT,
    other_charges_match         TEXT,
    update_timestamp            TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_DDL_SUBTOT_DETAIL = f"""
CREATE TABLE IF NOT EXISTS {OUTPUT_SCHEMA}.{TABLE_SUBTOT_DETAIL} (
    est_id                      TEXT,
    claim_number                TEXT,
    cieca_tot_typ_dsc           TEXT,
    subtot_type                 TEXT,

    expected_gross_amt          DOUBLE PRECISION,
    expected_adj_amt            DOUBLE PRECISION,
    expected_net_amt            DOUBLE PRECISION,
    expected_net_amt_calc       DOUBLE PRECISION,
    gross_amt                   DOUBLE PRECISION,
    adj_tot_amt                 DOUBLE PRECISION,
    tot_amt                      DOUBLE PRECISION,
    adj_pct                     DOUBLE PRECISION,
    expected_adj_pct            DOUBLE PRECISION,
    expected_adj_amt_calc       DOUBLE PRECISION,
    parts_gross_match           TEXT,
    adj_pct_match               TEXT,
    adj_match                   TEXT,
    adj_compliance_match        TEXT,
    parts_net_match             TEXT,
    overall_parts_subtot_match  TEXT,
    parts_subtot_mismatch_reason TEXT,

    actual_hrs                  DOUBLE PRECISION,
    expected_hrs                DOUBLE PRECISION,
    lbr_typ_hrs_match           TEXT,
    lbr_hr_direction            TEXT,
    actual_lbr_rate             DOUBLE PRECISION,
    expected_lbr_rate           DOUBLE PRECISION,
    lbr_typ_rate_match          TEXT,
    lbr_direction               TEXT,
    expected_lbr_amt            DOUBLE PRECISION,
    actual_lbr_amt              DOUBLE PRECISION,
    lbr_amt_match               TEXT,
    overall_lbr_match           TEXT,
    lbr_mismatch_reason         TEXT,

    actual_paint_amt               DOUBLE PRECISION,
    expected_paint_amt               DOUBLE PRECISION,
    paint_amt_match             TEXT,
    paint_hrs                DOUBLE PRECISION,
    expected_paint_rate         DOUBLE PRECISION,
    actual_paint_rate           DOUBLE PRECISION,
    paint_rate_match            TEXT,
    paint_rate_direction        TEXT,
    paint_note                  TEXT,
    bdy_lbr_rate                DOUBLE PRECISION,
    mchncl_lbr_rate             DOUBLE PRECISION,
    frm_lbr_rate                DOUBLE PRECISION,
    pnt_mtrl_rate               DOUBLE PRECISION,
    dmstc_part_disc_amt         DOUBLE PRECISION,
    frn_part_disc_amt           DOUBLE PRECISION,
    kyls_disc_amt               DOUBLE PRECISION,
    specl_instruct_txt          TEXT,
    grp_note_txt                TEXT,
    update_timestamp            TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


_DDL_OVERALL_SUMMARY = f"""
CREATE TABLE IF NOT EXISTS {OUTPUT_SCHEMA}.{TABLE_OVERALL_SUMMARY} (
    est_id                     TEXT UNIQUE,
    claim_number               TEXT,
    est_tot_amt                DOUBLE PRECISION,
    lbr_hr_qty                 DOUBLE PRECISION,
    grp_nbr                    TEXT,
    veh_make                   TEXT,
    lbr_est_pass               BOOLEAN,
    parts_est_pass             BOOLEAN,
    paint_est_pass             BOOLEAN,
    total_actual_lbr_amt       DOUBLE PRECISION,
    total_expected_lbr_amt     DOUBLE PRECISION,
    estimate_match             TEXT,
    vin_status                 BOOLEAN,
    plate_status               BOOLEAN,
    overall_match              TEXT,
    update_timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# ── Column sets derived from DDL — single source of truth ────────────────────
# Used to filter DataFrames before writing so only DDL-defined columns are sent.
_SQL_KEYWORDS = {"primary", "constraint", "unique", "check", "foreign", "references"}


def _cols_from_ddl(ddl: str) -> list[str]:
    """Extract column names from a CREATE TABLE DDL string."""
    return [
        m for m in re.findall(r"^\s+(\w+)\s", ddl, re.MULTILINE)
        if m.lower() not in _SQL_KEYWORDS
    ]


LINE_DETAIL_COLS:  list[str] = _cols_from_ddl(_DDL_LINE_DETAIL)
SUBTOT_DETAIL_COLS: list[str] = _cols_from_ddl(_DDL_SUBTOT_DETAIL)


def reset_em_tables() -> None:
    """Drop and recreate all EM output tables. Use during development/testing."""
    tables = [
        TABLE_EST_SUMMARY,
        TABLE_SUBTOT_DETAIL,
        TABLE_LINE_DETAIL,
        TABLE_OVERALL_SUMMARY,
    ]
    with get_engine().begin() as conn:
        for table in tables:
            conn.execute(text(f"DROP TABLE IF EXISTS {OUTPUT_SCHEMA}.{table} CASCADE"))
        logger.info("Dropped EM output tables: %s", tables)
    ensure_em_tables()


def ensure_em_tables() -> None:
    """Create EM output tables with explicit schema if they do not yet exist."""
    with get_engine().begin() as conn:
        conn.execute(text(_DDL_EST_SUMMARY))
        conn.execute(text(_DDL_LINE_DETAIL))
        conn.execute(text(_DDL_SUBTOT_DETAIL))
        conn.execute(text(_DDL_OVERALL_SUMMARY))


def _sanitise(df: pd.DataFrame) -> pd.DataFrame:
    """Replace null-like strings with NaN so Postgres float columns don't choke."""
    null_strings = {s for s in _NULL_STRINGS} | {s.upper() for s in _NULL_STRINGS}
    return df.where(~df.isin(null_strings), other=np.nan)


def _cast_output_dtypes(
    est_summary: pd.DataFrame,
    subtot_detail: pd.DataFrame,
    line_detail: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Cast boolean columns to pandas nullable boolean so they map cleanly to Postgres BOOLEAN."""

    def _bool(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
        for col in cols:
            if col in df.columns:
                df[col] = df[col].astype("boolean")
        return df

    return (
        _bool(est_summary.copy(), _BOOL_COLS_SUMMARY),
        _bool(subtot_detail.copy(), _BOOL_COLS_SUBTOT),
        _bool(line_detail.copy(), _BOOL_COLS_LINE),
    )


def save_results(
    est_summary: pd.DataFrame,
    subtot_detail: pd.DataFrame,
    line_detail: pd.DataFrame,
) -> None:
    """Persist the three EM output tables to Postgres.

    Each call deletes any existing rows for the est_ids in this batch before
    inserting, so re-running the pipeline on the same estimates overwrites
    rather than duplicates. Results for other est_ids are left untouched.
    """
    ensure_em_tables()

    est_summary, subtot_detail, line_detail = _cast_output_dtypes(
        _sanitise(est_summary),
        _sanitise(subtot_detail),
        _sanitise(line_detail),
    )

    est_ids = est_summary["est_id"].dropna().unique().tolist()
    if est_ids:
        with get_engine().begin() as conn:
            for table in [TABLE_EST_SUMMARY, TABLE_SUBTOT_DETAIL, TABLE_LINE_DETAIL]:
                conn.execute(
                    text(
                        f"DELETE FROM {OUTPUT_SCHEMA}.{table} WHERE est_id = ANY(:ids)"  # nosec B608
                    ),  # nosec B608
                    {"ids": est_ids},
                )

    write_table(
        est_summary, TABLE_EST_SUMMARY, schema=OUTPUT_SCHEMA, if_exists="append"
    )
    write_table(
        subtot_detail, TABLE_SUBTOT_DETAIL, schema=OUTPUT_SCHEMA, if_exists="append"
    )
    write_table(
        line_detail, TABLE_LINE_DETAIL, schema=OUTPUT_SCHEMA, if_exists="append"
    )
