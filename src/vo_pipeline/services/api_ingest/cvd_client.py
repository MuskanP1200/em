
"""
cvd_client.py
=============
Fetches license plate numbers from the CVD (Connected Vehicle Data) fleet
vehicle search API for a list of VINs.

The CVD API accepts up to 100 VINs per request. This module batches the input
list into chunks of `batch_size` (default 50) to keep payloads well under the
size limit, then merges results into a single VIN → plate dict.

Typical usage
-------------
    from api_ingest.cvd_auth import get_cvd_token
    from api_ingest.cvd_client import fetch_license_plates

    token = get_cvd_token(logon_id, password, CVD_AUTH_URL)
    plates = fetch_license_plates(token, vins, CVD_API_URL, CVD_CALLING_APP)
    # {"3N1CN7AP9KL981294": "ABC1234", "1G1105SA3JU100216": None, ...}

If a batch request fails, those VINs map to None and the pipeline falls back
to the license plate from the estimate XML — the pipeline never stops.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_CVD_TIMEOUT = 30
_CVD_ACCEPT = (
    "application/prs.com-ehi.vehicle.fleetVehicle.salesVehicleDetails+json; version=2.12.0"
)
_CVD_WORKFLOW_ID = "BP_Workflow"
_CVD_LOCALE = "eng-USA"


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_headers(token: str, calling_app: str) -> dict[str, str]:
    """Build per-request headers. Trace IDs are unique per call."""
    trace_id = str(uuid.uuid4())
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": _CVD_ACCEPT,
        "Ehi-Calling-Application": calling_app,
        "Ehi-Workflow-Id": _CVD_WORKFLOW_ID,
        "Ehi-Locale": _CVD_LOCALE,
        "X-B3-SpanId": trace_id,
        "X-B3-TraceId": trace_id,
    }


def _post_batch(
    token: str,
    vins: list[str],
    api_url: str,
    calling_app: str,
) -> list[dict]:
    """POST one batch of VINs and return the raw fleetVehicles list."""
    payload = json.dumps({
        "fleetVehicle": {"vehicleAsset": {"vins": vins}},
        "sort": None,
        "pagination": None,
    })
    resp = requests.post(
        api_url,
        headers=_build_headers(token, calling_app),
        data=payload,
        timeout=_CVD_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("fleetVehicles", [])


# ── Public API ────────────────────────────────────────────────────────────────


def fetch_license_plates(
    token: str,
    vins: list[str],
    api_url: str,
    calling_app: str,
    batch_size: int = 50,
) -> dict[str, Optional[str]]:
    """
    Look up registration plate numbers for ``vins`` via CVD.

    - Filters out None/empty VINs before sending.
    - Deduplicates VINs so each is queried only once.
    - Splits into chunks of ``batch_size`` (hard-capped at 100, the API limit).
    - On batch failure, logs the error and maps those VINs to None so the
      caller can fall back to the estimate-sourced plate.

    Returns
    -------
    dict mapping each input VIN to:
        - str  : plate number returned by CVD  (source: CVD)
        - None : CVD had no plate for this VIN (source: fallback to estimate)
    """
    clean_vins = [v for v in vins if v]
    unique_vins: list[str] = list(dict.fromkeys(clean_vins))  # dedup, preserve order

    if not unique_vins:
        logger.warning("CVD: no valid VINs supplied — skipping plate lookup")
        return {}

    batch_size = min(batch_size, 100)  # enforce API hard limit
    batches = [
        unique_vins[i : i + batch_size] for i in range(0, len(unique_vins), batch_size)
    ]

    logger.info(
        "CVD plate lookup: %d unique VIN(s) → %d batch(es) of up to %d",
        len(unique_vins),
        len(batches),
        batch_size,
    )

    result: dict[str, Optional[str]] = {}

    for batch_num, batch in enumerate(batches, start=1):
        logger.debug(
            "CVD: dispatching batch %d/%d — %d VINs (first=%s)",
            batch_num,
            len(batches),
            len(batch),
            batch[0],
        )

        try:
            fleet_vehicles = _post_batch(token, batch, api_url, calling_app)
        except Exception:
            logger.error(
                "CVD: batch %d/%d request failed — %d VINs will fall back to estimate plate",
                batch_num,
                len(batches),
                len(batch),
                exc_info=True,
            )
            for vin in batch:
                result.setdefault(vin, None)
            continue

        seen_vins: set[str] = set()

        for vehicle in fleet_vehicles:
            asset = vehicle.get("vehicleAsset") or {}
            vin = asset.get("vin")
            reg = asset.get("registrationPlate") or {}
            plate = reg.get("number")

            if not vin:
                logger.debug("CVD: response record missing 'vin' field — skipping")
                continue

            seen_vins.add(vin)
            result[vin] = plate

            if plate:
                logger.debug(
                    "CVD: VIN=%s → plate=%s [source=CVD]",
                    vin,
                    plate,
                )
            else:
                logger.debug(
                    "CVD: VIN=%s → no registrationPlate in response [source=CVD, plate=None]",
                    vin,
                )

        # VINs we sent that the API did not return a record for
        for vin in batch:
            if vin not in seen_vins:
                logger.debug(
                    "CVD: VIN=%s absent from API response — will fall back to estimate plate",
                    vin,
                )
                result.setdefault(vin, None)

    found = sum(1 for v in result.values() if v)
    missing = len(unique_vins) - found
    logger.info(
        "CVD lookup complete: %d/%d VIN(s) returned a plate | %d will fall back to estimate plate",
        found,
        len(unique_vins),
        missing,
    )

    return result