"""Pydantic v2 schemas for request validation and API serialization."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


# --- Known Person Schemas ---

class KnownPersonBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Full name or identity label")
    tag: str = Field(default="Authorized", max_length=64, description="Role or authorization tag")


class KnownPersonCreate(KnownPersonBase):
    snapshot_base64: Optional[str] = Field(default="", description="Base64 encoded JPEG/PNG face crop")
    person_id: Optional[str] = Field(default=None, description="Optional custom identifier")
    track_id: Optional[str] = Field(default=None, description="Optional tracked face ID to ingest multi-frame buffer")
    images_base64: Optional[List[str]] = Field(default=None, description="List of base64 images for multi-frame manual registration")


class KnownPersonManualCreate(KnownPersonBase):
    images_base64: List[str] = Field(..., min_length=1, description="List of base64 encoded images for manual enrollment")
    person_id: Optional[str] = Field(default=None, description="Optional custom identifier")


class KnownPersonUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    tag: Optional[str] = Field(default=None, max_length=64)


class KnownPersonResponse(KnownPersonBase):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    person_id: str
    snapshot_base64: Optional[str] = None
    snapshot_path: Optional[str] = None
    sample_count: Optional[int] = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class KnownPersonListResponse(BaseModel):
    status: str = "ok"
    count: int
    persons: List[KnownPersonResponse]


# --- Activity Event Schemas ---

class BoundingBoxSchema(BaseModel):
    bbox: List[int] = Field(..., description="[x, y, w, h]")
    confidence: float = 1.0
    person_name: str = "Unknown Person"
    tag: str = "Intruder"
    is_known: bool = False


class ActivityEventBase(BaseModel):
    event_type: str = Field(..., description="UNKNOWN_PERSON_INTRUSION, LAYOUT_CHANGED, NO_PANES_DETECTED, etc.")
    severity: str = Field(..., description="CRITICAL, WARNING, INFO")
    description: str
    mode: str = "DIRECT_ROOM_SURVEILLANCE"
    pane_id: Optional[str] = ""


class ActivityEventCreate(ActivityEventBase):
    bounding_boxes: Optional[List[Dict[str, Any]]] = None
    metadata_json: Optional[Dict[str, Any]] = None
    image_path: Optional[str] = ""
    thumbnail_base64: Optional[str] = ""


class ActivityEventResponse(ActivityEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    event_id: str
    timestamp: datetime
    bounding_boxes: Optional[List[Dict[str, Any]]] = None
    image_path: Optional[str] = ""
    thumbnail_base64: Optional[str] = ""
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, alias="metadata_json")


class EventListResponse(BaseModel):
    status: str = "ok"
    count: int
    events: List[Dict[str, Any]]


# --- Web Control Schemas ---

class ViewModeRequest(BaseModel):
    view_mode: str = Field(..., pattern="^(original|rectified)$", description="View mode: original or rectified")


class TestAlarmResponse(BaseModel):
    __test__ = False  # Prevent pytest from treating this Pydantic schema as a test suite
    status: str = "ok"
    triggered: bool
    message: str
