"""SQLAlchemy Database and Event Logger for storing unusual frames and security activity logs."""

import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional
import uuid
import cv2
import numpy as np

from ..db.config import SessionLocal
from ..db.models import ActivityEventModel
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.event_logger")


@dataclass
class UnusualEventRecord:
    """Structured record for an unusual security or frame event."""
    event_id: str
    timestamp: str
    event_type: str        # UNKNOWN_PERSON_INTRUSION, LAYOUT_CHANGED, NO_PANES_DETECTED, SECURITY_ALERT
    severity: str          # CRITICAL, WARNING, INFO
    description: str
    mode: str              # DIRECT_ROOM_SURVEILLANCE, CCTV_WALL_MONITOR
    pane_id: str = ""
    bounding_boxes: List[Dict[str, Any]] = None
    image_path: str = ""
    thumbnail_base64: str = ""
    metadata: Dict[str, Any] = None


class EventDatabase:
    """
    Thread-safe Database for persistent event logging and unusual frame snapshots
    powered by SQLAlchemy ORM with PostgreSQL & SQLite fallback support.
    """

    def __init__(
        self,
        images_dir: str = "data/events",
        db_path: Optional[str] = None,
        session_factory: Optional[Any] = None,
    ):
        self.images_dir = images_dir
        self.db_path = db_path
        self._lock = threading.Lock()
        self._last_event_times: Dict[str, float] = {}

        if session_factory:
            self._session_factory = session_factory
        elif db_path:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from ..db.config import Base
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            eng = create_engine(f"sqlite:///{os.path.abspath(db_path)}", connect_args={"check_same_thread": False})
            Base.metadata.create_all(bind=eng)
            self._session_factory = sessionmaker(autocommit=False, autoflush=False, bind=eng)
        else:
            self._session_factory = SessionLocal

        os.makedirs(os.path.abspath(self.images_dir), exist_ok=True)

    def log_unusual_frame(
        self,
        event_type: str,
        severity: str,
        description: str,
        frame_image: Optional[np.ndarray],
        mode: str = "DIRECT_ROOM_SURVEILLANCE",
        pane_id: str = "",
        bounding_boxes: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        debounce_seconds: float = 2.5,
    ) -> Optional[UnusualEventRecord]:
        """
        Log an unusual frame event to the database and save its JPEG snapshot.
        Includes debouncing to prevent high-frequency frame spam.
        """
        now = time.time()
        debounce_key = f"{event_type}_{pane_id}"

        # Debounce check
        if debounce_key in self._last_event_times:
            if (now - self._last_event_times[debounce_key]) < debounce_seconds:
                return None

        self._last_event_times[debounce_key] = now

        event_id = f"evt_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now_dt = datetime.now(timezone.utc)
        iso_timestamp = now_dt.isoformat()
        saved_image_path = ""
        thumb_b64 = ""

        # Save annotated/raw snapshot if image is provided
        if frame_image is not None and frame_image.size > 0:
            try:
                annotated = frame_image.copy()
                h, w = annotated.shape[:2]

                # Draw bounding box overlays on saved snapshot if available
                if bounding_boxes:
                    for b in bounding_boxes:
                        bx, by, bw, bh = b.get("bbox", (0, 0, 0, 0))
                        is_known = b.get("is_known", False)
                        color = (0, 220, 0) if is_known else (0, 0, 240)
                        label = b.get("person_name", "UNKNOWN PERSON") if not is_known else b.get("person_name", "AUTHORIZED")

                        cv2.rectangle(annotated, (bx, by), (bx + bw, by + bh), color, 2)
                        cv2.putText(
                            annotated,
                            f"{label} ({b.get('confidence', 1.0):.2f})",
                            (bx, max(20, by - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.55,
                            color,
                            2,
                            cv2.LINE_AA,
                        )

                # Save high-res JPEG
                filename = f"{event_id}.jpg"
                saved_image_path = os.path.join(self.images_dir, filename)
                cv2.imwrite(saved_image_path, annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 88])

                # Create compact thumbnail for instant web UI loading
                thumb_w = 320
                thumb_h = int(h * (thumb_w / float(w)))
                thumb = cv2.resize(annotated, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)
                _, enc = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                thumb_b64 = f"data:image/jpeg;base64,{base64.b64encode(enc).decode('utf-8')}"
            except Exception as e:
                logger.error(f"Failed to save unusual frame image: {e}")

        record = UnusualEventRecord(
            event_id=event_id,
            timestamp=iso_timestamp,
            event_type=event_type,
            severity=severity,
            description=description,
            mode=mode,
            pane_id=pane_id,
            bounding_boxes=bounding_boxes or [],
            image_path=saved_image_path,
            thumbnail_base64=thumb_b64,
            metadata=metadata or {},
        )

        try:
            with self._session_factory() as db:
                event_model = ActivityEventModel(
                    event_id=record.event_id,
                    timestamp=now_dt,
                    event_type=record.event_type,
                    severity=record.severity,
                    description=record.description,
                    mode=record.mode,
                    pane_id=record.pane_id,
                    bounding_boxes=record.bounding_boxes,
                    image_path=record.image_path,
                    thumbnail_base64=record.thumbnail_base64,
                    metadata_json=record.metadata,
                )
                db.add(event_model)
                db.commit()
        except Exception as e:
            logger.error(f"Failed to insert event to database: {e}")

        logger.info(f"💾 Saved unusual frame to DB: [{record.severity}] {record.event_type} - {record.description}")
        return record

    def get_events(
        self,
        limit: int = 50,
        offset: int = 0,
        severity: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query recent activity events from database using SQLAlchemy."""
        try:
            with self._session_factory() as db:
                query = db.query(ActivityEventModel)
                if severity:
                    query = query.filter(ActivityEventModel.severity == severity)
                if event_type:
                    query = query.filter(ActivityEventModel.event_type == event_type)

                rows = query.order_by(ActivityEventModel.id.desc()).offset(offset).limit(limit).all()
                return [r.to_dict() for r in rows]
        except Exception as e:
            logger.error(f"Failed to query events: {e}")
            return []

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Fetch single event by ID."""
        try:
            with self._session_factory() as db:
                row = db.query(ActivityEventModel).filter(ActivityEventModel.event_id == event_id).first()
                return row.to_dict() if row else None
        except Exception as e:
            logger.error(f"Failed to fetch event {event_id}: {e}")
            return None

    def delete_event(self, event_id: str) -> bool:
        """Delete an event and its corresponding image file."""
        event = self.get_event(event_id)
        if not event:
            return False

        if event.get("image_path") and os.path.exists(event["image_path"]):
            try:
                os.remove(event["image_path"])
            except Exception as e:
                logger.warning(f"Could not delete image file {event['image_path']}: {e}")

        try:
            with self._session_factory() as db:
                row = db.query(ActivityEventModel).filter(ActivityEventModel.event_id == event_id).first()
                if row:
                    db.delete(row)
                    db.commit()
                    return True
        except Exception as e:
            logger.error(f"Failed to delete event from DB: {e}")
        return False

    def clear_all(self) -> int:
        """Clear all event logs and images."""
        with self._lock:
            self._last_event_times.clear()
        try:
            with self._session_factory() as db:
                rows = db.query(ActivityEventModel).all()
                for row in rows:
                    if row.image_path and os.path.exists(row.image_path):
                        try:
                            os.remove(row.image_path)
                        except Exception:
                            pass
                    db.delete(row)
                db.commit()
        except Exception as e:
            logger.error(f"Failed to clear events: {e}")
        return 0


# Global singleton database instance
global_event_db = EventDatabase()
