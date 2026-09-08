"""Tests for FastLivePersonDetector and direct room surveillance mode."""

import cv2
import numpy as np
import pytest

from cctv_poc.config import POCConfig
from cctv_poc.extensions.person_detector import FastLivePersonDetector
from cctv_poc.realtime.pipeline import RealtimePipeline


def test_person_detector_initialization():
    """Verify person detector initializes cleanly without errors."""
    detector = FastLivePersonDetector()
    assert detector is not None

    # Test on blank image
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    boxes = detector.detect_persons(blank)
    assert isinstance(boxes, list)


def test_dual_mode_pipeline_dispatch(tmp_path):
    """Verify pipeline switches to direct room surveillance when no monitor is detected."""
    jsonl_file = str(tmp_path / "test_dual_mode.jsonl")
    config = POCConfig()
    config.output.jsonl = jsonl_file
    config.display.auto_detect = True

    pipeline = RealtimePipeline(config)

    try:
        # 1. Blank/ambient room image (no monitor)
        ambient_img = np.full((720, 1280, 3), 50, dtype=np.uint8)
        frame_data = pipeline.video_source.inject_synthetic_frame(ambient_img)
        res = pipeline.process_frame_data(frame_data)

        # Must switch to DIRECT_ROOM_SURVEILLANCE mode with 0 multi-panes and 0 false alerts
        assert res.mode == "DIRECT_ROOM_SURVEILLANCE"
        assert len(res.panes) == 0
        assert len(res.alerts) == 0  # Clean state: no false alerts on empty room

    finally:
        pipeline.stop()


def test_identity_registry_and_matching(tmp_path):
    """Verify person registration, feature matching, and known identity resolution."""
    from cctv_poc.extensions.identity_registry import IdentityRegistry

    db_file = str(tmp_path / "known_persons.json")
    registry = IdentityRegistry(storage_path=db_file, match_threshold=0.50, load_from_db=False)

    # 1. Create a synthetic test face/person crop
    person_crop = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.circle(person_crop, (50, 40), 25, (200, 160, 120), -1) # face shape
    cv2.circle(person_crop, (40, 35), 4, (30, 30, 30), -1)     # left eye
    cv2.circle(person_crop, (60, 35), 4, (30, 30, 30), -1)     # right eye

    # 2. Before registration: must NOT match
    is_match, matched, score = registry.match_person(person_crop)
    assert not is_match
    assert matched is None

    # 3. Register person
    record = registry.register_person(
        name="Rahul Sharma",
        crop=person_crop,
        tag="Security Admin",
        person_id="p_test_01",
    )
    assert record.name == "Rahul Sharma"
    assert record.tag == "Security Admin"

    # 4. After registration: matching same or slightly perturbed crop must match
    is_match, matched, score = registry.match_person(person_crop)
    assert is_match
    assert matched is not None
    assert matched.name == "Rahul Sharma"
    assert matched.tag == "Security Admin"
    assert score > 0.60

    # 5. List persons
    persons = registry.list_persons()
    assert len(persons) == 1
    assert persons[0]["name"] == "Rahul Sharma"

    # 6. Delete person
    deleted = registry.delete_person("p_test_01")
    assert deleted
    assert len(registry.list_persons()) == 0


def test_intrusion_change_tracker_no_loop_announcements():
    """Verify multi-frame temporal analysis before alerting, and single alert per intrusion event."""
    from cctv_poc.extensions.intrusion_tracker import IntrusionChangeTracker
    from cctv_poc.extensions.interfaces import DetectionBox

    tracker = IntrusionChangeTracker(min_consecutive_frames=3, clear_cooldown_seconds=3.0)

    p1 = DetectionBox(
        bbox=(200, 150, 80, 160),
        confidence=0.92,
        class_name="UNKNOWN_PERSON",
        pane_id="DIRECT_CAM",
        is_known=False,
    )

    # 1. Frames 1 and 2: Still analyzing multi-frame evidence -> should not prematurely alert
    s1, r1 = tracker.evaluate_intrusion_event([p1])
    assert s1 is False
    assert "ANALYZING_FRAMES" in r1

    s2, r2 = tracker.evaluate_intrusion_event([p1])
    assert s2 is False

    # 2. Frame 3: Multi-frame threshold reached -> Confirmed intrusion -> Raises alert ONCE!
    s3, r3 = tracker.evaluate_intrusion_event([p1])
    assert s3 is True
    assert "CONFIRMED_INTRUSION" in r3

    # 3. Next 20 frames with person still in room -> Must NOT announce again (0 repetitive alerts)
    for _ in range(20):
        jitter_p = DetectionBox(
            bbox=(202, 151, 80, 160),
            confidence=0.92,
            class_name="UNKNOWN_PERSON",
            pane_id="DIRECT_CAM",
            is_known=False,
        )
        should_announce, reason = tracker.evaluate_intrusion_event([jitter_p])
        assert should_announce is False

    # 4. Room clears (0 persons)
    should_announce, reason = tracker.evaluate_intrusion_event([])
    assert should_announce is False


