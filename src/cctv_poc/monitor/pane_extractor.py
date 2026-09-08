"""Extracts sub-image ROIs and maintains stable spatial pane identifiers (P01, P02, ...)."""

from dataclasses import dataclass
from typing import List, Tuple
import numpy as np

from .layout_discovery import DiscoveredLayout, PaneGeometry


@dataclass
class ExtractedPane:
    """Individual extracted pane with image ROI and metadata."""
    pane_id: str                      # "P01", "P02", ...
    index: int                        # 0, 1, 2, ...
    bbox: Tuple[int, int, int, int]   # (x, y, w, h) in rectified space
    normalized_bbox: Tuple[float, float, float, float]  # (x/W, y/H, w/W, h/H)
    image: np.ndarray                 # Cropped BGR sub-image
    geometry_confidence: float
    row: int
    col: int


def format_pane_id(index: int) -> str:
    """Format 0-based index to P01, P02, ..."""
    return f"P{index + 1:02d}"


class PaneExtractor:
    """Crops and labels pane ROIs from rectified monitor display."""

    @staticmethod
    def extract_panes(
        rectified_image: np.ndarray,
        layout: DiscoveredLayout,
    ) -> List[ExtractedPane]:
        """Extract cropped images and formatted IDs for all panes."""
        h, w = rectified_image.shape[:2]
        extracted: List[ExtractedPane] = []

        for p in layout.panes:
            x, y, bw, bh = p.bbox
            x1 = max(0, min(x, w - 1))
            y1 = max(0, min(y, h - 1))
            x2 = max(x1 + 1, min(x + bw, w))
            y2 = max(y1 + 1, min(y + bh, h))

            crop = rectified_image[y1:y2, x1:x2].copy()
            pane_id = format_pane_id(p.index)
            norm_bbox = (
                float(x1) / w,
                float(y1) / h,
                float(x2 - x1) / w,
                float(y2 - y1) / h,
            )

            extracted.append(
                ExtractedPane(
                    pane_id=pane_id,
                    index=p.index,
                    bbox=(x1, y1, x2 - x1, y2 - y1),
                    normalized_bbox=norm_bbox,
                    image=crop,
                    geometry_confidence=p.geometry_confidence,
                    row=p.row,
                    col=p.col,
                )
            )

        return extracted
