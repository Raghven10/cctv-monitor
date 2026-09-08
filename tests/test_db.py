"""Comprehensive test suite for Database Configuration, Fallback, SQLAlchemy ORM, and Pydantic Schemas."""

import os
from datetime import datetime, timezone
import numpy as np
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cctv_poc.db.config import (
    Base,
    create_db_engine,
    get_db,
    init_db,
    resolve_database_url,
)
from cctv_poc.db.models import ActivityEventModel, KnownPersonModel
from cctv_poc.db.schemas import (
    ActivityEventCreate,
    ActivityEventResponse,
    KnownPersonCreate,
    KnownPersonResponse,
    TestAlarmResponse,
    ViewModeRequest,
)
from cctv_poc.extensions.event_logger import EventDatabase
from cctv_poc.extensions.identity_registry import IdentityRegistry


def test_resolve_database_url_default(monkeypatch):
    """When no PostgreSQL variables are set, fallback to default SQLite URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)

    url = resolve_database_url()
    assert url.startswith("sqlite:///")
    assert "cctv_events.db" in url


def test_resolve_database_url_explicit_env(monkeypatch):
    """When DATABASE_URL is set in environment, use it."""
    test_pg = "postgresql://myuser:mypass@localhost:5432/cctv_test"
    monkeypatch.setenv("DATABASE_URL", test_pg)

    url = resolve_database_url()
    assert url == test_pg


def test_resolve_database_url_individual_postgres_vars(monkeypatch):
    """When POSTGRES_* environment variables are set, construct PostgreSQL URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_USER", "secuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret123")
    monkeypatch.setenv("POSTGRES_HOST", "db.example.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "cctv_prod")

    url = resolve_database_url()
    assert url == "postgresql://secuser:secret123@db.example.internal:5433/cctv_prod"


def test_create_db_engine_fallback_on_unreachable_postgres(monkeypatch, tmp_path):
    """When PostgreSQL is unreachable, engine should gracefully fallback to SQLite."""
    unreachable_pg = "postgresql://invalid_user:invalid_pass@127.0.0.1:59999/nonexistent_db"
    sqlite_fallback = f"sqlite:///{tmp_path}/fallback.db"

    engine = create_db_engine(database_url=unreachable_pg, fallback_sqlite_url=sqlite_fallback)
    assert engine is not None
    assert str(engine.url).startswith("sqlite:///")


def test_sqlalchemy_known_person_model(tmp_path):
    """Test KnownPersonModel ORM creation and querying."""
    test_db = f"sqlite:///{tmp_path}/test_person.db"
    engine = create_engine(test_db)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        person = KnownPersonModel(
            person_id="p_test_123",
            name="Alice Guard",
            tag="Admin",
            face_descriptor=[0.12, 0.45, 0.78],
            snapshot_base64="data:image/jpeg;base64,dummy",
        )
        db.add(person)
        db.commit()

        # Query
        fetched = db.query(KnownPersonModel).filter(KnownPersonModel.person_id == "p_test_123").first()
        assert fetched is not None
        assert fetched.name == "Alice Guard"
        assert fetched.tag == "Admin"
        assert fetched.face_descriptor == [0.12, 0.45, 0.78]

        data_dict = fetched.to_dict()
        assert data_dict["person_id"] == "p_test_123"
        assert data_dict["created_at"] is not None


def test_sqlalchemy_activity_event_model(tmp_path):
    """Test ActivityEventModel ORM creation and querying."""
    test_db = f"sqlite:///{tmp_path}/test_activity.db"
    engine = create_engine(test_db)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        event = ActivityEventModel(
            event_id="evt_test_999",
            event_type="UNKNOWN_PERSON_INTRUSION",
            severity="CRITICAL",
            description="Unidentified person entered perimeter",
            mode="DIRECT_ROOM_SURVEILLANCE",
            pane_id="ZONE_1",
            bounding_boxes=[{"bbox": [10, 10, 50, 50], "confidence": 0.95}],
            image_path="/data/events/evt_test_999.jpg",
            thumbnail_base64="data:image/jpeg;base64,thumb",
            metadata_json={"camera": "cam_front", "fps": 24.5},
        )
        db.add(event)
        db.commit()

        fetched = db.query(ActivityEventModel).filter(ActivityEventModel.event_id == "evt_test_999").first()
        assert fetched is not None
        assert fetched.severity == "CRITICAL"
        assert fetched.bounding_boxes == [{"bbox": [10, 10, 50, 50], "confidence": 0.95}]
        assert fetched.metadata_json["camera"] == "cam_front"

        data_dict = fetched.to_dict()
        assert data_dict["event_id"] == "evt_test_999"


def test_pydantic_schemas_validation():
    """Verify Pydantic v2 validation models."""
    # 1. Valid KnownPersonCreate
    person_req = KnownPersonCreate(
        name="Bob Security",
        tag="Officer",
        snapshot_base64="dGVzdF9iYXNlNjQ=",
    )
    assert person_req.name == "Bob Security"
    assert person_req.tag == "Officer"

    # 2. Invalid KnownPersonCreate (empty name)
    with pytest.raises(ValidationError):
        KnownPersonCreate(name="", snapshot_base64="dGVzdA==")

    # 3. ViewModeRequest validation
    view_req = ViewModeRequest(view_mode="rectified")
    assert view_req.view_mode == "rectified"

    with pytest.raises(ValidationError):
        ViewModeRequest(view_mode="invalid_mode")

    # 4. TestAlarmResponse
    alarm_resp = TestAlarmResponse(triggered=True, message="Unknown person detected!")
    assert alarm_resp.status == "ok"
    assert alarm_resp.triggered is True


def test_get_db_generator():
    """Verify FastAPI get_db dependency lifecycle."""
    gen = get_db()
    db_session = next(gen)
    assert db_session is not None
    # Close session
    try:
        next(gen)
    except StopIteration:
        pass


def test_identity_registry_and_event_db_integration(tmp_path):
    """Verify end-to-end integration of IdentityRegistry and EventDatabase with SQLAlchemy."""
    init_db()

    # Test IdentityRegistry register and list
    registry = IdentityRegistry(storage_path=str(tmp_path / "known.json"))
    dummy_crop = np.zeros((100, 100, 3), dtype=np.uint8)
    dummy_crop[20:80, 20:80] = [0, 255, 0]

    rec = registry.register_person(name="Integration User", crop=dummy_crop, tag="VIP")
    assert rec.name == "Integration User"
    assert rec.tag == "VIP"

    persons = registry.list_persons()
    assert any(p["name"] == "Integration User" for p in persons)

    # Test match
    matched, match_rec, score = registry.match_person(dummy_crop)
    assert matched is True
    assert match_rec.name == "Integration User"

    # Test EventDatabase logging
    event_db = EventDatabase(images_dir=str(tmp_path / "events"))
    evt = event_db.log_unusual_frame(
        event_type="LAYOUT_CHANGED",
        severity="WARNING",
        description="Grid changed from 2x2 to 3x3",
        frame_image=dummy_crop,
        debounce_seconds=0.0,
    )
    assert evt is not None
    assert evt.event_type == "LAYOUT_CHANGED"

    queried_events = event_db.get_events(limit=5)
    assert any(e["event_id"] == evt.event_id for e in queried_events)
