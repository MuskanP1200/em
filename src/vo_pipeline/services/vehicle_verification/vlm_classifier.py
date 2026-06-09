# ================================================================================
# FILE: vlm_classifier.py
# ================================================================================
# # azure_vlm_infer.py
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
from typing import Dict, Any, Optional, Tuple

from settings import get_settings

from openai import AzureOpenAI

logger = logging.getLogger(__name__)

# Keep the set conservative and aligned with what Azure vision models reliably accept.
SUPPORTED_VISION_MIME_PREFIXES = ("image/jpeg", "image/png", "image/webp", "image/gif")


class AzureVLMImageClassifier:
    """
    Classifies an image into one of:
      - VIN
      - License Plate
      - Odometer
      - Others

    Uses an Azure OpenAI vision-enabled chat model (e.g., GPT-4o series deployment).

    Behavior:
      - "License Plate" ONLY when the plate AND its characters are clearly visible/legible.
      - Returns compact payload:
        {
          "label": "VIN" | "License Plate" | "Odometer" | "Others",
          "confidence": float in [0,1] (best-effort),
          "extracted_text": str | None,   # VIN, plate characters, or odometer miles
          "success": bool,
          "error": str | None,

          # NEW
          "usage": { "prompt_tokens": int, "completion_tokens": int, "total_tokens": int } | None,
          "api_cost": float | None,
          "api_cost_currency": str | None,
          "latency_ms": float | None
        }

    New:
      - classify_image_bytes(...) and classify_image_base64(...) to support in-memory usage.
    """

    LABELS = ["VIN", "License Plate", "Odometer", "Others"]
    _CANONICAL = {
        "vin": "VIN",
        "license plate": "License Plate",
        "license-plate": "License Plate",
        "licenseplate": "License Plate",
        "plate": "License Plate",
        "odometer": "Odometer",
        "odo": "Odometer",
        "others": "Others",
        "other": "Others",
        "none": "Others",
        "unknown": "Others",
    }

    def __init__(self):
        settings = get_settings()
        self.deployment: str = settings.VLM_DEPLOYMENT
        self.api_version: str = settings.VLM_API_VERSION
        self.prompt_cost_per_1k: float = settings.VLM_PROMPT_COST_PER_1K
        self.completion_cost_per_1k: float = settings.VLM_COMPLETION_COST_PER_1K
        self.currency: str = settings.VLM_CURRENCY

        try:
            secrets = settings.model_dump()
            self.endpoint = secrets["AZURE_OPENAI_ENDPOINT"]
            self.api_key = secrets["AZURE_OPENAI_API_KEY"]
        except (KeyError, Exception):
            self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT").rstrip("/")
            self.api_key = os.getenv("AZURE_OPENAI_API_KEY")

        self.available = bool(self.endpoint and self.api_key)
        self.error = None
        if not self.available:
            missing = []
            if not self.endpoint:
                missing.append("AZURE_OPENAI_ENDPOINT")
            if not self.api_key:
                missing.append("AZURE_OPENAI_API_KEY")
            self.error = f"Missing Azure config: {', '.join(missing)}"

        if self.available:
            self.client = AzureOpenAI(
                api_key=self.api_key,
                api_version=self.api_version,
                azure_endpoint=self.endpoint,
            )
        else:
            self.client = None

    # --------------------------
    # Helpers
    # --------------------------
    def _guess_mime(self, filename: Optional[str]) -> str:
        # Detect MIME; fall back to JPEG if missing type.
        if filename:
            mime, _ = mimetypes.guess_type(filename)
            if mime:
                return mime
        return "image/jpeg"

    def _mime_supported(self, mime: str) -> bool:
        return any(mime.startswith(pref) for pref in SUPPORTED_VISION_MIME_PREFIXES)

    def _to_data_url_bytes(
        self,
        image_bytes: bytes,
        mime: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Build a data URL from raw bytes with an inferred or provided mime."""
        if not mime:
            mime = self._guess_mime(filename)

        if not self._mime_supported(mime):
            return None, f"unsupported_image_format_for_vlm: {mime}"

        try:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            return f"data:{mime};base64,{b64}", None
        except Exception as e:
            return None, f"base64_encoding_failed: {e}"

    def _canonical_label(self, raw_label: str) -> str:
        if not raw_label:
            return "Others"
        key = raw_label.strip().lower()
        return self._CANONICAL.get(
            key, raw_label.strip().title() if raw_label.strip() else "Others"
        )

    # --------------------------
    # Usage & Cost helpers (NEW)
    # --------------------------
    def _extract_usage(self, resp: Any) -> Optional[Dict[str, Any]]:
        """
        Try to extract usage (prompt_tokens, completion_tokens, total_tokens)
        from Azure OpenAI Chat Completions response object.
        """
        try:
            usage = getattr(resp, "usage", None)
            if usage:
                # OpenAI SDK objects often expose attributes
                pt = getattr(usage, "prompt_tokens", None)
                ct = getattr(usage, "completion_tokens", None)
                tt = getattr(usage, "total_tokens", None)
            else:
                # Sometimes the response could be dict-like
                usage_dict = None
                if isinstance(resp, dict):
                    usage_dict = resp.get("usage")
                elif hasattr(resp, "get"):
                    usage_dict = resp.get("usage")  # type: ignore
                if usage_dict:
                    pt = usage_dict.get("prompt_tokens")
                    ct = usage_dict.get("completion_tokens")
                    tt = usage_dict.get("total_tokens")
                else:
                    pt = ct = tt = None

            if pt is None and ct is None and tt is None:
                return None

            out = {
                "prompt_tokens": int(pt) if isinstance(pt, (int, float)) else None,
                "completion_tokens": int(ct) if isinstance(ct, (int, float)) else None,
                "total_tokens": int(tt) if isinstance(tt, (int, float)) else None,
            }
            return out
        except Exception:
            return None

    def _compute_cost_from_usage(self, usage: Optional[Dict[str, Any]]) -> float:
        """
        Compute approximate API cost from usage tokens and configured per-1k prices.
        If prices are unset, returns 0.0
        """
        pt = float(usage.get("prompt_tokens") or 0)
        ct = float(usage.get("completion_tokens") or 0)
        cost = (pt / 1000.0) * self.prompt_cost_per_1k + (
            ct / 1000.0
        ) * self.completion_cost_per_1k
        return round(cost, 3)

    # --------------------------
    # Core classification (shared)
    # --------------------------
    def _parse_json_content(self, content: str) -> Dict[str, Any]:
        try:
            obj = json.loads(content)
        except Exception:
            return {}

        label = self._canonical_label(obj.get("label"))

        conf = obj.get("confidence")
        if not isinstance(conf, (int, float)):
            conf = None
        else:
            conf = max(0.0, min(1.0, float(conf)))

        extracted = obj.get("extracted_text")
        if extracted is not None:
            extracted = str(extracted).strip()
            if extracted == "":
                extracted = None

        return {
            "label": label,
            "confidence": conf,
            "extracted_text": extracted,
        }

    def _build_prompts(self):
        """Return (system_prompt, user_instruction, json_schema) for classification."""
        system_prompt = (
            "You are a strict visual classifier. Carefully inspect the image and "
            "classify it into exactly ONE of the following categories:\n\n"
            '1) "VIN" - A TRUE vehicle identification number tag/plate/sticker '
            "*physically attached to the vehicle itself*, such as:\n"
            "  - Metal VIN plate riveted to dashboard near windshield\n"
            "  - Vehicle door jamb VIN sticker labels\n"
            "  - VIN etched on glass\n"
            "  - Factory-applied manufacturer VIN plates\n"
            "Requirements:\n"
            "  - Must be on the vehicle, not on paper or digital documents.\n"
            "  - Must look like a typical 17-character VIN (no I, O, Q).\n"
            "  - VINs shown on documents, invoices, repair sheets, computer displays, "
            "photos of paperwork, or any non-vehicle medium must NOT be classified as "
            'VIN -> classify those as "Others".\n\n'
            '2) "License Plate" - A vehicle registration plate physically mounted on '
            "the exterior of a vehicle (front or rear).\n"
            "Requirements:\n"
            "  - Plate AND characters must be clearly visible and legible.\n"
            "  - Classify as 'License Plate' if the plate AND its characters are clearly "
            "legible — even if the plate is not the primary subject of the image (e.g. "
            "visible in the background of a damage photo). Only use 'Others' if the "
            "characters are blurred, truncated, blocked, or too angled to read.\n"
            "  - Handle U.S. license plate variations including horizontal alphanumeric "
            "sequences, vertically stacked characters, left/right vertical suffixes, "
            "multi-line plates, specialty plates with icons/color bands/prefixes, and "
            "temporary paper plates.\n"
            "  - Some plates use a mixed-orientation format: 2–3 characters stacked "
            "vertically on the left edge, followed by the remaining characters running "
            "horizontally. Read the vertical stack top-to-bottom first, then append the "
            "horizontal characters — e.g. vertical 'N' over '6' + horizontal '56701' → 'N656701'.\n"
            "  - Examine carefully for vertical character groups - read them in correct "
            "top-to-bottom order when present.\n\n"
            '3) "Odometer" - A dashboard instrument cluster showing mileage or trip data.\n'
            "  - Can be analog (rolling numbers) or digital (LCD/LED) or hybrid (digital display inside analog cluster).\n"
            "  - Classify as 'Odometer' if a mileage/odometer reading is visible and legible anywhere "
            "in the image — even if it is part of a larger instrument cluster alongside speedometer, "
            "RPM, or other gauges.\n"
            "  - If miles covered is readable, extract the miles.\n"
            "  - Ignore other information from the dashboard, only extract the main Odometer reading.\n"
            "  - If the digits are partially obscured or blurry, extract what is readable and flag confidence as low.\n"
            '4) "Others" - Everything else.\n'
            "  - Includes VINs found on documents, printed pages, screens, invoices, "
            "inspection sheets, etc.\n"
            "  - Includes unclear, partial, or illegible license plates.\n"
            "  - Includes any content that does not match the above categories.\n\n"
            "Always pick the single best class even if slightly uncertain, but be strict "
            'about "License Plate" and "VIN".'
        )

        user_instruction = (
            "Return ONLY this JSON object:\n\n"
            "{\n"
            '  "label": one of ["VIN","License Plate","Odometer","Others"],\n'
            '  "confidence": number in [0,1],\n'
            '  "extracted_text": string or null\n'
            "}\n\n"
            "Rules:\n"
            "- If label = VIN -> extracted_text = the 17-character VIN (only if clearly readable).\n"
            "- If label = License Plate -> extracted_text = complete plate characters "
            "(horizontal, vertical, stacked, or multi-line).\n"
            "  NOTE: Some plates have 2–3 characters written vertically on the left edge, "
            "with the remaining characters horizontal. Always read the vertical characters "
            "top-to-bottom first, then append the horizontal portion to form the full plate string.\n"
            "- If label = Odometer -> extracted_text = numeric mileage reading.\n"
            "- If label = Others -> extracted_text = null.\n\n"
            "No explanations. No extra fields."
        )

        json_schema = {
            "name": "image_classification",
            "schema": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "enum": self.LABELS,
                        "description": "The chosen class label.",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "extracted_text": {
                        "type": ["string", "null"],
                        "description": "Class-relevant extracted text (VIN, plate number, or miles). Null if none or class is 'Others'.",
                    },
                },
                "required": ["label", "confidence", "extracted_text"],
                "additionalProperties": False,
            },
            "strict": True,
        }

        return system_prompt, user_instruction, json_schema

    def _make_api_call(self, messages, max_tokens, response_format):
        try:
            resp = self.client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.0,
                response_format=response_format,
            )
            return resp, None
        except Exception as e:
            return None, e

    def _api_call_with_retry(
        self,
        messages,
        max_tokens,
        response_format,
        retries: int = 3,
        backoff: float = 1.0,
    ):
        """Retry-aware wrapper around a single API call. Handles 429 + Retry-After."""
        last_err = None
        for attempt in range(retries):
            resp, err = self._make_api_call(messages, max_tokens, response_format)
            if resp is not None:
                return resp, None
            last_err = err
            retry_after = None
            http_resp = getattr(err, "response", None)
            if http_resp is not None:
                headers = getattr(http_resp, "headers", None) or {}
                ra = headers.get("Retry-After") or headers.get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (ValueError, TypeError):
                        pass
            wait = retry_after if retry_after else backoff * (2**attempt)
            status = getattr(http_resp, "status_code", None) if http_resp else None
            if status == 429:
                logger.warning(
                    "Azure VLM throttled (429), retry %d/%d in %.1fs",
                    attempt + 1,
                    retries,
                    wait,
                )
            elif attempt < retries - 1:
                logger.warning(
                    "Azure VLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    retries,
                    err,
                    wait,
                )
            time.sleep(wait)
        return None, last_err

    def _classify_with_data_url(
        self, data_url: str, max_tokens: int = 120
    ) -> Dict[str, Any]:
        out = {
            "label": None,
            "confidence": None,
            "extracted_text": None,
            "success": False,
            "error": None,
            "usage": None,
            "api_cost": None,
            "api_cost_currency": self.currency,
            "latency_ms": None,
        }
        if not self.available or not self.client:
            out["error"] = self.error or "vlm_not_configured"
            return out

        system_prompt, user_instruction, json_schema = self._build_prompts()
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_instruction},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                ],
            },
        ]

        # ---- Primary attempt: JSON Schema structured output (with retry) ----
        t0 = time.perf_counter()
        resp, err = self._api_call_with_retry(
            messages,
            max_tokens,
            {"type": "json_schema", "json_schema": json_schema},
        )
        if resp is not None:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            content = (resp.choices[0].message.content or "").strip()
            parsed = self._parse_json_content(content)
            if parsed.get("label"):
                out.update(parsed)
                out["success"] = True
                usage = self._extract_usage(resp)
                out["usage"] = usage
                out["api_cost"] = self._compute_cost_from_usage(usage)
                out["latency_ms"] = round(latency_ms, 3)
                return out

        # ---- Fallback: json_object (with retry) ----
        t1 = time.perf_counter()
        resp, err = self._api_call_with_retry(
            messages,
            max_tokens,
            {"type": "json_object"},
        )
        if resp is not None:
            latency_ms = (time.perf_counter() - t1) * 1000.0
            content = (resp.choices[0].message.content or "").strip()
            parsed = self._parse_json_content(content)
            if parsed.get("label"):
                out.update(parsed)
                out["success"] = True
                usage = self._extract_usage(resp)
                out["usage"] = usage
                out["api_cost"] = self._compute_cost_from_usage(usage)
                out["latency_ms"] = round(latency_ms, 3)
                return out
            out["error"] = "parse_failed_from_json_object_response"
            out["latency_ms"] = round(latency_ms, 3)
            return out

        out["error"] = f"azure_vlm_call_failed_all_retries: {err}"
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 3)
        return out

    # Classify Functions
    # --------------------------
    def classify_image_bytes(
        self, image_bytes: bytes, filename: Optional[str] = None, max_tokens: int = 120
    ) -> Dict[str, Any]:
        """
        In-memory classification from raw bytes.
        `filename` is optional but helps MIME detection; defaults to image/jpeg otherwise.
        """
        out = {
            "label": None,
            "confidence": None,
            "extracted_text": None,
            "success": False,
            "error": None,
            "usage": None,
            "api_cost": None,
            "api_cost_currency": self.currency,
            "latency_ms": None,
        }

        if not self.available or not self.client:
            out["error"] = self.error or "vlm_not_configured"
            return out

        data_url, err = self._to_data_url_bytes(
            image_bytes, mime=None, filename=filename
        )
        if err:
            out["error"] = err
            return out

        return self._classify_with_data_url(data_url, max_tokens=max_tokens)


# -------- Module-level factory & wrapper --------
def init_classifier():
    """Initialise the VLM classifier, returning (classifier, error_or_none)."""
    try:
        clf = AzureVLMImageClassifier()
        if not getattr(clf, "available", False):
            return None, getattr(clf, "error", "classifier_not_configured")
        return clf, None
    except Exception as e:
        return None, f"classifier_init_failed: {e}"


def classify_image_in_memory(image_bytes: bytes, classifier) -> Dict[str, Any]:
    """Classify an image from raw bytes using the given classifier instance."""
    out = {
        "classified_label": None,
        "classified_confidence": None,
        "classification_error": None,
        "extracted_text": None,
        "api_cost": None,
        "api_cost_currency": None,
        "usage": None,
    }
    if classifier is None:
        out["classification_error"] = "classifier_not_available"
        return out
    try:
        if hasattr(classifier, "classify_image_bytes"):
            r = classifier.classify_image_bytes(image_bytes)
        else:
            out["classification_error"] = "classifier_no_bytes_interface"
            return out

        if r and isinstance(r, dict) and r.get("success"):
            out["classified_label"] = r.get("label")
            out["classified_confidence"] = r.get("confidence")
            out["extracted_text"] = r.get("extracted_text")
            out["api_cost"] = r.get("api_cost", r.get("cost"))
            out["api_cost_currency"] = (
                r.get("api_cost_currency", r.get("currency")) or "USD"
            )
            out["usage"] = r.get("usage")
        else:
            out["classification_error"] = (
                r.get("error") if isinstance(r, dict) else None
            ) or "classification_failed"
        return out
    except Exception as e:
        out["classification_error"] = f"classifier_call_failed: {e}"
        return out
