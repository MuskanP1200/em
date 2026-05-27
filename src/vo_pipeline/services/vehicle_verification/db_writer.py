# ================================================================================
# FILE: db_writer.py
# ================================================================================
"""
db_writer.py
____________________________________________

Thread-safe database writer for the vehicle indicator pipeline.

Table names and constraint names are passed in at construction time
from the pipeline config — nothing is hard-coded here.

Usage in vi_pipeline_main.py:
    from db_writer import DBWriter

    db = DBWriter(
        pg_cfg=pg_cfg,
        folders_table=out_cfg["folders_table"],
        images_table=out_cfg["images_table"],
    )
    db.ensure_tables()

    db.upsert_folder(result)   # called per-folder after process_est_prefix()

    db.close()
"""

import logging
import threading
from typing import Any, Dict, List

import numpy as np
import psycopg2
import psycopg2.errors
import psycopg2.extras
from psycopg2.extras import Json

logger = logging.getLogger(__name__)


def _py_native(val):
    """Convert numpy types to Python natives for psycopg2."""
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        return float(val)
    if isinstance(val, np.bool_):
        return bool(val)
    return val


# ----------------------------------------------------------------------
# DB WRITER CLASS
# ----------------------------------------------------------------------


class DBWriter:
    """
    Thread-safe database writer for the vehicle indicator pipeline.

    Designed to be instantiated once in the entry point and called
    from within folder-processing threads.  All writes are serialised
    via an internal lock so psycopg2's connection is not accessed
    concurrently.

    Table names come from the YAML config so the same code can target
    different schemas without code changes.
    """

    def __init__(
        self,
        pg_cfg: Dict[str, Any],
        folders_table: str,
        images_table: str,
    ):
        self._folders_table = folders_table
        self._images_table = images_table
        _base = images_table.split(".")[-1]
        self._pk_constraint_name = f"{_base}_pk"
        self._fk_constraint_name = f"{_base}_folders_fk"

        self._lock = threading.Lock()
        self._conn = None
        self._connect(pg_cfg)

        # Build all SQL strings once, using the configured table names
        self._create_folders_sql = self._make_create_folders_sql()  # nosec B608
        self._create_images_sql = self._make_create_images_sql()  # nosec B608
        self._upsert_folder_sql = self._make_upsert_folder_sql()  # nosec B608
        self._upsert_image_sql = self._make_upsert_image_sql()  # nosec B608

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self, pg_cfg: Dict[str, Any]) -> None:
        try:
            self._conn = psycopg2.connect(
                host=pg_cfg.get("host", ""),
                port=int(pg_cfg.get("port", 5432)),
                dbname=pg_cfg.get("dbname", ""),
                user=pg_cfg.get("user", ""),
                password=pg_cfg.get("password", ""),
            )
            self._conn.autocommit = False
        except Exception as e:
            logger.error("DBWriter failed to connect to Postgres: %s", e)
            self._conn = None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    # ------------------------------------------------------------------
    # SQL builders (called once in __init__)
    # ------------------------------------------------------------------

    def _make_create_folders_sql(self) -> str:
        t = self._folders_table
        type_name = t.split(".")[-1]
        schema    = t.split(".")[0] if "." in t else "public"
        # Only drop the orphaned composite type when the TABLE does not exist.
        # PostgreSQL creates a row type alongside every table; if the table was
        # previously dropped without CASCADE the type lingers and blocks the next
        # CREATE TABLE.  Dropping it unconditionally would fail when the table is
        # still present (DependentObjectsStillExist).
        return f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = '{schema}' AND table_name = '{type_name}'
    ) THEN
        DROP TYPE IF EXISTS {schema}.{type_name};
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS {t} (
    folder_name                         TEXT PRIMARY KEY,
    est_id                              TEXT,
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
    processed_at                        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

    def _make_create_images_sql(self) -> str:
        t = self._images_table
        ft = self._folders_table
        pk = self._pk_constraint_name
        fk = self._fk_constraint_name
        type_name = t.split(".")[-1]
        schema    = t.split(".")[0] if "." in t else "public"
        return f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = '{schema}' AND table_name = '{type_name}'
    ) THEN
        DROP TYPE IF EXISTS {schema}.{type_name};
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS {t} (
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

    CONSTRAINT {fk}
        FOREIGN KEY (folder_name) REFERENCES {ft}(folder_name)
        ON DELETE CASCADE,

    CONSTRAINT {pk} PRIMARY KEY (folder_name, image_path)
);
"""

    def _make_upsert_folder_sql(self) -> str:
        t = self._folders_table  # nosec B608
        return f""" 
