"""
parse_estimate_xml.py
=====================
Parses the GetElectronicEstimateRS XML response (from the estimate API)
and returns two DataFrames that mirror the schema produced by load_estimates():

    est_line_df  — one row per damage line item (core + parts + labour + OC columns)
    subtot_df    — one row per subtotal entry (parts subtotals + labour subtotals)

Usage:
    from parse_estimate_xml import (
        parse_estimate_xml,
        parse_estimate_xml_from_string,
        parse_estimate_xml_files,
    )

    # Single file
    est_line_df, subtot_df = parse_estimate_xml("path/to/estimate.xml")

    # From a raw XML string (e.g. from GetElectronicEstimateRQ API response)
    est_line_df, subtot_df = parse_estimate_xml_from_string(xml_string)

    # Multiple files merged
    est_line_df, subtot_df = parse_estimate_xml_files(["file1.xml", "file2.xml"])
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Union
import defusedxml.ElementTree as ET  # nosec B405
import logging
import pandas as pd

logger = logging.getLogger(__name__)
# ── XML namespace map ─────────────────────────────────────────────────────────
NS = {
    "est": "http://erac.com/vrservices/webservice/estimateWeb",
    "veh": "http://erac.com/vrservices/webservice/vehicleWeb",
    "ven": "http://erac.com/vrservices/webservice/vendorWeb",
    "mes": "http://erac.com/services/common/message",
}

# ── Labour type code → description ───────────────────────────────────────────

LBR_TYPE_MAP = {
    "B": "Labor - Body",
    "M": "Labor - Mechanical",
    "F": "Labor - Frame",
    "R": "Labor - Refinish",
    "D": "Labor - Diagnostic",
    "E": "Labor - Electrical",
    "G": "Labor - Glass",
    "S": "Labor - Structural",
    "U": "User Defined Labor",
    "1": "User Defined Labor 1",
    "2": "User Defined Labor 2",
    "3": "User Defined Labor 3",
    "4": "User Defined Labor 4",  # confirm with client
}

# ── Part type code → description ─────────────────────────────────────────────
PART_TYPE_MAP = {
    "N": "Parts - New",
    "U": "Parts - Existing",
    "E": "Parts - Existing",
    "A": "Parts - Aftermarket",
    "C": "Parts - Re-chromed",
    "L": "Parts - Recycled",
    "M": "Parts - Remanufactured",
    "P": "Parts - New, partial",
    "R": "Parts - Re-cored",
    "G": "Glass",
}


def _txt(element, path: str, ns: dict = NS) -> str | None:
    """Return stripped text of a sub-element, or None if absent/nil."""
    if element is None:
        return None
    el = element.find(path, ns)
    if el is None:
        return None
    if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
        return None
    return (el.text or "").strip() or None


def _flt(element, path: str, ns: dict = NS) -> float | None:
    if element is None:
        return None
    v = _txt(element, path, ns)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(element, path: str, ns: dict = NS) -> int | None:
    if element is None:
        return None
    v = _txt(element, path, ns)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _bool(element, path: str, ns: dict = NS) -> bool | None:
    """Return True/False for 'true'/'false' text, None if absent."""
    v = _txt(element, path, ns)
    if v is None:
        return None
    return v.lower() == "true"


def _synthetic_id(*parts) -> int:
    """Generate a stable synthetic integer ID from string parts."""
    h = hashlib.md5(
        "_".join(str(p) for p in parts).encode(), usedforsecurity=False
    ).hexdigest()
    return int(h[:12], 16)


def _parse_estimate_root(root: ET.Element) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Core parsing logic — accepts a parsed ElementTree root element.
    Called by both parse_estimate_xml() and parse_estimate_xml_from_string().
    """
    est_info_outer = root.find("est:EstimateInfo", NS)
    if est_info_outer is None:
        raise ValueError("No <est:EstimateInfo> found in XML root element")

    est_info = est_info_outer.find("est:EstimateInfo", NS)
    veh_info = est_info_outer.find("est:VehicleDataInfo", NS)
    line_items = est_info_outer.find("est:EstimateLineItemsInfo", NS)
    totals = est_info_outer.find("est:EstimateTotalLinesItem", NS)

    # ── Header fields ─────────────────────────────────────────────────────────
    est_id = _txt(est_info, "est:EstimateId")
    date_created = _txt(est_info, "est:DateCreated")
    vendor_name = _txt(est_info_outer.find("est:HeaderInfo", NS), "est:VendorName")
    est_tot_amt = _flt(totals, "est:GrandTotal")

    # ── Vehicle fields ────────────────────────────────────────────────────────
    vin = _txt(veh_info, "veh:VIN")
    licplte_nbr = _txt(veh_info, "veh:LicensePlateNbr")
    odmtr_nbr = _flt(veh_info, "veh:Odometer")
    ymms = veh_info.find("veh:Ymms", NS) if veh_info is not None else None
    veh_make = _txt(ymms, "veh:Make")
    veh_year = _txt(ymms, "veh:Year")
    veh_model = _txt(ymms, "veh:Model")
    veh_color = _txt(veh_info, "veh:Color")

    # not extracted UnitNumber, can do if required.

    # ── Labour subtotals (for lbr_hr_qty and rates) ───────────────────────────
    lbr_hr_qty = 0.0
    bdy_lbr_rate = mchncl_lbr_rate = frm_lbr_rate = None
    lbr_subtot_rows = []

    if totals is not None:
        for lt in totals.findall("est:Labor/est:LaborTotalList", NS):
            lbr_type = _txt(lt, "est:LaborType")
            rate = _flt(lt, "est:Rate")
            hours = _flt(lt, "est:Hours")
            total = _flt(lt, "est:Total")
            if lbr_type and lbr_type != "Labors Total" and hours is not None:
                lbr_hr_qty += hours
                if rate is not None:
                    if "Body" in lbr_type:
                        bdy_lbr_rate = rate
                    elif "Mechanical" in lbr_type:
                        mchncl_lbr_rate = rate
                    elif "Frame" in lbr_type:
                        frm_lbr_rate = rate
                lbr_subtot_rows.append(
                    {
                        "est_id": est_id,
                        "tot_typ_cde": "labor",
                        "cieca_tot_typ_dsc": lbr_type,
                        "tot_hr": hours,
                        "lbr_rate": rate,
                        "tot_amt": total,
                    }
                )

    # ── Parts subtotals ───────────────────────────────────────────────────────
    parts_subtot_rows = []
    parts_adj_by_type: dict[str, float] = {}

    if totals is not None:
        for pt in totals.findall("est:Parts/est:PartsTotalList", NS):
            parts_type = _txt(pt, "est:PartsType")
            subtotal = _flt(pt, "est:Subtotal")
            adj_pct = _flt(pt, "est:AdjustmentPercent")
            adj_tot_amt = _flt(pt, "est:AdjustmentAmount")
            total = _flt(pt, "est:Total")
            if parts_type and parts_type != "Parts Total":
                parts_subtot_rows.append(
                    {
                        "est_id": est_id,
                        "tot_typ_cde": "parts",
                        "cieca_tot_typ_dsc": parts_type,
                        "gross_amt": subtotal,
                        "adj_pct": adj_pct,
                        "adj_tot_amt": adj_tot_amt,
                        "tot_amt": total,
                    }
                )
                if parts_type:
                    parts_adj_by_type[parts_type] = adj_pct or 0.0

    # ── Materials subtotals ───────────────────────────────────────────────────
    materials_subtot_rows = []

    if totals is not None:
        for mt in totals.findall("est:Materials/est:MaterialsTotalList", NS):
            mat_type = _txt(mt, "est:MaterialType")
            total = _flt(mt, "est:Total")
            if mat_type and mat_type != "Materials Total":
                materials_subtot_rows.append(
                    {
                        "est_id": est_id,
                        "tot_typ_cde": "material",
                        "cieca_tot_typ_dsc": mat_type,
                        "tot_amt": total,
                    }
                )

    # ── Miscellaneous subtotals ───────────────────────────────────────────────
    misc_subtot_rows = []

    if totals is not None:
        for ms in totals.findall("est:Miscellaneous/est:MiscellaneousTotalList", NS):
            misc_type = _txt(ms, "est:MiscellaneousType")
            total = _flt(ms, "est:Total")
            if misc_type and misc_type != "Miscellaneous Total":
                misc_subtot_rows.append(
                    {
                        "est_id": est_id,
                        "tot_typ_cde": "other",
                        "cieca_tot_typ_dsc": misc_type,
                        "tot_amt": total,
                    }
                )

    # ── Adjustments subtotals ─────────────────────────────────────────────────
    adj_subtot_rows = []

    if totals is not None:
        for at in totals.findall("est:Adjustments/est:AdjustmentsTotalList", NS):
            adj_dsc = _txt(at, "est:AdjustmentsTypeDescription")
            adj_code = _txt(at, "est:AdjustmentsTypeCode")
            adj_id = _txt(at, "est:AdjustmentsTypeId")
            total = _flt(at, "est:Total")
            if adj_dsc:
                adj_subtot_rows.append(
                    {
                        "est_id": est_id,
                        "tot_typ_cde": "adjustments",
                        "cieca_tot_typ_dsc": adj_dsc,
                        "adj_typ_code": adj_code,
                        "adj_typ_id": adj_id,
                        "tot_amt": total,
                    }
                )

    # ── Damage line items ─────────────────────────────────────────────────────
    line_rows = []
    damage_lines = (
        line_items.findall("est:DamageLineItems/est:DamageLine", NS)
        if line_items
        else []
    )
    latest_revision_nbr = (
        _txt(line_items, "est:LatestRevisionNbr") if line_items else None
    )

    # Synthetic IDs (stable, reproducible)
    # elctrnc_est_dtl_id = _synthetic_id(est_id, "elctrnc")
    # est_repr_id = _synthetic_id(est_id, "repr")

    for dl in damage_lines:
        line_nbr = _int(dl, "est:LineNbr")
        line_dsc = _txt(dl, "est:Description")
        op_code = _txt(dl, "est:OpCode")
        op_code_dsc = _txt(dl, "est:OpCodeDescription")
        part_type = _txt(dl, "est:Type")  # N / U / R / A
        part_nbr = _txt(dl, "est:PartNbr")
        price = _flt(dl, "est:Price")
        qty = _flt(dl, "est:Quantity")
        labor_hrs = _flt(dl, "est:Labor")
        lbr_code = _txt(dl, "est:LaborTypeCode")  # B / M / F
        paint_hrs = _flt(dl, "est:Paint")
        paint_code = _txt(dl, "est:PaintTypeCode")
        lbr_amt = _flt(dl, "est:LaborAmountTotal")

        # OpCodeJudgeIndicator, PartPriceJudgeIndicator, LabourHourJudgeIndicator, LineDescJudgeIndicator
        newly_added_ind = _bool(dl, "est:NewlyAddedIndicator")
        judgement_ind = _bool(dl, "est:JudgementIndicator")
        op_code_judge_ind = _bool(dl, "est:OpCodeJudgeIndicator")
        paint_lbr_judge_ind = _bool(dl, "est:PaintLaborJudgeIndicator")
        part_price_judge_ind = _bool(dl, "est:PartPriceJudgeIndicator")
        lbr_hr_judge_ind = _bool(dl, "est:LaborHourJudgeIndicator")
        line_dsc_judge_ind = _bool(dl, "est:LineDescJudgeIndicator")

        # Determine if this is a parts / labour / other-charge line
        has_part = part_type is not None and price is not None and price > 0
        has_labor = labor_hrs is not None and labor_hrs > 0
        # has_paint = paint_hrs is not None and paint_hrs > 0
        has_oc = None
        if op_code_dsc:
            has_oc = (
                op_code_dsc
                if any(
                    kw in op_code_dsc.lower()
                    for kw in ["sublet", "towing", "additional"]
                )
                else None
            )

        cieca_dtl_hdr_id = _synthetic_id(est_id, line_nbr)

        # Part-specific fields
        if has_part:
            part_type_dsc = PART_TYPE_MAP.get(part_type or "", part_type or "Unknown")
            adj_pct = parts_adj_by_type.get(part_type_dsc, 0.0)
            adj_amt = round(price * (adj_pct / 100), 2) if adj_pct else None
            net_amt = round(price + (adj_amt or 0), 2)
        else:
            part_type_dsc = adj_amt = net_amt = None

        # Labour-specific fields
        lbr_type_dsc = LBR_TYPE_MAP.get(lbr_code or "", lbr_code) if has_labor else None
        lbr_rate = (
            (
                bdy_lbr_rate
                if lbr_code == "B"
                else (
                    mchncl_lbr_rate
                    if lbr_code == "M"
                    else frm_lbr_rate if lbr_code == "F" else None
                )
            )
            if has_labor
            else None
        )

        row = {
            # ── Estimate header ───────────────────────────────────────────────
            "est_id": est_id,
            "created_date": date_created,
            "est_tot_amt": est_tot_amt,
            "lbr_hr_qty": lbr_hr_qty,
            "veh_make": veh_make,
            "veh_year": veh_year,
            "veh_model": veh_model,
            "veh_color": veh_color,
            "vin": vin,
            "licplte_nbr": licplte_nbr,
            "odmtr_nbr": odmtr_nbr,
            "vendor_name": vendor_name,
            "dmg_dsc": None,  # populated by future API; placeholder so column exists
            # ── IDs (synthetic) ───────────────────────────────────────────────
            # "elctrnc_est_dtl_id": elctrnc_est_dtl_id, #remove this eventually
            # "est_repr_id": est_repr_id, #remove this eventually
            "cieca_dtl_hdr_id": cieca_dtl_hdr_id,  # remove this eventually
            # ── Line item ─────────────────────────────────────────────────────
            "rvsn_nbr": latest_revision_nbr,
            "line_nbr": line_nbr,
            "line_dsc": line_dsc,
            "op_code": op_code,
            "op_code_dsc": op_code_dsc,
            # ── Judge indicators ──────────────────────────────────────────────
            "newly_added_ind": newly_added_ind,
            "judgement_ind": judgement_ind,
            "op_code_judge_ind": op_code_judge_ind,
            "paint_lbr_judge_ind": paint_lbr_judge_ind,
            "part_price_judge_ind": part_price_judge_ind,
            "lbr_hr_judge_ind": lbr_hr_judge_ind,
            "line_dsc_judge_ind": line_dsc_judge_ind,
            # ── Parts columns ─────────────────────────────────────────────────
            "cieca_part_typ_dsc": part_type_dsc if has_part else None,
            "cieca_part_dtl_line_id": (
                cieca_dtl_hdr_id if has_part else None
            ),  # Synthetic part-detail line ID — non-None for parts lines (used as filter marker)
            "dtl_part_nbr": part_nbr,
            "dtl_part_nbr_qty": qty if has_part else None,
            "dtl_act_part_price_amt": price if has_part else None,
            "dtl_tot_part_price_amt": price if has_part else None,
            "cieca_line_adj_amt": adj_amt,
            "cieca_line_net_amt": net_amt,
            # ── Labour columns ────────────────────────────────────────────────
            "cieca_lbr_typ_dsc": lbr_type_dsc,
            "cieca_lbr_dtl_line_id": (
                cieca_dtl_hdr_id if has_labor else None
            ),  # Synthetic labour-detail line ID — non-None for labour lines
            "dtl_lbr_hr_qty": labor_hrs if has_labor else None,
            "dtl_lbr_tot_amt": lbr_amt if has_labor else None,
            "lbr_rate": lbr_rate,
            # ── Paint / refinish ──────────────────────────────────────────────
            "paint_hrs": paint_hrs,
            "paint_type_code": paint_code,
            # ── Other Charges ──────────────────────────────────────────────
            "cieca_othr_chrg_dtl_line_id": cieca_dtl_hdr_id if has_oc else None,
            "dtl_othr_chrg_price_amt": price if has_oc else None,
            "dtl_othr_chrg_qty": qty if has_oc else None,
            "cieca_othr_chrg_typ_dsc": op_code_dsc if has_oc else None,
            # ── CDR contracted rates (filled by api_loader from GetCDRGroupVendor) ──
            "grp_nbr": None,
            "bdy_lbr_rate": bdy_lbr_rate,  # charged rate (proxy; overridden by CDR API)
            "mchncl_lbr_rate": mchncl_lbr_rate,
            "frm_lbr_rate": frm_lbr_rate,
            "pnt_mtrl_rate": None,
            "dmstc_part_disc_amt": None,
            "frn_part_disc_amt": None,
            "kyls_disc_amt": None,
            "almn_lbr_rate": None,
            "specl_instruct_txt": None,
            "grp_note_txt": None,
        }
        line_rows.append(row)

    est_line_df = pd.DataFrame(line_rows)
    # subtot_df = pd.DataFrame(parts_subtot_rows + lbr_subtot_rows)

    subtot_df = pd.DataFrame(
        parts_subtot_rows
        + lbr_subtot_rows
        + materials_subtot_rows
        + misc_subtot_rows
        + adj_subtot_rows
    )

    return est_line_df, subtot_df


