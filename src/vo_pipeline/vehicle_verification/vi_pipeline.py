from __future__ import annotations

import sys
import logging
import threading
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from ocr import init_azure_vision  # noqa: E402
from vlm_classifier import init_classifier  # noqa: E402
from processing import process_est_prefix  # noqa: E402
from utils import read_config  # noqa: E402
from db_writer import DBWriter  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from settings import get_settings  # noqa: E402
from storage import get_container_client  # noqa: E402

CONFIG_PATH = PROJECT_ROOT.parent / "config.yaml"  # services/config.yaml

logger = logging.getLogger("vi_pipeline")


def _fetch_est_record(
    conn, col_map: dict, staging_table: str, est_id: str
) -> Optional[dict]:
    """
    Query the staging table for a single EST record by its ID column.
    Returns a plain dict with keys: prefix, vin, license_plate, odometer.
    """
    id_col = col_map["id"]
    prefix_col = col_map["prefix"]
    vin_col = col_map.get("vin") or None
    plate_col = col_map.get("license_plate") or None
    odo_col = col_map.get("odometer") or None

    select_parts = [f"{prefix_col} AS prefix"]
    if vin_col:
        select_parts.append(f"{vin_col} AS vin")
    if plate_col:
        select_parts.append(f"{plate_col} AS license_plate")
    if odo_col:
        select_parts.append(f"{odo_col} AS odometer")

    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {staging_table} "
        f"WHERE {id_col} = %s "
        f"LIMIT 1"
    )

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (est_id,))
        row = cur.fetchone()

    return dict(row) if row else None


