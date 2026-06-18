"""
claims_client.py
================
CSS (Claims Service System) API client.

Three functions:
    get_css_token()       Authenticate and return an AppSec token.
    get_css_image_list()  DocumentSearchRQ  → list of document dicts.
    get_css_image_bytes() GetDocumentRQ     → (filename, raw_bytes).

The CSS service uses a different AppStaticId (388582721) from VR Services,
so it handles its own auth XML rather than sharing api_auth.get_token().
Both service calls go to the same CSS endpoint (CSS_API_URL from config).
"""

from __future__ import annotations

import base64
import logging

import defusedxml.ElementTree as ET
import requests

logger = logging.getLogger(__name__)

_CSS_TIMEOUT = 90           # bytes responses observed at ~6 MB / 12 s
_CSS_AUTH_TIMEOUT = 30

_CSS_AUTH_APP_STATIC_ID = "388582721"    # CSS AppSec registration
_CSS_SVC_STATIC_ID      = "1539344193"  # embedded in every CSS service request

_AUTH_HEADERS = {
    "Content-Type": "application/xml",
    "Accept": "application/xml",
}

_CSS_HEADERS = {
    "Content-Type": "application/xml",
    "ehi-locale": "en_US",
}

# ── XML templates ─────────────────────────────────────────────────────────────

_CSS_AUTH_XML = """<AuthenticateUserRQ xmlns="http://erac.com/appsec/enhanced/rsi/webservice/auth">
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
    <AppStaticId xmlns="">{app_static_id}</AppStaticId>
    <LogonId xmlns="">{username}</LogonId>
    <Password xmlns="">{password}</Password>
    <OrigEvent xmlns=""/>
</AuthenticateUserRQ>"""

_DOC_SEARCH_XML = """\
<DocumentSearchRQ xmlns="http://erac.com/claimsrv/webservice/documentWeb"
    xmlns:com="http://erac.com/claimsrv/webservice/common"
    xmlns:loc="http://erac.com/services/common/locale"
    xmlns:sec="http://erac.com/services/security">
    <com:Locale>
        <loc:CountryIso3Code>USA</loc:CountryIso3Code>
        <loc:LanguageIso3Code>eng</loc:LanguageIso3Code>
    </com:Locale>
    <com:Request>
        <CallerIdentity xmlns="">E192G5</CallerIdentity>
        <CallingProcess xmlns="">VEHREPR_DESKTOP</CallingProcess>
        <CallingApplicationName xmlns="">VEHREPR_DESKTOP</CallingApplicationName>
        <CallingApplicationVersion xmlns="">1</CallingApplicationVersion>
        <CallingInterfaceVersion xmlns="">1.0.0</CallingInterfaceVersion>
        <CallingHostOrWeblogicInstance xmlns="">tomcat8080:8080</CallingHostOrWeblogicInstance>
        <RequestId xmlns="">1</RequestId>
    </com:Request>
    <com:ServiceSecurityCredential>
        <sec:ServiceAccountToken>{token}</sec:ServiceAccountToken>
        <sec:CallingApplicationStaticId>{svc_static_id}</sec:CallingApplicationStaticId>
    </com:ServiceSecurityCredential>
    <ClaimId>{claim_id}</ClaimId>
    <ActiveOnlyIndicator>true</ActiveOnlyIndicator>
    <DocumentSearchCriteria>
        <TagId>1</TagId>
        <TagId>12</TagId>
        <ExcludedTagId>53</ExcludedTagId>
        <ExcludedTagId>57</ExcludedTagId>
    </DocumentSearchCriteria>
</DocumentSearchRQ>"""

