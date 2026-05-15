# ================================================================================
# FILE: processing.py
# ================================================================================
from __future__ import annotations

import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from tqdm import tqdm

from storage import (
    list_blobs_under_prefix,
    azure_blob_url,
    azure_folder_url,
    download_blob_bytes,
)
from matching import (
    find_best_match,
    calculate_edit_distance,
    is_one_char_checksum_substitution_match,
)
from ocr import ocr_image_bytes
from vlm_classifier import classify_image_in_memory
from utils import (
    is_image_name,
    is_pdf_name,
    is_thumbnail_name,
    leaf_name_of_prefix,
    normalize_for_vin_match,
    normalize_for_odometer_match,
    odo_to_str,
    UNSUPPORTED_FOR_VLM,
    DEFAULT_MIN_TEXT_LENGTH,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Image-detail dict factory
# ----------------------------------------------------------------------

_IMAGE_DETAIL_DEFAULTS: Dict[str, Any] = {
    "folder_name": None,
    "folder_path": None,
    "image_path": None,
    "text_detected": False,
    "raw_ocr_text": None,
    "ocr_success": False,
    "error": None,
    "classified_label": None,
    "classified_confidence": None,
    "extracted_text": None,
    "classification_error": None,
    "az_vision_time_sec": None,
    "vlm_time_sec": None,
    "vlm_api_cost": None,
    "vlm_api_cost_currency": None,
    "vlm_usage": None,
    "vin_ocr_match": None,
    "best_match_vin_ocr": None,
    "ocr_vin_mismatch_count": None,
    "vin_vlm_match": None,
    "best_match_vin_vlm": None,
    "vlm_vin_mismatch_count": None,
    "vin_ocr_checksum_substitution_promoted": False,
    "vin_ocr_checksum_substitution_pos": None,
    "vin_vlm_checksum_substitution_promoted": False,
    "vin_vlm_checksum_substitution_pos": None,
    "plate_ocr_match": None,
    "best_match_plate_ocr": None,
    "plate_ocr_mismatch_count": None,
    "plate_vlm_match": None,
    "best_match_plate_vlm": None,
    "plate_vlm_mismatch_count": None,
    "odometer_ocr_match": None,
    "best_match_odometer_ocr": None,
    "odometer_ocr_mismatch_count": None,
    "odometer_vlm_match": None,
    "best_match_odometer_vlm": None,
    "odometer_vlm_mismatch_count": None,
}


def _make_image_detail(
    folder_name: str, folder_url: str, img_url: str, **overrides: Any
) -> Dict[str, Any]:
    """Build an image-detail dict with safe defaults; override any key."""
    detail = dict(_IMAGE_DETAIL_DEFAULTS)
    detail["folder_name"] = folder_name
    detail["folder_path"] = folder_url
    detail["image_path"] = img_url
    detail.update(overrides)
    return detail


# ----------------------------------------------------------------------
# OCR wrapper
# ----------------------------------------------------------------------


def _run_ocr(raw: bytes, ocr_client: Optional[Any]) -> Tuple[Dict[str, Any], float]:
    """Run Azure Vision OCR. Returns (result_dict, elapsed_ms)."""
    if ocr_client is None:
        return {
            "raw_ocr_text": None,
            "text_detected": False,
            "ocr_success": False,
            "error": "ocr_not_available",
        }, 0.0
    t0 = time.perf_counter()
    res = ocr_image_bytes(raw, ocr_client)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return res, elapsed_ms


# ----------------------------------------------------------------------
# VLM wrapper
# ----------------------------------------------------------------------


def _run_vlm(raw: bytes, classifier: Any, ext: str) -> Tuple[Dict[str, Any], float]:
    """Run VLM classification. Returns (result_dict, elapsed_ms).
    Returns empty result if format is unsupported."""
    empty = {
        "classified_label": None,
        "classified_confidence": None,
        "extracted_text": None,
        "classification_error": None,
        "api_cost": None,
        "api_cost_currency": "USD",
        "usage": None,
    }
    if ext in UNSUPPORTED_FOR_VLM:
        empty["classification_error"] = f"unsupported_image_format_for_vlm: {ext}"
        return empty, 0.0

    t0 = time.perf_counter()
    cls = classify_image_in_memory(raw, classifier)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    return {
        "classified_label": cls.get("classified_label"),
        "classified_confidence": cls.get("classified_confidence"),
        "extracted_text": cls.get("extracted_text"),
        "classification_error": cls.get("classification_error"),
        "api_cost": cls.get("api_cost"),
        "api_cost_currency": cls.get("api_cost_currency") or "USD",
        "usage": cls.get("usage"),
    }, elapsed_ms


# ----------------------------------------------------------------------
# VIN matching (with checksum-aware promotion)
# ----------------------------------------------------------------------


def _match_vin(
    ocr_text: str, vlm_text: Optional[str], target_vin: str
) -> Dict[str, Any]:
    """Match OCR / VLM extracted text against a known VIN."""
    result: Dict[str, Any] = {
        "vin_ocr_match": None,
        "best_match_vin_ocr": None,
        "ocr_mismatch": None,
        "ocr_promoted": False,
        "ocr_pos": None,
        "vin_vlm_match": None,
        "best_match_vin_vlm": None,
        "vlm_mismatch": None,
        "vlm_promoted": False,
        "vlm_pos": None,
        "img_best_mismatch": None,
        "img_best_candidate": None,
        "ocr_hit": False,
        "vlm_hit": False,
    }

    # --- OCR path ---
    norm_ocr = normalize_for_vin_match(ocr_text)
    best_ocr = find_best_match(norm_ocr, target_vin)
    ocr_mismatch = calculate_edit_distance(best_ocr, target_vin)
    ocr_match = best_ocr == target_vin
    ocr_hit = ocr_match

    ocr_promoted = False
    ocr_pos = None
    if ocr_mismatch == 1:
        ok, pos = is_one_char_checksum_substitution_match(best_ocr, target_vin)
        if ok:
            ocr_promoted, ocr_pos = True, pos
            ocr_match = True
            ocr_mismatch = 0
            best_ocr = target_vin
            ocr_hit = True

    result.update(
        vin_ocr_match=ocr_match,
        best_match_vin_ocr=best_ocr,
        ocr_mismatch=ocr_mismatch,
        ocr_promoted=ocr_promoted,
        ocr_pos=ocr_pos,
        ocr_hit=ocr_hit,
    )

    # --- VLM path ---
    if vlm_text:
        norm_vlm = normalize_for_vin_match(vlm_text)
        best_vlm = find_best_match(norm_vlm, target_vin)
        vlm_mismatch = calculate_edit_distance(best_vlm, target_vin)
        vlm_match = best_vlm == target_vin
        vlm_hit = vlm_match

        vlm_promoted = False
        vlm_pos = None
        if vlm_mismatch == 1:
            ok, pos = is_one_char_checksum_substitution_match(best_vlm, target_vin)
            if ok:
                vlm_promoted, vlm_pos = True, pos
                vlm_match = True
                vlm_mismatch = 0
                best_vlm = target_vin
                vlm_hit = True

        result.update(
            vin_vlm_match=vlm_match,
            best_match_vin_vlm=best_vlm,
            vlm_mismatch=vlm_mismatch,
            vlm_promoted=vlm_promoted,
            vlm_pos=vlm_pos,
            vlm_hit=vlm_hit,
        )

    # --- Best across sources ---
    candidates = [(result["ocr_mismatch"], result["best_match_vin_ocr"])]
    if result["vlm_mismatch"] is not None:
        candidates.append((result["vlm_mismatch"], result["best_match_vin_vlm"]))
    non_none = [(m, c) for m, c in candidates if m is not None]
    if non_none:
        best = min(non_none, key=lambda x: x[0])
        result["img_best_mismatch"] = best[0]
        result["img_best_candidate"] = best[1]

    return result


# ----------------------------------------------------------------------
# License-plate matching
# ----------------------------------------------------------------------


def _match_plate(
    ocr_text: str, vlm_text: Optional[str], target_plate: str
) -> Dict[str, Any]:
    """Match OCR / VLM extracted text against a known license plate."""
    result: Dict[str, Any] = {
        "plate_ocr_match": None,
        "best_match_plate_ocr": None,
        "plate_ocr_mismatch": None,
        "ocr_hit": False,
        "plate_vlm_match": None,
        "best_match_plate_vlm": None,
        "plate_vlm_mismatch": None,
        "vlm_hit": False,
        "img_best_mismatch": None,
        "img_best_candidate": None,
    }

    # --- OCR path ---
    norm_ocr = normalize_for_vin_match(ocr_text)
    best_ocr = find_best_match(norm_ocr, target_plate)
    if target_plate is not None:
        ocr_mismatch = calculate_edit_distance(best_ocr, target_plate)
        ocr_match = best_ocr == target_plate
        result.update(
            plate_ocr_match=ocr_match,
            best_match_plate_ocr=best_ocr,
            plate_ocr_mismatch=ocr_mismatch,
            ocr_hit=ocr_match,
        )

    # --- VLM path ---
    if vlm_text and target_plate is not None:
        norm_vlm = normalize_for_vin_match(vlm_text)
        best_vlm = find_best_match(norm_vlm, target_plate)
        vlm_mismatch = calculate_edit_distance(best_vlm, target_plate)
        vlm_match = best_vlm == target_plate
        result.update(
            plate_vlm_match=vlm_match,
            best_match_plate_vlm=best_vlm,
            plate_vlm_mismatch=vlm_mismatch,
            vlm_hit=vlm_match,
        )

    # --- Best across sources ---
    candidates = []
    if result["plate_ocr_mismatch"] is not None:
        candidates.append(
            (result["plate_ocr_mismatch"], result["best_match_plate_ocr"])
        )
    if result["plate_vlm_mismatch"] is not None:
        candidates.append(
            (result["plate_vlm_mismatch"], result["best_match_plate_vlm"])
        )
    if candidates:
        best = min(candidates, key=lambda x: x[0])
        result["img_best_mismatch"] = best[0]
        result["img_best_candidate"] = best[1]

    return result


# ----------------------------------------------------------------------
# Odometer matching
# ----------------------------------------------------------------------


def _match_odo(
    ocr_text: str, vlm_text: Optional[str], target_odo: str
) -> Dict[str, Any]:
    """Match OCR / VLM extracted text against a known odometer reading."""
    result: Dict[str, Any] = {
        "odometer_ocr_match": None,
        "best_match_odometer_ocr": None,
        "odometer_ocr_mismatch": None,
        "ocr_hit": False,
        "odometer_vlm_match": None,
        "best_match_odometer_vlm": None,
        "odometer_vlm_mismatch": None,
        "vlm_hit": False,
        "img_best_mismatch": None,
        "img_best_candidate": None,
    }

    # --- OCR path ---
    norm_ocr = normalize_for_odometer_match(ocr_text)
    norm_ocr = str(norm_ocr) if norm_ocr is not None else ""
    best_ocr = find_best_match(norm_ocr, odo_to_str(target_odo))
    if target_odo is not None:
        ocr_mismatch = calculate_edit_distance(best_ocr, odo_to_str(target_odo))
        ocr_match = best_ocr == odo_to_str(target_odo)
        result.update(
            odometer_ocr_match=ocr_match,
            best_match_odometer_ocr=best_ocr,
            odometer_ocr_mismatch=ocr_mismatch,
            ocr_hit=ocr_match,
        )

    # --- VLM path ---
    if vlm_text and target_odo is not None:
        norm_vlm = normalize_for_odometer_match(vlm_text)
        norm_vlm = str(norm_vlm) if norm_vlm is not None else ""
        best_vlm = find_best_match(norm_vlm, odo_to_str(target_odo))
        vlm_mismatch = calculate_edit_distance(best_vlm, odo_to_str(target_odo))
        vlm_match = best_vlm == odo_to_str(target_odo)
        result.update(
            odometer_vlm_match=vlm_match,
            best_match_odometer_vlm=best_vlm,
            odometer_vlm_mismatch=vlm_mismatch,
            vlm_hit=vlm_match,
        )

    # --- Best across sources ---
    candidates = []
    if result["odometer_ocr_mismatch"] is not None:
        candidates.append(
            (result["odometer_ocr_mismatch"], result["best_match_odometer_ocr"])
        )
    if result["odometer_vlm_mismatch"] is not None:
        candidates.append(
            (result["odometer_vlm_mismatch"], result["best_match_odometer_vlm"])
        )
    if candidates:
        best = min(candidates, key=lambda x: x[0])
        result["img_best_mismatch"] = best[0]
        result["img_best_candidate"] = best[1]

    return result


# ----------------------------------------------------------------------
# Single-image worker (thread-safe)
# ----------------------------------------------------------------------


def _process_single_image(
    name: str,
    container_client: Any,
    folder_name: str,
    folder_url: str,
    ocr: Optional[Any],
    classifier: Optional[Any],
    vin: Optional[str],
    norm_vin: str,
    license_plate: Optional[str],
    odometer: Optional[str],
    min_text_length: int,
    ocr_sem: Optional[threading.Semaphore],
    vlm_sem: Optional[threading.Semaphore],
    inflight_sem: Optional[threading.Semaphore],
    log: logging.Logger,
) -> Dict[str, Any]:
    """
    Process a single image: download -> OCR -> VLM -> match.
    Acquires/releases semaphores for rate limiting.
    Returns a dict with all per-image fields plus internal counters.
    """
    if inflight_sem:
        inflight_sem.acquire()
    try:
        return _process_single_image_inner(
            name,
            container_client,
            folder_name,
            folder_url,
            ocr,
            classifier,
            vin,
            norm_vin,
            license_plate,
            odometer,
            min_text_length,
            ocr_sem,
            vlm_sem,
            log,
        )
    finally:
        if inflight_sem:
            inflight_sem.release()


def _process_single_image_inner(
    name: str,
    container_client: Any,
    folder_name: str,
    folder_url: str,
    ocr: Optional[Any],
    classifier: Optional[Any],
    vin: Optional[str],
    norm_vin: str,
    license_plate: Optional[str],
    odometer: Optional[str],
    min_text_length: int,
    ocr_sem: Optional[threading.Semaphore],
    vlm_sem: Optional[threading.Semaphore],
    log: logging.Logger,
) -> Dict[str, Any]:
    """Inner logic for a single image (after inflight_sem is acquired)."""
    img_url = azure_blob_url(container_client, name)

    # 1) Download
    try:
        raw = download_blob_bytes(container_client, name)
    except Exception as e:
        # BUG-7 FIX: odometer_result key was missing in this error-return path
        return {
            "detail": _make_image_detail(
                folder_name,
                folder_url,
                img_url,
                error=f"download_failed: {e}",
                classification_error="skipped_due_to_download_error",
            ),
            "has_text": False,
            "img_url": img_url,
            "vision_ms": 0.0,
            "vision_counted": False,
            "vlm_ms": 0.0,
            "vlm_counted": False,
            "vlm_cost": 0.0,
            "api_currency": "USD",
            "vin_result": {},
            "plate_result": {},
            "odometer_result": {},  # FIX: was missing
            "classified_label": None,
        }

    # 2) OCR (rate-limited)
    if ocr_sem:
        ocr_sem.acquire()
    try:
        ocr_res, ocr_elapsed = _run_ocr(raw, ocr)
    finally:
        if ocr_sem:
            ocr_sem.release()

    vision_counted = ocr_elapsed > 0
    txt = (ocr_res.get("raw_ocr_text") or "").strip()
    has_text = len(txt) >= min_text_length

    # 3) VLM classification (rate-limited)
    classified_label = classified_confidence = extracted_text = None
    classification_error = None
    api_cost = vlm_usage = None
    api_currency = "USD"
    vlm_elapsed = 0.0
    vlm_counted = False

    if has_text and classifier:
        ext = name.lower().rsplit(".", 1)[-1]
        if vlm_sem:
            vlm_sem.acquire()
        try:
            vlm_res, vlm_elapsed = _run_vlm(raw, classifier, ext)
        finally:
            if vlm_sem:
                vlm_sem.release()
        vlm_counted = vlm_elapsed > 0
        classified_label = vlm_res["classified_label"]
        classified_confidence = vlm_res["classified_confidence"]
        extracted_text = vlm_res["extracted_text"]
        classification_error = vlm_res["classification_error"]
        api_cost = vlm_res["api_cost"]
        api_currency = vlm_res["api_cost_currency"]
        vlm_usage = vlm_res["usage"]
    elif has_text:
        classification_error = "classifier_not_available"

    # Free raw bytes early
    del raw

    # 4) VIN matching
    vin_result: Dict[str, Any] = {}
    if norm_vin and classified_label == "VIN":
        vin_result = _match_vin(txt, extracted_text, vin)

    # 5) License plate matching
    plate_result: Dict[str, Any] = {}
    if classified_label == "License Plate":
        plate_result = _match_plate(txt, extracted_text, license_plate)

    # BUG-1 FIX: was `plate_result = _match_odo(...)` which overwrote plate_result
    # and left odometer_result as an empty dict — odometer data was silently lost.
    # 6) Odometer matching
    odometer_result: Dict[str, Any] = {}
    if classified_label == "Odometer":
        odometer_result = _match_odo(txt, extracted_text, odometer)

    # 7) Assemble image detail
    detail = _make_image_detail(
        folder_name,
        folder_url,
        img_url,
        text_detected=bool(ocr_res.get("text_detected")),
        raw_ocr_text=ocr_res.get("raw_ocr_text"),
        ocr_success=bool(ocr_res.get("ocr_success")),
        error=ocr_res.get("error"),
        classified_label=classified_label,
        classified_confidence=classified_confidence,
        extracted_text=extracted_text,
        classification_error=classification_error,
        vlm_api_cost=api_cost,
        vlm_api_cost_currency=api_currency,
        vlm_usage=vlm_usage,
        az_vision_time_sec=round(ocr_elapsed / 1000, 3) if ocr_elapsed > 0 else None,
        vlm_time_sec=round(vlm_elapsed / 1000, 3) if vlm_elapsed > 0 else None,
        # VIN fields
        vin_ocr_match=vin_result.get("vin_ocr_match"),
        best_match_vin_ocr=vin_result.get("best_match_vin_ocr"),
        ocr_vin_mismatch_count=vin_result.get("ocr_mismatch"),
        vin_vlm_match=vin_result.get("vin_vlm_match"),
        best_match_vin_vlm=vin_result.get("best_match_vin_vlm"),
        vlm_vin_mismatch_count=vin_result.get("vlm_mismatch"),
        vin_ocr_checksum_substitution_promoted=vin_result.get("ocr_promoted", False),
        vin_ocr_checksum_substitution_pos=vin_result.get("ocr_pos"),
        vin_vlm_checksum_substitution_promoted=vin_result.get("vlm_promoted", False),
        vin_vlm_checksum_substitution_pos=vin_result.get("vlm_pos"),
        # Plate fields
        plate_ocr_match=plate_result.get("plate_ocr_match"),
        best_match_plate_ocr=plate_result.get("best_match_plate_ocr"),
        plate_ocr_mismatch_count=plate_result.get("plate_ocr_mismatch"),
        plate_vlm_match=plate_result.get("plate_vlm_match"),
        best_match_plate_vlm=plate_result.get("best_match_plate_vlm"),
        plate_vlm_mismatch_count=plate_result.get("plate_vlm_mismatch"),
        # BUG-2 FIX: odometer fields were read from plate_result instead of odometer_result
        odometer_ocr_match=odometer_result.get("odometer_ocr_match"),
        best_match_odometer_ocr=odometer_result.get("best_match_odometer_ocr"),
        odometer_ocr_mismatch_count=odometer_result.get("odometer_ocr_mismatch"),
        odometer_vlm_match=odometer_result.get("odometer_vlm_match"),
        best_match_odometer_vlm=odometer_result.get("best_match_odometer_vlm"),
        odometer_vlm_mismatch_count=odometer_result.get("odometer_vlm_mismatch"),
    )

    return {
        "detail": detail,
        "has_text": has_text,
        "img_url": img_url,
        "vision_ms": ocr_elapsed,
        "vision_counted": vision_counted,
        "vlm_ms": vlm_elapsed,
        "vlm_counted": vlm_counted,
        "vlm_cost": api_cost if isinstance(api_cost, (int, float)) else 0.0,
        "api_currency": api_currency,
        "vin_result": vin_result,
        "plate_result": plate_result,
        "odometer_result": odometer_result,
        "classified_label": classified_label,
    }


# ----------------------------------------------------------------------
# Main orchestrator
# ----------------------------------------------------------------------


def process_est_prefix(
    container_client,
    prefix: str,
    thumb_key: str,
    recursive: bool,
    ocr: Optional[Any],
    classifier: Optional[Any],
    show_progress: bool,
    vin: Optional[str],
    license_plate: Optional[str],
    odometer: Optional[str],
    # OCR cost comes from config, not env, so callers pass it in explicitly
    az_vision_cost_per_1k: float = 0.0,
    min_text_length: int = DEFAULT_MIN_TEXT_LENGTH,
    # ---- parallelisation knobs ----
    image_workers: int = 1,
    ocr_sem: Optional[threading.Semaphore] = None,
    vlm_sem: Optional[threading.Semaphore] = None,
    inflight_sem: Optional[threading.Semaphore] = None,
    progress_bar=None,
    folder_log: Optional[logging.Logger] = None,
) -> Dict[str, Any]:
    """
    Process all images in a single EST folder prefix.

    Returns a result dict suitable for DBWriter.upsert_folder().
    CSV/JSON output has been removed; all persistence goes through DBWriter.
    """

    log = folder_log or logging.getLogger("vi_pipeline.folder")

    # ---- Enumerate blobs ----
    blob_names = list_blobs_under_prefix(container_client, prefix, recursive)
    images = [n for n in blob_names if is_image_name(n)]
    thumb_set = {n for n in images if is_thumbnail_name(n, thumb_key)}
    non_thumb_images = [n for n in images if n not in thumb_set]
    pdfs = [n for n in blob_names if is_pdf_name(n)]
    image_set = set(images)
    pdf_set = set(pdfs)
    others = [n for n in blob_names if n not in image_set and n not in pdf_set]

    folder_name = leaf_name_of_prefix(prefix)
    folder_url = azure_folder_url(container_client, prefix)

    log.info(
        "START folder=%s (VIN=%s, License Plate=%s, Odometer=%s)",
        folder_name,
        vin or "N/A",
        license_plate or "N/A",
        odometer or "N/A",
    )

    # ---- Accumulators ----
    image_details: List[Dict[str, Any]] = []
    with_text: List[str] = []
    without_text: List[str] = []
    classification_counts: Dict[Optional[str], int] = {}

    t0_folder = time.perf_counter()
    vision_ms = vlm_ms = vlm_cost_total = 0.0
    vision_count = vlm_count = 0
    api_currency = "USD"

    norm_vin = normalize_for_vin_match(vin)
    vin_ocr_hits = vin_vlm_hits = 0
    est_best_match_vin: Optional[str] = None
    est_vin_min_mismatches: Optional[float] = None
    vin_status: Optional[bool] = None if not norm_vin else False

    norm_plate = normalize_for_vin_match(license_plate)
    plate_ocr_hits = plate_vlm_hits = 0
    est_best_match_plate: Optional[str] = None
    est_plate_min_mismatches: Optional[float] = None
    plate_status: Optional[bool] = None if not norm_plate else False

    norm_odometer = normalize_for_odometer_match(odometer)
    odometer_ocr_hits = odometer_vlm_hits = 0
    est_best_match_odometer: Optional[str] = None
    est_odometer_min_mismatches: Optional[float] = None
    odometer_status: Optional[bool] = None if not norm_odometer else False

    # Update aggregate bar total if provided
    if progress_bar is not None:
        progress_bar.total = (progress_bar.total or 0) + len(non_thumb_images)
        progress_bar.refresh()

    # ---- Per-image loop (parallelised) ----
    use_local_bar = show_progress and progress_bar is None
    local_bar = (
        tqdm(
            total=len(non_thumb_images),
            desc=f"OCR+Classify: {folder_name}",
            unit="img",
            disable=not use_local_bar,
        )
        if use_local_bar
        else None
    )

    # Ordered results list (same length as non_thumb_images)
    ordered_results: List[Optional[Dict[str, Any]]] = [None] * len(non_thumb_images)

    with ThreadPoolExecutor(max_workers=image_workers) as pool:
        future_to_idx = {}
        for idx, name in enumerate(non_thumb_images):
            fut = pool.submit(
                _process_single_image,
                name=name,
                container_client=container_client,
                folder_name=folder_name,
                folder_url=folder_url,
                ocr=ocr,
                classifier=classifier,
                vin=vin,
                norm_vin=norm_vin,
                license_plate=license_plate,
                odometer=odometer,
                min_text_length=min_text_length,
                ocr_sem=ocr_sem,
                vlm_sem=vlm_sem,
                inflight_sem=inflight_sem,
                log=log,
            )
            future_to_idx[fut] = idx

        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                img_result = fut.result()
            except Exception as e:
                log.exception(
                    "[prefix=%s] Image worker failed for index %d: %s", prefix, idx, e
                )
                img_url = azure_blob_url(container_client, non_thumb_images[idx])
                img_result = {
                    "detail": _make_image_detail(
                        folder_name,
                        folder_url,
                        img_url,
                        error=f"worker_exception: {e}",
                        classification_error="skipped_due_to_worker_error",
                    ),
                    "has_text": False,
                    "img_url": img_url,
                    "vision_ms": 0.0,
                    "vision_counted": False,
                    "vlm_ms": 0.0,
                    "vlm_counted": False,
                    "vlm_cost": 0.0,
                    "api_currency": "USD",
                    "vin_result": {},
                    "plate_result": {},
                    "odometer_result": {},
                    "classified_label": None,
                }
            ordered_results[idx] = img_result

            if local_bar:
                local_bar.update(1)
            if progress_bar:
                progress_bar.update(1)

    if local_bar:
        local_bar.close()

    # ---- Aggregate results (main thread, deterministic order) ----
    for img_result in ordered_results:
        if img_result is None:
            continue

        image_details.append(img_result["detail"])

        if img_result["vision_counted"]:
            vision_ms += img_result["vision_ms"]
            vision_count += 1
        if img_result["vlm_counted"]:
            vlm_ms += img_result["vlm_ms"]
            vlm_count += 1
        vlm_cost_total += img_result["vlm_cost"]
        if img_result["api_currency"] != "USD":
            api_currency = img_result["api_currency"]

        label = img_result["classified_label"]
        classification_counts[label] = classification_counts.get(label, 0) + 1
        (with_text if img_result["has_text"] else without_text).append(
            img_result["img_url"]
        )

        # VIN aggregation
        vin_result = img_result["vin_result"]
        if vin_result:
            if vin_result.get("ocr_hit"):
                vin_ocr_hits += 1
            if vin_result.get("vlm_hit"):
                vin_vlm_hits += 1
            img_best = vin_result.get("img_best_mismatch")
            if img_best is not None:
                if est_vin_min_mismatches is None or img_best < est_vin_min_mismatches:
                    est_vin_min_mismatches = img_best
                    est_best_match_vin = vin_result["img_best_candidate"]
                if vin_status is not None and img_best == 0.0:
                    vin_status = True

        # Plate aggregation
        plate_result = img_result["plate_result"]
        if plate_result:
            if plate_result.get("ocr_hit"):
                plate_ocr_hits += 1
            if plate_result.get("vlm_hit"):
                plate_vlm_hits += 1
            img_best = plate_result.get("img_best_mismatch")
            if img_best is not None:
                if (
                    est_plate_min_mismatches is None
                    or img_best < est_plate_min_mismatches
                ):
                    est_plate_min_mismatches = img_best
                    est_best_match_plate = plate_result["img_best_candidate"]
                if plate_status is not None and img_best == 0.0:
                    plate_status = True

        # BUG-3 FIX: was reading img_best from plate_result instead of odometer_result
        # Odometer aggregation
        odometer_result = img_result["odometer_result"]
        if odometer_result:
            if odometer_result.get("ocr_hit"):
                odometer_ocr_hits += 1
            if odometer_result.get("vlm_hit"):
                odometer_vlm_hits += 1
            img_best = odometer_result.get("img_best_mismatch")
            if img_best is not None:
                if (
                    est_odometer_min_mismatches is None
                    or img_best < est_odometer_min_mismatches
                ):
                    est_odometer_min_mismatches = img_best
                    est_best_match_odometer = odometer_result["img_best_candidate"]
                if odometer_status is not None and img_best == 0.0:
                    odometer_status = True

    # # ---- Post-loop logging ----
    # if classification_counts:
    #     cls_summary = ", ".join(
    #         f"{(k if k is not None else 'None')}={v}"
    #         for k, v in sorted(classification_counts.items(), key=lambda x: str(x[0]))
    #     )
    # else:
    #     cls_summary = "none"

    # ---- Build final result ----
    return {
        "folder_name": folder_name,
        "folder_path": folder_url,
        "total_files": len(blob_names),
        "images": len(images),
        "thumbnails": len(thumb_set),
        "images_excl_thumbs": len(non_thumb_images),
        "pdfs": len(pdfs),
        "others": len(others),
        "vin": vin,
        "license_plate": license_plate,
        "odometer": odometer,
        "images_with_text": with_text,
        "images_without_text": without_text,
        "others_list": [azure_blob_url(container_client, n) for n in others],
        "count_images_with_text": len(with_text),
        "count_images_without_text": len(without_text),
        "image_details": image_details,
        "metrics": {
            "folder_wall_time_sec": round(time.perf_counter() - t0_folder, 3),
            "az_vision": {
                "images_processed": vision_count,
                "total_sec": round(vision_ms / 1000, 3),
                "ocr_cost_total": round(
                    (len(non_thumb_images) * az_vision_cost_per_1k) / 1000, 6
                ),
                "ocr_cost_currency": api_currency,
                "avg_sec_per_image": (
                    round((vision_ms / vision_count) / 1000, 3) if vision_count else 0
                ),
            },
            "vlm": {
                "images_classified": vlm_count,
                "total_sec": round(vlm_ms / 1000, 3),
                "avg_sec_per_image": (
                    round((vlm_ms / vlm_count) / 1000, 3) if vlm_count else 0
                ),
                "api_cost_total": round(vlm_cost_total, 6),
                "api_cost_currency": api_currency,
            },
        },
        "count_images_with_vin_in_ocr": vin_ocr_hits,
        "count_images_with_vin_in_vlm": vin_vlm_hits,
        "ocr_available": ocr is not None,
        "classifier_available": classifier is not None,
        "est_best_match_vin": est_best_match_vin,
        "est_vin_min_mismatches": est_vin_min_mismatches,
        "vin_status": vin_status,
        "count_images_with_plate_in_ocr": plate_ocr_hits,
        "count_images_with_plate_in_vlm": plate_vlm_hits,
        "est_best_match_plate": est_best_match_plate,
        "est_plate_min_mismatches": est_plate_min_mismatches,
        "plate_status": plate_status,
        "count_images_with_odometer_in_ocr": odometer_ocr_hits,
        "count_images_with_odometer_in_vlm": odometer_vlm_hits,
        "est_best_match_odometer": est_best_match_odometer,
        "est_odometer_min_mismatches": est_odometer_min_mismatches,
        "odometer_status": odometer_status,
    }