def run_vi_pipeline(est_id: str) -> int:
    """
    Process a single EST through the pipeline.

    Returns:
        0  — success
        1  — soft failure (no record found, or DB write failed)
        2  — hard failure (config / connection / processing error)
    """
    if not isinstance(est_id, str) or not est_id.strip():
        logger.error("est_id must be a non-empty string.")
        return 2
    est_id = est_id.strip()

    # ── Load config ───────────────────────────────────────────────────
    try:
        _full_cfg = read_config(CONFIG_PATH)
    except FileNotFoundError as exc:
        logger.error("Config not found: %s", exc)
        return 2

    cfg = _full_cfg["vehicle_verification"]
    _t = _full_cfg["tables"]
    staging_table = f"{_t['schema']}.{_t['staging']['est_raw']}"

    _schema = _t["schema"]
    out_cfg = _t["vi_output"]
    if not out_cfg.get("folders_table") or not out_cfg.get("images_table"):
        logger.error(
            "Config error: tables.vi_output.folders_table or images_table is empty"
        )
        return 2
    folders_table = f"{_schema}.{out_cfg['folders_table']}"
    images_table = f"{_schema}.{out_cfg['images_table']}"

    secrets = get_settings().model_dump()

    # ── Bridge settings → env vars expected by Azure SDK clients ─────

    logger.info("=" * 60)
    logger.info("vi_pipeline — est_id=%s", est_id)
    logger.info("=" * 60)

    # ── Parallelism ───────────────────────────────────────────────────
    par_cfg = cfg.get("parallelism", {})
    folder_workers = int(par_cfg.get("folder_workers", 3))
    image_workers = int(par_cfg.get("image_workers", 10))
    ocr_rate_limit = int(par_cfg.get("ocr_rate_limit", 20))
    vlm_rate_limit = int(par_cfg.get("vlm_rate_limit", 100))
    max_inflight_images = int(par_cfg.get("max_inflight_images", 50))
    pool_size = max(folder_workers * image_workers, max_inflight_images)

    # ── Connect to PostgreSQL ─────────────────────────────────────────
    pg_cfg = {
        "host": secrets["POSTGRES_HOST"],
        "port": secrets["POSTGRES_PORT"],
        "dbname": secrets["POSTGRES_DB"],
        "user": secrets["POSTGRES_USER"],
        "password": secrets["POSTGRESQL_PASSWORD"],
    }

    try:
        input_conn = psycopg2.connect(**pg_cfg)
    except Exception as exc:
        logger.error("Failed to connect to PostgreSQL: %s", exc, exc_info=True)
        return 2

    # ── Fetch EST record ──────────────────────────────────────────────
    try:
        record = _fetch_est_record(
            input_conn, cfg["input_columns"], staging_table, est_id
        )
    except Exception as exc:
        logger.error(
            "Failed to query staging table for est_id=%s: %s",
            est_id,
            exc,
            exc_info=True,
        )
        return 2
    finally:
        input_conn.close()

    if not record:
        logger.error("No record found for est_id=%s — nothing to process.", est_id)
        return 1

    prefix = (record.get("prefix") or "").strip()
    vin = record.get("vin") or None
    license_plate = record.get("license_plate") or None
    odometer = record.get("odometer") or None

    if not prefix:
        logger.error("est_id=%s has an empty blob prefix — nothing to process.", est_id)
        return 1

    logger.info(
        "EST record: prefix=%s | VIN=%s | plate=%s | odometer=%s",
        prefix,
        vin or "N/A",
        license_plate or "N/A",
        odometer or "N/A",
    )

    # ── Azure Blob Storage ────────────────────────────────────────────
    try:
        container_client = get_container_client(
            secrets["AZURE_BLOB_CONNECTION_STRING"],
            secrets["AZURE_CONTAINER_NAME"],
            pool_size=pool_size,
        )
        logger.info(
            "Azure container client initialized (container=%s, pool_size=%d)",
            secrets["AZURE_CONTAINER_NAME"],
            pool_size,
        )
    except Exception as exc:
        logger.error("Azure container init failed: %s", exc, exc_info=True)
        return 2

    # ── Azure Vision OCR ──────────────────────────────────────────────
    ocr, ocr_err = init_azure_vision(secrets, pool_size=pool_size)
    if ocr is None:
        logger.warning(
            "Azure Vision OCR not available: %s. Proceeding without OCR.", ocr_err
        )
    # else:
    #     logger.info("Azure Vision OCR initialized.")

    # ── Azure VLM classifier ──────────────────────────────────────────
    classifier, clf_err = init_classifier()
    if classifier is None:
        logger.warning(
            "VLM classifier not available: %s. Proceeding without classification.",
            clf_err,
        )
    # else:
    #     logger.info("VLM classifier initialized.")

    # ── Processing config ─────────────────────────────────────────────
    proc_cfg = cfg.get("processing", {})
    thumb_key = proc_cfg.get("thumb_key", "tmp")
    recursive = bool(proc_cfg.get("recursive", False))
    min_text_length = int(proc_cfg.get("min_text_length", 4))
    az_vision_cost = float(proc_cfg.get("az_vision_cost_per_1k", 0.0))

    # ── Semaphores ────────────────────────────────────────────────────
    ocr_sem = threading.Semaphore(ocr_rate_limit)
    vlm_sem = threading.Semaphore(vlm_rate_limit)
    inflight_sem = threading.Semaphore(max_inflight_images)

    # ── DB writer ─────────────────────────────────────────────────────
    db = DBWriter(
        pg_cfg=pg_cfg,
        folders_table=folders_table,
        images_table=images_table,
    )
    if not db.is_connected:
        logger.error("DBWriter failed to connect to PostgreSQL. Aborting.")
        return 2
    db.ensure_tables()

    # ── Process the folder ────────────────────────────────────────────
    logger.info("Processing prefix=%s …", prefix)
    try:
        result = process_est_prefix(
            container_client=container_client,
            prefix=prefix,
            thumb_key=thumb_key,
            recursive=recursive,
            ocr=ocr,
            classifier=classifier,
            show_progress=True,
            vin=vin,
            license_plate=license_plate,
            odometer=odometer,
            az_vision_cost_per_1k=az_vision_cost,
            min_text_length=min_text_length,
            image_workers=image_workers,
            ocr_sem=ocr_sem,
            vlm_sem=vlm_sem,
            inflight_sem=inflight_sem,
            folder_log=logging.getLogger(f"vi_pipeline.folder.{prefix}"),
        )
    except Exception as exc:
        logger.error(
            "process_est_prefix failed for est_id=%s / prefix=%s: %s",
            est_id,
            prefix,
            exc,
            exc_info=True,
        )
        db.close()
        return 2

    # ── Write results to DB ───────────────────────────────────────────
    result["est_id"] = est_id
    success = db.upsert_folder(result)
    if not success:
        logger.error("DB write failed for est_id=%s / prefix=%s.", est_id, prefix)
    db.close()

    metrics = result.get("metrics", {})
    logger.info(
        "Finished: prefix=%s | vin_status=%s | plate_status=%s | odometer_status=%s | "
        "ocr_images=%d | vlm_images=%d | wall_sec=%.3fs | db_write=%s",
        prefix,
        result.get("vin_status"),
        result.get("plate_status"),
        result.get("odometer_status"),
        metrics.get("az_vision", {}).get("images_processed", 0),
        metrics.get("vlm", {}).get("images_classified", 0),
        metrics.get("folder_wall_time_sec", 0.0),
        "ok" if success else "FAILED",
    )

    return 0 if success else 1


if __name__ == "__main__":
    from api_logging.config_logging import configure_logging

    configure_logging(get_settings())
    raise SystemExit(run_vi_pipeline("3021112631"))
