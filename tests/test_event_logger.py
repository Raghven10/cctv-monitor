"""Unit tests for EventDatabase and unusual frame activity logger."""

import os
import cv2
import numpy as np
import pytest

from cctv_poc.extensions.event_logger import EventDatabase


def test_event_database_logging_and_retrieval(tmp_path):
    """Verify logging unusual frames, saving snapshots, and querying SQLite DB."""
    db_file = str(tmp_path / "test_events.db")
    img_dir = str(tmp_path / "test_images")

    db = EventDatabase(db_path=db_file, images_dir=img_dir)

    # 1. Create dummy frame image
    dummy_frame = np.full((480, 640, 3), 100, dtype=np.uint8)
    cv2.circle(dummy_frame, (320, 240), 50, (0, 0, 255), -1)

    # 2. Log an unusual intrusion event
    record = db.log_unusual_frame(
        event_type="UNKNOWN_PERSON_INTRUSION",
        severity="CRITICAL",
        description="Unknown intruder detected in living room",
        frame_image=dummy_frame,
        mode="DIRECT_ROOM_SURVEILLANCE",
        pane_id="LIVE_ROOM_CAM",
        bounding_boxes=[{"bbox": [280, 200, 80, 80], "person_name": "Intruder", "confidence": 0.94, "is_known": False}],
        debounce_seconds=0.0,
    )

    assert record is not None
    assert record.event_id.startswith("evt_")
    assert record.severity == "CRITICAL"
    assert record.event_type == "UNKNOWN_PERSON_INTRUSION"
    assert os.path.exists(record.image_path)
    assert record.thumbnail_base64.startswith("data:image/jpeg;base64,")

    # 3. Query events list
    events = db.get_events(limit=10)
    assert len(events) == 1
    assert events[0]["event_id"] == record.event_id
    assert events[0]["severity"] == "CRITICAL"
    assert len(events[0]["bounding_boxes"]) == 1

    # 4. Query single event
    single = db.get_event(record.event_id)
    assert single is not None
    assert single["description"] == "Unknown intruder detected in living room"

    # 5. Delete event
    deleted = db.delete_event(record.event_id)
    assert deleted is True
    assert not os.path.exists(record.image_path)
    assert len(db.get_events()) == 0


def test_event_database_debouncing(tmp_path):
    """Verify that rapid-fire duplicate events are debounced cleanly."""
    db_file = str(tmp_path / "test_debounce.db")
    img_dir = str(tmp_path / "test_debounce_imgs")

    db = EventDatabase(db_path=db_file, images_dir=img_dir)
    dummy_frame = np.zeros((100, 100, 3), dtype=np.uint8)

    # First event logged
    r1 = db.log_unusual_frame("TEST_ALERT", "WARNING", "Alert 1", dummy_frame, debounce_seconds=2.0)
    assert r1 is not None

    # Immediate second event within debounce window should be skipped
    r2 = db.log_unusual_frame("TEST_ALERT", "WARNING", "Alert 2", dummy_frame, debounce_seconds=2.0)
    assert r2 is None

    events = db.get_events()
    assert len(events) == 1
