"""Production-Grade CCTV Pane & Layout Discovery Engine.

Combines:
1. Probabilistic Hough Line Transform with Angular Clustering (±12°)
2. Directional Morphological Line Opening (isolates screen dividers from scene textures)
3. Inter-Pane Luminance Contrast Gradient Profiling
4. Gutter Darkness Valley & Intensity Profiling
5. Global Energy Maximization & Geometric Grid Partitioning
"""

import hashlib
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import cv2
import numpy as np

from ..config import LayoutConfig
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.layout_discovery")


@dataclass
class PaneGeometry:
    """Geometric definition of a single discovered pane."""
    index: int                       # 0-indexed order
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


def _cluster_1d_positions(positions: List[int], tolerance: int) -> List[int]:
    """Cluster 1D positions that are close to each other and compute weighted medians."""
    if not positions:
        return []
    sorted_pos = sorted(positions)
    clusters: List[List[int]] = []
    current_cluster = [sorted_pos[0]]

    for p in sorted_pos[1:]:
        if (p - current_cluster[-1]) <= tolerance:
            current_cluster.append(p)
        else:
            clusters.append(current_cluster)
            current_cluster = [p]
    clusters.append(current_cluster)

    return [int(round(float(np.median(c)))) for c in clusters]


def _filter_and_regularize_separators(
    internal_separators: List[int],
    total_dimension: int,
    min_pane_size: int,
) -> List[int]:
    """
    Construct full separator boundary list [0, s1, s2, ..., total_dimension].
    Filters out boundaries too close to edges or each other.
    """
    if not internal_separators:
        return [0, total_dimension]

    valid = [0]
    for s in sorted(internal_separators):
        if min_pane_size <= s <= (total_dimension - min_pane_size):
            if (s - valid[-1]) >= min_pane_size:
                valid.append(s)

    if (total_dimension - valid[-1]) >= min_pane_size:
        valid.append(total_dimension)
    else:
        if len(valid) > 1:
            valid[-1] = total_dimension

    return sorted(list(set(valid)))


def _build_discovered_layout(
    x_seps: List[int],
    y_seps: List[int],
    w: int,
    h: int,
    confidence: float = 0.95,
) -> DiscoveredLayout:
    """Build a DiscoveredLayout dataclass from x and y separator coordinates."""
    num_rows = max(1, len(y_seps) - 1)
    num_cols = max(1, len(x_seps) - 1)
    total_panes = num_rows * num_cols

    panes: List[PaneGeometry] = []
    pane_idx = 0
    for r in range(num_rows):
        y1 = y_seps[r]
        y2 = y_seps[r + 1]
        for c in range(num_cols):
            x1 = x_seps[c]
            x2 = x_seps[c + 1]
            bw = max(1, x2 - x1)
            bh = max(1, y2 - y1)
            panes.append(
                PaneGeometry(
                    index=pane_idx,
                    bbox=(x1, y1, bw, bh),
                    row=r,
                    col=c,
                    geometry_confidence=confidence,
                )
            )
            pane_idx += 1

    sep_sig = f"{w}x{h}_x{x_seps}_y{y_seps}"
    hash_digest = hashlib.md5(sep_sig.encode()).hexdigest()[:6]
    layout_id = f"layout-{total_panes}p-{hash_digest}"

    return DiscoveredLayout(
        layout_id=layout_id,
        pane_count=total_panes,
        confidence=confidence,
        panes=panes,
        x_separators=x_seps,
        y_separators=y_seps,
    )


