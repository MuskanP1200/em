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

from .backend_queries import LIST_QUERY, CORE_QUERY, IMAGES_QUERY, FEEDBACK_INSERT_QUERY
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
        incident_id = _safe_str(r["est_id"])
        sub_text = _safe_str(r["vin"])  # TODO: swap ar.vin → real vehicle name column
        computed_status = _derive_status(r["overall_estimate_match"])

        if status_filter != "all" and computed_status != status_filter:
            continue

        if search_lower and (
            search_lower not in incident_id.lower()
            and search_lower not in sub_text.lower()
        ):
            continue

        results.append(
            {
                "id": incident_id,
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
            }
        )

    return photos


# ── Assembler ────────────────────────────────────────────────────


def _build_progress_tabs(core: Dict[str, Any]) -> List[Dict[str, str]]:
    try:
        vin_ok = core.get("vin_status")
        plate_ok = core.get("plate_status")
        vin_tab = (
            "approved"
            if vin_ok is True
            else ("flagged" if vin_ok is False else "pending")
        )
        plate_tab = (
            "approved"
            if plate_ok is True
            else ("flagged" if plate_ok is False else "pending")
        )
        id_tab = _tab_status([vin_tab, plate_tab])
    except Exception as e:
        logger.warning(f"Message='Could not build progress tabs' ErrorDetail='{e}'")
        id_tab = "pending"

    # TODO: derive remaining tab statuses once DB columns are confirmed
    return [
        {"label": "Vehicle ID check", "status": id_tab},
        {"label": "Parts rate & discount", "status": "pending"},
        {"label": "Labor rate & discount", "status": "pending"},
        {"label": "Materials check", "status": "pending"},
        {"label": "Totals & discount matching", "status": "pending"},
    ]


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

        # print(f"Checkpoint VIN: {vin_status}")
        # print(f"Checkpoint Plate: {plate_status}")
        # print(f"Checkpoint Odometer: {odo_status}")

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
                    "part_ai_status": _derive_line_ai_status(p.get("part_match")),
                    "lbr_ai_status": _derive_line_ai_status(p.get("lbr_match")),
                    "flag_special": False,
                }
            )
        except Exception as e:
            logger.warning(
                f"Message='Skipping malformed line item' Index={i} ErrorDetail='{e}'"
            )
    return items


def _build_breakdown(core, parts):
    """Build the four breakdown cards from em_subtot_json."""
    try:
        em_rows = _parse_json_col(core.get("em_subtot_json"))
    except Exception as e:
        logger.warning(f"em_subtot_json parse failed: {e}")
        em_rows = []

    # Group rows by subtot_type
    by_type = {"labor": [], "parts": [], "materials_misc": []}
    for row in em_rows:
        st = (row.get("subtot_type") or "").strip().lower()
        if st in by_type:
            by_type[st].append(row)
            # print(f"check_ram:{by_type[st]}")

    return {
        "labor": _build_labor_card(by_type["labor"]),
        "parts": _build_parts_card(by_type["parts"]),
        "materials_misc": _build_simple_card(
            by_type["materials_misc"], default_label="Others"
        ),
        # "miscellaneous": _build_simple_card(
        #     by_type["misc"], default_label="Other – sublet"
        # ),
    }


