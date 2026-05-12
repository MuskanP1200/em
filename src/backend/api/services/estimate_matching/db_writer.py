from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sqlalchemy import text

from sql_connection import get_engine, write_table
from estimate_matching.config import (
    OUTPUT_SCHEMA,
    TABLE_EST_SUMMARY,
    TABLE_SUBTOT_DETAIL,
    TABLE_LINE_DETAIL,
)

logger = logging.getLogger("estimate_matching.db_writer")

# ── Null-like strings to replace with NaN before writing ─────────────────────
_NULL_STRINGS = {"none", "null", "na", "n/a", "nan", ""}

# ── Boolean columns per table ─────────────────────────────────────────────────
_BOOL_COLS_SUMMARY = ["lbr_est_pass", "parts_est_pass"]
_BOOL_COLS_LINE = ["discount_match", "discount_expected"]
_BOOL_COLS_SUBTOT = [
    "overcharged", "undercharged",
    "parts_gross_match", "adj_match", "parts_net_match", "overall_parts_subtot_match",
    "lbr_typ_hrs_match", "lbr_typ_unit_cost_match", "overall_lbr_match",
]

# ── Explicit DDL ──────────────────────────────────────────────────────────────
_DDL_EST_SUMMARY = f"""
CREATE TABLE IF NOT EXISTS {OUTPUT_SCHEMA}.{TABLE_EST_SUMMARY} (
    est_id                BIGINT,
    est_tot_amt           DOUBLE PRECISION,
    lbr_hr_qty            DOUBLE PRECISION,
    grp_nbr               TEXT,
    veh_make              TEXT,
    lbr_est_pass          BOOLEAN,
    parts_est_pass        BOOLEAN,
    parts_line_issues     INTEGER,
    under_discount_lines  INTEGER,
    lbr_issues            INTEGER,
    estimate_match        TEXT
)
"""

_DDL_LINE_DETAIL = f"""
CREATE TABLE IF NOT EXISTS {OUTPUT_SCHEMA}.{TABLE_LINE_DETAIL} (
    est_id                      BIGINT,
    cieca_dtl_hdr_id            BIGINT,
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
    cieca_part_dtl_line_id      BIGINT,
    cieca_line_adj_id           BIGINT,
    cieca_part_typ_dsc          TEXT,
    dtl_part_nbr                TEXT,
    dtl_part_nbr_qty            DOUBLE PRECISION,
    dtl_act_part_price_amt      DOUBLE PRECISION,
    dtl_tot_part_price_amt      DOUBLE PRECISION,
    cieca_line_adj_amt          DOUBLE PRECISION,
    cieca_lbr_dtl_line_id       BIGINT,
    cieca_lbr_typ_dsc           TEXT,
    dtl_lbr_tot_amt             DOUBLE PRECISION,
    dtl_lbr_hr_qty              DOUBLE PRECISION,
    cieca_othr_chrg_dtl_line_id BIGINT,
    cieca_othr_chrg_typ_dsc     TEXT,
    dtl_othr_chrg_price_amt     DOUBLE PRECISION,
    applicable_discount_pct     DOUBLE PRECISION,
    discount_source             TEXT,
    evidence                    TEXT,
    discount_expected           BOOLEAN,
    expected_discount_amt       DOUBLE PRECISION,
    discount_match              BOOLEAN,
    discount_variance           DOUBLE PRECISION,
    discount_direction          TEXT,
    finding                     TEXT,
    other_charges_match         TEXT
)
"""

