from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from vehicle_verification.ocr import init_azure_vision
from vehicle_verification.vlm_classifier import init_classifier
from vehicle_verification.processing import process_est_prefix
from vehicle_verification.utils import read_config
from vehicle_verification.db_writer import DBWriter, ensure_vi_tables
from settings import get_settings
from storage import get_container_client
from sql_connection import get_engine
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"  # services/config.yaml

logger = logging.getLogger("vi_pipeline")


def _fetch_est_record(col_map: dict, staging_table: str, est_id: str) -> Optional[dict]:
    """
    Query the staging table for a single EST record by its ID column.
    Returns a plain dict with keys: prefix, vin, license_plate, odometer.
    """
    id_col = col_map["id"]
    prefix_col = col_map["prefix"]
    vin_col = col_map.get("vin") or None
    plate_col = col_map.get("license_plate") or None
    odo_col = col_map.get("odometer") or None

    select_parts = [f"{prefix_col} AS prefix", "claim_number"]
    if vin_col:
        select_parts.append(f"{vin_col} AS vin")
    if plate_col:
        select_parts.append(f"{plate_col} AS license_plate")
    if odo_col:
        select_parts.append(f"{odo_col} AS odometer")

    sql = (  # nosec B608
        f"SELECT {', '.join(select_parts)} "  # nosec B608
        f"FROM {staging_table} "  # nosec B608
        f"WHERE {id_col} = :est_id "  # nosec B608
        f"LIMIT 1"  # nosec B608
    )  # nosec B608

    with get_engine().connect() as conn:
        row = conn.execute(text(sql), {"est_id": est_id}).mappings().first()

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

    # ── Parallelism ───────────────────────────────────────────────────
    par_cfg = cfg.get("parallelism", {})
    folder_workers = int(par_cfg.get("folder_workers", 3))
    image_workers = int(par_cfg.get("image_workers", 10))
    ocr_rate_limit = int(par_cfg.get("ocr_rate_limit", 20))
    vlm_rate_limit = int(par_cfg.get("vlm_rate_limit", 100))
    max_inflight_images = int(par_cfg.get("max_inflight_images", 50))
    pool_size = max(folder_workers * image_workers, max_inflight_images)

    # ── Fetch EST record ──────────────────────────────────────────────
    try:
        record = _fetch_est_record(cfg["input_columns"], staging_table, est_id)
    except Exception as exc:
        logger.error(
            "Failed to query staging table for est_id=%s: %s",
            est_id,
            exc,
            exc_info=True,
        )
        return 2

    if not record:
        logger.error("No record found for est_id=%s — nothing to process.", est_id)
        return 1

    prefix = (record.get("prefix") or "").strip()
    claim_number = record.get("claim_number") or None
    vin = record.get("vin") or None
    license_plate = record.get("license_plate") or None
    odometer = record.get("odometer") or None

    if not prefix:
        logger.error("est_id=%s has an empty blob prefix — nothing to process.", est_id)
        return 1

    secrets = get_settings().model_dump()

    # ── Azure Blob Storage ────────────────────────────────────────────
    try:
        container_client = get_container_client(
            secrets["AZURE_BLOB_CONNECTION_STRING"],
            secrets["AZURE_CONTAINER_NAME"],
            pool_size=pool_size,
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
    ensure_vi_tables()
    db = DBWriter()

    # ── Process the folder ────────────────────────────────────────────
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
    result["claim_number"] = claim_number
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
