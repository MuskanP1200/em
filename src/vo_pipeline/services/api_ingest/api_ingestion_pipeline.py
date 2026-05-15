import logging
import sys
from pathlib import Path

_SERVICES = Path(__file__).resolve().parent.parent
_API = _SERVICES.parent
sys.path.insert(0, str(_API))  # api/  — for settings, sql_connection
sys.path.insert(0, str(_SERVICES))  # api/services/ — for api_auth, api_loader, etc.

from settings import get_settings  # noqa: E402
from api_ingest.api_auth import get_token  # noqa: E402
from api_ingest.image_loader import upload_estimate_images  # noqa: E402
from estimate_matching.config import (  # noqa: E402
    AUTH_URL,
    API_MAX_RECORDS,
    API_MAX_WORKERS,
    API_INGEST_SCHEMA,
    API_INGEST_EST_RAW,
    API_INGEST_EST_LINE,
    API_INGEST_EST_SUBTOT,
)
from storage import get_container_client  # noqa: E402
from api_ingest.estimate_loader import (  # noqa: E402
    search_and_save_new_estimates,  # noqa: E402
    fetch_estimate_details,  # noqa: E402
)  # noqa: E402
from api_ingest.db_staging import ensure_staging_tables  # noqa: E402
from sql_connection import update_rows  # noqa: E402

logger = logging.getLogger(__name__)

# Build one record per est_id with the vehicle fields from the XML parse


def run_api_ingestion_pipeline() -> list[str]:
    logger.info("=== API ingest pipeline start ===")
    ensure_staging_tables()

    creds = get_settings().model_dump()
    token = get_token(
        username=creds["ICE_API_USER_NAME"],
        password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
        auth_url=AUTH_URL,
    )
    logger.info("Token acquired.")

    container_client = get_container_client(
        creds["AZURE_BLOB_CONNECTION_STRING"],
        creds["AZURE_API_IMAGES_CONTAINER_NAME"],
    )

    # ── 1. Search estimates → save new rows to ice.api_estimates_raw ──────────
    est_ids_df = search_and_save_new_estimates(
        token,
        max_records=API_MAX_RECORDS,
        table=API_INGEST_EST_RAW,
        schema=API_INGEST_SCHEMA,
    )  # api_estimate_raw is created

    est_ids = est_ids_df["est_id"].dropna().to_list()

    if not est_ids:
        logger.info("No new estimates found. Pipeline complete.")
        return []

    logger.info("%d new estimate(s) to process.", len(est_ids))

    # ── 2. Fetch XML + CDR rates → save to ice.api_est_line / ice.api_est_subtot
    est_line_df, subtot_df = (
        fetch_estimate_details(  # ice.api_est_line / ice.api_est_subtot are crearted
            token=token,
            est_ids_df=est_ids_df,
            table_names=(API_INGEST_EST_LINE, API_INGEST_EST_SUBTOT),
            schema=API_INGEST_SCHEMA,
            max_workers=API_MAX_WORKERS,
        )
    )
    logger.info(
        "Estimate data loaded: %d line rows, %d subtotal rows.",
        len(est_line_df),
        len(subtot_df),
    )

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
        .assign(folder_prefix=lambda df: df["est_id"].map(lambda x: f"EST{int(x)}/"))
    )

    # ── 3. Fetch + upload images for each new estimate ────────────────────────
    for est_id in est_ids:
        est_id = int(est_id)
        try:
            saved = upload_estimate_images(
                token=token,
                est_id=est_id,
                container_client=container_client,
            )
            logger.info("est_id %s: %d image(s) uploaded to blob.", est_id, len(saved))
        except Exception as exc:
            logger.error("est_id %s: image upload failed — %s", est_id, exc)

    update_rows(
        schema=API_INGEST_SCHEMA,
        table=API_INGEST_EST_RAW,
        rows=vehicle_map.to_dict(orient="records"),
    )

    logger.info("=== API ingest pipeline complete ===")

    return est_ids


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    est_ids = run_api_ingestion_pipeline()
