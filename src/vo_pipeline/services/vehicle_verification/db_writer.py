"""
db_writer.py
____________________________________________

Database writer for the vehicle indicator pipeline.

Mirrors the structure of estimate_matching/db_writer.py:
  - DDL defined as module-level constants (_DDL_FOLDERS, _DDL_IMAGES)
  - ensure_vi_tables() / reset_vi_tables() as standalone functions
  - DBWriter class holds only upsert logic

Usage in vi_pipeline.py:
    from vehicle_verification.db_writer import DBWriter, ensure_vi_tables

    ensure_vi_tables()
    db = DBWriter()
    db.upsert_folder(result)   # called per-folder after process_est_prefix()
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import yaml
from sqlalchemy import text

from sql_connection import get_engine

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 500  # rows per bulk INSERT — keeps param count well under psycopg2's 65 535 limit

# ── Config ────────────────────────────────────────────────────────────────────

_cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
_vi_out = _cfg["tables"]["vi_output"]

OUTPUT_SCHEMA: str = _cfg["tables"]["schema"]
_FOLDERS_NAME: str = _vi_out["folders_table"]
_IMAGES_NAME: str  = _vi_out["images_table"]
FOLDERS_TABLE: str = f"{OUTPUT_SCHEMA}.{_FOLDERS_NAME}"
IMAGES_TABLE: str  = f"{OUTPUT_SCHEMA}.{_IMAGES_NAME}"

# ── Explicit DDL ──────────────────────────────────────────────────────────────
# Only drop the orphaned composite type when the TABLE does not exist.
# PostgreSQL creates a row type alongside every table; if the table was
# previously dropped without CASCADE the type lingers and blocks the next
# CREATE TABLE.  Dropping it unconditionally would fail when the table is
# still present (DependentObjectsStillExist).

_DDL_FOLDERS = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = '{OUTPUT_SCHEMA}' AND table_name = '{_FOLDERS_NAME}'
    ) THEN
        DROP TYPE IF EXISTS {OUTPUT_SCHEMA}.{_FOLDERS_NAME};
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS {FOLDERS_TABLE} (
    folder_name                         TEXT PRIMARY KEY,
    est_id                              TEXT,
    claim_number                        TEXT,
    folder_path                         TEXT,

    total_files                         INTEGER,
    images                              INTEGER,
    thumbnails                          INTEGER,
    images_excl_thumbs                  INTEGER,
    pdfs                                INTEGER,
    others                              INTEGER,
    count_images_with_text              INTEGER,
    count_images_without_text           INTEGER,

    vin_status                          BOOLEAN,
    plate_status                        BOOLEAN,
    odometer_status                     BOOLEAN,

    images_with_text                    JSONB,
    images_without_text                 JSONB,
    others_list                         JSONB,

    folder_wall_time_sec                DOUBLE PRECISION,
    az_vision_images_processed          INTEGER,
    az_vision_total_sec                 DOUBLE PRECISION,
    az_vision_avg_sec_per_image         DOUBLE PRECISION,
    az_vision_ocr_total_cost            DOUBLE PRECISION,
    az_vision_ocr_cost_currency         TEXT,
    vlm_images_classified               INTEGER,
    vlm_total_sec                       DOUBLE PRECISION,
    vlm_avg_sec_per_image               DOUBLE PRECISION,
    vlm_api_cost_total                  DOUBLE PRECISION,
    vlm_api_cost_currency               TEXT,

    count_images_with_vin_in_ocr        INTEGER,
    count_images_with_vin_in_vlm        INTEGER,
    est_best_match_vin                  TEXT,
    est_vin_min_mismatches              DOUBLE PRECISION,

    count_images_with_plate_in_ocr      INTEGER,
    count_images_with_plate_in_vlm      INTEGER,
    est_best_match_plate                TEXT,
    est_plate_min_mismatches            DOUBLE PRECISION,

    count_images_with_odometer_in_ocr   INTEGER,
    count_images_with_odometer_in_vlm   INTEGER,
    est_best_match_odometer             TEXT,
    est_odometer_min_mismatches         DOUBLE PRECISION,
    processed_at                        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    update_timestamp                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_DDL_IMAGES = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = '{OUTPUT_SCHEMA}' AND table_name = '{_IMAGES_NAME}'
    ) THEN
        DROP TYPE IF EXISTS {OUTPUT_SCHEMA}.{_IMAGES_NAME};
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS {IMAGES_TABLE} (
    folder_name                             TEXT NOT NULL,
    image_path                              TEXT NOT NULL,

    text_detected                           BOOLEAN,
    ocr_success                             BOOLEAN,
    error                                   TEXT,

    raw_ocr_text                            TEXT,
    extracted_text                          TEXT,
    classified_label                        TEXT,
    classified_confidence                   DOUBLE PRECISION,
    classification_error                    TEXT,

    vin_ocr_match                           BOOLEAN,
    vin_vlm_match                           BOOLEAN,
    best_match_vin_ocr                      TEXT,
    best_match_vin_vlm                      TEXT,
    ocr_vin_mismatch_count                  INTEGER,
    vlm_vin_mismatch_count                  INTEGER,
    vin_ocr_checksum_substitution_promoted  BOOLEAN,
    vin_ocr_checksum_substitution_pos       INTEGER,
    vin_vlm_checksum_substitution_promoted  BOOLEAN,
    vin_vlm_checksum_substitution_pos       INTEGER,

    plate_ocr_match                         BOOLEAN,
    plate_vlm_match                         BOOLEAN,
    best_match_plate_ocr                    TEXT,
    best_match_plate_vlm                    TEXT,
    plate_ocr_mismatch_count                INTEGER,
    plate_vlm_mismatch_count                INTEGER,

    odometer_ocr_match                      BOOLEAN,
    odometer_vlm_match                      BOOLEAN,
    best_match_odometer_ocr                 TEXT,
    best_match_odometer_vlm                 TEXT,
    odometer_ocr_mismatch_count             INTEGER,
    odometer_vlm_mismatch_count             INTEGER,

    az_vision_time_sec                      DOUBLE PRECISION,
    vlm_time_sec                            DOUBLE PRECISION,
    vlm_api_cost                            DOUBLE PRECISION,
    vlm_api_cost_currency                   TEXT,
    vlm_usage                               JSONB,
    image_json                              JSONB,
    processed_at                            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    update_timestamp                        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (folder_name, image_path)
)
"""

