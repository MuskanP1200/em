"""
api_request_builder.py
======================
Pure XML body builders for all VR Services SOAP API requests.
No I/O, no HTTP calls — just string construction.

Imported by api_client.py and image_fetcher.py.
"""

from xml.sax.saxutils import escape
from settings import get_settings

settings = get_settings()

# ── Config (moved out of hardcoded constants) ─────────────────────────────────
_APP_STATIC_ID = "759935158"
_CALLER_ID = "SVC_VEHREPR_DESKTOP"
_CALLING_APP = "VEHREPR_DESKTOP"
_CALLING_HOST = "tomcat8080:8080"
_CALLER_GRP = "98"

# ── Namespaces ────────────────────────────────────────────────────────────────

NS_EST = "http://erac.com/vrservices/webservice/estimateWeb"
NS_WEB = "http://erac.com/vrservices/webservice"
NS_SEC = "http://erac.com/services/security"
NS_LOC = "http://erac.com/services/common/locale"
NS_TYP = "http://erac.com/vrservices/webservice/typeCodeWeb"
NS_ATT = "http://erac.com/vrservices/webservice/attachmentWeb"
NS_REP = "http://erac.com/vrservices/webservice/repairWeb"


def _ns_block(default_ns: str) -> str:
    return f"""
    xmlns="{default_ns}"
    xmlns:web="{NS_WEB}"
    xmlns:sec="{NS_SEC}"
    xmlns:loc="{NS_LOC}"
    """


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


# ── Builders ──────────────────────────────────────────────────────────────────


def build_search_body(token, status_code, group, start_row, rows_per_page):
    return f"""<SearchEstimateRQ {_ns_block(NS_EST)} xmlns:typ="{NS_TYP}">
{_request_block()}
{_security_block(token)}
{_locale_block()}
<SearchCriteria>
    <web:OrderBy>estimateReceivedDate</web:OrderBy>
    <web:OrderDirection>desc</web:OrderDirection>
    <web:PaginationDetail>
        <web:StartRowNumber>{start_row}</web:StartRowNumber>
        <web:RowsPerPage>{rows_per_page}</web:RowsPerPage>
    </web:PaginationDetail>
    <CountryCode>USA</CountryCode>
    <Group>{escape(group)}</Group>
    <EstimateStatus>
        <typ:Code>{escape(status_code)}</typ:Code>
    </EstimateStatus>
</SearchCriteria>
</SearchEstimateRQ>"""


def build_estimate_detail_body(token, est_id):
    return f"""<GetEstimateDetailForSubtotalsRQ {_ns_block(NS_EST)}>
{_request_block()}
{_security_block(token)}
{_locale_block()}
<EstimateId>{escape(est_id)}</EstimateId>
</GetEstimateDetailForSubtotalsRQ>"""


def build_electronic_estimate_body(token, est_id):
    return f"""<GetElectronicEstimateRQ {_ns_block(NS_EST)}>
{_request_block()}
{_security_block(token)}
{_locale_block()}
<EstimateId>{escape(est_id)}</EstimateId>
</GetElectronicEstimateRQ>"""


def build_cdr_body(token, vendor_id, group_number):
    return f"""<GetCDRGroupVendorRQ {_ns_block(NS_CDR)}>
{_request_block()}
{_security_block(token)}
{_locale_block()}
<VendorId>{escape(vendor_id)}</VendorId>
<GroupNumber>{escape(group_number)}</GroupNumber>
</GetCDRGroupVendorRQ>"""


def build_image_list_body(token, est_id):
    return f"""<GetAttachmentsForEstimateRQ {_ns_block(NS_ATT)}>
{_request_block()}
{_security_block(token)}
{_locale_block()}
<EstimateId>{escape(est_id)}</EstimateId>
<NeedAttachmentBinaryDataIndicator>false</NeedAttachmentBinaryDataIndicator>
</GetAttachmentsForEstimateRQ>"""


def build_image_bytes_body(token, attachment_id):
    return f"""<GetAttachmentBytesRQ {_ns_block(NS_ATT)}>
{_request_block()}
{_security_block(token)}
{_locale_block()}
<AttachmentId>{escape(attachment_id)}</AttachmentId>
<NeedAttachmentBinaryDataIndicator>true</NeedAttachmentBinaryDataIndicator>
</GetAttachmentBytesRQ>"""


def build_repair_incident_body(token, repair_incident_id):
    return f"""<SearchRepairIncidentRQ {_ns_block(NS_REP)}>
{_request_block()}
{_security_block(token)}
{_locale_block()}
<SearchCriteria>
    <RepairIncidentId>{escape(repair_incident_id)}</RepairIncidentId>
    <IncludeDeleted>true</IncludeDeleted>
</SearchCriteria>
</SearchRepairIncidentRQ>"""