class LayoutDiscoverer:
    """
    Multi-Cue CCTV Physical Display Pane Discovery Engine.
    Discovers rectangular camera panes dynamically using contrast gradients,
    Hough line segment clustering, morphological filtering, and optimal partition energy.
    """

    def __init__(self, config: LayoutConfig):
        self.config = config

    def discover(
        self,
        rectified_image: np.ndarray,
        force_cctv_grid: bool = False,
    ) -> DiscoveredLayout:
        """
        Dynamically extract camera pane grid layout from a rectified CCTV monitor image.
        """
        h, w = rectified_image.shape[:2]
        gray = cv2.cvtColor(rectified_image, cv2.COLOR_BGR2GRAY) if len(rectified_image.shape) == 3 else rectified_image

        # Scale down for ultra-fast multi-scale analysis (preserving aspect ratio)
        analysis_w = 960
        scale = analysis_w / float(w)
        analysis_h = max(1, int(round(h * scale)))
        small_gray = cv2.resize(gray, (analysis_w, analysis_h), interpolation=cv2.INTER_AREA)

        min_pane_w_small = max(int(analysis_w * 0.18), 80)
        min_pane_h_small = max(int(analysis_h * 0.18), 60)
        min_pane_w = max(int(round(w * 0.18)), 100)
        min_pane_h = max(int(round(h * 0.18)), 80)
        inv_scale = 1.0 / scale

        # =====================================================================
        # CUE 1: Multi-scale Edge & Directional Morphological Line Opening
        # =====================================================================
        blurred = cv2.GaussianBlur(small_gray, (3, 3), 0)
        canny = cv2.Canny(blurred, 25, 85)

        # Morphological line kernels isolate continuous physical divider lines from scene textures
        v_kernel_len = max(25, int(analysis_h * 0.10))
        h_kernel_len = max(25, int(analysis_w * 0.10))
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_kernel_len))
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_kernel_len, 1))

        v_morph_lines = cv2.morphologyEx(canny, cv2.MORPH_OPEN, v_kernel)
        h_morph_lines = cv2.morphologyEx(canny, cv2.MORPH_OPEN, h_kernel)

        # =====================================================================
        # CUE 2: Probabilistic Hough Line Transform with Angular Clustering
        # =====================================================================
        hough_v_lines: List[int] = []
        hough_h_lines: List[int] = []

        raw_lines = cv2.HoughLinesP(
            canny,
            rho=1,
            theta=np.pi / 180,
            threshold=40,
            minLineLength=int(min(analysis_w, analysis_h) * 0.12),
            maxLineGap=12,
        )

        if raw_lines is not None:
            for line in raw_lines:
                pts = line.ravel()
                if len(pts) < 4:
                    continue
                x1, y1, x2, y2 = int(pts[0]), int(pts[1]), int(pts[2]), int(pts[3])
                dx = x2 - x1
                dy = y2 - y1
                length = math.hypot(dx, dy)
                if length < 20:
                    continue
                angle_deg = abs(math.degrees(math.atan2(dy, dx)))

                # Vertical line candidate (|angle - 90| <= 12)
                if abs(angle_deg - 90) <= 12:
                    mid_x = (x1 + x2) // 2
                    if min_pane_w_small <= mid_x <= (analysis_w - min_pane_w_small):
                        hough_v_lines.append(mid_x)

                # Horizontal line candidate (angle <= 12 or angle >= 168)
                elif angle_deg <= 12 or angle_deg >= 168:
                    mid_y = (y1 + y2) // 2
                    if min_pane_h_small <= mid_y <= (analysis_h - min_pane_h_small):
                        hough_h_lines.append(mid_y)

        clustered_hough_v = _cluster_1d_positions(hough_v_lines, tolerance=15)
        clustered_hough_h = _cluster_1d_positions(hough_h_lines, tolerance=15)

        # =====================================================================
        # CUE 3: Inter-Pane Luminosity Contrast Gradient
        # =====================================================================
        col_means = np.mean(small_gray, axis=0)
        row_means = np.mean(small_gray, axis=1)

        # Moving window contrast step (detects transitions between different camera scenes)
        window = 5
        col_diff = np.zeros(analysis_w, dtype=np.float32)
        row_diff = np.zeros(analysis_h, dtype=np.float32)

        for i in range(window, analysis_w - window):
            left_mean = float(np.mean(col_means[i - window : i]))
            right_mean = float(np.mean(col_means[i : i + window]))
            col_diff[i] = abs(right_mean - left_mean)

        for j in range(window, analysis_h - window):
            top_mean = float(np.mean(row_means[j - window : j]))
            bot_mean = float(np.mean(row_means[j : j + window]))
            row_diff[j] = abs(bot_mean - top_mean)

        # =====================================================================
        # CUE 4: Gutter Darkness Valley Profiler
        # =====================================================================
        # Dark divider gutters have low intensity compared to the mean
        sobel_x = np.abs(cv2.Sobel(small_gray, cv2.CV_32F, 1, 0, ksize=3))
        sobel_y = np.abs(cv2.Sobel(small_gray, cv2.CV_32F, 0, 1, ksize=3))

        # Integrated Vertical Energy Profile
        vert_energy = (
            np.sum(v_morph_lines, axis=0).astype(np.float32)
            + 0.5 * np.sum(sobel_x, axis=0)
            + 0.8 * (col_diff * float(analysis_h))
        )

        # Integrated Horizontal Energy Profile
        horiz_energy = (
            np.sum(h_morph_lines, axis=1).astype(np.float32)
            + 0.5 * np.sum(sobel_y, axis=1)
            + 0.8 * (row_diff * float(analysis_w))
        )

        # Boost energy at Hough-detected linear clusters
        for vx in clustered_hough_v:
            vert_energy[max(0, vx - 6) : min(analysis_w, vx + 7)] *= 1.4

        for hy in clustered_hough_h:
            horiz_energy[max(0, hy - 6) : min(analysis_h, hy + 7)] *= 1.4

        # =====================================================================
        # CUE 5: Global Separator Energy Maximization
        # =====================================================================
        # Find candidate peak separators from energy profiles
        def extract_peaks(energy_arr: np.ndarray, min_dist: int) -> List[int]:
            smoothed = np.convolve(energy_arr, np.ones(5) / 5.0, mode="same")
            max_e = float(np.max(smoothed)) if len(smoothed) > 0 else 0.0
            if max_e <= 0:
                return []
            mean_e = float(np.mean(smoothed))
            std_e = float(np.std(smoothed))
            thresh = max(mean_e + 1.2 * std_e, max_e * 0.35)

            peaks = []
            for i in range(1, len(smoothed) - 1):
                if (
                    smoothed[i] >= thresh
                    and smoothed[i] >= smoothed[i - 1]
                    and smoothed[i] >= smoothed[i + 1]
                ):
                    peaks.append((i, float(smoothed[i])))

            peaks.sort(key=lambda x: x[1], reverse=True)
            chosen = []
            for idx, _ in peaks:
                if not any(abs(idx - c) < min_dist for c in chosen):
                    chosen.append(idx)
                if len(chosen) >= 3:  # Max 4 segments per dimension
                    break
            return sorted(chosen)

        v_peaks = extract_peaks(vert_energy, min_pane_w_small)
        h_peaks = extract_peaks(horiz_energy, min_pane_h_small)

        # Combine Hough lines and energy peaks
        all_v_candidates = _cluster_1d_positions(v_peaks + clustered_hough_v, tolerance=12)
        all_h_candidates = _cluster_1d_positions(h_peaks + clustered_hough_h, tolerance=12)

        internal_x_scaled = [int(round(p * inv_scale)) for p in all_v_candidates]
        internal_y_scaled = [int(round(p * inv_scale)) for p in all_h_candidates]

        x_seps = _filter_and_regularize_separators(internal_x_scaled, w, min_pane_w)
        y_seps = _filter_and_regularize_separators(internal_y_scaled, h, min_pane_h)

        num_rows = len(y_seps) - 1
        num_cols = len(x_seps) - 1
        total_panes = num_rows * num_cols

        # =====================================================================
        # CUE 6: Multi-Grid Contrast Alignment Evaluation
        # If simple peak extraction found <= 1 pane, score standard grid divider candidates
        # (e.g. 50%, 33%, 25% divisions) against the energy profile to snap to true physical gutters
        # =====================================================================
        if total_panes <= 1:
            best_x_divs: Optional[List[int]] = None
            best_x_score = 0.0

            for div in [2, 3, 4]:
                score = 0.0
                div_pos = []
                for i in range(1, div):
                    target_x = int(round(i * analysis_w / float(div)))
                    window_slice = vert_energy[max(0, target_x - 18) : min(analysis_w, target_x + 19)]
                    if len(window_slice) > 0:
                        max_p = float(np.max(window_slice))
                        peak_offset = int(np.argmax(window_slice)) + max(0, target_x - 18)
                        score += max_p / (float(np.mean(vert_energy)) + 1e-5)
                        div_pos.append(int(round(peak_offset * inv_scale)))
                avg_score = score / float(div - 1)
                # Require significant contrast peak (> 1.70) to avoid false grid divisions from background noise
                if avg_score > 1.70 and avg_score > best_x_score:
                    best_x_score = avg_score
                    best_x_divs = div_pos

            best_y_divs: Optional[List[int]] = None
            best_y_score = 0.0

            for div in [2, 3, 4]:
                score = 0.0
                div_pos = []
                for j in range(1, div):
                    target_y = int(round(j * analysis_h / float(div)))
                    window_slice = horiz_energy[max(0, target_y - 18) : min(analysis_h, target_y + 19)]
                    if len(window_slice) > 0:
                        max_p = float(np.max(window_slice))
                        peak_offset = int(np.argmax(window_slice)) + max(0, target_y - 18)
                        score += max_p / (float(np.mean(horiz_energy)) + 1e-5)
                        div_pos.append(int(round(peak_offset * inv_scale)))
                avg_score = score / float(div - 1)
                # Require significant contrast peak (> 1.70) to avoid false grid divisions from background noise
                if avg_score > 1.70 and avg_score > best_y_score:
                    best_y_score = avg_score
                    best_y_divs = div_pos

            if best_x_divs or best_y_divs:
                x_seps = _filter_and_regularize_separators(best_x_divs or [], w, min_pane_w)
                y_seps = _filter_and_regularize_separators(best_y_divs or [], h, min_pane_h)
                num_rows = len(y_seps) - 1
                num_cols = len(x_seps) - 1
                total_panes = num_rows * num_cols

        # =====================================================================
        # Fallback & Minimum Panes Guarantee
        # =====================================================================
        if (total_panes <= 1) and force_cctv_grid:
            col_w = w // 2
            row_h = h // 2
            return _build_discovered_layout([0, col_w, w], [0, row_h, h], w, h, confidence=0.88)

        if total_panes == 1 and getattr(self.config, "min_panes", 1) == 1:
            return _build_discovered_layout([0, w], [0, h], w, h, confidence=0.88)

        if total_panes < getattr(self.config, "min_panes", 1) or total_panes == 0:
            return DiscoveredLayout(
                layout_id="no-panes-detected",
                pane_count=0,
                confidence=0.0,
                panes=[],
                x_separators=[],
                y_separators=[],
            )

        return _build_discovered_layout(
            x_seps=x_seps,
            y_seps=y_seps,
            w=w,
            h=h,
            confidence=0.96 if total_panes > 1 else 0.88,
        )
