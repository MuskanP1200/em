import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from settings import get_settings
from api_ingest.api_auth import get_token
from api_ingest.image_loader import upload_estimate_images
from estimate_matching.config import (
    AUTH_URL,
    API_MAX_RECORDS,
    API_MAX_WORKERS,
    API_IMAGE_WORKERS,
    API_INGEST_SCHEMA,
    API_INGEST_EST_RAW,
    API_INGEST_EST_LINE,
    API_INGEST_EST_SUBTOT,
)
from storage import get_container_client
from api_ingest.estimate_loader import (
    search_and_save_new_estimates,
    fetch_estimate_details,
)
from api_ingest.db_staging import ensure_staging_tables
from sql_connection import update_rows

logger = logging.getLogger(__name__)


def run_api_ingestion_pipeline() -> list[str]:
    logger.info("=== API ingest pipeline start ===")

    ensure_staging_tables()

    # ── Auth ─────────────────────────────────────────────────────────────────
    creds = get_settings().model_dump()

    token = get_token(
        username=creds["ICE_API_USER_NAME"],
        password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
        auth_url=AUTH_URL,
    )
    logger.info("Token acquired")

    container_client = get_container_client(
        creds["AZURE_BLOB_CONNECTION_STRING"],
        creds["AZURE_CONTAINER_NAME"],
    )

    # ── 1. Search + ingest ────────────────────────────────────────────────────
    est_ids_df = search_and_save_new_estimates(
        token,
        max_records=API_MAX_RECORDS,
        table=API_INGEST_EST_RAW,
        schema=API_INGEST_SCHEMA,
    )

    if est_ids_df.empty:
        logger.info("No new estimates found. Pipeline complete.")
        return []

    est_ids = est_ids_df["est_id"].dropna().tolist()
    logger.info("Processing %d new estimates", len(est_ids))

    # ── 2. Fetch estimate details ─────────────────────────────────────────────
    est_line_df, subtot_df = fetch_estimate_details(
        token=token,
        est_ids_df=est_ids_df,
        table_names=(API_INGEST_EST_LINE, API_INGEST_EST_SUBTOT),
        schema=API_INGEST_SCHEMA,
        max_workers=API_MAX_WORKERS,
    )

    if est_line_df.empty:
        logger.error("No estimate data fetched — aborting downstream steps")
        return est_ids

    # ── 3. Prepare vehicle map ────────────────────────────────────────────────
    vehicle_map = (
        est_line_df[
            [
                "est_id",
                "created_date",
                "dmg_dsc",
                "vin",
                "licplte_nbr",
                "odmtr_nbr",
                "veh_make",
                "veh_model",
                "veh_color",
                "veh_year",
            ]
        ]
        .drop_duplicates(subset="est_id")
        .assign(folder_prefix=lambda df: df["est_id"].map(lambda x: f"EST{x}/"))
    )

    # ── 4. Upload images (bounded concurrency) ────────────────────────────────
    with ThreadPoolExecutor(max_workers=API_MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                upload_estimate_images,
                token,
                eid,
                container_client,
                API_IMAGE_WORKERS,
            ): eid
            for eid in est_ids
        }
        for fut in as_completed(futures):
            eid = futures[fut]
            try:
                fut.result()
            except Exception:
                logger.error("est_id %s: image upload failed", eid, exc_info=True)

    # ── 5. Update metadata ────────────────────────────────────────────────────
    update_rows(
        schema=API_INGEST_SCHEMA,
        table=API_INGEST_EST_RAW,
        rows=vehicle_map.to_dict(orient="records"),
    )

    logger.info("=== API ingest pipeline complete ===")

    return est_ids


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    run_api_ingestion_pipeline()
