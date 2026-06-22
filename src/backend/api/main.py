"""
CDR Assistant — FastAPI backend
Run: uvicorn api.main:app --reload --port 8018
"""

import asyncio
import logging
import time
from typing import Annotated, List

import asyncpg
from azure.storage.blob import BlobServiceClient
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .middlewares.iap_middleware.iap_jwt_settings import IapJwtSettings
from .middlewares.iap_middleware.asgi import FastAPIAzureAuthenticationMiddleware
from .middlewares.iap_middleware.authentication_backend import IapAuthenticationBackend

from api.api_logging.config_logging import configure_logging
from api.models.incident_schemas import (
    FeedbackRequest,
    IncidentDetailOut,
    IncidentListItem,
)
from api.models.schemas import (
    HealthResponse,
)
from api.services.db import get_blob_service, get_db_pool, lifespan
from api.services.incident_orchestrator import (
    DBQueryError,
    fetch_incident_detail,
    fetch_incident_list,
    save_feedback,
)

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

if settings.use_jwt_middleware:
    logger.info("adding IAP JWT Middleware")
    jwt_settings = IapJwtSettings()
    app.add_middleware(
        FastAPIAzureAuthenticationMiddleware,
        backend=IapAuthenticationBackend(jwt_settings),
    )

_DEV_EMAIL = "localdev@local"


def _current_email(request: Request) -> str | None:
    # When JWT middleware is OFF (localdev/bypass) there is no token,
    # so fall back to a dev identity. In prod the middleware is ON and
    # identity comes strictly from the validated token scope
    if not settings.use_jwt_middleware:
        return _DEV_EMAIL
    return request.scope.get("preferred_username")


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


# ── Health ───────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse()


# ── Incident endpoints ───────────────────────────────────────────


@app.get("/api/incidents", response_model=List[IncidentListItem])
async def list_incidents(
    request: Request,
    pool: DBPool,
    search: str = Query(default="", max_length=100),
    status_filter: str = Query(default="all", alias="status"),
):

    user_email = _current_email(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="No authenticated identity")

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
    request: Request,
):
    user_email = _current_email(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="No authenticated identity")

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
async def submit_feedback(feedback: FeedbackRequest, request: Request):
    user_email = _current_email(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="No authenticated identity")

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
