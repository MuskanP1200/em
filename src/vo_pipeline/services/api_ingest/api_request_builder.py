"""
api_request_builder.py
======================
Pure XML body builders for all VR Services SOAP API requests.
No I/O, no HTTP calls — just string construction.

Imported by api_client.py and image_fetcher.py.
"""

# ── Caller metadata ───────────────────────────────────────────────────────────
_APP_STATIC_ID = "759935158"
_CALLER_ID = "SVC_VEHREPR_DESKTOP"
_CALLING_APP = "VEHREPR_DESKTOP"
_CALLING_HOST = "tomcat8080:8080"
_CALLER_GRP = "98"


# ── Shared block builders ─────────────────────────────────────────────────────


def _request_block() -> str:
    return f"""  <Request>
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
    return f"""  <ServiceSecurityCredential>
    <sec:ServiceAccountToken>{token}</sec:ServiceAccountToken>
    <sec:CallingApplicationStaticId>{_APP_STATIC_ID}</sec:CallingApplicationStaticId>
  </ServiceSecurityCredential>"""


def _locale_block() -> str:
    return """  <Locale>
    <loc:CountryIso3Code>USA</loc:CountryIso3Code>
    <loc:LanguageIso3Code>eng</loc:LanguageIso3Code>
  </Locale>"""


# ── Request body builders ─────────────────────────────────────────────────────


def build_search_body(
    token: str,
    status_code: str,
    group: str,
    start_row: int,
    rows_per_page: int,
) -> str:
    return f"""<SearchEstimateRQ
    xmlns="http://erac.com/vrservices/webservice/estimateWeb"
    xmlns:web="http://erac.com/vrservices/webservice"
    xmlns:sec="http://erac.com/services/security"
    xmlns:loc="http://erac.com/services/common/locale"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:typ="http://erac.com/vrservices/webservice/typeCodeWeb">
{_request_block()}
{_security_block(token)}
{_locale_block()}
  <SearchCriteria>
    <web:OrderBy>estimateReceivedDate</web:OrderBy>
    <web:OrderDirection>desc</web:OrderDirection>
    <ClaimNumber xsi:nil="true"/>
    <web:PaginationDetail>
      <web:StartRowNumber>{start_row}</web:StartRowNumber>
      <web:RowsPerPage>{rows_per_page}</web:RowsPerPage>
    </web:PaginationDetail>
    <CountryCode>USA</CountryCode>
    <Group>{group}</Group>
    <AdjusterUserId></AdjusterUserId>
    <SupervisorId xsi:nil="true"/>
    <ReceivedStartDate xsi:nil="true"/>
    <ReceivedEndDate xsi:nil="true"/>
    <CompletionStartDate xsi:nil="true"/>
    <CompletionEndDate xsi:nil="true"/>
    <EstimateStatus>
      <typ:Code>{status_code}</typ:Code>
    </EstimateStatus>
    <MultiVendor>false</MultiVendor>
    <IncludeWarnings>true</IncludeWarnings>
    <SearchRepairByAdjuster>false</SearchRepairByAdjuster>
  </SearchCriteria>
</SearchEstimateRQ>"""


def build_estimate_detail_body(token: str, est_id: str) -> str:
    return f"""<GetEstimateDetailForSubtotalsRQ
    xmlns="http://erac.com/vrservices/webservice/estimateWeb"
    xmlns:web="http://erac.com/vrservices/webservice"
    xmlns:sec="http://erac.com/services/security"
    xmlns:loc="http://erac.com/services/common/locale">
{_request_block()}
{_security_block(token)}
{_locale_block()}
  <EstimateId>{est_id}</EstimateId>
</GetEstimateDetailForSubtotalsRQ>"""


def build_electronic_estimate_body(token: str, est_id: str) -> str:
    return f"""<GetElectronicEstimateRQ
    xmlns="http://erac.com/vrservices/webservice/estimateWeb"
    xmlns:web="http://erac.com/vrservices/webservice"
    xmlns:sec="http://erac.com/services/security"
    xmlns:loc="http://erac.com/services/common/locale">
{_request_block()}
{_security_block(token)}
{_locale_block()}
  <EstimateId>{est_id}</EstimateId>
</GetElectronicEstimateRQ>"""


def build_cdr_body(token: str, vendor_id: str, group_number: str) -> str:
    return f"""<GetCDRGroupVendorRQ
    xmlns="http://erac.com/vrservices/webservice/cdrWeb"
    xmlns:sec="http://erac.com/services/security"
    xmlns:web="http://erac.com/vrservices/webservice"
    xmlns:loc="http://erac.com/services/common/locale">
{_security_block(token)}
{_request_block()}
{_locale_block()}
  <VendorId>{vendor_id}</VendorId>
  <GroupNumber>{group_number}</GroupNumber>
</GetCDRGroupVendorRQ>"""


def build_image_list_body(token: str, est_id: str) -> str:
    return f"""<GetAttachmentsForEstimateRQ
    xmlns="http://erac.com/vrservices/webservice/attachmentWeb"
    xmlns:web="http://erac.com/vrservices/webservice"
    xmlns:sec="http://erac.com/services/security"
    xmlns:loc="http://erac.com/services/common/locale">
{_request_block()}
{_security_block(token)}
{_locale_block()}
  <EstimateId>{est_id}</EstimateId>
  <NeedAttachmentBinaryDataIndicator>false</NeedAttachmentBinaryDataIndicator>
  <NewAttachmentIndicator>false</NewAttachmentIndicator>
</GetAttachmentsForEstimateRQ>"""


def build_image_bytes_body(token: str, attachment_id: str) -> str:
    return f"""<GetAttachmentBytesRQ
    xmlns="http://erac.com/vrservices/webservice/attachmentWeb"
    xmlns:web="http://erac.com/vrservices/webservice"
    xmlns:sec="http://erac.com/services/security"
    xmlns:loc="http://erac.com/services/common/locale">
{_request_block()}
{_security_block(token)}
{_locale_block()}
  <AttachmentId>{attachment_id}</AttachmentId>
  <NeedAttachmentBinaryDataIndicator>true</NeedAttachmentBinaryDataIndicator>
</GetAttachmentBytesRQ>"""


def build_repair_incident_body(token: str, repair_incident_id: str) -> str:
    """Build request body to fetch damage description for a repair incident."""
    return f"""<SearchRepairIncidentRQ
    xmlns="http://erac.com/vrservices/webservice/repairWeb"
    xmlns:web="http://erac.com/vrservices/webservice"
    xmlns:sec="http://erac.com/services/security"
    xmlns:loc="http://erac.com/services/common/locale">
{_request_block()}
{_security_block(token)}
{_locale_block()}
  <SearchCriteria>
    <web:OrderBy/>
    <RepairIncidentId>{repair_incident_id}</RepairIncidentId>
    <IncludeDeleted>true</IncludeDeleted>
  </SearchCriteria>
</SearchRepairIncidentRQ>"""