# ── Upsert SQL ────────────────────────────────────────────────────────────────

_UPSERT_FOLDER_SQL = f"""
INSERT INTO {FOLDERS_TABLE} (
    folder_name, est_id, claim_number, folder_path, total_files, images, thumbnails,
    images_excl_thumbs, pdfs, others, count_images_with_text,
    count_images_without_text, vin_status, plate_status, odometer_status,
    images_with_text, images_without_text, others_list,
    folder_wall_time_sec, az_vision_images_processed,
    az_vision_total_sec, az_vision_avg_sec_per_image,
    az_vision_ocr_total_cost, az_vision_ocr_cost_currency,
    vlm_images_classified, vlm_total_sec, vlm_avg_sec_per_image,
    vlm_api_cost_total, vlm_api_cost_currency,
    count_images_with_vin_in_ocr, count_images_with_vin_in_vlm,
    est_best_match_vin, est_vin_min_mismatches,
    count_images_with_plate_in_ocr, count_images_with_plate_in_vlm,
    est_best_match_plate, est_plate_min_mismatches,
    count_images_with_odometer_in_ocr, count_images_with_odometer_in_vlm,
    est_best_match_odometer, est_odometer_min_mismatches
)
VALUES (
    :folder_name, :est_id, :claim_number, :folder_path, :total_files, :images, :thumbnails,
    :images_excl_thumbs, :pdfs, :others, :count_images_with_text,
    :count_images_without_text, :vin_status, :plate_status, :odometer_status,
    :images_with_text, :images_without_text, :others_list,
    :folder_wall_time_sec, :az_vision_images_processed,
    :az_vision_total_sec, :az_vision_avg_sec_per_image,
    :az_vision_ocr_total_cost, :az_vision_ocr_cost_currency,
    :vlm_images_classified, :vlm_total_sec, :vlm_avg_sec_per_image,
    :vlm_api_cost_total, :vlm_api_cost_currency,
    :count_images_with_vin_in_ocr, :count_images_with_vin_in_vlm,
    :est_best_match_vin, :est_vin_min_mismatches,
    :count_images_with_plate_in_ocr, :count_images_with_plate_in_vlm,
    :est_best_match_plate, :est_plate_min_mismatches,
    :count_images_with_odometer_in_ocr, :count_images_with_odometer_in_vlm,
    :est_best_match_odometer, :est_odometer_min_mismatches
)
ON CONFLICT (folder_name) DO UPDATE SET
    est_id                              = EXCLUDED.est_id,
    claim_number                        = EXCLUDED.claim_number,
    folder_path                         = EXCLUDED.folder_path,
    total_files                         = EXCLUDED.total_files,
    images                              = EXCLUDED.images,
    thumbnails                          = EXCLUDED.thumbnails,
    images_excl_thumbs                  = EXCLUDED.images_excl_thumbs,
    pdfs                                = EXCLUDED.pdfs,
    others                              = EXCLUDED.others,
    count_images_with_text              = EXCLUDED.count_images_with_text,
    count_images_without_text           = EXCLUDED.count_images_without_text,
    vin_status                          = EXCLUDED.vin_status,
    plate_status                        = EXCLUDED.plate_status,
    odometer_status                     = EXCLUDED.odometer_status,
    images_with_text                    = EXCLUDED.images_with_text,
    images_without_text                 = EXCLUDED.images_without_text,
    others_list                         = EXCLUDED.others_list,
    folder_wall_time_sec                = EXCLUDED.folder_wall_time_sec,
    az_vision_images_processed          = EXCLUDED.az_vision_images_processed,
    az_vision_total_sec                 = EXCLUDED.az_vision_total_sec,
    az_vision_avg_sec_per_image         = EXCLUDED.az_vision_avg_sec_per_image,
    az_vision_ocr_total_cost            = EXCLUDED.az_vision_ocr_total_cost,
    az_vision_ocr_cost_currency         = EXCLUDED.az_vision_ocr_cost_currency,
    vlm_images_classified               = EXCLUDED.vlm_images_classified,
    vlm_total_sec                       = EXCLUDED.vlm_total_sec,
    vlm_avg_sec_per_image               = EXCLUDED.vlm_avg_sec_per_image,
    vlm_api_cost_total                  = EXCLUDED.vlm_api_cost_total,
    vlm_api_cost_currency               = EXCLUDED.vlm_api_cost_currency,
    count_images_with_vin_in_ocr        = EXCLUDED.count_images_with_vin_in_ocr,
    count_images_with_vin_in_vlm        = EXCLUDED.count_images_with_vin_in_vlm,
    est_best_match_vin                  = EXCLUDED.est_best_match_vin,
    est_vin_min_mismatches              = EXCLUDED.est_vin_min_mismatches,
    count_images_with_plate_in_ocr      = EXCLUDED.count_images_with_plate_in_ocr,
    count_images_with_plate_in_vlm      = EXCLUDED.count_images_with_plate_in_vlm,
    est_best_match_plate                = EXCLUDED.est_best_match_plate,
    est_plate_min_mismatches            = EXCLUDED.est_plate_min_mismatches,
    count_images_with_odometer_in_ocr   = EXCLUDED.count_images_with_odometer_in_ocr,
    count_images_with_odometer_in_vlm   = EXCLUDED.count_images_with_odometer_in_vlm,
    est_best_match_odometer             = EXCLUDED.est_best_match_odometer,
    est_odometer_min_mismatches         = EXCLUDED.est_odometer_min_mismatches,
    update_timestamp                    = NOW()
"""  # nosec B608


