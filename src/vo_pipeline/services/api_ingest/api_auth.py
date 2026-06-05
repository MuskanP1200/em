import logging
from typing import Optional

import requests
import defusedxml.ElementTree as ET

from settings import get_settings

logger = logging.getLogger(__name__)

AUTH_TIMEOUT = getattr(get_settings(), "API_AUTH_TIMEOUT", 30)

AUTH_HEADERS = {
    "Content-Type": "application/xml",
    "Accept": "application/xml",
}

AUTH_XML = """<AuthenticateUserRQ xmlns="http://erac.com/appsec/enhanced/rsi/webservice/auth">
    <Request xmlns="">
        <CallerIdentity/>
        <CallingProcess/>
        <CallingApplicationName>vrservices</CallingApplicationName>
        <CallingApplicationVersion>1</CallingApplicationVersion>
        <CallingInterfaceVersion/>
        <CallingHostOrWeblogicInstance>tomcat9090:9090</CallingHostOrWeblogicInstance>
        <RequestId>1</RequestId>
        <CacheId/>
    </Request>
    <Locale xmlns="">
        <ISOCountryCode>US</ISOCountryCode>
        <ISOLanguageCode>en</ISOLanguageCode>
    </Locale>
    <AppStaticId xmlns="">759935158</AppStaticId>
    <LogonId xmlns="">{username}</LogonId>
    <Password xmlns="">{password}</Password>
    <OrigEvent xmlns=""/>
</AuthenticateUserRQ>"""


class AuthError(RuntimeError):
    pass


def _extract_token(xml_text: str) -> Optional[str]:
    """Namespace-safe token extraction."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        logger.error("Auth response XML parsing failed", exc_info=True)
        return None

    # Search ignoring namespace prefix
    for el in root.iter():
        if el.tag.endswith("Token"):
            return (el.text or "").strip() or None

    return None


def get_token(username: str, password: str, auth_url: str) -> str:
    """
    Authenticate and return session token.
    """

    logger.info("Requesting AppSec token")

    try:
        resp = requests.post(
            auth_url,
            headers=AUTH_HEADERS,
            data=AUTH_XML.format(username=username, password=password),
            timeout=AUTH_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Auth request failed", exc_info=True)
        raise AuthError("Authentication HTTP failure") from e

    token = _extract_token(resp.text)

    if not token:
        logger.error("Token not found in auth response")
        raise AuthError("Authentication succeeded but token missing")

    logger.debug("Token retrieved successfully")

    return token


if __name__ == "__main__":
    from estimate_matching.config import AUTH_URL

    logging.basicConfig(level=logging.INFO)
    creds = get_settings().model_dump()
    token = get_token(
        username=creds["ICE_API_USER_NAME"],
        password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
        auth_url=AUTH_URL,
    )
    print(f"token: {token}")