def test_face_track_buffer_and_multi_frame_enrollment():
    """Verify FaceTrackBuffer associates tracks, accumulates up to 50 samples, and enables multi-frame enrollment."""
    from cctv_poc.extensions.person_detector import FaceTrackBuffer
    from cctv_poc.extensions.identity_registry import IdentityRegistry

    buffer = FaceTrackBuffer(max_samples_per_track=50, track_ttl_seconds=60.0)

    # 1. Match or create track
    box1 = (100, 100, 80, 80)
    tid1 = buffer.match_or_create_track(box1)
    assert tid1.startswith("trk_")

    # 2. Similar box nearby should map to same track
    box1_moved = (105, 102, 80, 80)
    tid2 = buffer.match_or_create_track(box1_moved)
    assert tid1 == tid2

    # 3. Add 55 samples (buffer must cap at 50)
    dummy_crop = np.full((112, 112, 3), 120, dtype=np.uint8)
    for i in range(55):
        emb = [float(i)] * 512
        buffer.add_sample(
            track_id=tid1,
            embedding=emb,
            snapshot_b64="dGVzdA==",
            aligned_crop=dummy_crop,
        )

    history = buffer.get_track_history(tid1)
    assert history["count"] == 50
    assert len(history["embeddings"]) == 50
    assert history["best_snapshot"] == "dGVzdA=="

    # 4. Register using multi-frame centroid
    registry = IdentityRegistry(load_from_db=False)
    centroid = registry.compute_centroid(history["embeddings"])
    assert len(centroid) == 512
    # Verify centroid is unit normalized (norm close to 1.0)
    assert abs(np.linalg.norm(np.array(centroid, dtype=np.float32)) - 1.0) < 1e-5


def test_manual_registration_from_images():
    """Verify manual registration from multiple images creates full gallery and centroid."""
    from cctv_poc.extensions.identity_registry import IdentityRegistry

    registry = IdentityRegistry(load_from_db=False)

    # Create 3 synthetic face crops
    images = []
    for shift in [0, 5, -5]:
        img = np.zeros((120, 120, 3), dtype=np.uint8)
        cv2.circle(img, (60 + shift, 50), 30, (200, 160, 120), -1)
        cv2.circle(img, (50 + shift, 45), 5, (20, 20, 20), -1)
        cv2.circle(img, (70 + shift, 45), 5, (20, 20, 20), -1)
        images.append(img)

    record = registry.register_person_from_images(
        name="Manual User",
        images=images,
        tag="VIP",
        person_id="p_manual_01",
    )

    assert record is not None
    assert record.name == "Manual User"
    assert record.tag == "VIP"
    assert len(record.feature_gallery) >= 1
    assert len(record.features) > 0

    # Matching with one of the source images
    is_match, matched, score = registry.match_person(images[0])
    assert is_match is True
    assert matched.person_id == "p_manual_01"


def test_online_auto_accumulation():
    """Verify online auto-accumulation appends distinct high-confidence pose vectors."""
    from cctv_poc.extensions.identity_registry import IdentityRegistry, KnownPersonRecord

    registry = IdentityRegistry(load_from_db=False)

    base_feat = np.random.randn(512).astype(np.float32)
    base_feat = (base_feat / np.linalg.norm(base_feat)).tolist()

    rec = KnownPersonRecord(
        person_id="p_online_01",
        name="Auto Learner",
        tag="Employee",
        registered_at=1000.0,
        snapshot_base64="",
        features=base_feat,
        template_shape=(112, 112),
        feature_gallery=[base_feat],
    )
    registry._persons["p_online_01"] = rec

    # Generate a slightly different pose embedding (~0.85 cosine similarity)
    diff_feat = np.array(base_feat, dtype=np.float32) + np.random.randn(512) * 0.1
    diff_feat = (diff_feat / np.linalg.norm(diff_feat)).tolist()

    # Add sample
    registry.add_online_sample("p_online_01", diff_feat)

    assert len(rec.feature_gallery) == 2
    assert len(rec.features) == 512