_GET_DOC_XML = """\
<GetDocumentRQ xmlns="http://erac.com/claimsrv/webservice/documentWeb"
    xmlns:com="http://erac.com/claimsrv/webservice/common"
    xmlns:loc="http://erac.com/services/common/locale"
    xmlns:sec="http://erac.com/services/security">
    <com:Locale>
        <loc:CountryIso3Code>USA</loc:CountryIso3Code>
        <loc:LanguageIso3Code>eng</loc:LanguageIso3Code>
    </com:Locale>
    <com:Request>
        <CallerIdentity xmlns="">E192G5</CallerIdentity>
        <CallingProcess xmlns="">VEHREPR_DESKTOP</CallingProcess>
        <CallingApplicationName xmlns="">VEHREPR_DESKTOP</CallingApplicationName>
        <CallingApplicationVersion xmlns="">1</CallingApplicationVersion>
        <CallingInterfaceVersion xmlns="">1.0.0</CallingInterfaceVersion>
        <CallingHostOrWeblogicInstance xmlns="">tomcat8080:8080</CallingHostOrWeblogicInstance>
        <RequestId xmlns="">1</RequestId>
    </com:Request>
    <com:ServiceSecurityCredential>
        <sec:ServiceAccountToken>{token}</sec:ServiceAccountToken>
        <sec:CallingApplicationStaticId>{svc_static_id}</sec:CallingApplicationStaticId>
    </com:ServiceSecurityCredential>
    <DocumentId>{doc_id}</DocumentId>
    <NeedDocumentBinaryDataIndicator>true</NeedDocumentBinaryDataIndicator>
</GetDocumentRQ>"""


# ── Public functions ──────────────────────────────────────────────────────────

def get_css_token(username: str, password: str, auth_url: str) -> str:
    """Authenticate against AppSec using the CSS AppStaticId. Returns session token."""
    logger.info("Requesting CSS AppSec token")
    resp = requests.post(
        auth_url,
        headers=_AUTH_HEADERS,
        data=_CSS_AUTH_XML.format(
            username=username,
            password=password,
            app_static_id=_CSS_AUTH_APP_STATIC_ID,
        ),
        timeout=_CSS_AUTH_TIMEOUT,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    for el in root.iter():
        if el.tag.endswith("Token"):
            token = (el.text or "").strip()
            if token:
                logger.info("CSS token acquired")
                return token

    raise RuntimeError("CSS auth succeeded but token not found in response")


def get_css_image_list(token: str, claim_id: str, api_url: str) -> list[dict]:
    """
    Fetch the document list for a claim from the CSS API.
    Returns list of dicts: [{doc_id, doc_name, mime_type}, ...]
    """
    payload = _DOC_SEARCH_XML.format(
        token=token,
        svc_static_id=_CSS_SVC_STATIC_ID,
        claim_id=claim_id,
    )
    resp = requests.post(api_url, headers=_CSS_HEADERS, data=payload, timeout=_CSS_TIMEOUT)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    documents: list[dict] = []
    for result in root.iter():
        if not result.tag.endswith("DocumentSearchResult"):
            continue
        doc_id = doc_name = mime_type = None
        for child in result.iter():
            local = child.tag.split("}")[-1]
            if local == "DocumentId" and doc_id is None:
                doc_id = child.text
            elif local == "DocumentName" and doc_name is None:
                doc_name = child.text
            elif local == "MimeTypeDescription" and mime_type is None:
                mime_type = child.text
        if doc_id:
            documents.append({
                "doc_id": doc_id,
                "doc_name": doc_name or f"doc_{doc_id}",
                "mime_type": mime_type,
            })

    logger.debug("CSS claim_id=%s: %d document(s) found", claim_id, len(documents))
    return documents


def get_css_image_bytes(token: str, doc_id: str, api_url: str) -> tuple[str, bytes]:
    """
    Fetch binary content for one document.
    Returns (filename, raw_bytes). Raises ValueError if bytes are missing.
    """
    payload = _GET_DOC_XML.format(
        token=token,
        svc_static_id=_CSS_SVC_STATIC_ID,
        doc_id=doc_id,
    )
    resp = requests.post(api_url, headers=_CSS_HEADERS, data=payload, timeout=_CSS_TIMEOUT)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    doc_name: str | None = None
    doc_bytes_b64: str | None = None

    for el in root.iter():
        local = el.tag.split("}")[-1]
        if local == "DocumentName" and doc_name is None:
            doc_name = el.text
        elif local == "DocumentBytes" and doc_bytes_b64 is None:
            doc_bytes_b64 = el.text

    if not doc_bytes_b64:
        raise ValueError(f"CSS GetDocument: DocumentBytes missing for doc_id={doc_id}")

    raw_bytes = base64.b64decode(doc_bytes_b64)
    logger.debug("CSS doc_id=%s: %d bytes filename=%s", doc_id, len(raw_bytes), doc_name)
    return doc_name or f"doc_{doc_id}.bin", raw_bytes