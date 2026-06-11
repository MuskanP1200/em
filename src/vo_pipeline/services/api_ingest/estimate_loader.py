"""
estimate_loader.py
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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from api_ingest.estimate_client import (  # noqa: E402
    search_estimates,
    get_estimate_detail,
    get_electronic_estimate_xml,
    get_cdr_group_vendor,
    get_repair_incident_detail,
)
from api_ingest.electronic_estimate_parser import (  # noqa: E402
    parse_estimate_xml_from_string,
)
from sqlalchemy import text

from sql_connection import get_engine, write_table
from api_ingest.db_staging import PIPELINE_STATUS_TABLE

log = logging.getLogger(__name__)


# ── Search + incremental ingest ───────────────────────────────────────────────


def search_and_save_new_estimates(
    token: str,
    status_code: str = "WAITONAUTH",
    group: str = "DR",
    max_records: int | None = None,
    table: str = "api_estimates_raw",
    schema: str = "analysis",
) -> pd.DataFrame:
    """
    Search for estimates via the VR Services API, insert only rows whose
    est_id is not already present in ``{schema}.{table}``, and return the
    list of newly inserted est_ids.

    Deduplication is enforced at the DB level via PRIMARY KEY on est_id +
    ON CONFLICT DO NOTHING, so concurrent runs or retries cannot produce
    duplicates regardless of application-level race conditions.

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
    pd.DataFrame with est_id and repr_incident_id columns (empty if nothing new)
    """
    search_rows = search_estimates(
        token,
        status_code=status_code,
        group=group,
        max_records=max_records,
    )

    if not search_rows:
        log.warning("SearchEstimates returned no results — nothing to ingest")
        return pd.DataFrame()

    df = pd.DataFrame(search_rows)
    new_est_ids = _insert_new_estimates(df, schema, table)

    if not new_est_ids:
        log.info("No new estimates (all %d already in %s.%s)", len(df), schema, table)
        return pd.DataFrame()

    log.info(
        "Inserted %d new rows into %s.%s (%d already existed)",
        len(new_est_ids),
        schema,
        table,
        len(df) - len(new_est_ids),
    )

    new_df = df[df["est_id"].isin(new_est_ids)]
    return new_df[["est_id", "claim_number", "repr_incident_id"]]


def _insert_new_estimates(df: pd.DataFrame, schema: str, table: str) -> list[str]:
    """
    Bulk-insert all rows into est_raw using ON CONFLICT (est_id) DO NOTHING.

    The DB PRIMARY KEY on est_id is the authoritative dedup guard — no
    application-level pre-check needed. Returns the est_ids that were
    actually inserted so the caller knows which are genuinely new.
    """
    cols = list(df.columns)
    col_list = ", ".join(cols)

    # Replace NaN/NA with None so they map to SQL NULL
    rows = df.where(df.notna(), other=None).to_dict(orient="records")

    placeholders: list[str] = []
    params: dict = {}
    for i, row in enumerate(rows):
        placeholders.append(f"({', '.join(f':{c}_{i}' for c in cols)})")
        for col in cols:
            params[f"{col}_{i}"] = row[col]

    sql = text(  # nosec B608
        f"INSERT INTO {schema}.{table} ({col_list}) "  # nosec B608
        f"VALUES {', '.join(placeholders)} "  # nosec B608
        f"ON CONFLICT (est_id) DO NOTHING "  # nosec B608
        f"RETURNING est_id"  # nosec B608
    )

    with get_engine().begin() as conn:
        result = conn.execute(sql, params)
        new_ids = [row[0] for row in result.fetchall()]

    # Initialise pipeline status for newly ingested estimates
    if new_ids:
        status_placeholders = ", ".join(f"(:est_id_{i})" for i in range(len(new_ids)))
        status_params = {f"est_id_{i}": eid for i, eid in enumerate(new_ids)}
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {PIPELINE_STATUS_TABLE} (est_id) "  # nosec B608
                    f"VALUES {status_placeholders} "  # nosec B608
                    f"ON CONFLICT (est_id) DO NOTHING"  # nosec B608
                ),
                status_params,
            )

    return new_ids


# ── Per-estimate fetch ────────────────────────────────────────────────────────


def _fetch_one_estimate(
    token: str, est_id: str, repair_incident_id: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch all data for a single est_id: XML, detail, CDR rates, repair incident."""
    log.debug(
        "est_id %s: inner fetch started — active threads: %d",
        est_id,
        threading.active_count(),
    )
    with ThreadPoolExecutor(max_workers=3) as inner:
        xml_fut = inner.submit(get_electronic_estimate_xml, token, est_id)
        detail_fut = inner.submit(get_estimate_detail, token, est_id)
        repair_fut = inner.submit(get_repair_incident_detail, token, repair_incident_id)

    est_lines, subtots = parse_estimate_xml_from_string(xml_fut.result())
    detail = detail_fut.result()
    est_lines["dmg_dsc"] = repair_fut.result()

    vr_vendor_id = detail.iloc[0].get("vr_vendor_id")
    grp_nbr = detail.iloc[0].get("grp_nbr")

    if vr_vendor_id and grp_nbr:
        try:
            cdr = get_cdr_group_vendor(token, vr_vendor_id, grp_nbr)
        except Exception:
            log.warning(
                "est_id %s: CDR fetch failed — rates will be null",
                est_id,
                exc_info=True,
            )
            cdr = {}
    else:
        log.warning(
            "est_id %s: missing vr_vendor_id=%s or grp_nbr=%s — CDR rates will be null",
            est_id,
            vr_vendor_id,
            grp_nbr,
        )
        cdr = {}

    for col, val in cdr.items():
        est_lines[col] = val

    if grp_nbr is not None:
        est_lines["grp_nbr"] = grp_nbr

    log.debug(
        "est_id %s: %d lines, %d subtotals, CDR bdy_rate=%s",
        est_id,
        len(est_lines),
        len(subtots),
        cdr.get("bdy_lbr_rate"),
    )

    return est_lines, subtots


