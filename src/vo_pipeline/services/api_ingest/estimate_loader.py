"""
api_loader.py
=============
Two public functions for pulling estimate data from the VR Services SOAP APIs.

    search_and_save_new_estimates(token)
        Calls SearchEstimates, inserts only NEW rows into ice.api_estimates_raw,
        and returns the list of newly ingested est_ids.

    fetch_estimate_details(token, est_ids, ...)
        For each est_id (parallel): fetches electronic estimate XML, estimate
        detail, CDR rates, and optionally uploads images to Azure Blob Storage.
        Returns (est_line_df, subtot_df) and optionally saves both to Postgres.

Typical call sequence
---------------------
    token    = get_token(...)
    est_ids  = search_and_save_new_estimates(token)
    est_line_df, subtot_df = fetch_estimate_details(
        token, est_ids,
        table_names=("api_est_line", "api_est_subtot"),
    )

Per-estimate API calls (parallel across estimates)
--------------------------------------------------
    1. GetElectronicEstimate          → parse_estimate_xml_from_string()
    2. GetEstimateDetailForSubtotals  → vendor GID + group number
    3. GetCDRGroupVendor              → contracted rates (lru-cached)
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

_SERVICES = Path(__file__).resolve().parent.parent
_API = _SERVICES.parent
sys.path.insert(0, str(_API))  # api/  — for settings
sys.path.insert(
    0, str(_SERVICES)
)  # api/services/ — for api_auth, sql_connection, estimate_matching.*

from api_ingest.estimate_client import (  # noqa: E402
    search_estimates,
    get_estimate_detail,
    get_electronic_estimate_xml,
    get_cdr_group_vendor,
    get_repair_incident_detail,
    _empty_cdr_rates,
)
from api_ingest.electronic_estimate_parser import (  # noqa: E402
    parse_estimate_xml_from_string,
)
from sql_connection import read_query, write_table  # noqa: E402

log = logging.getLogger(__name__)


# ── Search + incremental ingest ───────────────────────────────────────────────


def search_and_save_new_estimates(
    token: str,
    status_code: str = "WAITONAUTH",
    group: str = "DR",
    max_records: int | None = None,
    table: str = "api_estimates_raw",
    schema: str = "analysis",
) -> list[str]:
    """
    Search for estimates via the VR Services API, insert only rows whose
    est_id is not already present in ``{schema}.{table}``, and return the
    list of newly inserted est_ids.

    Parameters
    ----------
    token        : auth token from get_token()
    status_code  : estimate status filter passed to SearchEstimates
    group        : CDR group code passed to SearchEstimates
    max_records  : cap on total estimates fetched (None = all)
    table        : destination table name (default "api_estimates_raw")
    schema       : destination schema     (default "ice")

    Returns
    -------
    list of est_id strings that were just inserted (empty if nothing new)
    """
    search_rows = search_estimates(
        token,
        status_code=status_code,
        group=group,
        max_records=max_records,
    )

    if not search_rows:
        log.warning("SearchEstimates returned no results — nothing to ingest")
        return []

    df = pd.DataFrame(search_rows)

    # ── Find which est_ids are already stored ────────────────────────────────
    try:
        existing = read_query(f"SELECT est_id FROM {schema}.{table}")
        existing_ids = set(existing["est_id"].dropna().tolist())
    except Exception as e:
        # Table doesn't exist yet — all rows are new
        log.warning("Failed to query existing est_ids from %s.%s (table may not exist): %s", schema, table, e, exc_info=True)
        existing_ids = set()

    new_df = df[~df["est_id"].isin(existing_ids)]

    if new_df.empty:
        log.info("No new estimates (all %d already in %s.%s)", len(df), schema, table)
        return []

    write_table(new_df, table, schema=schema, if_exists="append")
    log.info(
        "Inserted %d new rows into %s.%s (%d already existed)",
        len(new_df),
        schema,
        table,
        len(df) - len(new_df),
    )

    # Return dict mapping est_id -> repair_incident_id for damage description fetching
    repair_incident_map = dict(zip(new_df["est_id"], new_df["repr_incident_id"]))
    return repair_incident_map


# ── Per-estimate fetch ────────────────────────────────────────────────────────


def _fetch_one_estimate(
    token: str,
    est_id: str,
    repair_incident_id: str = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """
    Fetch electronic estimate XML + detail + damage description for a single est_id.
    Returns (est_lines, subtots, detail_dict, damage_description).
    Runs API calls sequentially.
    """
    xml_str = get_electronic_estimate_xml(token, est_id)
    est_lines, subtots = parse_estimate_xml_from_string(xml_str)
    detail = get_estimate_detail(token, est_id)

    # Fetch damage description if repair_incident_id is available
    damage_desc = None
    if repair_incident_id:
        damage_desc = get_repair_incident_detail(token, repair_incident_id)

    return est_lines, subtots, detail, damage_desc


# ── CDR rate injection ────────────────────────────────────────────────────────


def _inject_cdr_rates(est_lines: pd.DataFrame, cdr: dict) -> pd.DataFrame:
    """
    Overwrite CDR rate columns in est_lines with values from GetCDRGroupVendor.
    Adds any column that doesn't yet exist.
    """
    for col, val in cdr.items():
        est_lines[col] = val
    return est_lines


# ── Main loader ───────────────────────────────────────────────────────────────


def fetch_estimate_details(
    token: str,
    est_ids: list[str] | dict,
    table_names: tuple[str, str] | None = None,
    schema: str = "ice",
    max_workers: int = 4,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fetch electronic estimate XML, CDR detail, and damage description for each est_id.
    Returns (est_line_df, subtot_df).

    Parameters
    ----------
    token            : auth token from get_token()
    est_ids          : list of est_ids OR dict mapping est_id -> repair_incident_id
    table_names      : (est_line_table, subtot_table) — if provided, both
                       DataFrames are appended to these Postgres tables in
                       ``schema``; pass None to skip SQL write
    schema           : Postgres schema for table_names (default "ice")
    max_workers      : parallel threads for per-estimate API calls
    """
    # Handle both list and dict inputs
    if isinstance(est_ids, dict):
        repair_incident_map = est_ids
        est_id_list = list(est_ids.keys())
    else:
        est_id_list = est_ids
        repair_incident_map = {}

    if not est_id_list:
        log.warning("No est_ids provided — returning empty DataFrames")
        return pd.DataFrame(), pd.DataFrame()

    log.info(
        "Loading %d estimates from API (max_workers=%d, save_sql=%s, damage_desc=%s)",
        len(est_id_list),
        max_workers,
        table_names is not None,
        bool(repair_incident_map),
    )

    all_lines: list[pd.DataFrame] = []
    all_subtot: list[pd.DataFrame] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for eid in est_id_list:
            repair_id = repair_incident_map.get(eid)
            fut = pool.submit(_fetch_one_estimate, token, eid, repair_id)
            futures[fut] = eid

        for fut in as_completed(futures):
            eid = futures[fut]
            try:
                est_lines, subtots, detail, damage_desc = fut.result()
            except Exception as exc:
                log.error("est_id %s: FETCH FAILED — %s", eid, exc, exc_info=True)
                continue

            # ── Inject CDR rates ──────────────────────────────────────────────
            vr_vendor_id = detail.iloc[0].get("vr_vendor_id")
            grp_nbr = detail.iloc[0].get("grp_nbr")

            if vr_vendor_id and grp_nbr:
                try:
                    cdr = get_cdr_group_vendor(token, vr_vendor_id, grp_nbr)
                except Exception as exc:
                    log.warning(
                        "est_id %s: CDR GROUP VENDOR FETCH FAILED (vendor_id=%s, grp=%s) — using default rates: %s",
                        eid,
                        vr_vendor_id,
                        grp_nbr,
                        exc,
                        exc_info=True,
                    )
                    cdr = _empty_cdr_rates()
            else:
                log.warning(
                    "est_id %s: missing vr_vendor_id=%s or grp_nbr=%s — CDR rates will be null",
                    eid,
                    vr_vendor_id,
                    grp_nbr,
                )
                cdr = _empty_cdr_rates()

            est_lines = _inject_cdr_rates(est_lines, cdr)

            if grp_nbr is not None:
                est_lines["grp_nbr"] = grp_nbr

            # Add damage description if available
            if damage_desc is not None:
                est_lines["dmg_dsc"] = damage_desc

            all_lines.append(est_lines)
            all_subtot.append(subtots)
            log.debug(
                "est_id %s: %d lines, %d subtotals, CDR bdy_rate=%s, damage_desc=%s",
                eid,
                len(est_lines),
                len(subtots),
                cdr.get("bdy_lbr_rate"),
                "✓" if damage_desc else "missing",
            )

    if not all_lines:
        log.error("DATA LOSS: No estimate data fetched successfully from %d est_ids — returning empty DataFrames", len(est_ids))
        return pd.DataFrame(), pd.DataFrame()

    try:
        est_line_df = pd.concat([df for df in all_lines if not df.empty], ignore_index=True)
        subtot_df = pd.concat([df for df in all_subtot if not df.empty], ignore_index=True)
    except Exception as e:
        log.error("Failed to concatenate API results: %s", e, exc_info=True)
        return pd.DataFrame(), pd.DataFrame()

    log.info(
        "API load complete: %d line rows, %d subtotal rows, %d unique est_ids (attempted %d)",
        len(est_line_df),
        len(subtot_df),
        est_line_df["est_id"].nunique() if not est_line_df.empty else 0,
        len(est_ids),
    )

    # ── Save to Postgres ──────────────────────────────────────────────────────
    if table_names is not None:
        est_line_table, subtot_table = table_names
        try:
            write_table(est_line_df, est_line_table, schema=schema, if_exists="append")
            log.info("Saved %d rows to %s.%s", len(est_line_df), schema, est_line_table)
        except Exception as e:
            log.error("Failed to write est_line_df to %s.%s: %s", schema, est_line_table, e, exc_info=True)

        try:
            write_table(subtot_df, subtot_table, schema=schema, if_exists="append")
            log.info("Saved %d rows to %s.%s", len(subtot_df), schema, subtot_table)
        except Exception as e:
            log.error("Failed to write subtot_df to %s.%s: %s", schema, subtot_table, e, exc_info=True)

    return est_line_df, subtot_df


if __name__ == "__main__":
    from settings import get_settings
    from api_ingest.api_auth import get_token
    from estimate_matching.config import (
        AUTH_URL,
        API_MAX_WORKERS,
        API_INGEST_SCHEMA,
        API_INGEST_EST_LINE,
        API_INGEST_EST_SUBTOT,
    )

    logging.basicConfig(level=logging.INFO)

    creds = get_settings().model_dump()
    _token = get_token(
        username=creds["ICE_API_USER_NAME"],
        password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
        auth_url=AUTH_URL,
    )
    _est_ids = search_and_save_new_estimates(_token, max_records=200, schema="analysis")
    log.debug(f"New est_ids: {_est_ids}")

    if _est_ids:
        est_line_df, subtot_df = fetch_estimate_details(
            _token,
            _est_ids,
            max_workers=API_MAX_WORKERS,
            table_names=(API_INGEST_EST_LINE, API_INGEST_EST_SUBTOT),
            schema=API_INGEST_SCHEMA,
        )
        log.debug(f"est_line_df shape: {est_line_df.shape}")
        log.debug(f"subtot_df   shape: {subtot_df.shape}")