# ── Table management ──────────────────────────────────────────────────────────


def reset_vi_tables() -> None:
    """Drop and recreate both VI output tables. Use during development/testing."""
    with get_engine().begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {IMAGES_TABLE} CASCADE"))
        conn.execute(text(f"DROP TABLE IF EXISTS {FOLDERS_TABLE} CASCADE"))
        logger.info("Dropped VI output tables: %s, %s", FOLDERS_TABLE, IMAGES_TABLE)
    ensure_vi_tables()


def ensure_vi_tables() -> None:
    """Create VI output tables with explicit schema if they do not yet exist."""
    with get_engine().begin() as conn:
        conn.execute(text(_DDL_FOLDERS))
        conn.execute(text(_DDL_IMAGES))


# ── Helpers ───────────────────────────────────────────────────────────────────


def _py_native(val):
    """Convert numpy types to Python natives for SQLAlchemy."""
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        return float(val)
    if isinstance(val, np.bool_):
        return bool(val)
    return val


def _to_json(val) -> str | None:
    """Serialize a value to a JSON string for JSONB columns; None stays NULL."""
    if val is None:
        return None
    return json.dumps(val)


# ── DB Writer class ───────────────────────────────────────────────────────────


class DBWriter:
    """
    Database writer for the vehicle indicator pipeline.

    Uses the shared SQLAlchemy engine from sql_connection so connection
    pooling and credentials are managed in one place, consistent with
    the estimate_matching pipeline.

    Table names and DDL are defined as module-level constants above.
    This class handles only upsert logic.
    """

    def ensure_tables(self) -> None:
        """Create both output tables if they don't already exist. Call once at startup."""
        ensure_vi_tables()

    def upsert_folder(self, result: Dict[str, Any]) -> bool:
        """
        Upsert one folder result + its image details into the DB.

        Args:
            result: the dict returned by process_est_prefix()

        Returns:
            True if successful, False otherwise.
        """
        folder_name = result.get("folder_name")

        try:
            folder_row = self._build_folder_row(result)
            image_rows = self._build_image_rows(result)
        except Exception as e:
            logger.error(
                "DBWriter: failed to build rows for %s: %s", folder_name, e, exc_info=True
            )
            return False

        try:
            with get_engine().begin() as conn:
                conn.execute(text(_UPSERT_FOLDER_SQL), folder_row)
                _bulk_upsert_images(conn, image_rows)
            return True

        except Exception as e:
            logger.error(
                "DBWriter: upsert failed for %s: %s", folder_name, e, exc_info=True
            )
            return False

    def close(self) -> None:
        """No-op — connection lifecycle is managed by the shared SQLAlchemy engine."""

    # ------------------------------------------------------------------
    # Row builders — extract fields from process_est_prefix() result
    # ------------------------------------------------------------------

    @staticmethod
    def _build_folder_row(folder: Dict[str, Any]) -> Dict[str, Any]:
        """Transform a folder result dict into a row for the upsert SQL."""
        metrics = folder.get("metrics") or {}
        az_vis = metrics.get("az_vision") or {}
        vlm = metrics.get("vlm") or {}

        row = {
            "folder_name":                       folder.get("folder_name"),
            "est_id":                            folder.get("est_id"),
            "claim_number":                      folder.get("claim_number"),
            "folder_path":                       folder.get("folder_path"),
            "total_files":                       folder.get("total_files"),
            "images":                            folder.get("images"),
            "thumbnails":                        folder.get("thumbnails"),
            "images_excl_thumbs":                folder.get("images_excl_thumbs"),
            "pdfs":                              folder.get("pdfs"),
            "others":                            folder.get("others"),
            "count_images_with_text":            folder.get("count_images_with_text"),
            "count_images_without_text":         folder.get("count_images_without_text"),
            "vin_status":                        folder.get("vin_status"),
            "plate_status":                      folder.get("plate_status"),
            "odometer_status":                   folder.get("odometer_status"),
            "images_with_text":                  _to_json(folder.get("images_with_text")),
            "images_without_text":               _to_json(folder.get("images_without_text")),
            "others_list":                       _to_json(folder.get("others_list")),
            "folder_wall_time_sec":              metrics.get("folder_wall_time_sec"),
            "az_vision_images_processed":        az_vis.get("images_processed"),
            "az_vision_total_sec":               az_vis.get("total_sec"),
            "az_vision_avg_sec_per_image":       az_vis.get("avg_sec_per_image"),
            "az_vision_ocr_total_cost":          az_vis.get("ocr_cost_total"),
            "az_vision_ocr_cost_currency":       az_vis.get("ocr_cost_currency"),
            "vlm_images_classified":             vlm.get("images_classified"),
            "vlm_total_sec":                     vlm.get("total_sec"),
            "vlm_avg_sec_per_image":             vlm.get("avg_sec_per_image"),
            "vlm_api_cost_total":                vlm.get("api_cost_total"),
            "vlm_api_cost_currency":             vlm.get("api_cost_currency"),
            # VIN
            "count_images_with_vin_in_ocr":      folder.get("count_images_with_vin_in_ocr"),
            "count_images_with_vin_in_vlm":      folder.get("count_images_with_vin_in_vlm"),
            "est_best_match_vin":                folder.get("est_best_match_vin"),
            "est_vin_min_mismatches":            folder.get("est_vin_min_mismatches"),
            # Plate
            "count_images_with_plate_in_ocr":    folder.get("count_images_with_plate_in_ocr"),
            "count_images_with_plate_in_vlm":    folder.get("count_images_with_plate_in_vlm"),
            "est_best_match_plate":              folder.get("est_best_match_plate"),
            "est_plate_min_mismatches":          folder.get("est_plate_min_mismatches"),
            # Odometer
            "count_images_with_odometer_in_ocr": folder.get("count_images_with_odometer_in_ocr"),
            "count_images_with_odometer_in_vlm": folder.get("count_images_with_odometer_in_vlm"),
            "est_best_match_odometer":           folder.get("est_best_match_odometer"),
            "est_odometer_min_mismatches":       folder.get("est_odometer_min_mismatches"),
        }
        return {k: _py_native(v) for k, v in row.items()}

    @staticmethod
    def _build_image_rows(folder: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Transform image_details from a folder result into rows for the upsert SQL."""
        folder_name = folder.get("folder_name")
        rows = []

        for img in folder.get("image_details") or []:
            if not isinstance(img, dict):
                continue

            row_dict = {
                "folder_name":                              folder_name,
                "image_path":                               img.get("image_path"),
                "text_detected":                            img.get("text_detected"),
                "ocr_success":                              img.get("ocr_success"),
                "error":                                    img.get("error"),
                "raw_ocr_text":                             img.get("raw_ocr_text"),
                "extracted_text":                           img.get("extracted_text"),
                "classified_label":                         img.get("classified_label"),
                "classified_confidence":                    img.get("classified_confidence"),
                "classification_error":                     img.get("classification_error"),
                # VIN
                "vin_ocr_match":                            img.get("vin_ocr_match"),
                "vin_vlm_match":                            img.get("vin_vlm_match"),
                "best_match_vin_ocr":                       img.get("best_match_vin_ocr"),
                "best_match_vin_vlm":                       img.get("best_match_vin_vlm"),
                "ocr_vin_mismatch_count":                   img.get("ocr_vin_mismatch_count"),
                "vlm_vin_mismatch_count":                   img.get("vlm_vin_mismatch_count"),
                "vin_ocr_checksum_substitution_promoted":   img.get("vin_ocr_checksum_substitution_promoted"),
                "vin_ocr_checksum_substitution_pos":        img.get("vin_ocr_checksum_substitution_pos"),
                "vin_vlm_checksum_substitution_promoted":   img.get("vin_vlm_checksum_substitution_promoted"),
                "vin_vlm_checksum_substitution_pos":        img.get("vin_vlm_checksum_substitution_pos"),
                # Plate
                "plate_ocr_match":                          img.get("plate_ocr_match"),
                "plate_vlm_match":                          img.get("plate_vlm_match"),
                "best_match_plate_ocr":                     img.get("best_match_plate_ocr"),
                "best_match_plate_vlm":                     img.get("best_match_plate_vlm"),
                "plate_ocr_mismatch_count":                 img.get("plate_ocr_mismatch_count"),
                "plate_vlm_mismatch_count":                 img.get("plate_vlm_mismatch_count"),
                # Odometer
                "odometer_ocr_match":                       img.get("odometer_ocr_match"),
                "odometer_vlm_match":                       img.get("odometer_vlm_match"),
                "best_match_odometer_ocr":                  img.get("best_match_odometer_ocr"),
                "best_match_odometer_vlm":                  img.get("best_match_odometer_vlm"),
                "odometer_ocr_mismatch_count":              img.get("odometer_ocr_mismatch_count"),
                "odometer_vlm_mismatch_count":              img.get("odometer_vlm_mismatch_count"),
                # Timings / cost
                "az_vision_time_sec":                       img.get("az_vision_time_sec"),
                "vlm_time_sec":                             img.get("vlm_time_sec"),
                "vlm_api_cost":                             img.get("vlm_api_cost"),
                "vlm_api_cost_currency":                    img.get("vlm_api_cost_currency"),
                # JSONB
                "vlm_usage":                                _to_json(img.get("vlm_usage")),
                "image_json":                               _to_json(img),
            }
            rows.append({k: _py_native(v) for k, v in row_dict.items()})

        return rows


# ── Bulk image upsert (module-level — no instance state needed) ───────────────


def _bulk_upsert_images(conn, image_rows: List[Dict[str, Any]]) -> None:
    """
    Insert/update image rows in a single multi-row VALUES statement per chunk.

    Builds:  INSERT INTO t (c1, c2, ...) VALUES (:c1_0, :c2_0, ...), (:c1_1, ...)
             ON CONFLICT ON CONSTRAINT <pk> DO UPDATE SET ...

    One round trip per _CHUNK_SIZE rows instead of one per row.
    """
    if not image_rows:
        return

    cols = list(image_rows[0].keys())
    col_list = ", ".join(cols)
    update_cols = [c for c in cols if c not in ("folder_name", "image_path")]
    set_clause = (
        ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        + ", update_timestamp = NOW()"
    )

    for offset in range(0, len(image_rows), _CHUNK_SIZE):
        chunk = image_rows[offset : offset + _CHUNK_SIZE]

        placeholders: list[str] = []
        params: dict[str, Any] = {}
        for i, row in enumerate(chunk):
            placeholders.append(f"({', '.join(f':{c}_{i}' for c in cols)})")
            for col in cols:
                params[f"{col}_{i}"] = row[col]

        sql = text(  # nosec B608
            f"INSERT INTO {IMAGES_TABLE} ({col_list}) "
            f"VALUES {', '.join(placeholders)} "
            f"ON CONFLICT (folder_name, image_path) DO UPDATE SET {set_clause}"
        )
        conn.execute(sql, params)
