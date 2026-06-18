import hashlib
import json
import logging

import requests

logger = logging.getLogger(__name__)

_CVD_AUTH_TIMEOUT = 30


class CVDAuthError(RuntimeError):
    pass


def get_cvd_token(logon_id: str, password: str, auth_url: str) -> str:
    """
    Authenticate with the CVD API and return a JWT token.

    The CVD API requires the password to be MD5-hashed before transmission —
    this is the API's own contract, not a security choice we made.
    """
    logger.debug("CVD auth: requesting JWT for logon_id=%s", logon_id)

    # MD5 hash required by the CVD API contract (not used for security)
    hashed_pw = hashlib.md5(password.encode()).hexdigest()  # noqa: S324

    payload = json.dumps({
        "logonId": logon_id,
        "password": hashed_pw,
        "audience": "com.ehi.vehicle",
    })
    headers = {
        "Content-Type": "application/prs.ehi-com.auth.audience+json",
        "Accept": "application/jwt",
    }

    try:
        resp = requests.post(
            auth_url,
            headers=headers,
            data=payload,
            timeout=_CVD_AUTH_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("CVD auth request failed: %s", exc)
        raise CVDAuthError("CVD authentication HTTP failure") from exc

    token = resp.text.strip()
    if not token:
        raise CVDAuthError("CVD auth succeeded but returned an empty token")

    logger.info("CVD JWT token acquired")
    return token