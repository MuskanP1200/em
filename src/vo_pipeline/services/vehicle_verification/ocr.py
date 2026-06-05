# ================================================================================
# FILE: ocr.py
# ================================================================================
from __future__ import annotations
import logging
import os
import time
from typing import Any, Dict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry as Urllib3Retry
from azure.core.pipeline.transport import RequestsTransport

logger = logging.getLogger(__name__)

# Hide all Azure SDK INFO logs (including http_logging_policy)
logging.getLogger("azure").setLevel(logging.WARNING)

# If you want to be very specific:
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
    logging.WARNING
)

# Optional: prevent double logging via root logger if you configured handlers there
logging.getLogger("azure").propagate = False

logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").propagate = False
logging.getLogger("httpcore").propagate = False


def _build_transport(pool_size: int) -> RequestsTransport:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool_size, pool_maxsize=pool_size, max_retries=Urllib3Retry(0)
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return RequestsTransport(session=session, session_owner=False)


# -------- Azure Vision OCR (replaces PaddleOCR) --------
def init_azure_vision(secrets, pool_size: int = 20):
    """
    Initialize Azure AI Vision Image Analysis client (READ).
    Requires env:
      - VISION_ENDPOINT
      - VISION_KEY
    Returns: (client, error_message_or_none)
    """
    try:
        from azure.ai.vision.imageanalysis import ImageAnalysisClient
        from azure.core.credentials import AzureKeyCredential
    except Exception as e:
        return None, f"azure_vision_import_failed: {e}"

    try:
        endpoint = secrets["AZURE_VISION_ENDPOINT"]
        key = secrets["AZURE_VISION_KEY"]
    except (KeyError, Exception):
        endpoint = os.getenv("VISION_ENDPOINT")
        key = os.getenv("VISION_KEY")

    if not endpoint or not key:
        return (
            None,
            "azure_vision_not_configured: missing VISION_ENDPOINT or VISION_KEY",
        )

    try:
        transport = _build_transport(pool_size)
        client = ImageAnalysisClient(
            endpoint=endpoint, credential=AzureKeyCredential(key), transport=transport
        )
        return client, None
    except Exception as e:
        return None, f"azure_vision_client_init_failed: {e}"


def _extract_text_from_vision_result(result: Any) -> str:
    """
    Robust text extraction from Azure Vision READ result.
    Concatenate all line contents from all blocks.
    """
    lines = []
    if result.read and result.read.blocks:
        for block in result.read.blocks:
            for line in block.lines:
                lines.append(line.text)

    return " ".join(lines)


def ocr_image_bytes(
    image_bytes: bytes,
    vision_client,
    retries: int = 3,
    backoff: float = 1.0,
) -> Dict[str, Any]:
    """
    OCR using Azure Vision (READ) with rate-limit-aware retry.
    Returns:
        {
            "raw_ocr_text": str or None,
            "text_detected": bool,
            "ocr_success": bool,
            "error": str or None
        }
    """
    out = {
        "raw_ocr_text": None,
        "text_detected": False,
        "ocr_success": False,
        "error": None,
    }
    if vision_client is None:
        out["error"] = "ocr_not_available"
        return out

    from azure.ai.vision.imageanalysis.models import VisualFeatures

    last_err = None
    for attempt in range(retries):
        try:
            result = vision_client.analyze(
                image_data=image_bytes,
                visual_features=[VisualFeatures.READ],
                logging_enable=False,
                language="en",
            )
            txt = _extract_text_from_vision_result(result)
            out["raw_ocr_text"] = txt
            out["text_detected"] = bool(txt and txt.strip())
            out["ocr_success"] = True
            return out
        except Exception as e:
            last_err = e
            # Parse Retry-After header if present
            retry_after = None
            resp = getattr(e, "response", None)
            if resp is not None:
                headers = getattr(resp, "headers", None) or {}
                ra = headers.get("Retry-After") or headers.get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (ValueError, TypeError):
                        pass
            wait = retry_after if retry_after else backoff * (2**attempt)
            status = getattr(resp, "status_code", None) if resp else None
            if status == 429:
                logger.warning(
                    "Azure Vision OCR throttled (429), retry %d/%d in %.1fs",
                    attempt + 1,
                    retries,
                    wait,
                )
            elif attempt < retries - 1:
                logger.warning(
                    "Azure Vision OCR failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    retries,
                    e,
                    wait,
                )
            time.sleep(wait)

    logger.exception("Azure Vision OCR call failed after %d retries", retries)
    out["error"] = f"azure_vision_ocr_failed: {last_err}"
    return out
