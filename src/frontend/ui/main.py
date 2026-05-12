import logging
import uvicorn
import sys
import secrets
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
import msal

from data import filter_incidents, get_incident_detail, send_feedback
from settings import get_settings

settings = get_settings()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cdr_assistant")
# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("cdr_assistant")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="CDR Assistant", version="1.0.0", docs_url="/docs")
templates = Jinja2Templates(directory="templates")

REDIRECT_PATH = settings.REDIRECT_PATH
SESSION_SECRET = settings.SESSION_SECRET

if settings.AUTH_ENABLED:
    CLIENT_ID = settings.VO_AZURE_CLIENT_ID
    CLIENT_SECRET = settings.VO_AZURE_CLIENT_SECRET
    TENANT_ID = settings.VO_AZURE_TENANT_ID
    AUTHORITY = settings.AUTHORITY
    REDIRECT_URI = f"{settings.REDIRECT_URI}{settings.REDIRECT_PATH}"
    SCOPES = settings.SCOPES
else:
    logger.warning("AUTH ENABLED=False - running with mock user")


DEV_USER = {"name": "Dev User", "email": "dev@localhost", "oid": "dev-oid-0000"}
# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="CDR Assistant", version="1.0.0", docs_url="/docs")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=True,
    same_site="lax",
)
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
templates = Jinja2Templates(directory="templates")


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=CLIENT_ID, client_credential=CLIENT_SECRET, authority=AUTHORITY
    )


# Auth dependency, gates protected API routes


def require_user(request: Request) -> dict:
    if not settings.AUTH_ENABLED:
        return DEV_USER
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    incident_id: str
    section: str
    rating: Optional[str] = None  # "up" | "down" | None
    comment: Optional[str] = None


# Routes Auth
@app.get("/login")
async def login(request: Request):
    if not settings.AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=302)
    state = secrets.token_urlsafe(16)
    request.session["auth_state"] = state
    auth_url = _msal_app().get_authorization_request_url(
        scopes=SCOPES, state=state, redirect_uri=REDIRECT_URI
    )
    logger.info("GET /login - redirecting to Entra ID")
    return RedirectResponse(url=auth_url, status_code=302)


@app.get(REDIRECT_PATH)
async def auth_callback(request: Request):
    expected_state = request.session.pop("auth_state", None)
    if not expected_state or request.query_params.get("state") != expected_state:
        logger.warning("Auth callback: state mismatch")
        raise HTTPException(status_code=400, detail="Invalid state")

    if "error" in request.query_params:
        logger.warning(
            "Auth callback error: %s", request.query_params.get("error_description")
        )
        raise HTTPException(
            status_code=400, detail=request.query_params.get("error_description")
        )

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    result = _msal_app().acquire_token_by_authorization_code(
        code=code, scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    if "error" in result:
        logger.warning("Token acquisition failed: %s", result.get("error_description"))
        raise HTTPException(status_code=400, detail=result.get("error_description"))

    claims = result.get("id_token_claims", {})
    request.session["user"] = {
        "name": claims.get("name"),
        "email": claims.get("preferred_username") or claims.get("email"),
        "oid": claims.get("oid"),
    }
    # request.session["access_token"] = result.get("access_token")
    # request.session["id_token"] = result.get("id_token")

    logger.info("user signed in: %s", request.session["user"]["email"])
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    base_url = REDIRECT_URI.rsplit(REDIRECT_URI, 1)[0]
    logout_url = (
        f"{AUTHORITY}/oauth2/v2.0/logout" f"?post_logout_redirect_uri={base_url}/"
    )
    logger.info("User signed out")
    return RedirectResponse(url=logout_url, status_code=302)


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not settings.AUTH_ENABLED:
        logger.info("GET / - auth disabled, serving main page as dev user.")
        return templates.TemplateResponse(
            request=request, name="index.html", context={"user": DEV_USER}
        )
    user = request.session.get("user")
    if not user:
        logger.info("GET / - unauthenticated, showing login page")
        return templates.TemplateResponse(
            request=request, name="login.html", context={}
        )
    logger.info("GET / - serving main page to %s", user["email"])
    return templates.TemplateResponse(
        request=request, name="index_new.html", context={"user": user}
    )


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------
@app.get("/api/incidents")
async def list_incidents(
    search: str = "", status: str = "all", user: dict = Depends(require_user)
):
    logger.info(
        "GET /api/incidents  search=%r  status=%r (user=%s)",
        search,
        status,
        user["email"],
    )
    results = filter_incidents(search=search, status=status)
    logger.info("  → returning %d incidents", len(results))
    return results


@app.get("/api/incidents/{incident_id}")
async def incident_detail(incident_id: str, user: dict = Depends(require_user)):
    logger.info("GET /api/incidents/%s (user=%s)", incident_id, user["email"])
    detail = get_incident_detail(incident_id)
    if detail is None:
        logger.warning("  → incident %s not found", incident_id)
        raise HTTPException(
            status_code=404, detail=f"Incident {incident_id!r} not found"
        )
    print(f"Checking: {detail}")
    return detail


@app.post("/api/feedback")
async def submit_feedback(
    feedback: FeedbackRequest, user: dict = Depends(require_user)
):
    snippet = (feedback.comment or "")[:80]
    logger.info(
        "POST /api/feedback  incident=%s  section=%r  rating=%s  comment=%r (user=%s)",
        feedback.incident_id,
        feedback.section,
        feedback.rating,
        snippet,
        user["email"],
    )

    try:
        return send_feedback(feedback.model_dump())
    except RuntimeError as e:
        logger.error("Feedback save failed: %s", e)
        raise HTTPException(status_code=503, detail="Could not save feedback")


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    # os.environ["USE_MOCK_DATA"] = "true"  ; os.environ["BACKEND_URL"] = ""           # MOCK
    # uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)

    os.environ["USE_MOCK_DATA"] = "false"
    os.environ["BACKEND_URL"] = "http://localhost:8018"  # REAL
    uvicorn.run("main:app", host="0.0.0.0", port=4200, reload=True)  # nosec B104