# ── Main loader ───────────────────────────────────────────────────────────────
def fetch_estimate_details(
    token: str,
    est_ids_df: pd.DataFrame,
    table_names: tuple[str, str] | None = None,
    schema: str = "ice",
    max_workers: int = 4,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fetch electronic estimate XML, CDR detail, and  for each
    est_id. Returns (est_line_df, subtot_df).

    """

    est_ids = est_ids_df["est_id"].dropna().to_list()

    if not est_ids:
        log.warning("No est_ids provided — returning empty DataFrames")
        return pd.DataFrame(), pd.DataFrame()

    log.info(
        "Loading %d estimates from API (max_workers=%d, save_sql=%s)",
        len(est_ids),
        max_workers,
        table_names is not None,
    )

    all_lines: list[pd.DataFrame] = []
    all_subtot: list[pd.DataFrame] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}

        for idx, row in est_ids_df.iterrows():
            eid = row["est_id"]
            repr_id = row["repr_incident_id"]
            fut = pool.submit(_fetch_one_estimate, token, eid, repr_id)
            futures[fut] = eid

        for fut in as_completed(futures):
            eid = futures[fut]
            try:
                est_lines, subtots = fut.result()
            except Exception:
                log.error("est_id %s: fetch failed", eid, exc_info=True)
                continue

            all_lines.append(est_lines)
            all_subtot.append(subtots)

    if not all_lines:
        log.error("No estimate data fetched successfully — returning empty DataFrames")
        return pd.DataFrame(), pd.DataFrame()

    est_line_df = pd.concat(
        [df for df in all_lines if not df.empty and not df.isna().all(axis=None)],
        ignore_index=True,
    )
    subtot_df = pd.concat(
        [df for df in all_subtot if not df.empty and not df.isna().all(axis=None)],
        ignore_index=True,
    )

    if "claim_number" in est_ids_df.columns:
        claim_map = est_ids_df[["est_id", "claim_number"]].drop_duplicates("est_id")
        est_line_df = est_line_df.merge(claim_map, on="est_id", how="left")
        subtot_df = subtot_df.merge(claim_map, on="est_id", how="left")

    log.info(
        "API load complete: %d line rows, %d subtotal rows, %d unique est_ids",
        len(est_line_df),
        len(subtot_df),
        est_line_df["est_id"].nunique(),
    )

    # ── Save to Postgres ──────────────────────────────────────────────────────
    if table_names is not None:
        est_line_table, subtot_table = table_names
        write_table(est_line_df, est_line_table, schema=schema, if_exists="append")
        log.info("Saved est_line_df to %s.%s", schema, est_line_table)
        write_table(subtot_df, subtot_table, schema=schema, if_exists="append")
        log.info("Saved subtot_df to %s.%s", schema, subtot_table)

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
    _est_ids_df = search_and_save_new_estimates(
        _token, max_records=200, schema="analysis"
    )

    _est_ids = _est_ids_df["est_id"].dropna().to_list()
    log.debug("New est_ids: %s", _est_ids)

    if _est_ids:
        est_line_df, subtot_df = fetch_estimate_details(
            _token,
            _est_ids_df,
            max_workers=API_MAX_WORKERS,
            table_names=(API_INGEST_EST_LINE, API_INGEST_EST_SUBTOT),
            schema=API_INGEST_SCHEMA,
        )
        log.debug("est_line_df shape: %s", est_line_df.shape)
        log.debug("subtot_df   shape: %s", subtot_df.shape)
