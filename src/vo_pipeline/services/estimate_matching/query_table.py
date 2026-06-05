"""
query_table.py
==============
Loads the full estimate line-item dataset by running four separate SQL queries
and merging them in pandas. This avoids overwhelming the Postgres server with
a single massive multi-way join.

Query split:
    1. core     — est + repr_ncdnt + veh + veh_ymms + vr_vndr + cdr_grp_vndr
                  + est_repr + elctrnc_est_dtl
                  (one row per elctrnc_est_dtl_id; LIMIT here = limit on estimates)
    2. hdr      — cieca_dtl_hdr (one row per cieca_dtl_hdr_id / line item)
    3. parts    — cieca_part_dtl_line + cieca_line_adj + cieca_part_typ
    4. labor    — cieca_lbr_dtl_line + cieca_lbr_typ
    5. oc       — cieca_othr_chrg_dtl_line + cieca_othr_chrg_typ

    Merge order in pandas:
        core → hdr   (on elctrnc_est_dtl_id)  → expands to one row per line item
             → parts (on cieca_dtl_hdr_id)
             → lbr   (on cieca_dtl_hdr_id)
             → oc    (on cieca_dtl_hdr_id)

KNOWN ISSUE:
    ~129,321 cieca_dtl_hdr_id values appear twice in cieca_lbr_dtl_line.
    Duplicates are always LAR + one of LAU/LAS/LAM, causing fanout.
    defan_part_oc_cols() nullifies part/OC columns on the extra rows so
    each row represents one unique labor entry.
"""

from __future__ import annotations

import logging
import os
from sqlalchemy import text
import pandas as pd

os.environ["PYDEVD_WARN_EVALUATION_TIMEOUT"] = "30"
os.environ["PYDEVD_UNBLOCK_THREADS_TIMEOUT"] = "30"

from estimate_matching.config import FILTERS, ICE_TABLES  # noqa: E402
from sql_connection import read_query  # noqa: E402

SAVE = False
SCHEMA = "public"
TABLE_NAME = "check123"


log = logging.getLogger(__name__)

# Columns to nullify on fanned-out rows
_DEFAN_PART_COLS = [
    "cieca_part_dtl_line_id",
    "cieca_line_adj_id",
    "cieca_part_typ_id",
    "dtl_part_nbr",
    "dtl_part_nbr_qty",
    "dtl_act_part_price_amt",
    "dtl_tot_part_price_amt",
    "cieca_part_typ_dsc",
    "cieca_line_adj_amt",
]
_DEFAN_OC_COLS = [
    "cieca_othr_chrg_dtl_line_id",
    "cieca_othr_chrg_typ_id",
    "dtl_othr_chrg_price_amt",
    "dtl_othr_chrg_qty",
    "cieca_othr_chrg_typ_dsc",
]


