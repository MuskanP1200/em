"""
CDR Assistant — FastAPI backend
Run: uvicorn api.main:app --reload --port 8018
"""

import asyncio
import logging
import time
from typing import Annotated, List, Optional

import asyncpg
from azure.storage.blob import BlobServiceClient
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.api_logging.config_logging import configure_logging
from api.models.incident_schemas import (
    FeedbackRequest,
    IncidentDetailOut,
    IncidentListItem,
)
from api.models.schemas import (
    HealthResponse,
    # ClassifiedBreakdown,
    # EstInfoOut,
    # FolderOut,
    # ImageListOut,
    # VLMStatsOut,
)
from api.services.db import get_blob_service, get_db_pool, lifespan
from api.services.incident_orchestrator import (
    DBQueryError,
    fetch_incident_detail,
    fetch_incident_list,
    save_feedback,
)

# from api.services.orchestrator import (
#     build_image_details,
#     fetch_discount,
#     fetch_est_info,
#     fetch_folders,
#     fetch_images,
#     fetch_parts,
#     fetch_vlm_stats,
# )
from api.settings import get_settings

# ─────────────────────────────────────────────────────────────────

settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CDR Assistant",
    version="2.0.0",
    docs_url="/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # allow_origins=settings.ALLOWED_ORIGINS,   # TODO: Discuss with Adrien
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

DBPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
BlobService = Annotated[BlobServiceClient, Depends(get_blob_service)]

VALID_STATUS_FILTERS = {"all", "ai_approved", "ai_flagged", "pending_ai_review"}


# ── Global error handlers ────────────────────────────────────────


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    logger.warning(
        f"Message='Validation error' Path={request.url.path} "
        f"Errors={exc.errors()} AppName={settings.APP_NAME}"
    )
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": exc.body},
    )


@app.exception_handler(DBQueryError)
async def handle_db_error(request: Request, exc: DBQueryError):
    logger.error(
        f"Message='Postgres query error' Path={request.url.path} "
        f"ErrorDetail='{exc}' AppName={settings.APP_NAME}"
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "Database error. Please try again."},
    )


# ── Request logging middleware ───────────────────────────────────


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = int((time.time() - start_time) * 1000)
    logger.info(
        f"HttpStatus={response.status_code} "
        f"DurationMillis={process_time} "
        f"RequestTarget={request.url.path} "
        f"AppName={settings.APP_NAME}"
    )
    return response


# ── Auth ─────────────────────────────────────────────────────────


