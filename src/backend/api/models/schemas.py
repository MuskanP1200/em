# models.py
from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional

# ---------- Shared / Core Models ----------


class HealthResponse(BaseModel):
    status: str = "ok"
    message: str = "Claims Management API is running"
    version: str = "1.0.0"


class FolderOut(BaseModel):
    folder_name: str
    overall_status: Optional[bool] = None
    vin_status: Optional[bool] = None
    plate_status: Optional[bool] = None
    est_match_status: Optional[bool] = None


class ClassifiedLabel(str, Enum):
    VIN = "VIN"
    LICENSE_PLATE = "License Plate"
    ODOMETER = "Odometer"
    OTHER = "Others"


# ---------- Estimate / VLM Models ----------


class EstInfoOut(BaseModel):
    """Normalized field names for clarity, mapped from DB columns."""

    folder_name: Optional[str] = None
    est_id: str
    repr_ncdnt_id: int
    legacy_claim_number: Optional[str] = None
    claim_number: Optional[str] = None
    claim_gbpr_id: Optional[int] = None
    accident_report_gbpr: Optional[str] = None
    damage_desc: Optional[str] = None
    odometer_number: Optional[str] = None
    state: Optional[str] = None
    vin: Optional[str] = None
    license_plate_number: Optional[str] = None
    color: Optional[str] = None
    # Year-Make-Model-State composed string
    ymms: Optional[str] = None


class ClassifiedBreakdown(BaseModel):
    """Breakdown by classification class.

    NOTE: Using snake_case keys (without spaces) for robust client usage.
    """

    vin: int = 0
    license_plate: int = 0
    odometer: int = 0
    other: int = 0


class VLMStatsOut(BaseModel):
    folder_name: str
    images_classified: int = 0
    images_with_text: int = 0
    relevant_found: int = 0
    classified: ClassifiedBreakdown


# ---------- Image Models ----------


class ImageDetailOut(BaseModel):
    image_path: str
    image_name: str
    image_data: Optional[str] = Field(
        default=None,
        description="Base64-encoded image data; omitted if retrieval fails or not needed.",
    )
    classified_label: Optional[ClassifiedLabel] = None
    text_detected: Optional[bool] = None
    ocr_success: Optional[bool] = None
    is_perfect_match_vin: Optional[bool] = None
    overall_best_match_vin: Optional[str] = None
    is_perfect_match_plate: Optional[bool] = None
    overall_best_match_plate: Optional[str] = None
    error: Optional[str] = Field(
        default=None, description="Blob retrieval error if any."
    )


class ImageListOut(BaseModel):
    folder_name: str
    total_images: int
    images: List[ImageDetailOut]
