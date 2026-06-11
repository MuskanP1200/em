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