INSERT INTO {t} (
    folder_name, est_id, folder_path, total_files, images, thumbnails,
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
    %(folder_name)s, %(est_id)s, %(folder_path)s, %(total_files)s, %(images)s, %(thumbnails)s,
    %(images_excl_thumbs)s, %(pdfs)s, %(others)s, %(count_images_with_text)s,
    %(count_images_without_text)s, %(vin_status)s, %(plate_status)s, %(odometer_status)s,
    %(images_with_text)s, %(images_without_text)s, %(others_list)s,
    %(folder_wall_time_sec)s, %(az_vision_images_processed)s,
    %(az_vision_total_sec)s, %(az_vision_avg_sec_per_image)s,
    %(az_vision_ocr_total_cost)s, %(az_vision_ocr_cost_currency)s,
    %(vlm_images_classified)s, %(vlm_total_sec)s, %(vlm_avg_sec_per_image)s,
    %(vlm_api_cost_total)s, %(vlm_api_cost_currency)s,
    %(count_images_with_vin_in_ocr)s, %(count_images_with_vin_in_vlm)s,
    %(est_best_match_vin)s, %(est_vin_min_mismatches)s,
    %(count_images_with_plate_in_ocr)s, %(count_images_with_plate_in_vlm)s,
    %(est_best_match_plate)s, %(est_plate_min_mismatches)s,
    %(count_images_with_odometer_in_ocr)s, %(count_images_with_odometer_in_vlm)s,
    %(est_best_match_odometer)s, %(est_odometer_min_mismatches)s
)
ON CONFLICT (folder_name) DO UPDATE SET
    est_id                              = EXCLUDED.est_id,
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
    est_odometer_min_mismatches         = EXCLUDED.est_odometer_min_mismatches;
"""  # nosec B608  # nosec B608

    def _make_upsert_image_sql(self) -> str:
        t = self._images_table
        pk = self._pk_constraint_name
        return f""" 