_DDL_SUBTOT_DETAIL = f"""
CREATE TABLE IF NOT EXISTS {OUTPUT_SCHEMA}.{TABLE_SUBTOT_DETAIL} (
    est_id                      BIGINT,
    cieca_tot_typ_dsc           TEXT,
    subtot_type                 TEXT,
    line_tot_part_amt           DOUBLE PRECISION,
    line_adj_amt                DOUBLE PRECISION,
    line_net_amt                DOUBLE PRECISION,
    tot_amt                     DOUBLE PRECISION,
    adj_tot_amt                 DOUBLE PRECISION,
    net_amt                     DOUBLE PRECISION,
    parts_gross_match           BOOLEAN,
    adj_match                   BOOLEAN,
    parts_net_match             BOOLEAN,
    overall_parts_subtot_match  BOOLEAN,
    lbr_typ_hrs_match           BOOLEAN,
    unit_cost_based_lbr_dsc     DOUBLE PRECISION,
    calc_unit_cost              DOUBLE PRECISION,
    lbr_typ_unit_cost_match     BOOLEAN,
    overall_lbr_match           BOOLEAN,
    overcharged                 BOOLEAN,
    undercharged                BOOLEAN,
    bdy_lbr_rate                DOUBLE PRECISION,
    mchncl_lbr_rate             DOUBLE PRECISION,
    frm_lbr_rate                DOUBLE PRECISION,
    pnt_mtrl_rate               DOUBLE PRECISION,
    dmstc_part_disc_amt         DOUBLE PRECISION,
    frn_part_disc_amt           DOUBLE PRECISION,
    kyls_disc_amt               DOUBLE PRECISION,
    specl_instruct_txt          TEXT,
    grp_note_txt                TEXT
)
"""


def reset_em_tables() -> None:
    """Drop and recreate all EM output tables. Use during development/testing."""
    from estimate_matching.config import TABLE_OVERALL_SUMMARY

    tables = [TABLE_EST_SUMMARY, TABLE_SUBTOT_DETAIL, TABLE_LINE_DETAIL, TABLE_OVERALL_SUMMARY]
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
    logger.debug("EM output tables ensured.")


def _sanitise(df: pd.DataFrame) -> pd.DataFrame:
    """Replace null-like strings with NaN so Postgres float columns don't choke."""
    null_map = {s: np.nan for s in _NULL_STRINGS} | {
        s.upper(): np.nan for s in _NULL_STRINGS
    }
    return df.replace(null_map).infer_objects(copy=False)


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
    if_exists: str = "replace",
) -> None:
    """Persist the three EM output tables to Postgres.

    Tables are created with explicit DDL on first call so column types are
    always correct. ``if_exists='replace'`` truncates existing data while
    preserving the schema; ``if_exists='append'`` just inserts.
    """
    ensure_em_tables()

    est_summary, subtot_detail, line_detail = _cast_output_dtypes(
        _sanitise(est_summary),
        _sanitise(subtot_detail),
        _sanitise(line_detail),
    )

    if if_exists == "replace":
        with get_engine().begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {OUTPUT_SCHEMA}.{TABLE_EST_SUMMARY}"))
            conn.execute(text(f"TRUNCATE TABLE {OUTPUT_SCHEMA}.{TABLE_SUBTOT_DETAIL}"))
            conn.execute(text(f"TRUNCATE TABLE {OUTPUT_SCHEMA}.{TABLE_LINE_DETAIL}"))

    write_table(
        est_summary, TABLE_EST_SUMMARY, schema=OUTPUT_SCHEMA, if_exists="append"
    )
    write_table(
        subtot_detail, TABLE_SUBTOT_DETAIL, schema=OUTPUT_SCHEMA, if_exists="append"
    )
    write_table(
        line_detail, TABLE_LINE_DETAIL, schema=OUTPUT_SCHEMA, if_exists="append"
    )

    logger.info("Saved to Postgres (if_exists=%s).", if_exists)
    logger.info("  %s.%s: %s", OUTPUT_SCHEMA, TABLE_EST_SUMMARY, est_summary.shape)
    logger.info("  %s.%s: %s", OUTPUT_SCHEMA, TABLE_SUBTOT_DETAIL, subtot_detail.shape)
    logger.info("  %s.%s: %s", OUTPUT_SCHEMA, TABLE_LINE_DETAIL, line_detail.shape)
