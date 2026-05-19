"""
estimate_client.py
==================
SOAP client for VR Services estimate APIs.

Provides seven public functions:
    search_estimates(token, ...)                → list[dict]
    get_estimate_detail(token, est_id)          → pd.DataFrame
    get_electronic_estimate_xml(token, est_id)  → str
    get_cdr_group_vendor(token, vendor_id, grp) → dict  (lru-cached)
    get_repair_incident_detail(token, id)       → Optional[str]
    get_image_list(token, est_id)               → list[dict]
    get_image_bytes(token, attachment)          → tuple[str, bytes]
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from typing import Optional

import defusedxml.ElementTree as ET  # nosec B405
import pandas as pd
import requests
import xmltodict

from settings import get_settings
from api_ingest.api_request_builder import (
    build_search_body,
    build_estimate_detail_body,
    build_electronic_estimate_body,
    build_cdr_body,
    build_repair_incident_body,
    build_image_list_body,
    build_image_bytes_body,
)
from api_ingest.rate_limiter import API_RATE_LIMITER

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL         = get_settings().API_BASE_URL
API_TIMEOUT      = getattr(get_settings(), "API_TIMEOUT",      30)
API_TIMEOUT_LONG = getattr(get_settings(), "API_TIMEOUT_LONG", 60)

_COMMON_HEADERS = {
    "Content-Type": "application/xml",
    "ehi-locale": "en_US",
}

log = logging.getLogger(__name__)

# ── Namespaces ────────────────────────────────────────────────────────────────
_NS_EST = "http://erac.com/vrservices/webservice/estimateWeb"
_NS_VEN = "http://erac.com/vrservices/webservice/vendorWeb"
_NS_CDR = "http://erac.com/vrservices/webservice/cdrWeb"
_NS_REP = "http://erac.com/vrservices/webservice/repairWeb"
_NS_MES = "http://erac.com/services/common/message"


# ── Exceptions ────────────────────────────────────────────────────────────────
class APIClientError(RuntimeError):
    """Normalized API client exception."""


# ── Core request executor ─────────────────────────────────────────────────────
def _execute_request(
    label: str,
    body: str,
    timeout: int,
    *,
    parse_xml: bool = True,
) -> str | ET.Element:
    try:
        API_RATE_LIMITER.acquire()
        resp = requests.post(
            BASE_URL,
            data=body.encode("utf-8"),
            headers=_COMMON_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()

        if not parse_xml:
            return resp.text

        root = ET.fromstring(resp.text)  # nosec B314
        _check_status(root, label)
        return root

    except requests.RequestException as e:
        log.error("%s: HTTP error", label, exc_info=True)
        raise APIClientError(f"{label}: HTTP failure") from e

    except ET.ParseError as e:
        log.error("%s: XML parse error", label, exc_info=True)
        raise APIClientError(f"{label}: invalid XML") from e

    except Exception:
        log.error("%s: unexpected error", label, exc_info=True)
        raise


# ── Common helpers ────────────────────────────────────────────────────────────
def _check_status(root: ET.Element, label: str) -> None:
    status_el = root.find(f".//{{{_NS_MES}}}StatusCode")
    if status_el is not None and status_el.text and status_el.text.strip() != "0":
        raise APIClientError(f"{label}: API status {status_el.text.strip()}")


def _get_text(el: ET.Element, tag: str, ns: str) -> Optional[str]:
    if el is None:
        return None
    child = el.find(f"{{{ns}}}{tag}")
    if child is None:
        return None
    if child.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
        return None
    return (child.text or "").strip() or None


def _get_bool(el: ET.Element, tag: str, ns: str) -> Optional[bool]:
    val = _get_text(el, tag, ns)
    return None if val is None else val.lower() == "true"


def _get_float(el: ET.Element, tag: str, ns: str) -> Optional[float]:
    val = _get_text(el, tag, ns)
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None


# ── 1. Search Estimates ───────────────────────────────────────────────────────
def search_estimates(
    token: str,
    status_code: str = "WAITONAUTH",
    group: str = "DR",
    rows_per_page: int = 50,
    max_records: Optional[int] = None,
) -> list[dict]:
    results: list[dict] = []
    start_row = 1

    while True:
        body = build_search_body(token, status_code, group, start_row, rows_per_page)
        root = _execute_request("SearchEstimates", body, API_TIMEOUT)

        items = root.findall(f".//{{{_NS_EST}}}EstimateSearchResultItem")
        if not items:
            if start_row == 1:
                log.warning("SearchEstimates: no results for status=%s group=%s", status_code, group)
            break

        for item in items:
            status_el = item.find(f"{{{_NS_EST}}}EstimateStatus")
            results.append(
                {
                    "est_id":                       _get_text(item, "EstimateId",                   _NS_EST),
                    "repr_incident_id":             _get_text(item, "RepairIncidentId",              _NS_EST),
                    "created_date":                 None,
                    "dmg_dsc":                      None,
                    "vendor_id":                    _get_text(item, "VendorId",                     _NS_EST),
                    "vendor_name":                  _get_text(item, "VendorName",                   _NS_EST),
                    "licplte_nbr":                  _get_text(item, "PlateNumber",                  _NS_EST),
                    "vin":                          _get_text(item, "VinLast8",                     _NS_EST),
                    "odmtr_nbr":                    None,
                    "veh_make":                     None,
                    "veh_model":                    None,
                    "veh_color":                    None,
                    "veh_year":                     None,
                    "folder_prefix":                None,
                    "est_total_amt":                _get_text(item, "EstimateTotal",                _NS_EST),
                    "est_stat_typ_id":              status_el.findtext(f"{{{_NS_EST}}}Id")          if status_el is not None else None,
                    "est_stat_typ_cde":             status_el.findtext(f"{{{_NS_EST}}}Code")        if status_el is not None else None,
                    "est_stat_typ_dsc":             status_el.findtext(f"{{{_NS_EST}}}Description") if status_el is not None else None,
                    "primary_adjuster_user_id":     _get_text(item, "PrimaryAdjusterUserId",        _NS_EST),
                    "primary_adjuster_first_name":  _get_text(item, "PrimaryAdjusterFirstName",     _NS_EST),
                    "primary_adjuster_last_name":   _get_text(item, "PrimaryAdjusterLastName",      _NS_EST),
                    "est_received_dt_str":          _get_text(item, "EstimateReceivedDateTimeString",_NS_EST),
                    "est_received_dt":              _get_text(item, "EstimateReceivedDateTime",      _NS_EST),
                    "managed_tow_followup_status":  _get_text(item, "ManagedTowFollowUpStatus",     _NS_EST),
                    "manual_estimate_ind":          _get_text(item, "ManualEstimateInd",             _NS_EST),
                    "note_to_shop":                 _get_text(item, "NoteToShop",                   _NS_EST),
                }
            )

        log.info("SearchEstimates start_row=%d → %d items", start_row, len(items))

        if len(items) < rows_per_page:
            break

        if max_records and len(results) >= max_records:
            return results[:max_records]

        start_row += rows_per_page

    return results


# ── 2. Estimate Detail ────────────────────────────────────────────────────────
def get_estimate_detail(token: str, est_id: str) -> pd.DataFrame:
    body = build_estimate_detail_body(token, est_id)
    root = _execute_request(f"GetEstimateDetail({est_id})", body, API_TIMEOUT)

    est_info = root.find(f"{{{_NS_EST}}}EstimateInfo")
    if est_info is None:
        log.warning("est_id=%s: missing EstimateInfo", est_id)

    vr_vendor_id = None
    if est_info is not None:
        vendor_el = est_info.find(f"{{{_NS_EST}}}Vendor")
        if vendor_el is not None:
            for path in [
                f".//{{{_NS_VEN}}}VrVendorId",
                f".//{{{_NS_EST}}}VrVendorId",
            ]:
                el = vendor_el.find(path)
                if el is not None and el.text:
                    vr_vendor_id = el.text.strip()
                    break

    return pd.DataFrame(
        [
            {
                "est_id":           _get_text(est_info, "EstimateId",           _NS_EST) or est_id,
                "vr_vendor_id":     vr_vendor_id,
                "grp_nbr":          _get_text(est_info, "VendorGroup",          _NS_EST),
                "repr_incident_id": _get_text(est_info, "ReprIncidentId",       _NS_EST),
                "og_rcvd_dte":      _get_text(est_info, "ReceivedOriginalDate",  _NS_EST),
                "manual_estimate":  _get_bool(est_info, "ManualEstimate",       _NS_EST),
            }
        ]
    )


# ── 3. Electronic Estimate (raw XML) ─────────────────────────────────────────
def get_electronic_estimate_xml(token: str, est_id: str) -> str:
    """Returns large payload unparsed — pass to parse_estimate_xml_from_string()."""
    return _execute_request(
        f"GetElectronicEstimate({est_id})",
        build_electronic_estimate_body(token, est_id),
        API_TIMEOUT_LONG,
        parse_xml=False,
    )


# ── 4. CDR Group Vendor (cached) ──────────────────────────────────────────────
@lru_cache(maxsize=512)
def get_cdr_group_vendor(token: str, vendor_id: str, group_number: str) -> dict:
    body = build_cdr_body(token, vendor_id, group_number)
    root = _execute_request(
        f"GetCDRGroupVendor(vendor={vendor_id}, group={group_number})",
        body,
        API_TIMEOUT,
    )

    cdr = root.find(f".//{{{_NS_CDR}}}CDRGroupVendor")
    if cdr is None:
        log.warning("vendor=%s group=%s: missing CDRGroupVendor element", vendor_id, group_number)

    result = {
        # ── Identity ──────────────────────────────────────────────────────────
        "grp_nbr":              _get_text(cdr,  "GroupNumber",             _NS_CDR),
        "vndr_name":            _get_text(cdr,  "VendorName",              _NS_CDR),
        "grp_note_txt":         _get_text(cdr,  "GroupNotes",              _NS_CDR),
        "specl_instruct_txt":   _get_text(cdr,  "SpecialInstructions",     _NS_CDR),
        "xcld_frm_cdr_ind":     _get_bool(cdr,  "ExcludeFromCDRIndicator", _NS_CDR),
        # ── Labour rates ──────────────────────────────────────────────────────
        "bdy_lbr_rate":         _get_float(cdr, "BodyLaborRate",           _NS_CDR),
        "mchncl_lbr_rate":      _get_float(cdr, "MechanicalLaborRate",     _NS_CDR),
        "frm_lbr_rate":         _get_float(cdr, "FrameLaborRate",          _NS_CDR),
        "almn_lbr_rate":        _get_float(cdr, "AluminumLaborRate",       _NS_CDR),
        # ── Parts discounts ───────────────────────────────────────────────────
        "pnt_mtrl_rate":        _get_float(cdr, "PaintAndMaterial",        _NS_CDR),
        "dmstc_part_disc_amt":  _get_float(cdr, "DomesticPartsDiscount",   _NS_CDR),
        "frn_part_disc_amt":    _get_float(cdr, "ForeignPartsDiscount",    _NS_CDR),
        "kyls_disc_amt":        _get_float(cdr, "KeylessDiscount",         _NS_CDR),
        # ── CDR amount range ──────────────────────────────────────────────────
        "cdr_included_amt_frm": _get_float(cdr, "CDRIncludedAmountFrom",   _NS_CDR),
        "cdr_included_amt_to":  _get_float(cdr, "CDRIncludedAmountTo",     _NS_CDR),
        "thrshld_amt":          _get_float(cdr, "Threshold",               _NS_CDR),
        # ── Sublet / negotiated service amounts ───────────────────────────────
        "anti_crsn_dsc":        _get_float(cdr, "AntiCorrosion",           _NS_CDR),
        "car_cvr_dsc":          _get_float(cdr, "CarCover",                _NS_CDR),
        "clr_snd_bf_dsc":       _get_float(cdr, "ColorSandAndBuff",        _NS_CDR),
        "flx_add_dsc":          _get_float(cdr, "FlexAdditive",            _NS_CDR),
        "four_whl_algn_dsc":    _get_float(cdr, "FourWheelAlignment",      _NS_CDR),
        "frm_pull_tm_dsc":      _get_float(cdr, "FramePullTime",           _NS_CDR),
        "frm_setup_dsc":        _get_float(cdr, "FrameSetup",              _NS_CDR),
        "frnt_whl_algn_dsc":    _get_float(cdr, "FrontWheelAlignment",     _NS_CDR),
        "hzrd_wst_dsc":         _get_float(cdr, "HazardousWaste",          _NS_CDR),
        "msk_jams_dsc":         _get_float(cdr, "MaskJams",                _NS_CDR),
        "mnt_bal_tir_dsc":      _get_float(cdr, "MountAndBalTires",        _NS_CDR),
        "sm_slr_dsc":           _get_float(cdr, "SeamSealer",              _NS_CDR),
        "adhsn_prm_dsc":        _get_float(cdr, "AdhesionPromoter",        _NS_CDR),
        # ── Scan / calibration fees ───────────────────────────────────────────
        "clbrtn":               _get_float(cdr, "Calibration",             _NS_CDR),
        "prescn":               _get_float(cdr, "PreScan",                 _NS_CDR),
        "postscn":              _get_float(cdr, "PostScan",                _NS_CDR),
        # ── Other fees ────────────────────────────────────────────────────────
        "est_fee":              _get_float(cdr, "EstimateFee",             _NS_CDR),
        "sublet_mrkup":         _get_float(cdr, "SubletMarkup",            _NS_CDR),
        "tear_down_fee":        _get_float(cdr, "TearDownFee",             _NS_CDR),
    }

    log.debug(
        "CDR rates vendor=%s group=%s: bdy=%.0f mech=%s frm=%s dom_disc=%s",
        vendor_id, group_number,
        result["bdy_lbr_rate"] or 0,
        result["mchncl_lbr_rate"],
        result["frm_lbr_rate"],
        result["dmstc_part_disc_amt"],
    )
    return result


# ── 5. Repair Incident (tolerant) ─────────────────────────────────────────────
def get_repair_incident_detail(token: str, repair_incident_id: str) -> Optional[str]:
    if not repair_incident_id:
        return None

    try:
        root = _execute_request(
            f"SearchRepairIncident({repair_incident_id})",
            build_repair_incident_body(token, repair_incident_id),
            API_TIMEOUT,
        )
    except APIClientError:
        log.warning("repair_incident_id=%s: fetch failed, dmg_dsc will be None", repair_incident_id)
        return None

    el = root.find(f".//{{{_NS_REP}}}RepairIncidentSearchResultItem")
    if el is None:
        return None

    return _get_text(el, "DamageDescription", _NS_REP)


# ── 6. Image list ─────────────────────────────────────────────────────────────
def get_image_list(token: str, est_id: str) -> list[dict]:
    raw = _execute_request(
        f"GetAttachmentsForEstimate({est_id})",
        build_image_list_body(token, est_id),
        API_TIMEOUT,
        parse_xml=False,
    )
    response = xmltodict.parse(raw)
    attachments = response.get("att:GetAttachmentsForEstimateRS", {}).get("att:Attachment", [])
    if isinstance(attachments, dict):
        attachments = [attachments]
    return attachments


# ── 7. Image bytes ────────────────────────────────────────────────────────────
def get_image_bytes(token: str, attachment: dict) -> tuple[str, bytes]:
    attachment_id   = attachment.get("att:Id")
    attachment_name = attachment.get("att:Name", f"{attachment_id}.jpg")

    raw = _execute_request(
        f"GetAttachmentBytes({attachment_id})",
        build_image_bytes_body(token, attachment_id),
        API_TIMEOUT,
        parse_xml=False,
    )
    response = xmltodict.parse(raw)
    image_b64 = response.get("att:GetAttachmentBytesRS", {}).get("att:AttachmentBytes")
    if not image_b64:
        raise APIClientError(f"GetAttachmentBytes({attachment_id}): no image bytes in response")

    return attachment_name, base64.b64decode(image_b64)


if __name__ == "__main__":
    from api_ingest.api_auth import get_token
    from estimate_matching.config import AUTH_URL

    import logging
    logging.basicConfig(level=logging.DEBUG)
    creds = get_settings().model_dump()
    token = get_token(
        username=creds["ICE_API_USER_NAME"],
        password=creds["SVC_AI_VEH_REPAIR_PASSWORD"],
        auth_url=AUTH_URL,
    )
    results = search_estimates(token, max_records=5)
    for r in results:
        print(r)
