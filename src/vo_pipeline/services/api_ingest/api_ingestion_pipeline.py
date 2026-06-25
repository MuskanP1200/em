import logging
from concurrent.futures import ThreadPoolExecutor, as_completed


from settings import get_settings
from api_ingest.api_auth import get_token
from api_ingest.cvd_auth import get_cvd_token, CVDAuthError
from api_ingest.cvd_client import fetch_license_plates
from api_ingest.claims_client import get_css_token
from api_ingest.image_loader import upload_estimate_images, upload_claims_images
from estimate_matching.config import (
    AUTH_URL,
    API_MAX_RECORDS,
    API_MAX_WORKERS,
    API_IMAGE_WORKERS,
    API_INGEST_SCHEMA,
    API_INGEST_EST_RAW,
    API_INGEST_EST_LINE,
    API_INGEST_EST_SUBTOT,
    CVD_AUTH_URL,
    CVD_API_URL,
    CVD_BATCH_SIZE,
    CVD_CALLING_APP,
    CSS_API_URL,
)
from storage import get_container_client
from api_ingest.estimate_loader import (
    search_and_save_new_estimates,
    fetch_estimate_details,
    save_estimate_details,
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
    logger.info("VR Services token acquired")

    css_token: str | None = None
    try:
        css_token = get_css_token(
            username=creds["ICE_API_USER_NAME"],
            password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
            auth_url=AUTH_URL,
        )
        logger.info("CSS Claims token acquired")
    except Exception:
        logger.error(
            "CSS Claims authentication failed — claims images will be skipped",
            exc_info=True,
        )

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

    # ── 2. Fetch estimate details (no DB write yet — CVD must run first) ─────────
    est_line_df, subtot_df = fetch_estimate_details(
        token=token,
        est_ids_df=est_ids_df,
        schema=API_INGEST_SCHEMA,
        max_workers=API_MAX_WORKERS,
    )

    if est_line_df.empty:
        logger.error("No estimate data fetched — aborting downstream steps")
        return est_ids

    # ── 2b. CVD license plate enrichment ─────────────────────────────────────
    # Runs before saving est_line_df so the CVD plate is written to the DB.
    # If CVD fails the estimate XML plate already in est_line_df is kept as-is.
    cvd_plates: dict[str, str | None] = {}
    try:
        cvd_token = get_cvd_token(
            logon_id=creds["ICE_API_USER_NAME"],
            password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
            auth_url=CVD_AUTH_URL,
        )
        unique_vins = (
            est_line_df[["est_id", "vin"]]
            .drop_duplicates("est_id")["vin"]
            .dropna()
            .tolist()
        )
        cvd_plates = fetch_license_plates(
            token=cvd_token,
            vins=unique_vins,
            api_url=CVD_API_URL,
            calling_app=CVD_CALLING_APP,
            batch_size=CVD_BATCH_SIZE,
        )
    except CVDAuthError:
        logger.error(
            "CVD authentication failed — all estimates will use license plate from estimate XML",
            exc_info=True,
        )
    except Exception:
        logger.error(
            "CVD enrichment failed — all estimates will use license plate from estimate XML",
            exc_info=True,
        )

    # Apply CVD plates to est_line_df before saving to DB
    if cvd_plates:
        est_line_df["licplte_nbr"] = est_line_df.apply(
            lambda r: cvd_plates.get(r["vin"]) or r["licplte_nbr"], axis=1
        )
        logger.debug("CVD plates applied to est_line_df before DB save")

    # ── 2c. Save est_line + subtot (CVD plates now applied) ──────────────────
    save_estimate_details(
        est_line_df,
        subtot_df,
        table_names=(API_INGEST_EST_LINE, API_INGEST_EST_SUBTOT),
        schema=API_INGEST_SCHEMA,
    )

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
        .copy()
    )

    # ── 4. Upload VR images (bounded concurrency) ─────────────────────────────
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
                logger.error("est_id=%s: VR image upload failed", eid, exc_info=True)

    # ── 4b. Upload Claims images (bounded concurrency) ────────────────────────
    if css_token:
        claim_map = (
            est_ids_df[["est_id", "claim_number"]]
            .drop_duplicates("est_id")
            .set_index("est_id")["claim_number"]
            .to_dict()
        )

        with ThreadPoolExecutor(max_workers=API_MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    upload_claims_images,
                    css_token,
                    claim_map[eid],
                    eid,
                    container_client,
                    CSS_API_URL,
                    API_IMAGE_WORKERS,
                ): eid
                for eid in est_ids
                if claim_map.get(eid)
            }
            for fut in as_completed(futures):
                eid = futures[fut]
                try:
                    fut.result()
                except Exception:
                    logger.error(
                        "est_id=%s: Claims image upload failed", eid, exc_info=True
                    )
    else:
        logger.warning(
            "CSS token unavailable — Claims image upload skipped for all estimates"
        )

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
