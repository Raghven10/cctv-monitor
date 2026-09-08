"""Dynamic CCTV Pane & Layout Discovery without hard-coded grid assumptions."""

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import cv2
import numpy as np

from ..config import LayoutConfig
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.layout_discovery")


@dataclass
class PaneGeometry:
    """Geometric definition of a single discovered pane."""
    index: int                  # 0-indexed order
    bbox: Tuple[int, int, int, int]  # (x, y, w, h) in rectified space
    row: int
    col: int
    geometry_confidence: float = 0.95


@dataclass
class DiscoveredLayout:
    """Discovered layout geometry and metadata."""
    layout_id: str
    pane_count: int
    confidence: float
    panes: List[PaneGeometry]
    x_separators: List[int] = field(default_factory=list)
    y_separators: List[int] = field(default_factory=list)


def _find_projection_peaks(
    profile: np.ndarray,
    min_distance: int,
    threshold: float,
) -> List[int]:
    """
    Find significant peaks in a 1D projection profile.
    Uses local maxima filtering with minimum distance spacing.
    """
    peaks = []
    length = len(profile)
    
    smoothed = np.convolve(profile, np.ones(5) / 5.0, mode="same")
    
    for i in range(1, length - 1):
        if smoothed[i] > threshold and smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
            if not peaks or (i - peaks[-1]) >= min_distance:
                peaks.append(i)
            elif smoothed[i] > smoothed[peaks[-1]]:
                peaks[-1] = i

    return peaks


def _filter_and_regularize_separators(
    separators: List[int],
    total_dimension: int,
    min_pane_size: int,
) -> List[int]:
    """
    Filter separators that are too close to boundaries or each other.
    Ensures outer boundaries [0, total_dimension] are included.
    """
    valid = [0]
    for s in separators:
        if s > min_pane_size and s < (total_dimension - min_pane_size):
            if (s - valid[-1]) >= min_pane_size:
                valid.append(s)

    if (total_dimension - valid[-1]) >= min_pane_size:
        valid.append(total_dimension)
    else:
        valid[-1] = total_dimension

    return sorted(list(set(valid)))


