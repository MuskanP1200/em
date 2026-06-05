"""
services/incident_orchestrator.py

Postgres queries and blob downloads for the incident API.

Detail endpoint fans out two things concurrently via asyncio.gather:
  - fetch_incident_core()   → one CTE covering est_info + parts + discounts
  - fetch_incident_images() → image rows from DB + all blob downloads in parallel

All DB calls go through _db_fetch / _db_fetchrow which enforce a timeout and
wrap asyncpg exceptions into DBQueryError.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import asyncpg
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas

from .backend_queries import (
    LIST_QUERY,
    CORE_QUERY,
    IMAGES_QUERY,
    FEEDBACK_INSERT_QUERY,
    FEEDBACK_TABLE_DDL,
)
from ..settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

DB_TIMEOUT = 30  # seconds


# ── Exception ────────────────────────────────────────────────────


class DBQueryError(Exception):
    """Wraps asyncpg errors into one type for route handlers to catch."""

    pass


# ── Helpers ──────────────────────────────────────────────────────


def _folder_name(incident_id: str) -> str:
    return f"EST{incident_id}"


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _fmt_rate(value: Any, unit: str) -> str:
    """Format a rate like '$40.00 / hr' or '$10.00 flat'. Returns '—' if missing."""
    try:
        return f"${float(value):.2f} {unit}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(value: Any) -> str:
    """Format a discount value as '-X%' (assumes value is already in percent units)."""
    try:
        return f"-{float(value):g}%"  # ':g' drops trailing zeros: 15.0 → '15', 15.5 → '15.5'
    except (TypeError, ValueError):
        return ""
    
def _parse_money(s) -> float:
    """Parse '$1,234.56' / '-$1,234.56' / '+$1,234.56' back to a float."""
    if not s:
        return 0.0
    try:
        return float(str(s).replace("$", "").replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _derive_status(estimate_match: Optional[str]) -> str:
    # "match" → ai_approved (green), "mismatch" → ai_flagged (red)
    # anything else (null, unexpected) → pending_ai_review (blue)
    # TODO column output values hardcoded
    if not estimate_match:
        return "pending_ai_review"
    val = estimate_match.strip().lower()
    if val == "match":
        return "ai_approved"
    if val == "no match":
        return "ai_flagged"
    return "pending_ai_review"


def _match_to_status(value):
    """DB match value → frontend ai_status. 'Match' → approved, 'No Match' → flagged, else pending."""
    if value is None:
        return "pending"
    v = str(value).strip().lower()
    if v == "match":
        return "approved"
    if v == "no match":
        return "flagged"
    return "pending"


def _is_empty_row(amount: float, status: str) -> bool:
    """True when there's no value AND no AI verdict — row not worth showing."""
    return amount == 0 and status not in ("approved", "flagged")


