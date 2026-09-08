"""Tests for perspective rectification and inverse coordinate mapping."""

import numpy as np
import pytest

from cctv_poc.monitor.perspective import PerspectiveCorrector


def test_perspective_rectification_and_mapping():
    """Verify forward and inverse homography coordinate mappings."""
    # Trapezoidal distorted quadrilateral in 1920x1080 frame
    src_corners = np.array(
        [[200, 150], [1700, 100], [1800, 950], [150, 1000]], dtype=np.float32
    )
    target_w, target_h = 3840, 2160
    corrector = PerspectiveCorrector(src_corners, target_width=target_w, target_height=target_h)

    # 1. Map corners to rectified space
    rectified_pts = corrector.map_original_to_rectified(src_corners)
    expected_rectified = np.array(
        [[0, 0], [target_w - 1, 0], [target_w - 1, target_h - 1], [0, target_h - 1]],
        dtype=np.float32,
    )
    assert np.allclose(rectified_pts, expected_rectified, atol=1.0)

    # 2. Map back to original space
    orig_recovered = corrector.map_rectified_to_original(expected_rectified)
    assert np.allclose(orig_recovered, src_corners, atol=1.0)

    # 3. Test bounding box to polygon mapping
    bbox = (100, 100, 500, 400)
    poly = corrector.map_rectified_bbox_to_original_polygon(bbox)
    assert poly.shape == (4, 2)