class LayoutDiscoverer:
    """Infers variable CCTV pane layouts dynamically from rectified monitor images."""

    def __init__(self, config: LayoutConfig):
        self.config = config

    def discover(self, rectified_image: np.ndarray) -> DiscoveredLayout:
        """
        Dynamically discover horizontal and vertical separators and derive rectangular panes.
        """
        h, w = rectified_image.shape[:2]
        gray = cv2.cvtColor(rectified_image, cv2.COLOR_BGR2GRAY) if len(rectified_image.shape) == 3 else rectified_image

        analysis_w = 960
        scale = analysis_w / float(w)
        analysis_h = int(h * scale)
        small_gray = cv2.resize(gray, (analysis_w, analysis_h), interpolation=cv2.INTER_AREA)

        # 1. Edge & gradient extraction
        blurred = cv2.GaussianBlur(small_gray, (3, 3), 0)
        canny = cv2.Canny(blurred, 30, 100)

        # Vertical edges
        sobel_x = cv2.Sobel(small_gray, cv2.CV_32F, 1, 0, ksize=3)
        abs_sobel_x = np.abs(sobel_x)

        # Horizontal edges
        sobel_y = cv2.Sobel(small_gray, cv2.CV_32F, 0, 1, ksize=3)
        abs_sobel_y = np.abs(sobel_y)

        # 2. Vertical and horizontal projection analysis
        vert_proj = np.sum(abs_sobel_x, axis=0)
        horiz_proj = np.sum(abs_sobel_y, axis=1)

        min_pane_w_small = max(int(analysis_w * 0.15), 60)
        min_pane_h_small = max(int(analysis_h * 0.15), 50)

        v_std = np.std(vert_proj)
        h_std = np.std(horiz_proj)
        v_max = np.max(vert_proj) if len(vert_proj) > 0 else 0
        h_max = np.max(horiz_proj) if len(horiz_proj) > 0 else 0

        # Adaptive thresholding for separator peaks
        v_thresh = np.mean(vert_proj) + 1.1 * v_std
        h_thresh = np.mean(horiz_proj) + 1.1 * h_std

        v_peaks_small = _find_projection_peaks(vert_proj, min_pane_w_small, v_thresh) if (v_max > 1.2 * v_thresh) else []
        h_peaks_small = _find_projection_peaks(horiz_proj, min_pane_h_small, h_thresh) if (h_max > 1.2 * h_thresh) else []

        # 3. Continuous line verification
        valid_v_peaks = []
        for vp in v_peaks_small:
            band = canny[:, max(0, vp - 3) : min(analysis_w, vp + 4)]
            cont = float(np.sum(np.any(band > 0, axis=1))) / float(analysis_h)
            if cont >= 0.22:
                valid_v_peaks.append(vp)

        valid_h_peaks = []
        for hp in h_peaks_small:
            band = canny[max(0, hp - 3) : min(analysis_h, hp + 4), :]
            cont = float(np.sum(np.any(band > 0, axis=0))) / float(analysis_w)
            if cont >= 0.22:
                valid_h_peaks.append(hp)

        # Map verified peaks back to full coordinate space
        inv_scale = 1.0 / scale
        x_seps_raw = [int(p * inv_scale) for p in valid_v_peaks]
        y_seps_raw = [int(p * inv_scale) for p in valid_h_peaks]

        min_pane_w = int(w * 0.15)
        min_pane_h = int(h * 0.15)

        x_seps = _filter_and_regularize_separators(x_seps_raw, w, min_pane_w)
        y_seps = _filter_and_regularize_separators(y_seps_raw, h, min_pane_h)

        num_rows = len(y_seps) - 1
        num_cols = len(x_seps) - 1
        total_panes = num_rows * num_cols

        if len(x_seps_raw) == 0 and len(y_seps_raw) == 0:
            x_seps = [0, w]
            y_seps = [0, h]
            num_rows = 1
            num_cols = 1
            total_panes = 1

        # 4. Generate rectangular panes & validate geometry
        panes: List[PaneGeometry] = []
        pane_idx = 0
        is_geometry_valid = True

        if total_panes > self.config.max_panes or total_panes < self.config.min_panes:
            total_panes = 1
            x_seps = [0, w]
            y_seps = [0, h]
            num_rows = 1
            num_cols = 1

        for r in range(num_rows):
            y1 = y_seps[r]
            y2 = y_seps[r + 1]
            for c in range(num_cols):
                x1 = x_seps[c]
                x2 = x_seps[c + 1]
                bw = max(1, x2 - x1)
                bh = max(1, y2 - y1)

                if total_panes > 1:
                    aspect = float(bw) / float(bh) if bh > 0 else 0.0
                    area_frac = float(bw * bh) / float(w * h)
                    if aspect < 0.35 or aspect > 3.2 or area_frac < 0.02:
                        is_geometry_valid = False

                panes.append(
                    PaneGeometry(
                        index=pane_idx,
                        bbox=(x1, y1, bw, bh),
                        row=r,
                        col=c,
                        geometry_confidence=0.96 if total_panes > 1 else 0.90,
                    )
                )
                pane_idx += 1

        if not is_geometry_valid:
            x_seps = [0, w]
            y_seps = [0, h]
            total_panes = 1
            panes = [
                PaneGeometry(
                    index=0,
                    bbox=(0, 0, w, h),
                    row=0,
                    col=0,
                    geometry_confidence=0.90,
                )
            ]

        # 5. Compute deterministic layout_id
        sep_sig = f"{w}x{h}_x{x_seps}_y{y_seps}"
        hash_digest = hashlib.md5(sep_sig.encode()).hexdigest()[:6]
        layout_id = f"layout-{total_panes}p-{hash_digest}"

        confidence = 0.95 if total_panes > 1 else 0.88

        return DiscoveredLayout(
            layout_id=layout_id,
            pane_count=total_panes,
            confidence=confidence,
            panes=panes,
            x_separators=x_seps,
            y_separators=y_seps,
        )