def _derive_line_ai_status(value: Any) -> Optional[str]:
    """Map DB match value → frontend ai_status.

    'Match'    (case-insensitive) → 'approved'
    'No Match' (case-insensitive) → 'flagged'
    anything else (None/unexpected) → None  (no pill rendered)
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if v == "match":
        return "approved"
    if v == "no match":
        return "flagged"
    if v == "pending":
        return "pending"
    return None


def _clean_num(v: Any) -> Optional[Any]:
    """Return None for None / empty string / zero. Else return the value untouched.

    Used to suppress 0-valued numeric fields (hours, qty, dollar amounts) so the
    UI shows empty cells instead of '0' / '$0.00'.
    """
    if v is None or v == "":
        return None
    try:
        return None if float(v) == 0 else v
    except (TypeError, ValueError):
        return v


def _tab_status(statuses: List[Optional[str]]) -> str:
    vals = [s for s in statuses if s]
    if not vals:
        return "pending"
    if "flagged" in vals:
        return "flagged"
    if all(v == "approved" for v in vals):
        return "approved"
    return "pending"


def _label_to_badge(label: Optional[str]) -> str:
    return {
        "VIN": "vin",
        "License Plate": "plate",
        "Odometer": "odo",
        "Other": "damage",
    }.get(label or "", "ok")


def _parse_json_col(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return list(value)
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(f"Message='Could not parse JSON column' Error='{exc}'")
        return []


# ── Postgres wrappers ────────────────────────────────────────────


async def _db_fetch(pool: asyncpg.Pool, query: str, *args) -> List[asyncpg.Record]:
    try:
        async with pool.acquire() as conn:
            return await asyncio.wait_for(conn.fetch(query, *args), timeout=DB_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(
            f"Message='Postgres query timed out' Timeout={DB_TIMEOUT}s AppName={settings.APP_NAME}"
        )
        raise
    except asyncpg.TooManyConnectionsError as e:
        logger.error(
            f"Message='Postgres pool exhausted' ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise DBQueryError("Connection pool exhausted") from e
    except asyncpg.InterfaceError as e:
        logger.error(
            f"Message='Postgres interface error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise DBQueryError("Connection error") from e
    except asyncpg.PostgresError as e:
        logger.error(
            f"Message='Postgres error' SqlState={e.sqlstate} ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise DBQueryError(f"Query failed: {e.sqlstate}") from e


async def _db_fetchrow(
    pool: asyncpg.Pool, query: str, *args
) -> Optional[asyncpg.Record]:
    try:
        async with pool.acquire() as conn:
            return await asyncio.wait_for(
                conn.fetchrow(query, *args), timeout=DB_TIMEOUT
            )
    except asyncio.TimeoutError:
        logger.error(
            f"Message='Postgres fetchrow timed out' Timeout={DB_TIMEOUT}s AppName={settings.APP_NAME}"
        )
        raise
    except asyncpg.TooManyConnectionsError as e:
        logger.error(
            f"Message='Postgres pool exhausted' ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise DBQueryError("Connection pool exhausted") from e
    except asyncpg.InterfaceError as e:
        logger.error(
            f"Message='Postgres interface error' ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise DBQueryError("Connection error") from e
    except asyncpg.PostgresError as e:
        logger.error(
            f"Message='Postgres error' SqlState={e.sqlstate} ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        raise DBQueryError(f"Query failed: {e.sqlstate}") from e


# ── Incident list ────────────────────────────────────────────────


async def fetch_incident_list(
    pool: asyncpg.Pool,
    search: str = "",
    status_filter: str = "all",
) -> List[Dict[str, Any]]:
    rows = await _db_fetch(pool, LIST_QUERY)

    results = []
    search_lower = search.strip().lower()

    for r in rows:
        incident_id = _safe_str(r["est_id"])  # Using claim ID Now
        claim_id = _safe_str(r["claim_number"])
        sub_text = _safe_str(r["vin"])  # TODO: swap ar.vin → real vehicle name column
        computed_status = _derive_status(r["overall_estimate_match"])

        if status_filter != "all" and computed_status != status_filter:
            continue

        if search_lower and (
            search_lower not in claim_id.lower()
            and search_lower not in sub_text.lower()
        ):
            continue

        results.append(
            {
                "id": incident_id,
                "claim_nbr": claim_id,
                "sub_text": sub_text,
                "status": computed_status,
            }
        )

    return results


# ── Incident core (est_info + parts + discounts in one CTE) ──────
async def fetch_incident_core(
    pool: asyncpg.Pool, incident_id: str
) -> Optional[Dict[str, Any]]:
    row = await _db_fetchrow(pool, CORE_QUERY, incident_id)
    return dict(row) if row else None


# ── Images ───────────────────────────────────────────────────────


# Maps each column to its display label and badge
_CATEGORY_META = {
    "vin": ("VIN Number", "vin"),
    "plate": ("License Plate", "plate"),
    "odo": ("Odometer", "odo"),
    "others": ("Other", "others"),
}


# def _build_sas_url(
#     blob_service: BlobServiceClient,
#     blob_path: str,
#     user_delegation_key,
# ) -> str:
#     """
#     Build a time-limited SAS URL for a blob using a User Delegation Key.
#     Works with DefaultAzureCredential / managed identity (no shared key needed).
#     SAS expires in 1 hour.
#     """
#     try:
#         sas_token = generate_blob_sas(
#             account_name=blob_service.account_name,
#             container_name=settings.AZURE_CONTAINER_NAME,
#             blob_name=blob_path,
#             user_delegation_key=user_delegation_key,
#             permission=BlobSasPermissions(read=True),
#             expiry=datetime.now(timezone.utc) + timedelta(hours=1),
#         )
#         return (
#             f"https://{blob_service.account_name}.blob.core.windows.net"
#             f"/{settings.AZURE_CONTAINER_NAME}/{blob_path}?{sas_token}"
#         )
#     except Exception as e:
#         logger.error(f"Message='SAS URL generation failed' BlobPath='{blob_path}' ErrorDetail='{e}'")
#         return ""


