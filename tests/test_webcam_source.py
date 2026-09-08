"""Tests for WebcamSource and LatestFrameBuffer."""

import time
import numpy as np
import pytest

from cctv_poc.config import VideoConfig
from cctv_poc.video.source import FrameData, LatestFrameBuffer, WebcamSource


def test_latest_frame_buffer_drop_oldest():
    """Verify that buffer keeps only latest frame and increments dropped count."""
    buf = LatestFrameBuffer(maxsize=1)
    
    img1 = np.zeros((100, 100, 3), dtype=np.uint8)
    img2 = np.ones((100, 100, 3), dtype=np.uint8)
    img3 = np.full((100, 100, 3), 2, dtype=np.uint8)

    t0 = time.time()
    f1 = FrameData(1, t0, img1, 100, 100)
    f2 = FrameData(2, t0 + 0.01, img2, 100, 100)
    f3 = FrameData(3, t0 + 0.02, img3, 100, 100)

    buf.put(f1)
    buf.put(f2)
    buf.put(f3)

    assert buf.dropped_count == 2
    assert buf.queue_depth == 1

    latest = buf.pop_latest(timeout=0.1)
    assert latest is not None
    assert latest.frame_index == 3
    assert buf.queue_depth == 0


def test_webcam_source_synthetic_injection():
    """Verify synthetic frame injection into video source."""
    config = VideoConfig(device=0)
    source = WebcamSource(config)

    test_img = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame = source.inject_synthetic_frame(test_img)

    assert frame.frame_index == 1
    assert frame.width == 1280
    assert frame.height == 720
    assert frame.age_ms >= 0.0

    retrieved = source.buffer.pop_latest(timeout=0.1)
    assert retrieved is not None
    assert retrieved.frame_index == 1
