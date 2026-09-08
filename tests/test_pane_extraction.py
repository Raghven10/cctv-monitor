"""Tests for pane ROI extraction and P01..PN labeling."""

import cv2
import numpy as np
import pytest

from cctv_poc.config import LayoutConfig
from cctv_poc.monitor.layout_discovery import LayoutDiscoverer
from cctv_poc.monitor.pane_extractor import PaneExtractor, format_pane_id


def create_synthetic_cctv_layout(rows: int, cols: int, width: int = 1920, height: int = 1080) -> np.ndarray:
    img = np.full((height, width, 3), 40, dtype=np.uint8)
    pw = width // cols
    ph = height // rows
    for r in range(rows):
        for c in range(cols):
            x1, y1 = c * pw, r * ph
            x2, y2 = (c + 1) * pw if c < cols - 1 else width, (r + 1) * ph if r < rows - 1 else height
            cv2.rectangle(img, (x1 + 3, y1 + 3), (x2 - 3, y2 - 3), (50, 70, 90), -1)
            cv2.rectangle(img, (x1, y1), (x2, y2), (240, 240, 240), 4)
    return img


def test_pane_extraction_ids_and_crops():
    """Verify pane IDs (P01, P02, ...), normalized coordinates, and image crop sizes."""
    config = LayoutConfig()
    discoverer = LayoutDiscoverer(config)
    extractor = PaneExtractor()

    synth_img = create_synthetic_cctv_layout(2, 3, width=1200, height=800)
    layout = discoverer.discover(synth_img)
    panes = extractor.extract_panes(synth_img, layout)

    assert len(panes) == 6
    expected_ids = ["P01", "P02", "P03", "P04", "P05", "P06"]
    assert [p.pane_id for p in panes] == expected_ids

    for p in panes:
        assert p.image.shape[0] == p.bbox[3]
        assert p.image.shape[1] == p.bbox[2]
        nx, ny, nw, nh = p.normalized_bbox
        assert 0.0 <= nx <= 1.0
        assert 0.0 <= ny <= 1.0
        assert 0.0 < nw <= 1.0
        assert 0.0 < nh <= 1.0


def test_format_pane_id():
    assert format_pane_id(0) == "P01"
    assert format_pane_id(8) == "P09"
    assert format_pane_id(15) == "P16"
