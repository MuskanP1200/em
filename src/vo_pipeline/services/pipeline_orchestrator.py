# Pipeline orchestrator
# Step 1 — API ingest: search estimates, save to Postgres, upload images
# Step 2 — VI: vehicle image verification per est_id
# Step 3 — EM: estimate matching (parts + labour validation) per est_id

import threading
import time
import yaml
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
from estimate_matching.config import (
    EST_LINE_NUMERIC_COLS,
    SUBTOT_NUMERIC_COLS,
    API_INGEST_EST_LINE,
    API_INGEST_EST_SUBTOT,
    API_INGEST_SCHEMA,
    OUTPUT_SCHEMA,
    TABLE_EST_SUMMARY,
    TABLE_OVERALL_SUMMARY,
)
from sqlalchemy import text
from sql_connection import (
    get_engine,
    read_table,
    read_query,
    build_in_clause,
    write_table,
)
from estimate_matching.em_pipeline import run_em_pipeline
from estimate_matching.llm import create_llm_client

from estimate_matching.db_writer import reset_em_tables
from api_ingest.db_staging import reset_staging_tables, PIPELINE_STATUS_TABLE
from vehicle_verification.db_writer import reset_vi_tables
from vehicle_verification.vi_pipeline import run_vi_pipeline
from api_ingest.api_ingestion_pipeline import (
    run_api_ingestion_pipeline,
)

from api_logging.config_logging import configure_logging, current_est_id
from settings import get_settings

settings = get_settings()
configure_logging(settings)

logger = logging.getLogger("pipeline_orchestrator")

# ── Run flags ─────────────────────────────────────────────────────────────────
_run = yaml.safe_load(open(Path(__file__).resolve().parent / "config.yaml")).get(
    "run", {}
)
RUN_INGESTION = _run.get("ingestion", True)
RUN_VI = _run.get("vehicle_verification", True)
RUN_EM = _run.get("estimate_matching", True)
RESET_STAGING = _run.get("reset_staging_tables", False)
RESET_OUTPUT = _run.get("reset_output_tables", False)


