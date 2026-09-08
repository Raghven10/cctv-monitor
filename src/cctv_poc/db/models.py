"""Standard SQLAlchemy ORM models for CCTV Monitoring, Identities, and Security Events."""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
)
from .config import Base


class KnownPersonModel(Base):
    """Registered authorized and known person identities."""
    __tablename__ = "known_persons"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    person_id = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(128), nullable=False, index=True)
    tag = Column(String(64), default="Authorized", nullable=False)
    face_descriptor = Column(JSON, nullable=True)  # Stored color/gradient histogram features
    snapshot_path = Column(String(255), nullable=True)
    snapshot_base64 = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "person_id": self.person_id,
            "name": self.name,
            "tag": self.tag,
            "face_descriptor": self.face_descriptor,
            "snapshot_path": self.snapshot_path,
            "snapshot_base64": self.snapshot_base64,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ActivityEventModel(Base):
    """Security activity events and unusual frame records."""
    __tablename__ = "activity_events"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    event_id = Column(String(64), unique=True, index=True, nullable=False)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
        nullable=False,
    )
    event_type = Column(String(64), index=True, nullable=False)  # UNKNOWN_PERSON_INTRUSION, LAYOUT_CHANGED, etc.
    severity = Column(String(32), index=True, nullable=False)    # CRITICAL, WARNING, INFO
    description = Column(Text, nullable=False)
    mode = Column(String(64), default="DIRECT_ROOM_SURVEILLANCE", nullable=False)
    pane_id = Column(String(64), default="", nullable=True)
    bounding_boxes = Column(JSON, default=list, nullable=True)
    image_path = Column(String(255), default="", nullable=True)
    thumbnail_base64 = Column(Text, default="", nullable=True)
    metadata_json = Column(JSON, default=dict, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "event_type": self.event_type,
            "severity": self.severity,
            "description": self.description,
            "mode": self.mode,
            "pane_id": self.pane_id,
            "bounding_boxes": self.bounding_boxes or [],
            "image_path": self.image_path or "",
            "thumbnail_base64": self.thumbnail_base64 or "",
            "metadata": self.metadata_json or {},
        }
