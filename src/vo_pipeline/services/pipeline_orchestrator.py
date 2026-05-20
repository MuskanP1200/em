# Pipeline orchestrator
# Step 1 — API ingest: search estimates, save to Postgres, upload images
# Step 2 — VI: vehicle image verification per est_id
# Step 3 — EM: estimate matching (parts + labour validation) per est_id

import threading
import time
import yaml
import logging
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
from sql_connection import read_table, write_table
from estimate_matching.em_pipeline import (
    run_em_pipeline,
    create_llm_client,
)
from estimate_matching.db_writer import reset_em_tables
from api_ingest.db_staging import reset_staging_tables
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


EM_VI_WORKERS = 3  # number of estimates processed concurrently in Steps 2+3


def run_pipeline():
    pipeline_start = time.perf_counter()
    vi_ok, vi_fail, em_ok, em_fail = 0, 0, 0, 0
    _lock = threading.Lock()

    # ── Reset tables (dev/test only) ──────────────────────────────────────────
    if RESET_STAGING:
        logger.info(
            "reset_staging_tables=true — dropping and recreating staging tables."
        )
        reset_staging_tables()

    if RESET_OUTPUT:
        logger.info("reset_output_tables=true — dropping and recreating output tables.")
        reset_em_tables()

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
        df = read_table(f"{API_INGEST_SCHEMA}.{API_INGEST_EST_LINE}")
        est_ids = df["est_id"].dropna().astype(str).unique().tolist()
        if not est_ids:
            logger.info("No est_ids found in staging table — nothing to process.")
            return
        logger.info("Found %d est_id(s) in staging table.", len(est_ids))

    # ── Load estimate data from Postgres ──────────────────────────────────────
    est_line_df = cast_numeric(
        read_table(f"{API_INGEST_SCHEMA}.{API_INGEST_EST_LINE}"), EST_LINE_NUMERIC_COLS
    )
    subtot_df = cast_numeric(
        read_table(f"{API_INGEST_SCHEMA}.{API_INGEST_EST_SUBTOT}"), SUBTOT_NUMERIC_COLS
    )
    est_line_df = est_line_df[
        est_line_df["est_id"].astype(int).astype(str).isin(est_ids)
    ]
    subtot_df = subtot_df[subtot_df["est_id"].astype(int).astype(str).isin(est_ids)]

    client = create_llm_client() if RUN_EM else None

    def _process_one(est_id: str) -> None:
        nonlocal vi_ok, vi_fail, em_ok, em_fail
        token = current_est_id.set(str(est_id))
        try:
            est_rows = est_line_df[est_line_df["est_id"] == int(est_id)]
            subtot_rows = subtot_df[subtot_df["est_id"] == int(est_id)]

            # ── Step 2: Vehicle verification ──────────────────────────────────
            if RUN_VI:
                t = time.perf_counter()
                try:
                    run_vi_pipeline(est_id)
                    logger.info("VI  ✓  %.1fs", time.perf_counter() - t)
                    with _lock:
                        vi_ok += 1
                except Exception as e:
                    logger.error("VI  ✗  %s", e, exc_info=True)
                    with _lock:
                        vi_fail += 1

            # ── Step 3: Estimate matching ──────────────────────────────────────
            if RUN_EM:
                t = time.perf_counter()
                try:
                    run_em_pipeline(est_id, est_rows, subtot_rows, client)
                    logger.info("EM  ✓  %.1fs", time.perf_counter() - t)
                    with _lock:
                        em_ok += 1
                except Exception as e:
                    logger.error("EM  ✗  %s", e, exc_info=True)
                    with _lock:
                        em_fail += 1
        finally:
            current_est_id.reset(token)

    with ThreadPoolExecutor(max_workers=EM_VI_WORKERS) as pool:
        futures = {pool.submit(_process_one, est_id): est_id for est_id in est_ids[:1000]}
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc:
                logger.error("est_id %s: unhandled error — %s", futures[fut], exc, exc_info=True)

    # ── Step 4: Enrich EM summary with VI results ─────────────────────────────
    if RUN_VI and RUN_EM:
        logger.info("=== Step 4: Enriching EM summary with VI results ===")
        try:
            _cfg = yaml.safe_load(open(Path(__file__).resolve().parent / "config.yaml"))
            _t = _cfg["tables"]
            vi_table = f"{_t['schema']}.{_t['vi_output']['folders_table']}"

            vi_df = read_table(vi_table)[["est_id", "vin_status", "plate_status"]]
            vi_df["est_id"] = vi_df["est_id"].astype(str)

            em_df = read_table(f"{OUTPUT_SCHEMA}.{TABLE_EST_SUMMARY}")
            em_df["est_id"] = em_df["est_id"].astype(str)

            merged = em_df.merge(vi_df, on="est_id", how="left")
            merged["overall_match"] = np.where(
                merged["vin_status"]
                & merged["plate_status"]
                & (merged["estimate_match"] == "Match"),
                "Match",
                "No Match",
            )

            write_table(
                merged,
                TABLE_OVERALL_SUMMARY,
                schema=OUTPUT_SCHEMA,
                if_exists="replace",
            )
            logger.info(
                "Overall summary written to %s.%s (%d rows).",
                OUTPUT_SCHEMA,
                TABLE_OVERALL_SUMMARY,
                len(merged),
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
