"""
models/incident_schemas.py

Pydantic models for the incident API endpoints.
Field names must match what the frontend render functions expect.
"""

from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

INCIDENT_STATUSES = {"ai_approved", "ai_flagged", "pending_ai_review"}
AI_STATUSES = {"approved", "flagged", "pending", "not_available"}
TAB_STATUSES = {"approved", "flagged", "pending", "inactive", "not_available"}
VALID_RATINGS = {"up", "down"}


# ── Incident list ────────────────────────────────────────────────


class IncidentListItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=50)
    sub_text: str = Field(default="")
    status: str

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        return v if v in INCIDENT_STATUSES else "pending_ai_review"


# ── Topbar ───────────────────────────────────────────────────────


class TopbarOut(BaseModel):
    incident_num: str = ""
    vehicle: str = ""
    color: str = ""
    state: str = ""
    plate: str = ""
    status: str = "In Progress"


# ── Progress tabs ────────────────────────────────────────────────


class ProgressTabOut(BaseModel):
    label: str
    status: str = "pending"

    @field_validator("status")
    @classmethod
    def check_tab_status(cls, v: str) -> str:
        return v if v in TAB_STATUSES else "pending"


# ── Vehicle info ─────────────────────────────────────────────────


class VehicleFieldOut(BaseModel):
    label: str
    value: Optional[str] = None
    muted: Optional[bool] = False

    @field_validator("value", mode="before")
    @classmethod
    def coerce_to_str(cls, v: Any) -> Optional[str]:
        return None if v is None else str(v)


class AiFieldOut(BaseModel):
    label: str
    value: Optional[str] = None
    value_per_ai: Optional[str] = None
    ai_status: str = "pending"

    @field_validator("ai_status")
    @classmethod
    def check_ai_status(cls, v: str) -> str:
        return v if v in AI_STATUSES else "pending"

    @field_validator("value", "value_per_ai", mode="before")
    @classmethod
    def coerce_to_str(cls, v: Any) -> Optional[str]:
        return None if v is None else str(v)


class VehicleInfoOut(BaseModel):
    ai_status: str
    fields: List[List[VehicleFieldOut]] = Field(default_factory=list)
    vin: AiFieldOut
    license_plate: AiFieldOut
    odometer: AiFieldOut
    damage_description: str = ""

    @field_validator("damage_description", mode="before")
    @classmethod
    def coerce_damage(cls, v: Any) -> str:
        return "" if v is None else str(v)


# ── Photos ───────────────────────────────────────────────────────


class PhotoOut(BaseModel):
    url: str = ""
    lightbox_url: str = ""
    label: str = ""
    badge: str = "ok"
    orientation: str = "landscape"
    image_data: Optional[str] = None  # base64, None if blob unavailable


# ── Line items ───────────────────────────────────────────────────


class LineItemOut(BaseModel):
    line: Optional[str] = None
    op: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    part_num: Optional[str] = None
    price: Optional[str] = None
    qty: Optional[str] = None
    labor: Optional[str] = None
    paint: Optional[str] = None
    ai_status: Optional[str] = None
    flag_special: bool = False

    @field_validator("ai_status")
    @classmethod
    def check_ai_status(cls, v: Optional[str]) -> Optional[str]:
        return v if v in AI_STATUSES else None


# ── Breakdown cards ──────────────────────────────────────────────


class BreakdownItemOut(BaseModel):
    label: str
    value: str = "$0.00"
    value_per_ai: str = "$0.00"
    ai_status: str = "pending"
    negative: bool = False

    @field_validator("ai_status")
    @classmethod
    def check_ai_status(cls, v: str) -> str:
        return v if v in AI_STATUSES else "pending"


class BreakdownSectionOut(BaseModel):
    total: str = "$0.00"
    items: List[BreakdownItemOut] = Field(default_factory=list)


class BreakdownOut(BaseModel):
    labor: BreakdownSectionOut = Field(default_factory=BreakdownSectionOut)
    parts: BreakdownSectionOut = Field(default_factory=BreakdownSectionOut)
    materials: BreakdownSectionOut = Field(default_factory=BreakdownSectionOut)
    miscellaneous: BreakdownSectionOut = Field(default_factory=BreakdownSectionOut)


# ── Total bar ────────────────────────────────────────────────────


class TotalOut(BaseModel):
    amount: str = "$0.00"
    tag: str = ""
    threshold: str = ""
    ai_status: Optional[str] = "pending"


# ── Rates panel ──────────────────────────────────────────────────


class RateItemOut(BaseModel):
    label: str
    value: str = ""


# ── Full detail response ─────────────────────────────────────────


class IncidentDetailOut(BaseModel):
    topbar: TopbarOut
    progress_tabs: List[ProgressTabOut] = Field(default_factory=list)
    vehicle_info: VehicleInfoOut
    photos: List[PhotoOut] = Field(default_factory=list)
    line_items: List[LineItemOut] = Field(default_factory=list)
    line_items_alert: Optional[str] = None
    breakdown: BreakdownOut = Field(default_factory=BreakdownOut)
    total: TotalOut = Field(default_factory=TotalOut)
    labor_rates: List[RateItemOut] = Field(default_factory=list)
    sublet_rates: List[RateItemOut] = Field(default_factory=list)
    discounts: List[str] = Field(default_factory=list)
    special_instruction: Optional[str] = ""
    group_note: Optional[str] = ""


# ── Feedback ─────────────────────────────────────────────────────


class FeedbackRequest(BaseModel):
    incident_id: str = Field(..., min_length=1, max_length=50)
    section: str = Field(..., min_length=1, max_length=100)
    rating: Optional[str] = Field(default=None, description="'up' or 'down'")
    comment: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("incident_id", "section")
    @classmethod
    def must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field cannot be blank")
        return v.strip()

    @field_validator("rating")
    @classmethod
    def check_rating(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_RATINGS:
            raise ValueError(f"rating must be 'up' or 'down', got '{v}'")
        return v

    @model_validator(mode="after")
    def needs_rating_or_comment(self) -> "FeedbackRequest":
        if self.rating is None and not (self.comment or "").strip():
            raise ValueError("Provide at least a rating or a comment")
        return self


class FeedbackResponse(BaseModel):
    success: bool
    message: str