def _build_core_query(tables: dict[str, str], filters: dict | None = None) -> str:
    t = tables
    f = filters or FILTERS

    exclude_modules = f.get("exclude_modules") or []
    country_codes = f.get("country_codes") or []
    stat_type_ids = f.get("est_stat_typ_ids") or []
    since_minutes = f.get("since_minutes")
    timestamp_col = f.get("timestamp_col", "create_timestamp")
    limit = f.get("limit")

    clauses = []
    if exclude_modules:
        quoted = ", ".join(f"'{m}'" for m in exclude_modules)
        clauses.append(f"e.create_module NOT IN ({quoted})")
    if country_codes:
        quoted = ", ".join(f"'{c}'" for c in country_codes)
        clauses.append(f"vndr.cntry_iso_cde IN ({quoted})")
    if stat_type_ids:
        ids = ", ".join(str(i) for i in stat_type_ids)
        clauses.append(f"e.est_stat_typ_id IN ({ids})")
    if since_minutes is not None:
        clauses.append(
            f"e.{timestamp_col} >= NOW() - INTERVAL '{int(since_minutes)} minutes'"
        )

    where = ("WHERE " + "\n          AND ".join(clauses)) if clauses else ""
    limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""

    query = f"""
        SELECT
            e.est_id,
            e.repr_ncdnt_id,
            e.vr_vndr_id,
            e.vndr_grp_nbr,
            e.lbr_hr_qty,
            e.est_tot_amt,
            e.create_module,
            e.est_stat_typ_id,
            repr.dmg_dsc,
            repr.veh_id,
            repr.odmtr_nbr,
            veh.veh_ymms_id,
            veh.vin,
            veh.licplte_nbr,
            veh.licplte_st,
            veh.licplte_cntry,
            ymms.veh_make,
            vndr.cntry_iso_cde,
            cdr.grp_nbr,
            cdr.bdy_lbr_rate,
            cdr.mchncl_lbr_rate,
            cdr.frm_lbr_rate,
            cdr.pnt_mtrl_rate,
            cdr.dmstc_part_disc_amt,
            cdr.frn_part_disc_amt,
            cdr.kyls_disc_amt,
            cdr.almn_lbr_rate,
            cdr.specl_instruct_txt,
            cdr.grp_note_txt,
            er.est_repr_id,
            el.elctrnc_est_dtl_id
        FROM {t['est']} e
        JOIN {t['repr']}        repr ON e.repr_ncdnt_id       = repr.repr_ncdnt_id
        JOIN {t['vndr']}        vndr ON e.vr_vndr_id          = vndr.vr_vndr_id
        JOIN {t['veh']}         veh  ON repr.veh_id           = veh.veh_id
        JOIN {t['veh_ymms']}    ymms ON veh.veh_ymms_id       = ymms.veh_ymms_id
        JOIN {t['cdr']}         cdr  ON e.vr_vndr_id          = cdr.vr_vndr_id
                                   AND e.vndr_grp_nbr         = cdr.grp_nbr
        JOIN {t['est_repr']}    er   ON e.est_id              = er.est_id
        JOIN {t['elctrnc_est']} el   ON er.est_repr_id        = el.est_repr_id
        {where}
        {limit_clause}
    """  # nosec B608
    return text(query)


def _build_hdr_query(tables: dict[str, str]) -> str:
    t = tables
    query = f"""
        SELECT
            cdh.cieca_dtl_hdr_id,
            cdh.elctrnc_est_dtl_id,
            cdh.line_dsc,
            cdh.line_nbr
        FROM {t['cieca_dtl_hdr']} cdh
    """  # nosec B608
    return text(query)


def _build_part_query(tables: dict[str, str]) -> str:
    t = tables
    query = f"""
        SELECT
            part.cieca_dtl_hdr_id,
            part.cieca_part_dtl_line_id,
            part.cieca_line_adj_id,
            part.cieca_part_typ_id,
            part.dtl_part_nbr,
            part.dtl_part_nbr_qty,
            part.dtl_act_part_price_amt,
            part.dtl_tot_part_price_amt,
            pt.cieca_part_typ_dsc,
            adj.cieca_line_adj_amt
        FROM {t['cieca_part_dtl']} part
        LEFT JOIN {t['cieca_line_adj']} adj ON part.cieca_line_adj_id = adj.cieca_line_adj_id
        LEFT JOIN (
            SELECT "CIECA_PART_TYP_ID"  AS cieca_part_typ_id,
                   "CIECA_PART_TYP_DSC" AS cieca_part_typ_dsc
            FROM {t['cieca_part_typ']}
        ) pt ON CAST(part.cieca_part_typ_id AS bigint) = pt.cieca_part_typ_id
    """  # nosec B608
    return text(query)


def _build_lbr_query(tables: dict[str, str]) -> str:
    t = tables
    query = f"""
        SELECT
            CAST(lbr.cieca_dtl_hdr_id AS numeric) AS cieca_dtl_hdr_id,
            lbr.cieca_lbr_dtl_line_id,
            lbr.cieca_lbr_typ_id,
            lbr.dtl_lbr_tot_amt,
            lbr.dtl_lbr_hr_qty,
            lt.cieca_lbr_typ_dsc
        FROM {t['cieca_lbr_dtl']} lbr
        LEFT JOIN (
            SELECT "CIECA_LBR_TYP_ID"  AS cieca_lbr_typ_id,
                   "CIECA_LBR_TYP_DSC" AS cieca_lbr_typ_dsc
            FROM {t['cieca_lbr_typ']}
        ) lt ON CAST(lbr.cieca_lbr_typ_id AS bigint) = lt.cieca_lbr_typ_id
    """  # nosec B608
    return text(query)