def _build_labor_card(rows):
    """Labor rows: 'Body (2.8 hrs @ $40.00) → $112.00 [pill]'."""
    items = []
    total = 0.0
    for r in rows:
        try:
            category = _safe_str(r.get("category"))  # 'Labor - Body'
            label = category.replace("Labor -", "").strip(" -") or category or "Labor"

            try:
                amount = float(r.get("tot_amt") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            try:
                rate = float(r.get("rate") or 0)
            except (TypeError, ValueError):
                rate = 0.0
            try:
                hours = float(r.get("tot_hr") or 0)  # ← from staging
            except (TypeError, ValueError):
                hours = (amount / rate) if rate else 0.0

            full_label = (
                f"{label} ({hours:.1f} hrs @ {_fmt_money(rate)})"
                if rate and hours
                else label
            )

            total += amount
            value_fmt = _fmt_money(amount)
            status = _match_to_status(r.get("overall_lbr_match"))
            if _is_empty_row(amount, status):
                continue  # ← skip the row
            items.append(
                {
                    "label": full_label,
                    "value": value_fmt,
                    "value_per_ai": value_fmt,
                    "ai_status": status,
                    "negative": False,
                }
            )
        except Exception as e:
            logger.warning(f"Skipping labor row: {e}")
    return {"total": _fmt_money(total), "items": items}


def _build_parts_card(rows):
    """Parts subsections: Subtotal + Adjustment(-X%)."""
    subsections = []
    total = 0.0
    for r in rows:
        try:
            category = _safe_str(r.get("category"))  # 'Parts - Domestic'
            short = category.replace("Parts -", "").strip(" -")
            label = f"{short} Parts" if short else (category or "Parts")

            try:
                gross = float(r.get("gross_amt") or 0)
            except (TypeError, ValueError):
                gross = 0.0
            try:
                adj = float(r.get("adj_tot_amt") or 0)  # ← already signed in DB
            except (TypeError, ValueError):
                adj = 0.0
            try:
                net = float(r.get("tot_amt") or 0)
            except (TypeError, ValueError):
                net = gross + adj
            total += net

            pct_label = ""
            if gross > 0 and adj:
                pct = abs(adj / gross) * 100
                pct_label = f"-{pct:g}%"

            # No parts-specific match on the EM table yet → pending
            status = _match_to_status(r.get("overall_parts_match"))
            if gross == 0 and adj == 0 and status not in ("approved", "flagged"):
                continue  # ← skip the sub

            gross_fmt = _fmt_money(gross)
            adj_fmt = _fmt_money(-abs(adj)) if adj else "$0.00"

            subsections.append(
                {
                    "label": label,
                    "subtotal": gross_fmt,
                    "subtotal_per_ai": gross_fmt,
                    "subtotal_ai_status": status,
                    "adjustment": adj_fmt,
                    "adjustment_per_ai": adj_fmt,
                    "adjustment_ai_status": status,
                    "adjustment_label": pct_label or "-0%",
                }
            )
        except Exception as e:
            logger.warning(f"Skipping parts row: {e}")
    return {"total": _fmt_money(total), "subsections": subsections}


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

    # Build discount labels from the discount columns on the estimate
    discount_labels = []
    try:
        if core.get("dmstc_part_disc_amt"):
            discount_labels.append(
                f"Domestic parts {_fmt_pct(core.get('dmstc_part_disc_amt'))}"
            )
        if core.get("frn_part_disc_amt"):
            discount_labels.append(
                f"Foreign parts {_fmt_pct(core.get('frn_part_disc_amt'))}"
            )
        if core.get("kyls_disc_amt"):
            discount_labels.append(f"Keyless {_fmt_pct(core.get('kyls_disc_amt'))}")
    except Exception as e:
        logger.warning(f"Message='Could not build discount labels' ErrorDetail='{e}'")

    # ── Total claim before taxes ──────────────────────────────────────────────
    # Both values come from a2_dd_est_overall_results (the overall_results CTE).
    try:
        total_amount = float(core.get("est_total_amount") or 0)
    except (TypeError, ValueError):
        total_amount = 0.0

    # Map DB match value → frontend ai_status
    match_val = _safe_str(core.get("overall_match")).strip().lower()
    if match_val == "match":
        total_ai_status = "approved"
    elif match_val == "no match":
        total_ai_status = "flagged"
    else:
        total_ai_status = "pending"

    # Threshold comparison          # TODO Understand thsi part
    try:
        threshold = float(core.get("threshold") or 0)  # TODO Understand thsi part
        total_tag = (
            "Above threshold"
            if threshold and total_amount > threshold
            else "Below threshold"
        )
        threshold_str = _fmt_money(threshold) if threshold else "N/A"
    except (TypeError, ValueError):
        total_tag = "Below threshold"
        threshold_str = "N/A"

    try:
        topbar = {
            "incident_num": _safe_str(core.get("est_id")),
            "vehicle": " ".join(
                filter(
                    None,
                    [
                        _safe_str(core.get("veh_year")),
                        _safe_str(core.get("veh_make")),
                        _safe_str(core.get("veh_model")),
                    ],
                )
            ),
            "color": _safe_str(core.get("veh_color")) or "Not Available",
            "state": _safe_str(core.get("state"))
            or "Not Available",  # TODO: add state column when available
            "plate": _safe_str(core.get("licplte_nbr")) or "Not Available",
            "status": _safe_str(core.get("est_stat_typ_dsc")) or "In Progress",
        }
    except Exception as e:
        logger.error(f"Message='topbar assembly failed' ErrorDetail='{e}'")
        topbar = {
            "incident_num": "",
            "vehicle": "",
            "color": "",
            "state": "",
            "plate": "",
            "status": "In Progress",
        }

    # Threshold comparison for total bar tag        # TODO Understand thsi part
    try:
        threshold = float(core.get("threshold") or 0)
        total_tag = "Above threshold" if total_amount > threshold else "Below threshold"
        threshold_str = _fmt_money(threshold) if threshold else "N/A"
    except (TypeError, ValueError):
        total_tag = "Below threshold"
        threshold_str = "N/A"

    print(f"check_ram_2:{_build_breakdown(core, parts)}")

    return {
        "topbar": topbar,
        "progress_tabs": _build_progress_tabs(core),  # TODO -> Fix appropriately
        "vehicle_info": _build_vehicle_info(core),
        "photos": images,
        "line_items": _build_line_items(parts),
        "line_items_alert": None,
        "breakdown": _build_breakdown(core, parts),
        "total": {
            "amount": _fmt_money(total_amount),
            "tag": total_tag,
            "threshold": threshold_str,
            "ai_status": total_ai_status,
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
        "discounts": discount_labels,
        "special_instruction": _safe_str(core.get("specl_instruct_txt")),
        "group_note": _safe_str(core.get("grp_note_txt")),
    }


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
