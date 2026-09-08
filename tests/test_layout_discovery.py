"""Tests for dynamic CCTV layout discovery across variable pane grids."""

import cv2
import numpy as np
import pytest

from cctv_poc.config import LayoutConfig
from cctv_poc.monitor.layout_discovery import LayoutDiscoverer


def create_synthetic_cctv_layout(rows: int, cols: int, width: int = 1920, height: int = 1080) -> np.ndarray:
    """Helper to draw synthetic CCTV layout with clear divider borders and labels."""
    img = np.full((height, width, 3), 40, dtype=np.uint8)
    pw = width // cols
    ph = height // rows

    for r in range(rows):
        for c in range(cols):
            x1, y1 = c * pw, r * ph
            x2, y2 = (c + 1) * pw if c < cols - 1 else width, (r + 1) * ph if r < rows - 1 else height
            # Draw distinct inner pane color
            color = ((r * 60 + 30) % 255, (c * 60 + 40) % 255, 80)
            cv2.rectangle(img, (x1 + 3, y1 + 3), (x2 - 3, y2 - 3), color, -1)
            # Draw strong white divider borders
            cv2.rectangle(img, (x1, y1), (x2, y2), (240, 240, 240), 4)

    return img


@pytest.mark.parametrize(
    "rows,cols,expected_panes",
    [
        (1, 1, 1),
        (2, 2, 4),
        (2, 3, 6),
        (3, 3, 9),
        (4, 4, 16),
    ],
)
def test_dynamic_layout_discovery_variable_grids(rows, cols, expected_panes):
    """Verify that layout discovery discovers 1, 4, 6, 9, 16 panes without hardcoded assumptions."""
    config = LayoutConfig(dynamic=True, min_panes=1, max_panes=64)
    discoverer = LayoutDiscoverer(config)

    synth_image = create_synthetic_cctv_layout(rows, cols, width=1920, height=1080)
    layout = discoverer.discover(synth_image)

    assert layout.pane_count == expected_panes
    assert len(layout.panes) == expected_panes
    assert layout.confidence >= 0.85
    assert layout.layout_id.startswith(f"layout-{expected_panes}p-")

    # Verify all panes have valid positive dimensions
    for p in layout.panes:
        x, y, w, h = p.bbox
        assert w > 0 and h > 0
        assert x >= 0 and y >= 0
        assert (x + w) <= 1920
        assert (y + h) <= 1080