async def fetch_incident_images(
    pool: asyncpg.Pool,
    blob_service: BlobServiceClient,
    incident_id: str,
) -> List[Dict[str, Any]]:
    rows = await _db_fetch(pool, IMAGES_QUERY, incident_id)
    if not rows:
        return []

    container = settings.AZURE_CONTAINER_NAME
    marker = f"/{container}/"

    # User Delegation Key — works with DefaultAzureCredential / managed identity
    try:
        now = datetime.now(timezone.utc)
        result = blob_service.get_user_delegation_key(now, now + timedelta(hours=1))
        udk = await result if asyncio.iscoroutine(result) else result
    except Exception as e:
        logger.error(
            f"Message='User delegation key fetch failed' ErrorDetail='{e}' AppName={settings.APP_NAME}"
        )
        return []

    photos = []
    for row in rows:
        image_url = _safe_str(row["image_path"])
        category = _safe_str(row["category"]) or "others"

        if not image_url or marker not in image_url:
            continue

        label, badge = _CATEGORY_META.get(category, _CATEGORY_META["others"])
        blob_name = image_url.split(marker, 1)[1]

        try:
            sas_token = generate_blob_sas(
                account_name=blob_service.account_name,
                container_name=container,
                blob_name=blob_name,
                user_delegation_key=udk,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            sas_url = f"{image_url}?{sas_token}"
        except Exception as e:
            logger.error(
                f"Message='SAS URL generation failed' Url='{image_url}' ErrorDetail='{e}'"
            )
            continue

        photos.append(
            {
                "url": sas_url,
                "lightbox_url": sas_url,
                "label": label,
                "badge": badge,
                "orientation": "portrait",
                "image_data": None,
                "is_best_match": bool(row["is_best_match"]),
            }
        )

    return photos


# ── Assembler ────────────────────────────────────────────────────
def _build_progress_tabs(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    New tab model aligned with UI:
      - Vehicle Identification
      - Line Items
      - Subtotals
      - Totals
    """

    vi = detail.get("vehicle_info", {}) or {}
    bd = detail.get("breakdown", {}) or {}
    total = detail.get("total", {}) or {}
    line_items = detail.get("line_items", []) or []

    # ✅ VEHICLE STATUS
    vehicle_status = _tab_status(
        [
            (vi.get("vin") or {}).get("ai_status"),
            (vi.get("license_plate") or {}).get("ai_status"),
            (vi.get("odometer") or {}).get("ai_status"),
        ]
    )

    # ✅ LINE ITEMS STATUS (NEW — important)
    line_statuses = []
    for item in line_items:
        line_statuses.extend(
            [
                item.get("part_ai_status"),
                item.get("lbr_ai_status"),
                item.get("other_chrg_ai_status"),
            ]
        )

    line_items_status = _tab_status(line_statuses)

    # ✅ SUBTOTALS (BREAKDOWN)
    breakdown_statuses = []

    # labor
    for i in (bd.get("labor", {}) or {}).get("items", []):
        breakdown_statuses.append(i.get("ai_status"))

    # parts
    for s in (bd.get("parts", {}) or {}).get("subsections", []):
        breakdown_statuses.append(s.get("subtotal_ai_status"))
        breakdown_statuses.append(s.get("adjustment_ai_status"))

    # materials & misc
    for i in (bd.get("materials_misc", {}) or {}).get("items", []):
        breakdown_statuses.append(i.get("ai_status"))

    breakdown_status = _tab_status(breakdown_statuses)

    # ✅ TOTAL
    total_status = total.get("ai_status") or "pending"

    # ✅ FINAL CLEAN STRUCTURE
    return [
        {
            "label": "Vehicle Identification",
            "status": vehicle_status,
            "target": "section-vehicle",
        },
        {
            "label": "Line Items",
            "status": line_items_status,
            "target": "section-lineitems",
        },
        {
            "label": "Subtotals",
            "status": breakdown_status,
            "target": "section-breakdown",
        },
        {
            "label": "Totals",
            "status": total_status,
            "target": "section-total",
        },
    ]


def _tab_status(statuses):
    """Roll up a list of ai_status values to one tab status.

    Rules (in order):
      1. any flagged                                  → flagged
      2. none flagged AND none pending AND ≥1 approved → approved
      3. any pending                                  → pending
      4. otherwise (empty / unknown values only)      → approved
    """
    vals = [v for v in statuses if v]  # drop None / empty

    if "flagged" in vals:
        return "flagged"
    if "pending" not in vals and "approved" in vals:
        return "approved"
    if "pending" in vals:
        return "pending"
    return "approved"


def _fmt_date(value: Any) -> str:
    """Format date/datetime/ISO-string as MM/DD/YYYY."""
    if value is None:
        return ""

    # Native datetime/date object
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%m/%d/%Y")
        except Exception as e:
            logger.warning("Failed to parse date value: %s", e)

    # String input — try ISO parsing, then date-prefix fallback
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        if not s:
            return ""
        try:
            return datetime.fromisoformat(s).strftime("%m/%d/%Y")
        except ValueError:
            pass
        # Fallback: take YYYY-MM-DD prefix
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%m/%d/%Y")
            except ValueError:
                pass

    return _safe_str(value)


def _build_vehicle_info(core: Dict[str, Any]) -> Dict[str, Any]:
    try:
        claim_id = _safe_str(core.get("claim_number"))
        est_id = _safe_str(core.get("est_id"))
        vin_ok = core.get("vin_status")
        plate_ok = core.get("plate_status")
        odo_ok = core.get("odometer_status")

        adjuster = " ".join(
            filter(
                None,
                [
                    _safe_str(core.get("primary_adjuster_first_name")),
                    _safe_str(core.get("primary_adjuster_last_name")),
                ],
            )
        )

        fields = [
            [
                {"label": "Claim ID", "value": claim_id},
                {"label": "Est ID", "value": est_id},
                {
                    "label": "Repair Incident ID",
                    "value": _safe_str(core.get("repr_incident_id")),
                },
                {"label": "Created Date", "value": _fmt_date(core.get("created_date"))},
                {
                    "label": "Received",
                    "value": _safe_str(core.get("est_received_dt_str")),
                },
            ],
            [
                {"label": "Vendor ID", "value": _safe_str(core.get("vendor_id"))},
                {"label": "Vendor Name", "value": _safe_str(core.get("vendor_name"))},
                {
                    "label": "Adjuster ID",
                    "value": _safe_str(core.get("primary_adjuster_user_id")),
                },
                {"label": "Adjuster Name", "value": adjuster},
            ],
            [
                {"label": "Make", "value": _safe_str(core.get("veh_make"))},
                {"label": "Year", "value": _safe_str(core.get("veh_year"))},
                {"label": "Model", "value": _safe_str(core.get("veh_model"))},
                {"label": "Color", "value": _safe_str(core.get("veh_color"))},
            ],
        ]

        vin_status = (
            "not_available"
            if vin_ok is None
            else (
                "approved"
                if vin_ok is True
                else "flagged" if vin_ok is False else "pending"
            )
        )
        plate_status = (
            "not_available"
            if plate_ok is None
            else (
                "approved"
                if plate_ok is True
                else "flagged" if plate_ok is False else "pending"
            )
        )
        odo_status = (
            "not_available"
            if odo_ok is None
            else (
                "approved"
                if odo_ok is True
                else "flagged" if odo_ok is False else "pending"
            )
        )

        overall_ai_status = (
            "approved"
            if vin_status == "approved" and plate_status == "approved"
            else "flagged"
        )

        return {
            "ai_status": overall_ai_status,
            "fields": fields,
            "vin": {
                "label": "VIN",
                "value": _safe_str(core.get("vin")),
                "value_per_ai": _safe_str(core.get("est_best_match_vin"))
                or _safe_str(core.get("vin")),
                "ai_status": vin_status,
            },
            "license_plate": {
                "label": "License plate",
                "value": _safe_str(core.get("licplte_nbr")),
                "value_per_ai": _safe_str(core.get("est_best_match_plate"))
                or _safe_str(core.get("licplte_nbr")),
                "ai_status": plate_status,
            },
            "odometer": {
                "label": "Odometer (Mi/Km)",
                "value": _safe_str(core.get("odmtr_nbr")),
                "value_per_ai": _safe_str(core.get("est_best_match_odometer"))
                or _safe_str(core.get("odmtr_nbr")),
                "ai_status": odo_status,
            },
            "damage_description": _safe_str(core.get("dmg_dsc")),
        }

    except Exception as e:
        logger.error(f"Message='vehicle_info assembly failed' ErrorDetail='{e}'")
        return {
            "ai_verified": False,
            "fields": [],
            "vin": {
                "label": "VIN",
                "value": None,
                "value_per_ai": None,
                "ai_status": "pending",
            },
            "license_plate": {
                "label": "License plate",
                "value": None,
                "value_per_ai": None,
                "ai_status": "pending",
            },
            "odometer": {
                "label": "Odometer (Mi/Km)",
                "value": None,
                "value_per_ai": None,
                "ai_status": "pending",
            },
            "damage_description": "",
        }


def _build_line_items(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items = []
    for i, p in enumerate(parts, start=1):
        try:
            # Pre-clean numeric values (None for missing/zero)
            part_price = _clean_num(p.get("part_price"))
            part_qty = _clean_num(p.get("part_qty"))
            lbr_amt = _clean_num(p.get("lbr_amt"))
            lbr_hrs = _clean_num(p.get("lbr_hrs"))
            other_chrg_amt = _clean_num(p.get("other_chrg_amt"))
            other_chrg_qty = _clean_num(p.get("other_chrg_qty"))

            items.append(
                {
                    "line": _safe_str(p.get("line_nbr")).zfill(3),
                    "op": _safe_str(p.get("op_code")) or None,
                    "description": _safe_str(p.get("line_dsc")) or None,
                    "part_type": _safe_str(p.get("part_type")) or None,
                    "part_num": _safe_str(p.get("part_num")) or None,
                    "part_price": (
                        _fmt_money(part_price) if part_price is not None else None
                    ),
                    "part_qty": _safe_str(part_qty) if part_qty is not None else None,
                    "lbr_type": _safe_str(p.get("lbr_type")) or None,
                    "lbr_amt": _fmt_money(lbr_amt) if lbr_amt is not None else None,
                    "lbr_hrs": _safe_str(lbr_hrs) if lbr_hrs is not None else None,
                    "other_chrg_desc": (
                        _safe_str(p.get("other_chrg_desc"))
                        if p.get("other_chrg_desc") is not None
                        else None
                    ),
                    "other_chrg_amt": (
                        _fmt_money(other_chrg_amt)
                        if other_chrg_amt is not None
                        else None
                    ),
                    "other_chrg_qty": (
                        _safe_str(other_chrg_qty)
                        if other_chrg_qty is not None
                        else None
                    ),
                    "part_ai_status": _derive_line_ai_status(p.get("part_match")),
                    "lbr_ai_status": _derive_line_ai_status(p.get("lbr_match")),
                    "other_chrg_ai_status": _derive_line_ai_status(
                        p.get("other_chrg_match")
                    ),
                    "flag_special": False,
                }
            )
        except Exception as e:
            logger.warning(
                f"Message='Skipping malformed line item' Index={i} ErrorDetail='{e}'"
            )
    return items


def _build_breakdown(core):
    """Build the 3 breakdown cards from em_subtot_json."""
    try:
        em_rows = _parse_json_col(core.get("em_subtot_json"))
    except Exception as e:
        logger.warning(f"em_subtot_json parse failed: {e}")
        em_rows = []

    by_type = {"labor": [], "parts": [], "materials_misc": []}
    for row in em_rows:
        st = (row.get("subtot_type") or "").strip().lower()
        if st in by_type:
            by_type[st].append(row)

    parts_card = _build_parts_card(by_type["parts"])
    parts_card["findings"] = _safe_str(core.get("parts_findings"))

    labor_card = _build_labor_card(by_type["labor"])
    labor_card["findings"] = _safe_str(core.get("labor_findings"))   


    return {
        "labor": labor_card,
        "parts": parts_card,
        "materials_misc": _build_simple_card(
            by_type["materials_misc"], default_label="Others"
        ),
    }


def _build_labor_card(rows):
    """Labor rows: 'Body (2.8 hrs @ $40.00) → $112.00 [pill]'.
    Amounts come pre-computed from emsd (actual_lbr_amt / expected_lbr_amt).
    """
    items = []
    total = 0.0
    total_per_ai = 0.0
    for r in rows:
        try:
            category = _safe_str(r.get("category"))
            label = category.replace("Labor -", "").strip(" -") or category or "Labor"

            try:    actual_rate = float(r.get("actual_rate") or 0)
            except (TypeError, ValueError): actual_rate = 0.0

            try:    expected_rate = float(r.get("expected_rate") or 0)
            except (TypeError, ValueError): expected_rate = 0.0

            try:    actual_hours = float(r.get("actual_hrs") or 0)
            except (TypeError, ValueError): actual_hours = 0.0
        
            try:    expected_hours = float(r.get("expected_hrs") or 0)
            except (TypeError, ValueError): expected_hours = 0.0

            # Read pre-computed amounts from EM table — single source of truth
            try:    actual_amount = float(r.get("actual_lbr_amt") or 0)
            except (TypeError, ValueError): actual_amount = 0.0

            try:    expected_amount = float(r.get("expected_lbr_amt") or 0)
            except (TypeError, ValueError): expected_amount = 0.0

            # Label uses expected_rate (per agreed convention) with actual_rate fallback
            full_label = (
                f"{label} ({expected_hours:.1f} hrs @ {_fmt_money(expected_rate or actual_rate)})"
                if expected_rate and expected_hours
                else label
            )

            total        += actual_amount
            total_per_ai += expected_amount

            status = _match_to_status(r.get("overall_lbr_match"))
            if _is_empty_row(actual_amount, status):
                continue

            items.append(
                {
                    "label":        full_label,
                    "value":        _fmt_money(actual_amount),
                    "value_per_ai": _fmt_money(expected_amount),
                    "ai_status":    status,
                    "negative":     False,
                }
            )
        except Exception as e:
            logger.warning(f"Skipping labor row: {e}")

    # Card total only diverges when at least one row is flagged — suppress rounding noise
    has_flagged = any(item.get("ai_status") == "flagged" for item in items)
    return {
        "total":        _fmt_money(total),
        "total_per_ai": _fmt_money(total_per_ai) if has_flagged else _fmt_money(total),
        "items":        items,
    }


def _build_parts_card(rows):
    """Parts subsections with actual/expected strikethrough.
    Subtotal row    → gross_amt        (actual) vs line_tot_part_amt (expected)
    Adjustment row  → adj_tot_amt      (actual) vs line_adj_amt      (expected)
    Card total      → sum of tot_amt   (actual) vs line_net_amt      (expected)
    Statuses: parts_gross_match, adj_match (per row); overall is computed by summing nets.
    """
    subsections = []
    total = 0.0
    total_per_ai = 0.0

    def _fmt_adj(v: float) -> str:
        if v < 0:    return f"-${abs(v):,.2f}"
        if v > 0:    return f"+${v:,.2f}"
        return "$0.00"

    for r in rows:
        try:
            category = _safe_str(r.get("category"))
            short = category.replace("Parts -", "").strip(" -")
            label = f"{short} Parts" if short else (category or "Parts")

            # ── Actual amounts ─────────────────────────────────────
            try:    actual_gross = float(r.get("actual_gross_amt") or 0)
            except (TypeError, ValueError): actual_gross = 0.0

            try:    actual_adj = float(r.get("actual_adj_amt") or 0)
            except (TypeError, ValueError): actual_adj = 0.0

            try:    actual_net = float(r.get("actual_net_amt") or 0)
            except (TypeError, ValueError): actual_net = actual_gross + actual_adj

            # ── Expected amounts (from line_* columns) ─────────────
            try:    expected_gross = float(r.get("expected_gross_amt") or 0)
            except (TypeError, ValueError): expected_gross = 0.0

            try:    expected_adj = float(r.get("expected_adj_amt") or 0)
            except (TypeError, ValueError): expected_adj = 0.0

            try:    expected_net = float(r.get("expected_net_amt") or 0)
            except (TypeError, ValueError): expected_net = expected_gross + expected_adj

            # ── Percentages ────────────────────────────────────────
            try:    expected_pct = float(r.get("expected_adj_pct") or 0)
            except (TypeError, ValueError): expected_pct = 0.0

            try:    actual_pct = float(r.get("actual_adj_pct") or 0)
            except (TypeError, ValueError): actual_pct = 0.0

            # ── Per-row statuses ──────────────────────────────────
            subtotal_status   = _match_to_status(r.get("parts_gross_match"))
            adjustment_status = _match_to_status(r.get("adj_match"))

            # Skip rows with no money AND no verdicts
            if (actual_gross == 0 and actual_adj == 0
                and subtotal_status not in ("approved", "flagged")
                and adjustment_status not in ("approved", "flagged")):
                continue

            total        += actual_net
            total_per_ai += expected_net

            # Label uses EXPECTED % (parallel to labor's expected_rate)
            def _fmt_pct_signed(v: float) -> str:
                if not v: return "0%"
                sign = "-" if v < 0 else "+"
                return f"{sign}{abs(v):g}%"

            actual_pct_str   = _fmt_pct_signed(actual_pct)    # for strikethrough
            expected_pct_str = _fmt_pct_signed(expected_pct)  # primary label

            subsections.append(
                {
                    "label":                label,
                    "subtotal":             _fmt_money(actual_gross),
                    "subtotal_per_ai":      _fmt_money(expected_gross),
                    "subtotal_ai_status":   subtotal_status,
                    "adjustment":           _fmt_adj(actual_adj),
                    "adjustment_per_ai":    _fmt_adj(expected_adj),
                    "adjustment_ai_status": adjustment_status,
                    "adjustment_label":     expected_pct_str,    # expected % 
                    "adjustment_pct":       actual_pct_str,      # actual % (struck when flagged)
                }
            )
        except Exception as e:
            logger.warning(f"Skipping parts row: {e}")

    # Card total only diverges when at least one subtotal or adjustment row is flagged
    has_flagged = any(
        sub.get("subtotal_ai_status")   == "flagged"
        or sub.get("adjustment_ai_status") == "flagged"
        for sub in subsections
    )
    return {
        "total":        _fmt_money(total),
        "total_per_ai": _fmt_money(total_per_ai) if has_flagged else _fmt_money(total),
        "subsections":  subsections,
    }


def _build_simple_card(rows, default_label="—"):
    """Generic single-row card (Materials / Misc)."""
    items = []
    total = 0.0
    for r in rows:
        try:
            try:
                amount = float(r.get("tot_amt") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            total += amount
            value_fmt = _fmt_money(amount)
            status = _match_to_status(r.get("others_match"))
            if _is_empty_row(amount, status):
                continue  # ← skip the row

            items.append(
                {
                    "label": _safe_str(r.get("category")) or default_label,
                    "value": value_fmt,
                    "value_per_ai": value_fmt,
                    "ai_status": status,
                    "negative": False,
                }
            )
        except Exception as e:
            logger.warning(f"Skipping row: {e}")
    return {"total": _fmt_money(total), "items": items}


def assemble_incident_detail(
    core: Dict[str, Any],
    images: List[Dict[str, Any]],
) -> Dict[str, Any]:
    parts = _parse_json_col(core.get("line_items_json"))

    # ── Discount labels ─────────────────────────────────
    discount_labels = []
    try:
        if core.get("dmstc_part_disc_amt"):
            discount_labels.append(f"Domestic parts {_fmt_pct(core.get('dmstc_part_disc_amt'))}")
        if core.get("frn_part_disc_amt"):
            discount_labels.append(f"Foreign parts {_fmt_pct(core.get('frn_part_disc_amt'))}")
        if core.get("kyls_disc_amt"):
            discount_labels.append(f"Keyless {_fmt_pct(core.get('kyls_disc_amt'))}")
    except Exception as e:
        logger.warning(f"Message='Could not build discount labels' ErrorDetail='{e}'")

    # ── Build breakdown (needed for total computation) ───
    breakdown = _build_breakdown(core)
    labor_card = breakdown.get("labor", {})
    parts_card = breakdown.get("parts", {})
    mat_card   = breakdown.get("materials_misc", {})

    # ── Compute grand totals from card sums ──────────────
    total_actual = (
        _parse_money(labor_card.get("total"))
        + _parse_money(parts_card.get("total"))
        + _parse_money(mat_card.get("total"))
    )
    total_expected = (
        _parse_money(labor_card.get("total_per_ai") or labor_card.get("total"))
        + _parse_money(parts_card.get("total_per_ai") or parts_card.get("total"))
        + _parse_money(mat_card.get("total_per_ai")   or mat_card.get("total"))
    )
    if abs(total_actual - total_expected) < 1.0:
        total_expected = total_actual

    # ── Status derived from computed totals ──────────────
    # Total AI status follows whether actual matches expected after tolerance
    if total_actual != total_expected:
        total_ai_status = "flagged"
    else:
        total_ai_status = "approved"

    # ── Threshold tag (single block, no more duplicate) ──
    try:
        threshold = float(core.get("threshold") or 0)
        total_tag = "Above threshold" if threshold and total_actual > threshold else "Below threshold"
        threshold_str = _fmt_money(threshold) if threshold else "N/A"
    except (TypeError, ValueError):
        total_tag = "Below threshold"
        threshold_str = "N/A"

    # ── Topbar (uses total_ai_status for consistency) ────
    try:
        topbar = {
            "claim_num": _safe_str(core.get("claim_number")),
            "vehicle": " ".join(filter(None, [
                _safe_str(core.get("veh_year")),
                _safe_str(core.get("veh_make")),
                _safe_str(core.get("veh_model")),
            ])),
            "color":  _safe_str(core.get("veh_color")) or "Not Available",
            "plate":  _safe_str(core.get("licplte_nbr")) or "Not Available",
            "status": total_ai_status,
        }
    except Exception as e:
        logger.error(f"Message='topbar assembly failed' ErrorDetail='{e}'")
        topbar = {
            "claim_num": "",
            "vehicle":   "",
            "color":     "",
            "plate":     "",
            "status":    "pending",
        }

    detail = {
        "topbar": topbar,
        "vehicle_info": _build_vehicle_info(core),
        "photos": images,
        "line_items": _build_line_items(parts),
        "line_items_alert": None,
        "breakdown": breakdown,
        "total": {
            "amount":        _fmt_money(total_actual),
            "amount_per_ai": _fmt_money(total_expected),
            "tag":           total_tag,
            "threshold":     threshold_str,
            "ai_status":     total_ai_status,
        },
        "labor_rates": [
            {
                "label": "Body labor rate",
                "value": _fmt_rate(core.get("bdy_lbr_rate"), "/ hr"),
            },
            {
                "label": "Mechanical labor rate",
                "value": _fmt_rate(core.get("mchncl_lbr_rate"), "/ hr"),
            },
            {
                "label": "Frame labor rate",
                "value": _fmt_rate(core.get("frm_lbr_rate"), "/ hr"),
            },
            {
                "label": "Paint & material",
                "value": _fmt_rate(core.get("pnt_mtrl_rate"), "/ hr"),
            },
        ],
        "sublet_rates": [
            {
                "label": "Anti corrosion",
                "value": _safe_str(core.get("anti_crsn_dsc") or "-"),
            },
            {"label": "Car cover", "value": _safe_str(core.get("car_cvr_dsc") or "-")},
            {
                "label": "Hazardous waste",
                "value": _safe_str(core.get("hzrd_wst_dsc") or "-"),
            },
            {"label": "Post-scan", "value": _safe_str(core.get("postscn") or "-")},
            {
                "label": "Front radar calibration",
                "value": _safe_str(core.get("clbrtn") or "-"),
            },
        ],
        "discounts":    discount_labels,
        "special_instruction": _safe_str(core.get("specl_instruct_txt")),
        "group_note":          _safe_str(core.get("grp_note_txt")),
    }
    detail["progress_tabs"] = _build_progress_tabs(detail)
    return detail

# ── Entry point ──────────────────────────────────────────────────


async def fetch_incident_detail(
    pool: asyncpg.Pool,
    blob_service: BlobServiceClient,
    incident_id: str,
) -> Optional[Dict[str, Any]]:
    # folder = _folder_name(incident_id)

    # Both queries now use incident_id directly
    core, images = await asyncio.gather(
        fetch_incident_core(pool, incident_id),
        fetch_incident_images(pool, blob_service, incident_id),
    )

    if core is None:
        logger.warning(
            f"Message='Incident not found' IncidentId={incident_id} AppName={settings.APP_NAME}"
        )
        return None

    return assemble_incident_detail(core, images)


async def save_feedback(
    pool: asyncpg.Pool,
    incident_id: str,
    section: str,
    rating: Optional[str],
    comment: Optional[str],
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Insert a feedback row. Returns {id, created_at} on success, None on failure."""
    try:
        await pool.execute(FEEDBACK_TABLE_DDL)
        row = await _db_fetchrow(
            pool,
            FEEDBACK_INSERT_QUERY,
            incident_id,
            section,
            rating,
            comment,
            user_id,
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
    except DBQueryError:
        logger.error(
            "Feedback save failed  incident=%s  section=%r",
            incident_id,
            section,
        )
        return None
