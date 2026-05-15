"""
api_client.py
=============
SOAP client for the VR Services estimate APIs.

Provides four public functions:

    search_estimates(token, ...)                → list[dict]       (all pages)
    get_estimate_detail(token, est_id)          → pd.DataFrame     (vr_vendor_id + grp_nbr)
    get_electronic_estimate_xml(token, est_id)  → str              (raw XML)
    get_cdr_group_vendor(token, vendor_id, grp) → dict             (lru-cached per vendor+group)

Authentication is handled by api_auth.get_token().
Request bodies are built by api_request_builder.
"""

from __future__ import annotations

import logging
from pathlib import Path
import defusedxml.ElementTree as ET  # nosec B405
from functools import lru_cache
from typing import Optional
import pandas as pd
import requests
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import get_settings  # noqa: E402

from api_ingest.api_request_builder import (
    build_search_body,
    build_estimate_detail_body,
    build_electronic_estimate_body,
    build_cdr_body,
    build_repair_incident_body,
)

# ── Endpoints ─────────────────────────────────────────────────────────────────
BASE_URL = get_settings().API_BASE_URL

# ── XML namespaces (for response parsing) ─────────────────────────────────────
_NS_EST = "http://erac.com/vrservices/webservice/estimateWeb"
_NS_VEN = "http://erac.com/vrservices/webservice/vendorWeb"
_NS_CDR = "http://erac.com/vrservices/webservice/cdrWeb"
_NS_REP = "http://erac.com/vrservices/webservice/repairWeb"
_NS_WEB = "http://erac.com/vrservices/webservice"
_NS_MES = "http://erac.com/services/common/message"

_COMMON_HEADERS = {
    "Content-Type": "application/xml",
    "ehi-locale": "en_US",
}

log = logging.getLogger(__name__)


# ── Internal HTTP + parsing helpers ───────────────────────────────────────────


