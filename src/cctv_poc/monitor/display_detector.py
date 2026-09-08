"""Automatic physical monitor detection and boundary localization."""

from dataclasses import dataclass
from typing import List, Optional, Tuple
import cv2
import numpy as np

from ..config import DisplayConfig
from ..utils.logging import setup_logger
from .calibration import CalibrationStore

logger = setup_logger("cctv_poc.display_detector")


@dataclass
class DisplayDetectionResult:
    """Result of physical monitor detection."""
    corners: np.ndarray          # Shape (4, 2) in [TL, TR, BR, BL] order
    confidence: float            # 0.0 to 1.0
    bbox: Tuple[int, int, int, int]  # (x, y, w, h) in original webcam frame
    is_detected: bool = False
    is_calibrated_fallback: bool = False
    is_full_frame_fallback: bool = False


def order_corners(pts: np.ndarray) -> np.ndarray:
    """
    Order 4 (x, y) points into [Top-Left, Top-Right, Bottom-Right, Bottom-Left].
    pts shape: (4, 2) or (4, 1, 2)
    """
    pts = pts.reshape((4, 2)).astype(np.float32)
    rect = np.zeros((4, 2), dtype=np.float32)

    # Top-Left has smallest sum (x + y), Bottom-Right has largest sum (x + y)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]

    # Top-Right has smallest diff (y - x), Bottom-Left has largest diff (y - x)
    diff = np.diff(pts, axis=1).squeeze()
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]

    return rect


class DisplayDetector:
    """Detects physical monitor display outer boundary in webcam frames with adaptive multi-scale edge filtering."""

    def __init__(self, config: DisplayConfig):
        self.config = config
        self.calibration_store = CalibrationStore(config.calibration_file)
        self._last_confident_corners: Optional[np.ndarray] = None
        # Effective minimum area fraction (e.g. 0.12 if playing on laptop/screen in view)
        self.min_area_fraction = getattr(config, "min_area_fraction", 0.12) or 0.12

    def detect(self, image: np.ndarray) -> DisplayDetectionResult:
        """
        Detect monitor boundary. If no monitor is in view, return is_detected=False.
        """
        h, w = image.shape[:2]
        frame_area = float(w * h)

        if self.config.auto_detect:
            auto_res = self._detect_auto(image, w, h, frame_area)
            if auto_res is not None and auto_res.confidence >= 0.50:
                auto_res.is_detected = True
                self._last_confident_corners = auto_res.corners
                return auto_res

        # Fallback 1: Calibrated corners (if user explicitly calibrated)
        calib_corners = self.calibration_store.load_corners(w, h)
        if calib_corners is not None:
            ordered = order_corners(calib_corners)
            x_min, y_min = int(np.min(ordered[:, 0])), int(np.min(ordered[:, 1]))
            x_max, y_max = int(np.max(ordered[:, 0])), int(np.max(ordered[:, 1]))
            return DisplayDetectionResult(
                corners=ordered,
                confidence=0.85,
                bbox=(x_min, y_min, max(1, x_max - x_min), max(1, y_max - y_min)),
                is_detected=True,
                is_calibrated_fallback=True,
            )

        # No physical monitor detected in view -> default to full frame
        full_corners = np.array(
            [[0.0, 0.0], [float(w - 1), 0.0], [float(w - 1), float(h - 1)], [0.0, float(h - 1)]],
            dtype=np.float32,
        )
        return DisplayDetectionResult(
            corners=full_corners,
            confidence=0.0,
            bbox=(0, 0, w, h),
            is_detected=False,
            is_full_frame_fallback=True,
        )

    def _detect_auto(
        self, image: np.ndarray, w: int, h: int, frame_area: float
    ) -> Optional[DisplayDetectionResult]:
        """Multi-pass computer-vision detection of rectangular monitor bezel."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image

        # Pass 1: Standard Gaussian blur + Canny
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges1 = cv2.Canny(blurred, 25, 120)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        dilated1 = cv2.dilate(edges1, kernel, iterations=2)

        # Pass 2: Otsu adaptive edge thresholding for high-contrast screens
        _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        edges2 = cv2.Canny(otsu, 50, 150)
        dilated2 = cv2.dilate(edges2, kernel, iterations=2)

        combined_edges = cv2.bitwise_or(dilated1, dilated2)
        contours, _ = cv2.findContours(combined_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        best_corners: Optional[np.ndarray] = None
        best_score = 0.0
        best_bbox = (0, 0, w, h)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            area_frac = area / frame_area
            # Allow screens that occupy from 12% to 98% of the camera view
            if area_frac < self.min_area_fraction or area_frac > 0.99:
                continue

            peri = cv2.arcLength(cnt, True)
            # Approximate polygon with progressive tolerance
            for eps_factor in (0.02, 0.03, 0.04):
                approx = cv2.approxPolyDP(cnt, eps_factor * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    corners = order_corners(approx.reshape(4, 2))
                    
                    tl, tr, br, bl = corners
                    width_top = np.linalg.norm(tr - tl)
                    width_bot = np.linalg.norm(br - bl)
                    height_left = np.linalg.norm(bl - tl)
                    height_right = np.linalg.norm(br - tr)
                    
                    avg_width = (width_top + width_bot) / 2.0
                    avg_height = (height_left + height_right) / 2.0
                    
                    if avg_height <= 20 or avg_width <= 20:
                        continue
                        
                    aspect = avg_width / avg_height
                    # Standard display aspect ratios: 4:3 (1.33), 16:10 (1.6), 16:9 (1.78), 21:9 (2.33)
                    if aspect < 0.85 or aspect > 3.0:
                        continue

                    aspect_score = 1.0 - min(abs(aspect - 1.777) / 1.777, 0.5)
                    area_score = min(area_frac / 0.50, 1.0)
                    
                    score = (0.5 * area_score) + (0.5 * aspect_score)
                    if score > best_score:
                        best_score = score
                        best_corners = corners
                        x, y, bw, bh = cv2.boundingRect(approx)
                        best_bbox = (x, y, bw, bh)
                    break

        if best_corners is not None and best_score >= 0.45:
            return DisplayDetectionResult(
                corners=best_corners,
                confidence=float(min(best_score + 0.15, 0.98)),
                bbox=best_bbox,
            )

        return None