INSERT INTO {t} (
    folder_name, image_path,
    text_detected, ocr_success, error,
    raw_ocr_text, extracted_text, classified_label, classified_confidence,
    classification_error,
    vin_ocr_match, vin_vlm_match, best_match_vin_ocr, best_match_vin_vlm,
    ocr_vin_mismatch_count, vlm_vin_mismatch_count,
    vin_ocr_checksum_substitution_promoted, vin_ocr_checksum_substitution_pos,
    vin_vlm_checksum_substitution_promoted, vin_vlm_checksum_substitution_pos,
    plate_ocr_match, plate_vlm_match, best_match_plate_ocr, best_match_plate_vlm,
    plate_ocr_mismatch_count, plate_vlm_mismatch_count,
    odometer_ocr_match, odometer_vlm_match, best_match_odometer_ocr, best_match_odometer_vlm,
    odometer_ocr_mismatch_count, odometer_vlm_mismatch_count,
    az_vision_time_sec, vlm_time_sec, vlm_api_cost, vlm_api_cost_currency,
    vlm_usage, image_json
)
VALUES (
    %(folder_name)s, %(image_path)s,
    %(text_detected)s, %(ocr_success)s, %(error)s,
    %(raw_ocr_text)s, %(extracted_text)s, %(classified_label)s, %(classified_confidence)s,
    %(classification_error)s,
    %(vin_ocr_match)s, %(vin_vlm_match)s, %(best_match_vin_ocr)s, %(best_match_vin_vlm)s,
    %(ocr_vin_mismatch_count)s, %(vlm_vin_mismatch_count)s,
    %(vin_ocr_checksum_substitution_promoted)s, %(vin_ocr_checksum_substitution_pos)s,
    %(vin_vlm_checksum_substitution_promoted)s, %(vin_vlm_checksum_substitution_pos)s,
    %(plate_ocr_match)s, %(plate_vlm_match)s, %(best_match_plate_ocr)s, %(best_match_plate_vlm)s,
    %(plate_ocr_mismatch_count)s, %(plate_vlm_mismatch_count)s,
    %(odometer_ocr_match)s, %(odometer_vlm_match)s, %(best_match_odometer_ocr)s, %(best_match_odometer_vlm)s,
    %(odometer_ocr_mismatch_count)s, %(odometer_vlm_mismatch_count)s,
    %(az_vision_time_sec)s, %(vlm_time_sec)s, %(vlm_api_cost)s, %(vlm_api_cost_currency)s,
    %(vlm_usage)s, %(image_json)s
)
ON CONFLICT ON CONSTRAINT {pk} DO UPDATE SET
    text_detected                           = EXCLUDED.text_detected,
    ocr_success                             = EXCLUDED.ocr_success,
    error                                   = EXCLUDED.error,
    raw_ocr_text                            = EXCLUDED.raw_ocr_text,
    extracted_text                          = EXCLUDED.extracted_text,
    classified_label                        = EXCLUDED.classified_label,
    classified_confidence                   = EXCLUDED.classified_confidence,
    classification_error                    = EXCLUDED.classification_error,
    vin_ocr_match                           = EXCLUDED.vin_ocr_match,
    vin_vlm_match                           = EXCLUDED.vin_vlm_match,
    best_match_vin_ocr                      = EXCLUDED.best_match_vin_ocr,
    best_match_vin_vlm                      = EXCLUDED.best_match_vin_vlm,
    ocr_vin_mismatch_count                  = EXCLUDED.ocr_vin_mismatch_count,
    vlm_vin_mismatch_count                  = EXCLUDED.vlm_vin_mismatch_count,
    vin_ocr_checksum_substitution_promoted  = EXCLUDED.vin_ocr_checksum_substitution_promoted,
    vin_ocr_checksum_substitution_pos       = EXCLUDED.vin_ocr_checksum_substitution_pos,
    vin_vlm_checksum_substitution_promoted  = EXCLUDED.vin_vlm_checksum_substitution_promoted,
    vin_vlm_checksum_substitution_pos       = EXCLUDED.vin_vlm_checksum_substitution_pos,
    plate_ocr_match                         = EXCLUDED.plate_ocr_match,
    plate_vlm_match                         = EXCLUDED.plate_vlm_match,
    best_match_plate_ocr                    = EXCLUDED.best_match_plate_ocr,
    best_match_plate_vlm                    = EXCLUDED.best_match_plate_vlm,
    plate_ocr_mismatch_count               = EXCLUDED.plate_ocr_mismatch_count,
    plate_vlm_mismatch_count               = EXCLUDED.plate_vlm_mismatch_count,
    odometer_ocr_match                      = EXCLUDED.odometer_ocr_match,
    odometer_vlm_match                      = EXCLUDED.odometer_vlm_match,
    best_match_odometer_ocr                 = EXCLUDED.best_match_odometer_ocr,
    best_match_odometer_vlm                 = EXCLUDED.best_match_odometer_vlm,
    odometer_ocr_mismatch_count             = EXCLUDED.odometer_ocr_mismatch_count,
    odometer_vlm_mismatch_count             = EXCLUDED.odometer_vlm_mismatch_count,
    az_vision_time_sec                      = EXCLUDED.az_vision_time_sec,
    vlm_time_sec                            = EXCLUDED.vlm_time_sec,
    vlm_api_cost                            = EXCLUDED.vlm_api_cost,
    vlm_api_cost_currency                   = EXCLUDED.vlm_api_cost_currency,
    vlm_usage                               = EXCLUDED.vlm_usage,
    image_json                              = EXCLUDED.image_json;
