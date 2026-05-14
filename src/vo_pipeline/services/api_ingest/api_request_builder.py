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
