"""Tests for display boundary detection, corner ordering, and calibration fallback."""

import cv2
import numpy as np
import pytest

from cctv_poc.config import DisplayConfig
from cctv_poc.monitor.calibration import CalibrationStore
from cctv_poc.monitor.display_detector import DisplayDetector, order_corners


def test_order_corners():
    """Verify ordering of 4 corners into [TL, TR, BR, BL]."""
    # Shuffled corners of a rectangle [100, 50, 500, 300]
    pts = np.array([[500, 50], [100, 300], [100, 50], [500, 300]], dtype=np.float32)
    ordered = order_corners(pts)

    # TL
    assert np.allclose(ordered[0], [100, 50])
    # TR
    assert np.allclose(ordered[1], [500, 50])
    # BR
    assert np.allclose(ordered[2], [500, 300])
    # BL
    assert np.allclose(ordered[3], [100, 300])


def test_display_detection_auto_synthetic():
    """Detect a high-contrast rectangular monitor on a dark background."""
    config = DisplayConfig(auto_detect=True, min_area_fraction=0.30)
    detector = DisplayDetector(config)

    frame = np.full((1080, 1920, 3), 10, dtype=np.uint8)
    # Draw monitor bezel
    cv2.rectangle(frame, (200, 100), (1720, 980), (220, 220, 220), -1)

    result = detector.detect(frame)
    assert result.confidence >= 0.70
    assert not result.is_full_frame_fallback

    # Check corners are close to drawn rectangle
    tl, tr, br, bl = result.corners
    assert abs(tl[0] - 200) < 20 and abs(tl[1] - 100) < 20
    assert abs(tr[0] - 1720) < 20 and abs(tr[1] - 100) < 20
    assert abs(br[0] - 1720) < 20 and abs(br[1] - 980) < 20
    assert abs(bl[0] - 200) < 20 and abs(bl[1] - 980) < 20


def test_display_detection_calibration_fallback(tmp_path):
    """Verify fallback to calibration store when auto detection cannot find monitor."""
    calib_file = str(tmp_path / "calib.json")
    config = DisplayConfig(auto_detect=False, calibration_file=calib_file)
    store = CalibrationStore(calib_file)
    
    # Save calibrated points
    test_corners = np.array([[100, 100], [1800, 100], [1800, 1000], [100, 1000]], dtype=np.float32)
    store.save_corners(test_corners, source_width=1920, source_height=1080)

    detector = DisplayDetector(config)
    blank_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    res = detector.detect(blank_frame)

    assert res.is_calibrated_fallback
    assert np.allclose(res.corners, test_corners)
