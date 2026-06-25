import uuid
from typing import Optional

import msal
from fastapi import Request

from settings import get_settings

_settings = get_settings()

_DEV_USER = {
    "name": "Dev Tester",
    "email": "localdev@local",
    "preferred_username": "localdev@local",
}


def _build_msal_app(cache=None) -> msal.ConfidentialClientApplication:
    """Construct MSAL ConfidentialClientApplication."""
    return msal.ConfidentialClientApplication(
        client_id=_settings.VO_AZURE_CLIENT_ID,
        client_credential=_settings.VO_AZURE_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{_settings.VO_AZURE_TENANT_ID}",
        token_cache=cache,
    )


def _load_cache(request: Request) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if request.session.get("token_cache"):
        cache.deserialize(request.session["token_cache"])
    return cache


def _save_cache(request: Request, cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        request.session["token_cache"] = cache.serialize()


def get_access_token(request: Request) -> Optional[str]:
    """Valid backend access token, silently refreshed via MSAL cache."""
    result = get_token_from_cache(request)
    return result.get("access_token") if result else None


def _get_redirect_uri(request: Request) -> str:
    if _settings.REDIRECT_URI:
        return _settings.REDIRECT_URI
    base = str(request.base_url).rstrip("/")
    return f"{base}{_settings.REDIRECT_PATH}"


def get_auth_url(request: Request) -> str:
    """Generate the Microsoft login redirect URL."""
    request.session["auth_state"] = str(uuid.uuid4())
    msal_app = _build_msal_app()
    return msal_app.get_authorization_request_url(
        scopes=_settings.scopes,
        state=request.session["auth_state"],
        redirect_uri=_get_redirect_uri(request),
    )


def acquire_token_by_auth_code(
    request: Request, code: str, state: str
) -> Optional[dict]:
    """Exchange authorization code for access + ID tokens."""
    if state != request.session.get("auth_state"):
        return None

    cache = _load_cache(request)
    msal_app = _build_msal_app(cache)
    result = msal_app.acquire_token_by_authorization_code(
        code=code,
        scopes=_settings.scopes,
        redirect_uri=_get_redirect_uri(request),
    )
    if "error" in result:
        return None

    _save_cache(request, cache)
    return result


def get_token_from_cache(request: Request) -> Optional[dict]:
    """Return a valid access token from cache, silently refreshing if needed."""
    cache = _load_cache(request)
    msal_app = _build_msal_app(cache)
    accounts = msal_app.get_accounts()
    if not accounts:
        return None

    result = msal_app.acquire_token_silent(
        scopes=_settings.scopes,
        account=accounts[0],
    )
    _save_cache(request, cache)
    return result


def get_current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")


def logout_user(request: Request) -> None:
    request.session.clear()


def get_user_initials(user: dict) -> str:
    """Extract 2-letter initials from the user's display name."""
    name = user.get("name") or user.get("email") or user.get("preferred_username", "DT")
    parts = name.replace(".", " ").replace("_", " ").split()
    if len(parts) >= 2:
        return f"{parts[0][0]}{parts[-1][0]}".upper()
    return name[:2].upper()


def get_user_display_name(user: dict) -> str:
    name = user.get("name") or user.get("email") or "Dev Tester"
    return name.strip()