"""  # nosec B608

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_tables(self) -> None:
        """Create both output tables if they don't already exist. Call once at startup."""
        if not self.is_connected:
            logger.warning("DBWriter: not connected, skipping table creation")
            return
        with self._lock:
            try:
                with self._conn:
                    with self._conn.cursor() as cur:
                        cur.execute(self._create_folders_sql)
                        cur.execute(self._create_images_sql)
            except Exception as e:
                logger.error("DBWriter: table creation failed: %s", e, exc_info=True)

    def upsert_folder(self, result: Dict[str, Any]) -> bool:
        """
        Upsert one folder result + its image details into the DB.

        Args:
            result: the dict returned by process_est_prefix()

        Returns:
            True if successful, False otherwise.
        """
        if not self.is_connected:
            logger.warning(
                "DBWriter: not connected, skipping upsert for %s",
                result.get("folder_name", "?"),
            )
            return False

        folder_name = result.get("folder_name")

        try:
            folder_row = self._build_folder_row(result)
            image_rows = self._build_image_rows(result)
        except Exception as e:
            logger.error(
                "DBWriter: failed to build rows for %s: %s",
                folder_name,
                e,
                exc_info=True,
            )
            return False

        with self._lock:
            try:
                with self._conn:
                    with self._conn.cursor() as cur:
                        # Upsert folder first (FK parent)
                        cur.execute(self._upsert_folder_sql, folder_row)

                        # Upsert images in batch
                        if image_rows:
                            psycopg2.extras.execute_batch(
                                cur, self._upsert_image_sql, image_rows, page_size=500
                            )

                return True

            except Exception as e:
                logger.error(
                    "DBWriter: upsert failed for %s: %s", folder_name, e, exc_info=True
                )
                try:
                    self._conn.rollback()
                except Exception as rollback_err:
                    logger.warning("DBWriter: rollback failed: %s", rollback_err)
                return False

    def close(self) -> None:
        """Close the DB connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()

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
            "folder_name": folder.get("folder_name"),
            "est_id": folder.get("est_id"),
            "folder_path": folder.get("folder_path"),
            "total_files": folder.get("total_files"),
            "images": folder.get("images"),
            "thumbnails": folder.get("thumbnails"),
            "images_excl_thumbs": folder.get("images_excl_thumbs"),
            "pdfs": folder.get("pdfs"),
            "others": folder.get("others"),
            "count_images_with_text": folder.get("count_images_with_text"),
            "count_images_without_text": folder.get("count_images_without_text"),
            "vin_status": folder.get("vin_status"),
            "plate_status": folder.get("plate_status"),
            "odometer_status": folder.get("odometer_status"),
            "images_with_text": Json(folder.get("images_with_text")),
            "images_without_text": Json(folder.get("images_without_text")),
            "others_list": Json(folder.get("others_list")),
            "folder_wall_time_sec": metrics.get("folder_wall_time_sec"),
            "az_vision_images_processed": az_vis.get("images_processed"),
            "az_vision_total_sec": az_vis.get("total_sec"),
            "az_vision_avg_sec_per_image": az_vis.get("avg_sec_per_image"),
            "az_vision_ocr_total_cost": az_vis.get("ocr_cost_total"),
            "az_vision_ocr_cost_currency": az_vis.get("ocr_cost_currency"),
            "vlm_images_classified": vlm.get("images_classified"),
            "vlm_total_sec": vlm.get("total_sec"),
            "vlm_avg_sec_per_image": vlm.get("avg_sec_per_image"),
            "vlm_api_cost_total": vlm.get("api_cost_total"),
            "vlm_api_cost_currency": vlm.get("api_cost_currency"),
            # VIN
            "count_images_with_vin_in_ocr": folder.get("count_images_with_vin_in_ocr"),
            "count_images_with_vin_in_vlm": folder.get("count_images_with_vin_in_vlm"),
            "est_best_match_vin": folder.get("est_best_match_vin"),
            "est_vin_min_mismatches": folder.get("est_vin_min_mismatches"),
            # Plate
            "count_images_with_plate_in_ocr": folder.get(
                "count_images_with_plate_in_ocr"
            ),
            "count_images_with_plate_in_vlm": folder.get(
                "count_images_with_plate_in_vlm"
            ),
            "est_best_match_plate": folder.get("est_best_match_plate"),
            "est_plate_min_mismatches": folder.get("est_plate_min_mismatches"),
            # Odometer
            "count_images_with_odometer_in_ocr": folder.get(
                "count_images_with_odometer_in_ocr"
            ),
            "count_images_with_odometer_in_vlm": folder.get(
                "count_images_with_odometer_in_vlm"
            ),
            "est_best_match_odometer": folder.get("est_best_match_odometer"),
            "est_odometer_min_mismatches": folder.get("est_odometer_min_mismatches"),
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
                "folder_name": folder_name,
                "image_path": img.get("image_path"),
                "text_detected": img.get("text_detected"),
                "ocr_success": img.get("ocr_success"),
                "error": img.get("error"),
                "raw_ocr_text": img.get("raw_ocr_text"),
                "extracted_text": img.get("extracted_text"),
                "classified_label": img.get("classified_label"),
                "classified_confidence": img.get("classified_confidence"),
                "classification_error": img.get("classification_error"),
                # VIN
                "vin_ocr_match": img.get("vin_ocr_match"),
                "vin_vlm_match": img.get("vin_vlm_match"),
                "best_match_vin_ocr": img.get("best_match_vin_ocr"),
                "best_match_vin_vlm": img.get("best_match_vin_vlm"),
                "ocr_vin_mismatch_count": img.get("ocr_vin_mismatch_count"),
                "vlm_vin_mismatch_count": img.get("vlm_vin_mismatch_count"),
                "vin_ocr_checksum_substitution_promoted": img.get(
                    "vin_ocr_checksum_substitution_promoted"
                ),
                "vin_ocr_checksum_substitution_pos": img.get(
                    "vin_ocr_checksum_substitution_pos"
                ),
                "vin_vlm_checksum_substitution_promoted": img.get(
                    "vin_vlm_checksum_substitution_promoted"
                ),
                "vin_vlm_checksum_substitution_pos": img.get(
                    "vin_vlm_checksum_substitution_pos"
                ),
                # Plate
                "plate_ocr_match": img.get("plate_ocr_match"),
                "plate_vlm_match": img.get("plate_vlm_match"),
                "best_match_plate_ocr": img.get("best_match_plate_ocr"),
                "best_match_plate_vlm": img.get("best_match_plate_vlm"),
                "plate_ocr_mismatch_count": img.get("plate_ocr_mismatch_count"),
                "plate_vlm_mismatch_count": img.get("plate_vlm_mismatch_count"),
                # Odometer
                "odometer_ocr_match": img.get("odometer_ocr_match"),
                "odometer_vlm_match": img.get("odometer_vlm_match"),
                "best_match_odometer_ocr": img.get("best_match_odometer_ocr"),
                "best_match_odometer_vlm": img.get("best_match_odometer_vlm"),
                "odometer_ocr_mismatch_count": img.get("odometer_ocr_mismatch_count"),
                "odometer_vlm_mismatch_count": img.get("odometer_vlm_mismatch_count"),
                # Timings / cost
                "az_vision_time_sec": img.get("az_vision_time_sec"),
                "vlm_time_sec": img.get("vlm_time_sec"),
                "vlm_api_cost": img.get("vlm_api_cost"),
                "vlm_api_cost_currency": img.get("vlm_api_cost_currency"),
                # JSONB
                "vlm_usage": Json(img.get("vlm_usage")),
                "image_json": Json(img),
            }
            rows.append({k: _py_native(v) for k, v in row_dict.items()})

        return rows