# ── Data helpers ─────────────────────────────────────────────────────────────
def cast_numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Coerce specified columns to numeric, setting invalid values to NaN."""
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


EM_VI_WORKERS = 2  # number of estimates processed concurrently in Steps 2+3
MAX_PIPELINE_ATTEMPTS = 2  # max attempts per stage before giving up on an est_id


def _get_incomplete_est_ids(run_vi: bool, run_em: bool) -> list[str]:
    """
    Return est_ids where required stages haven't succeeded yet and haven't
    exhausted their retry budget (MAX_PIPELINE_ATTEMPTS).
    """
    conditions = []
    if run_vi:
        conditions.append(
            f"(vi_success IS NOT TRUE AND vi_attempt_count < {MAX_PIPELINE_ATTEMPTS})"
        )
    if run_em:
        conditions.append(
            f"(em_success IS NOT TRUE AND em_attempt_count < {MAX_PIPELINE_ATTEMPTS})"
        )
    if not conditions:
        return []
    where = " OR ".join(conditions)
    try:
        df = read_query(
            f"SELECT est_id FROM {PIPELINE_STATUS_TABLE} WHERE {where}"  # nosec B608
        )
        return df["est_id"].dropna().astype(str).tolist()
    except Exception as e:
        logger.warning("Could not fetch incomplete est_ids: %s", e)
        return []


def _update_pipeline_status(est_id: str, stage: str, success: bool) -> None:
    """
    Upsert pipeline stage result for an est_id.
    Always increments attempt_count regardless of success/failure.
    Uses INSERT ... ON CONFLICT so missing rows (e.g. RUN_INGESTION=False) are handled.
    """
    col = "vi" if stage == "vi" else "em"
    try:
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {PIPELINE_STATUS_TABLE} (est_id, {col}_completed_at, {col}_success, {col}_attempt_count) "  # nosec B608
                    f"VALUES (:est_id, NOW(), :success, 1) "  # nosec B608
                    f"ON CONFLICT (est_id) DO UPDATE SET "  # nosec B608
                    f"    {col}_completed_at  = NOW(), "  # nosec B608
                    f"    {col}_success       = :success, "  # nosec B608
                    f"    {col}_attempt_count = {PIPELINE_STATUS_TABLE}.{col}_attempt_count + 1, "  # nosec B608
                    f"    update_timestamp    = NOW()"  # nosec B608
                ),
                {"success": success, "est_id": est_id},
            )
    except Exception as e:
        logger.warning(
            "Could not update pipeline status for est_id=%s stage=%s: %s",
            est_id,
            stage,
            e,
        )


def run_pipeline():
    pipeline_start = time.perf_counter()
    vi_ok, vi_fail, em_ok, em_fail = 0, 0, 0, 0
    _lock = threading.Lock()

    client = create_llm_client() if RUN_EM else None

    # ── Reset tables (dev/test only) ──────────────────────────────────────────
    if RESET_STAGING:
        logger.info(
            "reset_staging_tables=true — dropping and recreating staging tables."
        )
        reset_staging_tables()

    if RESET_OUTPUT:
        logger.info("reset_output_tables=true — dropping and recreating output tables.")
        reset_em_tables()
        reset_vi_tables()

    # ── Step 1: API ingestion ─────────────────────────────────────────────────
    if RUN_INGESTION:
        logger.info("=== Step 1: API ingestion ===")
        est_ids = run_api_ingestion_pipeline()
        if not est_ids:
            logger.info("No new estimates ingested — nothing to process.")
            return
        logger.info("Ingested %d new estimate(s).", len(est_ids))
    else:
        logger.info(
            "=== Step 1: Ingestion skipped — reading est_ids from staging table ==="
        )
        df = cast_numeric(
            read_table(f"{API_INGEST_SCHEMA}.{API_INGEST_EST_LINE}"),
            EST_LINE_NUMERIC_COLS,
        )
        # est_ids = df["est_id"].dropna().astype(str).unique().tolist()
        # est_ids = df["est_id"].dropna().astype(str).unique().tolist()[:10]
        # est_ids = ["3021957335", "3021957321", "3021957116", "3021957171", "3021957091"]

        if not est_ids:
            logger.info("No est_ids found in staging table — nothing to process.")
            return
        logger.info("Found %d est_id(s) in staging table.", len(est_ids))

    # ── Pick up any estimates where VI or EM didn't complete in a prior run ──────
    incomplete = _get_incomplete_est_ids(RUN_VI, RUN_EM)
    if incomplete:
        logger.info(
            "Found %d incomplete est_id(s) from previous runs — adding to batch.",
            len(incomplete),
        )
    est_ids = list(
        dict.fromkeys(est_ids + incomplete)
    )  # union, deduplicated, order preserved

    # ── Load estimate data from Postgres ──────────────────────────────────────

    df = cast_numeric(
        read_table(f"{API_INGEST_SCHEMA}.{API_INGEST_EST_LINE}"),
        EST_LINE_NUMERIC_COLS,
    )
    est_line_df = df[df["est_id"].isin(est_ids)]

    subtot_df = cast_numeric(
        read_query(
            f"SELECT * FROM {API_INGEST_SCHEMA}.{API_INGEST_EST_SUBTOT} WHERE est_id in ({build_in_clause(est_ids)})"  # nosec B608
        ),
        SUBTOT_NUMERIC_COLS,
    )
    # subtot_df = subtot_df[subtot_df["est_id"].isin(est_ids)]

    def _process_one(est_id: str) -> None:
        nonlocal vi_ok, vi_fail, em_ok, em_fail
        token = current_est_id.set(str(est_id))
        logger.debug(
            "est_id %s: worker started — active threads: %d",
            est_id,
            threading.active_count(),
        )
        try:
            est_rows = est_line_df[est_line_df["est_id"] == est_id]
            subtot_rows = subtot_df[subtot_df["est_id"] == est_id]

            # ── Step 2: Vehicle verification ──────────────────────────────────
            if RUN_VI:
                t = time.perf_counter()
                try:
                    run_vi_pipeline(est_id)
                    logger.debug("VI  ✓  %.1fs", time.perf_counter() - t)
                    _update_pipeline_status(est_id, "vi", success=True)
                    with _lock:
                        vi_ok += 1
                except Exception as e:
                    logger.error("VI  ✗  %s", e, exc_info=True)
                    _update_pipeline_status(est_id, "vi", success=False)
                    with _lock:
                        vi_fail += 1

            # ── Step 3: Estimate matching ──────────────────────────────────────
            if RUN_EM:
                t = time.perf_counter()
                try:
                    run_em_pipeline(est_id, est_rows, subtot_rows, client)
                    logger.debug("EM  ✓  %.1fs", time.perf_counter() - t)
                    _update_pipeline_status(est_id, "em", success=True)
                    with _lock:
                        em_ok += 1
                except Exception as e:
                    logger.error("EM  ✗  %s", e, exc_info=True)
                    _update_pipeline_status(est_id, "em", success=False)
                    with _lock:
                        em_fail += 1
        finally:
            logger.debug(
                "est_id %s: worker done — active threads: %d",
                est_id,
                threading.active_count(),
            )
            current_est_id.reset(token)

    with ThreadPoolExecutor(max_workers=EM_VI_WORKERS) as pool:
        futures = {pool.submit(_process_one, est_id): est_id for est_id in est_ids}
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc:
                logger.error(
                    "est_id %s: unhandled error — %s", futures[fut], exc, exc_info=True
                )

    # ── Step 4: Enrich EM summary with VI results ─────────────────────────────
    if RUN_VI and RUN_EM:
        logger.info("=== Step 4: Enriching EM summary with VI results ===")
        try:
            _cfg = yaml.safe_load(open(Path(__file__).resolve().parent / "config.yaml"))
            _t = _cfg["tables"]
            vi_table = f"{_t['schema']}.{_t['vi_output']['folders_table']}"

            # Only fetch data for the current batch — no full table scans
            in_clause = build_in_clause(est_ids)
            vi_df = read_query(
                f"SELECT est_id, vin_status, plate_status FROM {vi_table} "  # nosec B608
                f"WHERE est_id IN ({in_clause})"  # nosec B608
            )
            vi_df["est_id"] = vi_df["est_id"].astype(str)

            em_df = read_query(
                f"SELECT * FROM {OUTPUT_SCHEMA}.{TABLE_EST_SUMMARY} "  # nosec B608
                f"WHERE est_id IN ({in_clause})"  # nosec B608
            )
            em_df["est_id"] = em_df["est_id"].astype(str)

            merged = em_df.merge(vi_df, on="est_id", how="left")
            merged["overall_match"] = np.where(
                merged["vin_status"].eq(True)
                & merged["plate_status"].eq(True)
                & (merged["estimate_match"] == "Match"),
                "Match",
                "No Match",
            )

            # Delete existing rows for this batch then append — never touch other est_ids
            with get_engine().begin() as conn:
                conn.execute(
                    text(
                        f"DELETE FROM {OUTPUT_SCHEMA}.{TABLE_OVERALL_SUMMARY} WHERE est_id IN ({in_clause})"  # nosec B608
                    ),
                )
            write_table(
                merged,
                TABLE_OVERALL_SUMMARY,
                schema=OUTPUT_SCHEMA,
                if_exists="append",
            )
            logger.info(
                "Overall summary updated for %d est_id(s) in %s.%s.",
                len(merged),
                OUTPUT_SCHEMA,
                TABLE_OVERALL_SUMMARY,
            )
        except Exception as e:
            logger.error("Step 4 enrichment failed: %s", e, exc_info=True)

    # ── Run summary ───────────────────────────────────────────────────────────
    total_sec = time.perf_counter() - pipeline_start
    logger.info(
        "=" * 60
        + "\nPIPELINE COMPLETE  %.1fs"
        + "\n  estimates : %d"
        + "\n  VI  : %d ok  %d failed"
        + "\n  EM  : %d ok  %d failed"
        + "\n"
        + "=" * 60,
        total_sec,
        len(est_ids),
        vi_ok,
        vi_fail,
        em_ok,
        em_fail,
    )


if __name__ == "__main__":
    run_pipeline()
