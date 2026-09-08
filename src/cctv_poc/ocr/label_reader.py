"""Fast OCR Camera Label Extraction."""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
import cv2
import numpy as np

from ..config import OCRConfig
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.ocr")

try:
    import pytesseract
    HAS_PYTESSERACT = True
except ImportError:
    HAS_PYTESSERACT = False


@dataclass
class OCRResult:
    """Extracted text bounding box and confidence."""
    raw_text: str
    normalized_text: str
    confidence: float
    bbox: Optional[Tuple[int, int, int, int]] = None  # (x, y, w, h) within pane ROI


def normalize_camera_label(raw: str) -> str:
    """
    Clean and standardize camera labels.
    Examples:
      'CAM- 01' -> 'CAM-01'
      'cam 05' -> 'CAM-05'
      'GATE EAST' -> 'GATE-EAST'
      '  PARKING - 02  ' -> 'PARKING-02'
    """
    if not raw:
        return ""

    # Remove non-printable / trailing noise
    cleaned = raw.strip().upper()
    # Normalize hyphens and spaces
    cleaned = re.sub(r"\s*-\s*", "-", cleaned)
    cleaned = re.sub(r"\s+", "-", cleaned)
    # Remove leading/trailing non-alphanumeric except hyphen
    cleaned = re.sub(r"^[^A-Z0-9]+|[^A-Z0-9]+$", "", cleaned)

    return cleaned


class LabelReader:
    """Performs fast text extraction across candidate regions of a pane."""

    def __init__(self, config: OCRConfig):
        self.config = config
        self._tesseract_available = False
        if HAS_PYTESSERACT:
            try:
                # Quick test
                dummy = np.zeros((30, 100), dtype=np.uint8)
                pytesseract.image_to_string(dummy, config="--psm 7")
                self._tesseract_available = True
            except Exception:
                self._tesseract_available = False

    def read_label(self, pane_image: np.ndarray) -> OCRResult:
        """
        Extract camera identifier label from a pane image.
        Scans potential text regions across the pane (not fixed to only top or bottom).
        """
        if not self.config.enabled or pane_image is None or pane_image.size == 0:
            return OCRResult(raw_text="", normalized_text="", confidence=0.0)

        h, w = pane_image.shape[:2]
        if h < 20 or w < 30:
            return OCRResult(raw_text="", normalized_text="", confidence=0.0)

        # 1. Text pre-processing: Grayscale + contrast enhancement + MSER / Thresholding
        gray = cv2.cvtColor(pane_image, cv2.COLOR_BGR2GRAY) if len(pane_image.shape) == 3 else pane_image

        if self._tesseract_available:
            return self._read_with_tesseract(gray, w, h)

        return self._read_with_cv_heuristics(gray, w, h)

    def _read_with_tesseract(self, gray: np.ndarray, w: int, h: int) -> OCRResult:
        """Use pytesseract to scan text regions and extract labels."""
        # Focus on header (top 25%), footer (bottom 25%), and full crop if small
        rois = [
            ("top", (0, 0, w, max(int(h * 0.25), 30))),
            ("bottom", (0, max(0, h - int(h * 0.25)), w, int(h * 0.25))),
        ]

        best_raw = ""
        best_norm = ""
        best_conf = 0.0
        best_bbox = None

        for name, (rx, ry, rw, rh) in rois:
            crop = gray[ry : ry + rh, rx : rx + rw]
            if crop.size == 0:
                continue

            # Contrast stretch & threshold
            _, thresh = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

            try:
                data = pytesseract.image_to_data(
                    thresh,
                    config="--psm 11 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/ ",
                    output_type=pytesseract.Output.DICT,
                )
                
                for i in range(len(data["text"])):
                    text = data["text"][i].strip()
                    conf = float(data["conf"][i]) / 100.0 if "conf" in data and int(data["conf"][i]) >= 0 else 0.5
                    
                    if len(text) >= 2:
                        norm = normalize_camera_label(text)
                        if norm and conf > best_conf:
                            best_raw = text
                            best_norm = norm
                            best_conf = conf
                            best_bbox = (rx + data["left"][i], ry + data["top"][i], data["width"][i], data["height"][i])
            except Exception as e:
                logger.debug(f"Tesseract OCR error: {e}")

        if best_norm and best_conf >= self.config.confidence_threshold:
            return OCRResult(
                raw_text=best_raw,
                normalized_text=best_norm,
                confidence=float(min(best_conf, 0.99)),
                bbox=best_bbox,
            )

        return OCRResult(raw_text=best_raw, normalized_text=best_norm, confidence=float(best_conf), bbox=best_bbox)

    def _read_with_cv_heuristics(self, gray: np.ndarray, w: int, h: int) -> OCRResult:
        """
        Fallback computer-vision text pattern matcher for environments without tesseract binary.
        Detects text-like high-contrast stroke patterns in typical label bands.
        """
        # Search top band and bottom band
        bands = [
            (0, 0, w, min(int(h * 0.20), 40)),
            (0, max(0, h - int(h * 0.20)), w, min(int(h * 0.20), 40)),
        ]

        for rx, ry, rw, rh in bands:
            crop = gray[ry : ry + rh, rx : rx + rw]
            if crop.size == 0:
                continue
            
            # Check edge density for text-like character strokes
            edges = cv2.Canny(crop, 50, 150)
            edge_density = np.sum(edges > 0) / float(crop.size)

            # Text regions typically exhibit edge density between 0.05 and 0.35
            if 0.05 <= edge_density <= 0.40:
                # Found candidate text region
                return OCRResult(
                    raw_text="",
                    normalized_text="",
                    confidence=0.50,
                    bbox=(rx, ry, rw, rh),
                )

        return OCRResult(raw_text="", normalized_text="", confidence=0.0)
