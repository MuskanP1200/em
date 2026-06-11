from __future__ import annotations

import logging
import yaml
from pathlib import Path

from sqlalchemy import text
from sql_connection import get_engine

_cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "config.yaml"))
_schema = _cfg["tables"]["schema"]
_est_raw = _cfg["tables"]["staging"]["est_raw"]
_est_line = _cfg["tables"]["staging"]["est_line"]
_est_subtot = _cfg["tables"]["staging"]["est_subtot"]
_pipeline_status = "pipeline_run_status"

logger = logging.getLogger(__name__)

# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL_EST_RAW = f"""
CREATE TABLE IF NOT EXISTS {_schema}.{_est_raw} (
    est_id                      TEXT PRIMARY KEY,
    claim_number                TEXT,
    repr_incident_id            TEXT,
    created_date                TEXT,
    dmg_dsc                     TEXT,
    vendor_id                   TEXT,
    vendor_name                 TEXT,
    licplte_nbr                 TEXT,
    vin                         TEXT,
    odmtr_nbr                   DOUBLE PRECISION,
    veh_make                    TEXT,
    veh_model                   TEXT,
    veh_color                   TEXT,
    veh_year                    TEXT,
    folder_prefix               TEXT,
    est_total_amt               DOUBLE PRECISION,
    est_stat_typ_id             TEXT,
    est_stat_typ_cde            TEXT,
    est_stat_typ_dsc            TEXT,
    primary_adjuster_user_id    TEXT,
    primary_adjuster_first_name TEXT,
    primary_adjuster_last_name  TEXT,
    est_received_dt_str         TEXT,
    est_received_dt             TEXT,
    managed_tow_followup_status TEXT,
    manual_estimate_ind         TEXT,
    note_to_shop                TEXT,
    update_timestamp            TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_DDL_EST_LINE = f"""
CREATE TABLE IF NOT EXISTS {_schema}.{_est_line} (
    est_id                      TEXT,
    claim_number                TEXT,
    created_date                TEXT,
    est_tot_amt                 DOUBLE PRECISION,
    lbr_hr_qty                  DOUBLE PRECISION,
    veh_make                    TEXT,
    veh_year                    TEXT,
    veh_model                   TEXT,
    veh_color                   TEXT,
    vin                         TEXT,
    licplte_nbr                 TEXT,
    odmtr_nbr                   DOUBLE PRECISION,
    vendor_name                 TEXT,
    dmg_dsc                     TEXT,
    cieca_dtl_hdr_id            TEXT,
    rvsn_nbr                    TEXT,
    line_nbr                    INTEGER,
    line_dsc                    TEXT,
    op_code                     TEXT,
    op_code_dsc                 TEXT,
    newly_added_ind             BOOLEAN,
    judgement_ind               BOOLEAN,
    op_code_judge_ind           BOOLEAN,
    paint_lbr_judge_ind         BOOLEAN,
    part_price_judge_ind        BOOLEAN,
    lbr_hr_judge_ind            BOOLEAN,
    line_dsc_judge_ind          BOOLEAN,
    cieca_part_typ_dsc          TEXT,
    cieca_part_dtl_line_id      TEXT,
    dtl_part_nbr                TEXT,
    dtl_part_nbr_qty            DOUBLE PRECISION,
    dtl_act_part_price_amt      DOUBLE PRECISION,
    dtl_tot_part_price_amt      DOUBLE PRECISION,
    cieca_line_adj_amt          DOUBLE PRECISION,
    cieca_line_net_amt          DOUBLE PRECISION,
    cieca_lbr_typ_dsc           TEXT,
    cieca_lbr_dtl_line_id       TEXT,
    dtl_lbr_hr_qty              DOUBLE PRECISION,
    dtl_lbr_tot_amt             DOUBLE PRECISION,
    lbr_rate                    DOUBLE PRECISION,
    paint_hrs                   DOUBLE PRECISION,
    paint_type_code             TEXT,
    cieca_othr_chrg_dtl_line_id TEXT,
    dtl_othr_chrg_price_amt     DOUBLE PRECISION,
    dtl_othr_chrg_qty           DOUBLE PRECISION,
    cieca_othr_chrg_typ_dsc     TEXT,
    grp_nbr                     TEXT,
    vndr_name                   TEXT,
    bdy_lbr_rate                DOUBLE PRECISION,
    mchncl_lbr_rate             DOUBLE PRECISION,
    frm_lbr_rate                DOUBLE PRECISION,
    almn_lbr_rate               DOUBLE PRECISION,
    pnt_mtrl_rate               DOUBLE PRECISION,
    dmstc_part_disc_amt         DOUBLE PRECISION,
    frn_part_disc_amt           DOUBLE PRECISION,
    kyls_disc_amt               DOUBLE PRECISION,
    specl_instruct_txt          TEXT,
    grp_note_txt                TEXT,
    xcld_frm_cdr_ind            BOOLEAN,
    cdr_included_amt_frm        DOUBLE PRECISION,
    cdr_included_amt_to         DOUBLE PRECISION,
    thrshld_amt                 DOUBLE PRECISION,
    clbrtn                      DOUBLE PRECISION,
    prescn                      DOUBLE PRECISION,
    postscn                     DOUBLE PRECISION,
    est_fee                     DOUBLE PRECISION,
    sublet_mrkup                DOUBLE PRECISION,
    tear_down_fee               DOUBLE PRECISION,
    anti_crsn_dsc               DOUBLE PRECISION,
    car_cvr_dsc                 DOUBLE PRECISION,
    clr_snd_bf_dsc              DOUBLE PRECISION,
    flx_add_dsc                 DOUBLE PRECISION,
    four_whl_algn_dsc           DOUBLE PRECISION,
    frm_pull_tm_dsc             DOUBLE PRECISION,
    frm_setup_dsc               DOUBLE PRECISION,
    frnt_whl_algn_dsc           DOUBLE PRECISION,
    hzrd_wst_dsc                DOUBLE PRECISION,
    msk_jams_dsc                DOUBLE PRECISION,
    mnt_bal_tir_dsc             DOUBLE PRECISION,
    sm_slr_dsc                  DOUBLE PRECISION,
    adhsn_prm_dsc               DOUBLE PRECISION,
    update_timestamp            TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_DDL_EST_SUBTOT = f"""
CREATE TABLE IF NOT EXISTS {_schema}.{_est_subtot} (
    est_id            TEXT,
    claim_number      TEXT,
    tot_typ_cde       TEXT,
    cieca_tot_typ_dsc TEXT,
    gross_amt         DOUBLE PRECISION,
    adj_pct           DOUBLE PRECISION,
    adj_tot_amt       DOUBLE PRECISION,
    tot_amt           DOUBLE PRECISION,
    tot_hr            DOUBLE PRECISION,
    lbr_rate          DOUBLE PRECISION,
    adj_typ_code      TEXT,
    adj_typ_id        TEXT,
    update_timestamp  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


_DDL_PIPELINE_STATUS = f"""
CREATE TABLE IF NOT EXISTS {_schema}.{_pipeline_status} (
    est_id              TEXT PRIMARY KEY,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vi_completed_at     TIMESTAMPTZ,
    vi_success          BOOLEAN,
    vi_attempt_count    INTEGER NOT NULL DEFAULT 0,
    em_completed_at     TIMESTAMPTZ,
    em_success          BOOLEAN,
    em_attempt_count    INTEGER NOT NULL DEFAULT 0,
    update_timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def ensure_staging_tables() -> None:
    """Create staging tables with explicit schema if they do not yet exist."""
    with get_engine().begin() as conn:
        conn.execute(text(_DDL_EST_RAW))
        conn.execute(text(_DDL_EST_LINE))
        conn.execute(text(_DDL_EST_SUBTOT))
        conn.execute(text(_DDL_PIPELINE_STATUS))
    logger.debug("Staging tables ensured.")


def reset_staging_tables() -> None:
    """Drop and recreate all staging tables. Use during development/testing."""
    tables = [_est_raw, _est_line, _est_subtot, _pipeline_status]
    with get_engine().begin() as conn:
        for table in tables:
            conn.execute(text(f"DROP TABLE IF EXISTS {_schema}.{table} CASCADE"))
        logger.info("Dropped staging tables: %s", tables)
    ensure_staging_tables()


PIPELINE_STATUS_TABLE: str = f"{_schema}.{_pipeline_status}"