def parse_estimate_xml(
    source: Union[str, Path],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse a single estimate XML file.

    Returns
    -------
    est_line_df : pd.DataFrame
        One row per damage line item. Columns mirror load_estimates() output.
    subtot_df : pd.DataFrame
        One row per subtotal entry (parts + labour).
    """
    path = Path(source)
    tree = ET.parse(path)
    return _parse_estimate_root(tree.getroot())


def parse_estimate_xml_from_string(
    xml_string: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse an estimate XML string (e.g. the raw response from GetElectronicEstimateRQ).

    Returns the same (est_line_df, subtot_df) as parse_estimate_xml().
    """
    root = ET.fromstring(xml_string)
    return _parse_estimate_root(root)


def parse_estimate_xml_files(
    paths: list[Union[str, Path]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse multiple XML files and concatenate into single DataFrames."""
    all_lines = []
    all_subtot = []
    for p in paths:
        lines, subtot = parse_estimate_xml(p)
        all_lines.append(lines)
        all_subtot.append(subtot)
    est_line_df = (
        pd.concat(all_lines, ignore_index=True) if all_lines else pd.DataFrame()
    )
    subtot_df = (
        pd.concat(all_subtot, ignore_index=True) if all_subtot else pd.DataFrame()
    )
    return est_line_df, subtot_df


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    data_dir = Path(__file__).resolve().parent.parent / "data" / "input"
    files = list(data_dir.glob("*.txt")) + list(data_dir.glob("*.xml"))

    if not files:
        logger.error("No XML/txt files found in data/input/")
        sys.exit(1)

    logger.debug(f"Parsing {len(files)} file(s)...\n")
    est_line_df, subtot_df = parse_estimate_xml_files(files)

    logger.info("=== est_line_df ===")
    logger.info(f"Shape: {est_line_df.shape}")
    logger.info(
        est_line_df[
            [
                "est_id",
                "line_nbr",
                "line_dsc",
                "dtl_part_nbr",
                "dtl_tot_part_price_amt",
                "cieca_lbr_typ_dsc",
                "dtl_lbr_hr_qty",
                "dtl_lbr_tot_amt",
            ]
        ].to_string()
    )

    logger.info("=== subtot_df ===")
    logger.info(f"Shape: {subtot_df.shape}")
    logger.info(subtot_df.to_string())