def _build_oc_query(tables: dict[str, str]) -> str:
    t = tables
    query = f"""
        SELECT
            CAST(oc.cieca_dtl_hdr_id AS numeric) AS cieca_dtl_hdr_id,
            oc.cieca_othr_chrg_dtl_line_id,
            oc.cieca_othr_chrg_typ_id,
            oc.dtl_othr_chrg_price_amt,
            oc.dtl_othr_chrg_qty,
            oct.cieca_othr_chrg_typ_dsc
        FROM {t['cieca_othr_chrg_dtl']} oc
        LEFT JOIN (
            SELECT "CIECA_OTHR_CHRG_TYP_ID"  AS cieca_othr_chrg_typ_id,
                   "CIECA_OTHR_CHRG_TYP_DSC" AS cieca_othr_chrg_typ_dsc
            FROM {t['cieca_othr_chrg_typ']}
        ) oct ON CAST(oc.cieca_othr_chrg_typ_id AS bigint) = oct.cieca_othr_chrg_typ_id
    """  # nosec B608
    return text(query)


def defan_part_oc_cols(df: pd.DataFrame) -> pd.DataFrame:
    """
    Nullifies part and other-charge columns on fanned-out rows so that each
    row represents exactly one labor entry per cieca_dtl_hdr_id.
    """
    mask = df.duplicated(subset=["cieca_dtl_hdr_id"], keep="first")
    log.info(f"Fanned-out rows: {mask.sum():,}")
    df.loc[mask, _DEFAN_PART_COLS] = None
    df.loc[mask, _DEFAN_OC_COLS] = None
    log.info("defan complete.")
    return df


def load_estimates(filters: dict | None = None) -> pd.DataFrame:
    """Load all estimate line-items from Postgres.

    Pass ``filters`` to override config.yaml values at runtime, e.g.:
        load_estimates(filters={"est_stat_typ_ids": [6], "since_minutes": 10})
    Omit it to use whatever is set in config.yaml.
    """
    tables = ICE_TABLES
    active_filters = filters or FILTERS

    log.info("Loading core (est→elctrnc_est_dtl) | filters: %s", active_filters)
    core = read_query(_build_core_query(tables, filters=active_filters))
    log.info(f"  core: {len(core):,} rows, {len(core.columns)} columns")

    log.info("Loading cieca_dtl_hdr (line headers)...")
    hdr = read_query(_build_hdr_query(tables))
    log.info(f"  hdr: {len(hdr):,} rows")

    log.info("Loading part details...")
    parts = read_query(_build_part_query(tables))
    log.info(f"  parts: {len(parts):,} rows")

    log.info("Loading labor details...")
    lbr = read_query(_build_lbr_query(tables))
    log.info(f"  labor: {len(lbr):,} rows")

    log.info("Loading other-charge details...")
    oc = read_query(_build_oc_query(tables))
    log.info(f"  oc: {len(oc):,} rows")

    log.info("Merging in pandas...")
    df = (
        core.merge(hdr, on="elctrnc_est_dtl_id", how="left")
        .merge(parts, on="cieca_dtl_hdr_id", how="left")
        .merge(lbr, on="cieca_dtl_hdr_id", how="left")
        .merge(oc, on="cieca_dtl_hdr_id", how="left")
    )
    log.info(f"Merged shape: {df.shape}")

    df = defan_part_oc_cols(df)
    log.info(f"Final shape: {df.shape}")
    return df


def main():
    df = load_estimates()
    log.info(df.shape)
    log.info(df.head())

    if SAVE:
        from sql_connection import get_engine

        with get_engine().begin() as conn:
            df.to_sql(
                name=TABLE_NAME,
                con=conn,
                schema=SCHEMA,
                if_exists="replace",
                index=False,
                chunksize=5000,
            )

    return df


if __name__ == "__main__":
    main()
