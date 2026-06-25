import logging
import uvicorn
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from starsessions import SessionMiddleware, SessionAutoloadMiddleware
from starsessions.stores.redis import RedisStore
from redis_client import build_redis_client

from auth import (
    acquire_token_by_auth_code,
    get_auth_url,
    get_access_token,
    logout_user,
    get_user_initials,
    get_user_display_name,
    _DEV_USER,
)

from ui_logging.config_logging import configure_logging
from data import filter_incidents, get_incident_detail, send_feedback
from settings import get_settings, AppEnvironment

settings = get_settings()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="CDR Assistant", version="1.0.0", docs_url="/docs")

store = RedisStore(connection=build_redis_client(settings.REDIS_HOST), prefix="vo:")
app.add_middleware(SessionAutoloadMiddleware)
app.add_middleware(
    SessionMiddleware,
    store=store,
    cookie_https_only=(settings.APP_ENVIRONMENT == AppEnvironment.PRODUCTION),
    lifetime=28800,
    cookie_same_site="lax",
)

app.mount("/assets", StaticFiles(directory="assets"), name="assets")
templates = Jinja2Templates(directory="templates")


# Auth dependency, gates protected API routes


def require_user(request: Request) -> dict:
    if settings.BYPASS_AUTH:
        return request.session.setdefault("user", _DEV_USER)
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


class SessionExpired(Exception):
    """Session is alive but the access token can't be refreshed - needs re-login"""


def _auth_headers(request: Request) -> dict:
    if settings.BYPASS_AUTH:
        return {}
    token = get_access_token(request)
    if token is None:
        raise SessionExpired()
    return {"Authorization": f"Bearer {token}"}


@app.exception_handler(SessionExpired)
async def _session_expired(request: Request, exc: SessionExpired):
    if settings.BYPASS_AUTH:
        return RedirectResponse("/", status_code=302)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": "session_expired", "redirect": "/login"}, status_code=401
        )
    return RedirectResponse("/login", status_code=302)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    incident_id: str
    section: str
    rating: Optional[str] = None  # "up" | "down" | None
    comment: Optional[str] = None
    user_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_template_user(user: dict) -> dict:
    """
    Normalizes the user object for the UI.
    Uses Entra ID name when auth is enabled, otherwise Dev Tester.
    """

    return {
        **(user or {}),
        "display_name": get_user_display_name(user) if user else "Dev Tester",
        "initials": get_user_initials(user) if user else "DT",
    }


# Routes Auth
@app.get("/login")
async def login(request: Request):
    if settings.BYPASS_AUTH:
        request.session.setdefault("user", _DEV_USER)
        return RedirectResponse(url="/", status_code=302)
    logger.info("GET /login - redirecting to Entra ID")
    return RedirectResponse(url=get_auth_url(request), status_code=302)


@app.get(settings.REDIRECT_PATH)
async def auth_callback(request: Request):
    error = request.query_params.get("error")
    if error:
        logger.warning(
            "Auth callback error: %s", request.query_params.get("error_description")
        )
        raise HTTPException(
            status_code=400, detail=request.query_params.get("error_description")
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    result = acquire_token_by_auth_code(request, code, state)
    if not result:
        raise HTTPException(status_code=400, detail="Token acquisition failed")

    claims = result.get("id_token_claims", {})
    request.session["user"] = {
        "name": claims.get("name"),
        "email": claims.get("preferred_username") or claims.get("email"),
        "oid": claims.get("oid"),
    }
    logger.info("User signed in: %s", request.session["user"]["email"])
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    logout_user(request)
    if settings.BYPASS_AUTH:
        return RedirectResponse(url="/", status_code=302)
    tenant_id = settings.VO_AZURE_TENANT_ID
    base_url = str(request.base_url).rstrip("/")
    logout_url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={base_url}"
    )
    logger.info("User signed out")
    return RedirectResponse(url=logout_url, status_code=302)


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if settings.BYPASS_AUTH:
        logger.info("GET / - auth disabled, serving main page as dev user")
        return templates.TemplateResponse(
            request, name="index.html", context={"user": build_template_user(_DEV_USER)}
        )

    user = request.session.get("user")
    if not user:
        logger.info("GET / - unauthenticated, showing login page")
        return templates.TemplateResponse(request, name="login.html", context={})

    logger.info("GET / -serving main page to %s", user["email"])
    return templates.TemplateResponse(
        request, name="index.html", context={"user": build_template_user(user)}
    )


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------
@app.get("/api/incidents")
async def list_incidents(
    request: Request,
    search: str = "",
    status: str = "all",
    user: dict = Depends(require_user),
):
    logger.info(
        "GET /api/incidents  search=%r  status=%r (user=%s)",
        search,
        status,
        user["email"],
    )
    headers = _auth_headers(request)
    results = filter_incidents(search=search, status=status, headers=headers)
    logger.info("  → returning %d incidents", len(results))
    return results


@app.get("/api/incidents/{incident_id}")
async def incident_detail(
    incident_id: str, request: Request, user: dict = Depends(require_user)
):
    logger.info("GET /api/incidents/%s (user=%s)", incident_id, user["email"])
    headers = _auth_headers(request)
    detail = get_incident_detail(incident_id, headers=headers)
    if detail is None:
        logger.warning("  → incident %s not found", incident_id)
        raise HTTPException(
            status_code=404, detail=f"Incident {incident_id!r} not found"
        )
    logger.debug("incident detail: %s", detail)
    return detail


@app.post("/api/feedback")
async def submit_feedback(
    feedback: FeedbackRequest, request: Request, user: dict = Depends(require_user)
):
    snippet = (feedback.comment or "")[:80]
    logger.info(
        "POST /api/feedback  incident=%s  section=%r  rating=%s  comment=%r (user=%s)",
        feedback.incident_id,
        feedback.section,
        feedback.rating,
        snippet,
        feedback.user_id,
    )
    headers = _auth_headers(request)

    try:
        return send_feedback(feedback.model_dump(), headers=headers)
    except RuntimeError as e:
        logger.error("Feedback save failed: %s", e)
        raise HTTPException(status_code=503, detail="Could not save feedback")


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=4200, reload=True)  # nosec B104