async def verify_entra_token(
    authorization: Annotated[Optional[str], Header()] = None,
) -> dict:
    """
    Validates the Entra ID Bearer token from the frontend.

    TODO: Replace placeholder with real JWT validation:
      1. Fetch JWKS: https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys
      2. Decode: jwt.decode(token, key, algorithms=["RS256"], audience=settings.AZURE_CLIENT_ID)
      3. Verify claims: aud, iss, exp, scp
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Empty token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # PLACEHOLDER — swap out once Entra ID is wired in
    return {"sub": "placeholder-user", "token": token}


EntraUser = Annotated[dict, Depends(verify_entra_token)]


# ── Health ───────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse()


# ── Incident endpoints ───────────────────────────────────────────


@app.get("/api/incidents", response_model=List[IncidentListItem])
async def list_incidents(
    pool: DBPool,
    _user: EntraUser,
    search: str = Query(default="", max_length=100),
    status_filter: str = Query(default="all", alias="status"),
):
    if status_filter not in VALID_STATUS_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{status_filter}'. Must be one of: {sorted(VALID_STATUS_FILTERS)}",
        )
    try:
        rows = await fetch_incident_list(
            pool, search=search, status_filter=status_filter
        )
        logger.info(
            f"Message='Fetched incident list' Search='{search}' "
            f"Status={status_filter} Count={len(rows)} AppName={settings.APP_NAME}"
        )
        return rows
    except DBQueryError:
        raise
    except asyncio.TimeoutError as e:
        logger.error(
            f"Message='Request timed out' ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise HTTPException(status_code=504, detail="Request timed out")
    except Exception as e:
        logger.exception(
            f"Message='Unexpected error fetching incident list' ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/incidents/{incident_id}", response_model=IncidentDetailOut)
async def get_incident_detail(
    incident_id: str,
    pool: DBPool,
    blob: BlobService,
    _user: EntraUser,
):
    incident_id = incident_id.strip()

    if not incident_id:
        raise HTTPException(status_code=400, detail="incident_id must not be empty")
    if not incident_id.lstrip("-").isdigit():
        raise HTTPException(
            status_code=400, detail=f"incident_id must be numeric, got '{incident_id}'"
        )
    if len(incident_id) > 20:
        raise HTTPException(status_code=400, detail="incident_id too long")

    try:
        detail = await fetch_incident_detail(pool, blob, incident_id)

        if detail is None:
            raise HTTPException(
                status_code=404, detail=f"Incident {incident_id} not found"
            )

        logger.info(
            f"Message='Fetched incident detail' IncidentId={incident_id} AppName={settings.APP_NAME}"
        )
        return detail
    except HTTPException:
        raise
    except DBQueryError:
        raise
    except asyncio.TimeoutError as e:
        logger.error(
            f"Message='Request timed out' IncidentId={incident_id} ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise HTTPException(status_code=504, detail="Request timed out")
    except Exception as e:
        logger.exception(
            f"Message='Unexpected error fetching incident detail' IncidentId={incident_id} ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/feedback")
async def submit_feedback(feedback: FeedbackRequest):
    snippet = (feedback.comment or "")[:80]
    # user_id = "placeholder"  # TODO
    logger.info(
        "POST /api/feedback  incident=%s  section=%r  rating=%s  comment=%r (user=%s)",
        feedback.incident_id,
        feedback.section,
        feedback.rating,
        snippet,
        feedback.user_id,
    )

    result = await save_feedback(
        pool=app.state.db_pool,
        incident_id=feedback.incident_id,
        section=feedback.section,
        rating=feedback.rating,
        comment=feedback.comment,
        user_id=feedback.user_id,
    )
    if result is None:
        raise HTTPException(status_code=503, detail="Could not save feedback")

    return {"success": True, "message": "Feedback recorded", **result}


# Old code
# # ── Legacy folder routes — kept for backward compatibility ───────

# @app.get("/folders", response_model=List[FolderOut])
# async def get_folders(
#     pool: DBPool,
#     limit: int  = Query(1000),
#     offset: int = Query(0),
# ):
#     try:
#         rows = await fetch_folders(pool, limit, offset)
#         logger.info(
#             f"Message='Fetched folders' FolderCount={len(rows)} AppName={settings.APP_NAME}"
#         )
#         return [
#             FolderOut(
#                 folder_name=r["folder_name"],
#                 vin_status=r["vin_status"],
#                 plate_status=r["plate_status"],
#                 est_match_status=r["discount_match_status"],
#                 overall_status=r["status_flag"],
#             )
#             for r in rows
#         ]
#     except TimeoutError as e:
#         logger.error(
#             f"ErrorCode=TIMEOUT Message='Request timed out' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=504, detail="Request timed out")
#     except Exception as e:
#         logger.exception(
#             f"ErrorCode=INTERNAL_ERROR Message='Unexpected error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=500, detail="Internal server error")


# @app.get("/folders/{folder_name}/est_info", response_model=EstInfoOut)
# async def get_est_info(folder_name: str, pool: DBPool):
#     try:
#         row = await fetch_est_info(pool, folder_name)

#         if not row:
#             logger.warning(
#                 f"Message='Est info not found' FolderName={folder_name} AppName={settings.APP_NAME}"
#             )
#             raise HTTPException(status_code=404, detail="Not found")

#         ymms = f"{row['veh_yr']} {row['veh_make']} {row['veh_mod']} {row['licplte_st']}".strip()
#         logger.info(
#             f"Message='Fetched est info' FolderName={folder_name} AppName={settings.APP_NAME}"
#         )
#         return EstInfoOut(
#             folder_name=row["folder_name"],       est_id=str(row["est_id"]),
#             repr_ncdnt_id=row["repr_ncdnt_id"],   legacy_claim_number=row["lgcy_clm_nbr"],
#             claim_number=row["clm_nbr"],           claim_gbpr_id=row["clm_gbpr_id"],
#             damage_desc=str(row["dmg_dsc"]),       accident_report_gbpr=row["acdnt_rpt_gbpr"],
#             odometer_number=str(row["odmtr_nbr"]), state=row["licplte_st"],
#             vin=row["vin"],                        license_plate_number=row["licplte_nbr"],
#             color=row["xtr_colr_dsc"],             ymms=ymms,
#         )
#     except HTTPException:
#         raise
#     except TimeoutError as e:
#         logger.error(
#             f"ErrorCode=TIMEOUT Message='Request timed out' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=504, detail="Request timed out")
#     except Exception as e:
#         logger.exception(
#             f"ErrorCode=INTERNAL_ERROR Message='Unexpected error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=500, detail="Internal server error")


# @app.get("/folders/{folder_name}/vlm-stats", response_model=VLMStatsOut)
# async def get_vlm(folder_name: str, pool: DBPool):
#     try:
#         row = await fetch_vlm_stats(pool, folder_name)
#         classified = ClassifiedBreakdown(
#             vin=row["vin_count"] or 0,
#             license_plate=row["license_plate_count"] or 0,
#             odometer=row["odometer_count"] or 0,
#             other=row["other_count"] or 0,
#         )
#         logger.info(
#             f"Message='Fetched VLM stats' FolderName={folder_name} AppName={settings.APP_NAME}"
#         )
#         return VLMStatsOut(
#             folder_name=folder_name,
#             images_classified=row["images_classified"] or 0,
#             images_with_text=row["images_with_text"] or 0,
#             relevant_found=sum(classified.dict().values()),
#             classified=classified,
#         )
#     except TimeoutError as e:
#         logger.error(
#             f"ErrorCode=TIMEOUT Message='Request timed out' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=504, detail="Request timed out")
#     except Exception as e:
#         logger.exception(
#             f"ErrorCode=INTERNAL_ERROR Message='Unexpected error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=500, detail="Internal server error")


# @app.get("/folders/{folder_name}/images", response_model=ImageListOut)
# async def get_images(folder_name: str, pool: DBPool, blob: BlobService):
#     try:
#         rows   = await fetch_images(pool, folder_name)
#         images = await build_image_details(rows, blob)
#         logger.info(
#             f"Message='Fetched images' FolderName={folder_name} "
#             f"TotalImages={len(images)} AppName={settings.APP_NAME}"
#         )
#         return ImageListOut(folder_name=folder_name, total_images=len(images), images=images)
#     except TimeoutError as e:
#         logger.error(
#             f"ErrorCode=TIMEOUT Message='Request timed out' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=504, detail="Request timed out")
#     except Exception as e:
#         logger.exception(
#             f"ErrorCode=INTERNAL_ERROR Message='Unexpected error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=500, detail="Internal server error")


# @app.get("/folders/{folder_name}/parts")
# async def get_parts(folder_name: str, pool: DBPool):
#     try:
#         est_id = folder_name.replace("EST", "")
#         rows   = [dict(r) for r in await fetch_parts(pool, est_id)]
#         if rows and rows[0].get("dtl_tot_part_price_amt"):
#             for row in rows:
#                 row["Line Description"] = row.pop("line_dsc", None)
#                 row["Parts Price"]      = row.pop("dtl_tot_part_price_amt", None)
#         logger.info(
#             f"Message='Fetched parts' FolderName={folder_name} "
#             f"PartCount={len(rows)} AppName={settings.APP_NAME}"
#         )
#         return rows
#     except TimeoutError as e:
#         logger.error(
#             f"ErrorCode=TIMEOUT Message='Request timed out' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=504, detail="Request timed out")
#     except Exception as e:
#         logger.exception(
#             f"ErrorCode=INTERNAL_ERROR Message='Unexpected error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=500, detail="Internal server error")


# @app.get("/folders/{folder_name}/discount")
# async def get_discount(folder_name: str, pool: DBPool):
#     try:
#         est_id = folder_name.replace("EST", "")
#         rows   = await fetch_discount(pool, est_id)
#         logger.info(
#             f"Message='Fetched discount' FolderName={folder_name} AppName={settings.APP_NAME}"
#         )
#         return [dict(r) for r in rows]
#     except TimeoutError as e:
#         logger.error(
#             f"ErrorCode=TIMEOUT Message='Request timed out' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=504, detail="Request timed out")
#     except Exception as e:
#         logger.exception(
#             f"ErrorCode=INTERNAL_ERROR Message='Unexpected error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
#         )
#         raise HTTPException(status_code=500, detail="Internal server error")


# _________________________________________________________________________________________________________
