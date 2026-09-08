"""Tests for temporal stabilization of layout transitions and noisy camera label observations."""

import pytest

from cctv_poc.camera.identity_resolver import ResolvedPaneIdentity
from cctv_poc.config import StabilizationConfig
from cctv_poc.monitor.layout_discovery import DiscoveredLayout, PaneGeometry
from cctv_poc.stabilization.temporal import CameraStabilizer, LayoutStabilizer


def test_layout_stabilizer_hysteresis():
    """Verify layout change requires N consecutive frames before activating."""
    config = StabilizationConfig(layout_frames_required=5)
    stabilizer = LayoutStabilizer(config)

    l1 = DiscoveredLayout("layout-4p-aaa", 4, 0.95, [])
    l2 = DiscoveredLayout("layout-9p-bbb", 9, 0.95, [])

    # Initial layout becomes active immediately
    active, changed = stabilizer.update(l1)
    assert active.layout_id == "layout-4p-aaa"
    assert changed is True

    # 1 noisy frame with 9 panes should not switch active layout
    active, changed = stabilizer.update(l2)
    assert active.layout_id == "layout-4p-aaa"
    assert changed is False

    # Feed 3 more frames (total 4) -> still should not switch
    for _ in range(3):
        active, changed = stabilizer.update(l2)
        assert active.layout_id == "layout-4p-aaa"
        assert changed is False

    # 5th frame reaches threshold -> switches active layout!
    active, changed = stabilizer.update(l2)
    assert active.layout_id == "layout-9p-bbb"
    assert changed is True


def test_camera_stabilizer_noise_rejection():
    """Verify single transient OCR error does not flip stable camera mapping."""
    config = StabilizationConfig(label_observations_required=3, change_confirmation_frames=5)
    stabilizer = CameraStabilizer(config)

    ident_cam1 = ResolvedPaneIdentity("P01", "CAM-01", "CAM-01", 0.95, 0.0, 0.98, "CAM-01", 0.95)
    ident_cam7_noise = ResolvedPaneIdentity("P01", "CAM-07", "CAM-07", 0.90, 0.0, 0.98, "CAM-07", 0.90)

    # 3 consecutive observations of CAM-01 make it stable
    for _ in range(3):
        state = stabilizer.update_observation(ident_cam1)

    assert state.is_stable is True
    assert state.stable_label == "CAM-01"

    # 1 noisy observation of CAM-07 should NOT change stable label
    state = stabilizer.update_observation(ident_cam7_noise)
    assert state.stable_label == "CAM-01"
    assert state.is_stable is True