def _post_xml(url: str, body: str, timeout: int = 30) -> ET.Element:
    """POST XML body, raise on HTTP error, return parsed ElementTree root."""
    resp = requests.post(
        url,
        data=body.encode("utf-8"),
        headers=_COMMON_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    xml_string = resp.content
    return ET.fromstring(xml_string)  # nosec B314


def _check_status(root: ET.Element, label: str) -> None:
    """Raise RuntimeError if the response status code is not 0."""
    status_el = root.find(f".//{{{_NS_MES}}}StatusCode")
    if status_el is not None and status_el.text and status_el.text.strip() != "0":
        raise RuntimeError(
            f"{label}: API returned error status {status_el.text.strip()}"
        )


def _txt(el: ET.Element, tag: str, ns: str) -> Optional[str]:
    child = el.find(f"{{{ns}}}{tag}")
    if (
        child is None
        or child.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
    ):
        return None
    return (child.text or "").strip() or None


def _bool(element, path: str, ns: str) -> bool | None:
    """Return True/False for 'true'/'false' text, None if absent."""
    v = _txt(element, path, ns)
    if v is None:
        return None
    return v.lower() == "true"


# ── 1. Search Estimates ────────────────────────────────────────────────────────


def search_estimates(
    token: str,
    status_code: str = "WAITONAUTH",
    group: str = "DR",
    rows_per_page: int = 50,
    max_records: Optional[int] = None,
) -> list[dict]:
    """
    Paginate through SearchEstimateRQ and return all matching estimates.

    Returns a list of dicts with keys:
        est_id, vendor_id, vendor_name, plate_number, vin,
        est_total_amt, repr_incident_id, est_status_code
    """
    results: list[dict] = []
    start_row = 1

    while True:
        body = build_search_body(token, status_code, group, start_row, rows_per_page)
        root = _post_xml(BASE_URL, body)
        _check_status(root, "SearchEstimates")

        items = root.findall(f".//{{{_NS_EST}}}EstimateSearchResultItem")
        if not items:
            break

        for item in items:

            def t(tag: str) -> Optional[str]:
                return _txt(item, tag, _NS_EST)

            status_el = item.find(f"{{{_NS_EST}}}EstimateStatus")
            est_stat_typ_id = (
                status_el.findtext(f"{{{_NS_EST}}}Id")
                if status_el is not None
                else None
            )
            est_stat_typ_cde = (
                status_el.findtext(f"{{{_NS_EST}}}Code")
                if status_el is not None
                else None
            )
            est_stat_typ_dsc = (
                status_el.findtext(f"{{{_NS_EST}}}Description")
                if status_el is not None
                else None
            )

            results.append(
                {
                    "est_id": t("EstimateId"),
                    "repr_incident_id": t("RepairIncidentId"),
                    "created_date": None,
                    "dmg_dsc": None,
                    "vendor_id": t("VendorId"),
                    "vendor_name": t("VendorName"),
                    "licplte_nbr": t("PlateNumber"),
                    "vin": t("VinLast8"),
                    "odmtr_nbr": None,
                    "veh_make": None,
                    "veh_model": None,
                    "veh_color": None,
                    "veh_year": None,
                    "folder_prefix": None,
                    "est_total_amt": t("EstimateTotal"),
                    "est_stat_typ_id": est_stat_typ_id,
                    "est_stat_typ_cde": est_stat_typ_cde,
                    "est_stat_typ_dsc": est_stat_typ_dsc,
                    "primary_adjuster_user_id": t("PrimaryAdjusterUserId"),
                    "primary_adjuster_first_name": t("PrimaryAdjusterFirstName"),
                    "primary_adjuster_last_name": t("PrimaryAdjusterLastName"),
                    "est_received_dt_str": t("EstimateReceivedDateTimeString"),
                    "est_received_dt": t("EstimateReceivedDateTime"),
                    "managed_tow_followup_status": t("ManagedTowFollowUpStatus"),
                    "manual_estimate_ind": t("ManualEstimateInd"),
                    "note_to_shop": t("NoteToShop"),
                }
            )

        log.info(
            "SearchEstimates page start_row=%d → %d items (total so far: %d)",
            start_row,
            len(items),
            len(results),
        )

        if len(items) < rows_per_page:
            break
        if max_records and len(results) >= max_records:
            results = results[:max_records]
            break

        start_row += rows_per_page

    log.info("SearchEstimates complete: %d estimates", len(results))
    return results


# ── 2. Get Estimate Detail ────────────────────────────────────────────────────


def get_estimate_detail(token: str, est_id: str) -> pd.DataFrame:
    """
    Call GetEstimateDetailForSubtotalsRQ.

    Returns a single-row DataFrame with columns:
        est_id, vr_vendor_id, grp_nbr, repr_incident_id,
        og_rcvd_dte, manual_estimate
    """
    body = build_estimate_detail_body(token, est_id)
    root = _post_xml(BASE_URL, body)
    _check_status(root, f"GetEstimateDetail({est_id})")

    est_info = root.find(f"{{{_NS_EST}}}EstimateInfo")
    if est_info is None:
        log.warning("est_id %s: no EstimateInfo in detail response", est_id)
        return pd.DataFrame(
            [
                {
                    "est_id": est_id,
                    "vr_vendor_id": None,
                    "grp_nbr": None,
                    "repr_incident_id": None,
                    "og_rcvd_dte": None,
                    "manual_estimate": None,
                }
            ]
        )

    vr_vendor_id = None
    vendor_el = est_info.find(f"{{{_NS_EST}}}Vendor")
    if vendor_el is not None:
        for path in [
            f"{{{_NS_VEN}}}VendorAssociations/{{{_NS_VEN}}}VrVendorId",
            f"{{{_NS_EST}}}VendorAssociations/{{{_NS_EST}}}VrVendorId",
            f".//{{{_NS_VEN}}}VrVendorId",
            f".//{{{_NS_EST}}}VrVendorId",
            f"{{{_NS_VEN}}}VendorGID",
            f"{{{_NS_EST}}}VendorGID",
        ]:
            el = vendor_el.find(path)
            if el is not None and el.text:
                vr_vendor_id = el.text.strip()
                break

    return pd.DataFrame(
        [
            {
                "est_id": _txt(est_info, "EstimateId", _NS_EST),
                "vr_vendor_id": vr_vendor_id,
                "grp_nbr": _txt(est_info, "VendorGroup", _NS_EST),
                "repr_incident_id": _txt(est_info, "ReprIncidentId", _NS_EST),
                "og_rcvd_dte": _txt(est_info, "ReceivedOriginalDate", _NS_EST),
                "manual_estimate": _bool(est_info, "ManualEstimate", _NS_EST),
            }
        ]
    )


# ── 3. Get Electronic Estimate (raw XML) ──────────────────────────────────────


def get_electronic_estimate_xml(token: str, est_id: str) -> str:
    """
    Call GetElectronicEstimateRQ and return the raw XML response string.
    Pass the returned string to parse_estimate_xml_from_string() for parsing.
    """
    body = build_electronic_estimate_body(token, est_id)
    resp = requests.post(
        BASE_URL,
        data=body.encode("utf-8"),
        headers=_COMMON_HEADERS,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


# ── 4. Get CDR Group Vendor (cached per vendor + group) ───────────────────────


@lru_cache(maxsize=512)
def get_cdr_group_vendor(token: str, vendor_id: str, group_number: str) -> dict:
    """
    Call GetCDRGroupVendorRQ and return contracted rate/discount fields.
    Result is LRU-cached per (token, vendor_id, group_number).

    Returns dict with keys:
        grp_nbr, grp_note_txt, specl_instruct_txt,
        bdy_lbr_rate, mchncl_lbr_rate, frm_lbr_rate, almn_lbr_rate,
        pnt_mtrl_rate, dmstc_part_disc_amt, frn_part_disc_amt, kyls_disc_amt
    """
    body = build_cdr_body(token, vendor_id, group_number)
    root = _post_xml(BASE_URL, body)
    _check_status(root, f"GetCDRGroupVendor(vendor={vendor_id}, group={group_number})")

    cdr = root.find(f".//{{{_NS_CDR}}}CDRGroupVendor")
    if cdr is None:
        log.warning(
            "vendor_id=%s group=%s: no CDRGroupVendor element in response",
            vendor_id,
            group_number,
        )
        return _empty_cdr_rates()

    def flt(tag: str) -> Optional[float]:
        el = cdr.find(f"{{{_NS_CDR}}}{tag}")
        if (
            el is None
            or el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
        ):
            return None
        try:
            return float(el.text.strip()) if el.text else None
        except ValueError:
            return None

    def stxt(tag: str) -> Optional[str]:
        el = cdr.find(f"{{{_NS_CDR}}}{tag}")
        if (
            el is None
            or el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
        ):
            return None
        return (el.text or "").strip() or None

    def sbool(tag: str) -> Optional[bool]:
        v = stxt(tag)
        return None if v is None else v.lower() == "true"

    result = {
        # ── Identity ──────────────────────────────────────────────────────────
        "grp_nbr": stxt("GroupNumber"),
        "vndr_name": stxt("VendorName"),
        "grp_note_txt": stxt("GroupNotes"),
        "specl_instruct_txt": stxt("SpecialInstructions"),
        "xcld_frm_cdr_ind": sbool("ExcludeFromCDRIndicator"),
        # ── Labour rates ──────────────────────────────────────────────────────
        "bdy_lbr_rate": flt("BodyLaborRate"),
        "mchncl_lbr_rate": flt("MechanicalLaborRate"),
        "frm_lbr_rate": flt("FrameLaborRate"),
        "almn_lbr_rate": flt("AluminumLaborRate"),
        # ── Parts discounts ───────────────────────────────────────────────────
        "pnt_mtrl_rate": flt("PaintAndMaterial"),
        "dmstc_part_disc_amt": flt("DomesticPartsDiscount"),
        "frn_part_disc_amt": flt("ForeignPartsDiscount"),
        "kyls_disc_amt": flt("KeylessDiscount"),
        # ── CDR included amount range ─────────────────────────────────────────
        "cdr_included_amt_frm": flt("CDRIncludedAmountFrom"),
        "cdr_included_amt_to": flt("CDRIncludedAmountTo"),
        "thrshld_amt": flt("Threshold"),
        # ── Sublet / negotiated service amounts ───────────────────────────────
        "anti_crsn_dsc": flt("AntiCorrosion"),
        "car_cvr_dsc": flt("CarCover"),
        "clr_snd_bf_dsc": flt("ColorSandAndBuff"),
        "flx_add_dsc": flt("FlexAdditive"),
        "four_whl_algn_dsc": flt("FourWheelAlignment"),
        "frm_pull_tm_dsc": flt("FramePullTime"),
        "frm_setup_dsc": flt("FrameSetup"),
        "frnt_whl_algn_dsc": flt("FrontWheelAlignment"),
        "hzrd_wst_dsc": flt("HazardousWaste"),
        "msk_jams_dsc": flt("MaskJams"),
        "mnt_bal_tir_dsc": flt("MountAndBalTires"),
        "sm_slr_dsc": flt("SeamSealer"),
        "adhsn_prm_dsc": flt("AdhesionPromoter"),
        # ── Scan / calibration fees ───────────────────────────────────────────
        "clbrtn": flt("Calibration"),
        "prescn": flt("PreScan"),
        "postscn": flt("PostScan"),
        # ── Other fees ────────────────────────────────────────────────────────
        "est_fee": flt("EstimateFee"),
        "sublet_mrkup": flt("SubletMarkup"),
        "tear_down_fee": flt("TearDownFee"),
    }

    log.debug(
        "CDR rates vendor=%s group=%s: bdy=%.0f mech=%s frm=%s dom_disc=%s",
        vendor_id,
        group_number,
        result["bdy_lbr_rate"] or 0,
        result["mchncl_lbr_rate"],
        result["frm_lbr_rate"],
        result["dmstc_part_disc_amt"],
    )
    return result


def _empty_cdr_rates() -> dict:
    """Return a dict of None values matching all keys in get_cdr_group_vendor()."""
    return {
        k: None
        for k in [
            "grp_nbr",
            "vndr_name",
            "grp_note_txt",
            "specl_instruct_txt",
            "xcld_frm_cdr_ind",
            "bdy_lbr_rate",
            "mchncl_lbr_rate",
            "frm_lbr_rate",
            "almn_lbr_rate",
            "pnt_mtrl_rate",
            "dmstc_part_disc_amt",
            "frn_part_disc_amt",
            "kyls_disc_amt",
            "cdr_included_amt_frm",
            "cdr_included_amt_to",
            "thrshld_amt",
            "anti_crsn_dsc",
            "car_cvr_dsc",
            "clr_snd_bf_dsc",
            "flx_add_dsc",
            "four_whl_algn_dsc",
            "frm_pull_tm_dsc",
            "frm_setup_dsc",
            "frnt_whl_algn_dsc",
            "hzrd_wst_dsc",
            "msk_jams_dsc",
            "mnt_bal_tir_dsc",
            "sm_slr_dsc",
            "adhsn_prm_dsc",
            "clbrtn",
            "prescn",
            "postscn",
            "est_fee",
            "sublet_mrkup",
            "tear_down_fee",
        ]
    }


# ── 5. Get Repair Incident (damage description) ────────────────────────────────


def get_repair_incident_detail(token: str, repair_incident_id: str) -> Optional[str]:
    """
    Fetch damage description for a repair incident from SearchRepairIncidentRQ.

    Parameters
    ----------
    token                : auth token from get_token()
    repair_incident_id   : repair incident ID from estimate search results

    Returns
    -------
    Damage description string, or None if not found or error

    Response contains:
        branch, buyBack, claimNumber, damageDescription, groupNumber,
        incidentId, incidentTypeDesc, legacyClaimNumber
    """
    if not repair_incident_id:
        return None

    try:
        body = build_repair_incident_body(token, repair_incident_id)
        root = _post_xml(BASE_URL, body, timeout=30)
        _check_status(root, f"SearchRepairIncident(id={repair_incident_id})")
    except Exception as e:
        log.warning(
            "repair_incident_id=%s: DAMAGE DESCRIPTION FETCH FAILED — %s",
            repair_incident_id,
            e,
            exc_info=True,
        )
        return None

    # Find repairIncident element in response
    repair_incident_el = root.find(f".//{{{_NS_REP}}}RepairIncidentSearchResultItem")
    if repair_incident_el is None:
        log.debug(
            "repair_incident_id=%s: no RepairIncident element in response",
            repair_incident_id,
        )
        return None

    # Extract damageDescription from RepairIncident
    damage_el = repair_incident_el.find(f"{{{_NS_REP}}}DamageDescription")
    if (
        damage_el is None
        or damage_el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true"
    ):
        return None

    damage_desc = (damage_el.text or "").strip() if damage_el.text else None
    if damage_desc:
        log.debug(
            "repair_incident_id=%s: damage_description='%s'",
            repair_incident_id,
            damage_desc[:100],
        )
    return damage_desc
