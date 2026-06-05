"""
api_request_builder.py
======================
Pure XML body builders for VR Services SOAP APIs.

Uses a generic envelope builder to eliminate duplication.
No I/O, no HTTP calls.
"""

from xml.sax.saxutils import escape  # nosec B406

from settings import get_settings

_settings = get_settings()

# ── Caller metadata ───────────────────────────────────────────────────────────

_APP_STATIC_ID = getattr(_settings, "API_APP_STATIC_ID", "759935158")
_CALLER_ID = getattr(_settings, "API_CALLER_ID", "SVC_AI_VEH_REPAIR")
_CALLING_APP = getattr(_settings, "API_CALLING_APP", "SVC_AI_VEH_REPAIR")
_CALLING_HOST = getattr(_settings, "API_CALLING_HOST", "tomcat8080:8080")
_CALLER_GRP = getattr(_settings, "API_CALLER_GROUP", "98")

# ── Namespaces ────────────────────────────────────────────────────────────────

NS_EST = "http://erac.com/vrservices/webservice/estimateWeb"
NS_CDR = "http://erac.com/vrservices/webservice/cdrWeb"
NS_ATT = "http://erac.com/vrservices/webservice/attachmentWeb"
NS_REP = "http://erac.com/vrservices/webservice/repairWeb"
NS_WEB = "http://erac.com/vrservices/webservice"
NS_SEC = "http://erac.com/services/security"
NS_LOC = "http://erac.com/services/common/locale"
NS_TYP = "http://erac.com/vrservices/webservice/typeCodeWeb"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"


# ── Shared blocks ─────────────────────────────────────────────────────────────


def _request_block() -> str:
    return f"""<Request>
    <web:CallerIdentity>{_CALLER_ID}</web:CallerIdentity>
    <web:CallingProcess>{_CALLING_APP}</web:CallingProcess>
    <web:CallingApplicationName>{_CALLING_APP}</web:CallingApplicationName>
    <web:CallingApplicationVersion>1</web:CallingApplicationVersion>
    <web:CallingInterfaceVersion>1.0.0</web:CallingInterfaceVersion>
    <web:CallingHostOrWeblogicInstance>{_CALLING_HOST}</web:CallingHostOrWeblogicInstance>
    <web:RequestId>1</web:RequestId>
    <web:CallerFirstName>Service</web:CallerFirstName>
    <web:CallerLastName>Account</web:CallerLastName>
    <web:CallerGroup>{_CALLER_GRP}</web:CallerGroup>
</Request>"""


def _security_block(token: str) -> str:
    return f"""<ServiceSecurityCredential>
    <sec:ServiceAccountToken>{escape(token)}</sec:ServiceAccountToken>
    <sec:CallingApplicationStaticId>{_APP_STATIC_ID}</sec:CallingApplicationStaticId>
</ServiceSecurityCredential>"""


def _locale_block() -> str:
    return """<Locale>
    <loc:CountryIso3Code>USA</loc:CountryIso3Code>
    <loc:LanguageIso3Code>eng</loc:LanguageIso3Code>
</Locale>"""


# ── Generic envelope builder ──────────────────────────────────────────────────


def _build_envelope(
    *,
    root_tag: str,
    default_ns: str,
    token: str,
    body: str,
    extra_ns: dict[str, str] | None = None,
) -> str:
    ns_parts = [
        f'xmlns="{default_ns}"',
        f'xmlns:web="{NS_WEB}"',
        f'xmlns:sec="{NS_SEC}"',
        f'xmlns:loc="{NS_LOC}"',
    ]
    if extra_ns:
        ns_parts.extend(f'xmlns:{k}="{v}"' for k, v in extra_ns.items())

    ns_block = "\n    ".join(ns_parts)

    return f"""<{root_tag}
    {ns_block}>
{_request_block()}
{_security_block(token)}
{_locale_block()}
{body}
</{root_tag}>"""


# ── Builders ──────────────────────────────────────────────────────────────────


def build_search_body(
    token: str,
    status_code: str,
    group: str,
    start_row: int,
    rows_per_page: int,
) -> str:
    body = f"""<SearchCriteria>
    <web:OrderBy>estimateReceivedDate</web:OrderBy>
    <web:OrderDirection>desc</web:OrderDirection>
    <ClaimNumber xsi:nil="true"/>
    <web:PaginationDetail>
        <web:StartRowNumber>{start_row}</web:StartRowNumber>
        <web:RowsPerPage>{rows_per_page}</web:RowsPerPage>
    </web:PaginationDetail>
    <CountryCode>USA</CountryCode>
    <Group>{escape(group)}</Group>
    <AdjusterUserId></AdjusterUserId>
    <SupervisorId xsi:nil="true"/>
    <ReceivedStartDate xsi:nil="true"/>
    <ReceivedEndDate xsi:nil="true"/>
    <CompletionStartDate xsi:nil="true"/>
    <CompletionEndDate xsi:nil="true"/>
    <EstimateStatus>
        <typ:Code>{escape(status_code)}</typ:Code>
    </EstimateStatus>
    <MultiVendor>false</MultiVendor>
    <IncludeWarnings>true</IncludeWarnings>
    <SearchRepairByAdjuster>false</SearchRepairByAdjuster>
</SearchCriteria>"""

    return _build_envelope(
        root_tag="SearchEstimateRQ",
        default_ns=NS_EST,
        token=token,
        body=body,
        extra_ns={"xsi": NS_XSI, "typ": NS_TYP},
    )


def build_estimate_detail_body(token: str, est_id: str) -> str:
    return _build_envelope(
        root_tag="GetEstimateDetailForSubtotalsRQ",
        default_ns=NS_EST,
        token=token,
        body=f"<EstimateId>{escape(est_id)}</EstimateId>",
    )


def build_electronic_estimate_body(token: str, est_id: str) -> str:
    return _build_envelope(
        root_tag="GetElectronicEstimateRQ",
        default_ns=NS_EST,
        token=token,
        body=f"<EstimateId>{escape(est_id)}</EstimateId>",
    )


def build_cdr_body(token: str, vendor_id: str, group_number: str) -> str:
    return _build_envelope(
        root_tag="GetCDRGroupVendorRQ",
        default_ns=NS_CDR,
        token=token,
        body=f"""<VendorId>{escape(vendor_id)}</VendorId>
<GroupNumber>{escape(group_number)}</GroupNumber>""",
    )


def build_image_list_body(token: str, est_id: str) -> str:
    return _build_envelope(
        root_tag="GetAttachmentsForEstimateRQ",
        default_ns=NS_ATT,
        token=token,
        body=f"""<EstimateId>{escape(est_id)}</EstimateId>
<NeedAttachmentBinaryDataIndicator>false</NeedAttachmentBinaryDataIndicator>
<NewAttachmentIndicator>false</NewAttachmentIndicator>""",
    )


def build_image_bytes_body(token: str, attachment_id: str) -> str:
    return _build_envelope(
        root_tag="GetAttachmentBytesRQ",
        default_ns=NS_ATT,
        token=token,
        body=f"""<AttachmentId>{escape(attachment_id)}</AttachmentId>
<NeedAttachmentBinaryDataIndicator>true</NeedAttachmentBinaryDataIndicator>""",
    )


def build_repair_incident_body(token: str, repair_incident_id: str) -> str:
    return _build_envelope(
        root_tag="SearchRepairIncidentRQ",
        default_ns=NS_REP,
        token=token,
        body=f"""<SearchCriteria>
    <RepairIncidentId>{escape(repair_incident_id)}</RepairIncidentId>
    <IncludeDeleted>true</IncludeDeleted>
</SearchCriteria>""",
    )
