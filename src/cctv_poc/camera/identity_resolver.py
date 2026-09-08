"""Spatial label-to-pane association and OCR + VLM fusion."""

from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np

from ..ocr.label_reader import OCRResult
from ..vlm.label_validator import VLMResult
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.identity_resolver")


@dataclass
class ResolvedPaneIdentity:
    """Resolved identity for a single pane at a single instant."""
    pane_id: str
    raw_text: str
    normalized_text: str
    ocr_confidence: float
    vlm_confidence: float
    association_confidence: float
    final_label: str
    confidence: float
    is_unknown: bool = False


class IdentityResolver:
    """Combines OCR fast-path, VLM semantic validation, and spatial containment."""

    @staticmethod
    def resolve_pane_identity(
        pane_id: str,
        ocr_result: Optional[OCRResult] = None,
        vlm_result: Optional[VLMResult] = None,
        association_confidence: float = 0.98,
    ) -> ResolvedPaneIdentity:
        """
        Fuse OCR and VLM evidence into a candidate camera identity.
        Never fabricate a label if confidence is insufficient.
        """
        raw_text = ocr_result.raw_text if ocr_result else ""
        norm_text = ocr_result.normalized_text if ocr_result else ""
        ocr_conf = ocr_result.confidence if ocr_result else 0.0

        vlm_label = vlm_result.label if vlm_result and vlm_result.is_valid else ""
        vlm_conf = vlm_result.confidence if vlm_result and vlm_result.is_valid else 0.0

        final_label = ""
        final_conf = 0.0

        # Case 1: Both OCR and VLM are present and agree
        if norm_text and vlm_label and norm_text == vlm_label:
            final_label = norm_text
            final_conf = min(0.99, max(ocr_conf, vlm_conf) * 1.05)

        # Case 2: High confidence OCR
        elif norm_text and ocr_conf >= 0.70:
            final_label = norm_text
            final_conf = ocr_conf

        # Case 3: High confidence VLM override / validation
        elif vlm_label and vlm_conf >= 0.80:
            final_label = vlm_label
            final_conf = vlm_conf

        # Case 4: Moderate OCR or VLM
        elif norm_text and ocr_conf >= 0.50:
            final_label = norm_text
            final_conf = ocr_conf
        elif vlm_label and vlm_conf >= 0.50:
            final_label = vlm_label
            final_conf = vlm_conf

        # Case 5: Insufficient evidence
        else:
            final_label = "UNKNOWN"
            final_conf = 0.0

        is_unknown = (final_label == "UNKNOWN" or not final_label or final_conf < 0.40)

        return ResolvedPaneIdentity(
            pane_id=pane_id,
            raw_text=raw_text,
            normalized_text=norm_text,
            ocr_confidence=ocr_conf,
            vlm_confidence=vlm_conf,
            association_confidence=association_confidence,
            final_label=final_label if not is_unknown else "UNKNOWN",
            confidence=final_conf if not is_unknown else 0.0,
            is_unknown=is_unknown,
        )

    @staticmethod
    def associate_text_box_to_pane(
        text_bbox: Tuple[int, int, int, int],  # (x, y, w, h) in rectified space
        pane_bboxes: List[Tuple[str, Tuple[int, int, int, int]]],  # [(pane_id, (x, y, w, h))]
    ) -> Tuple[Optional[str], float]:
        """
        Find which pane contains or is closest to a given text bounding box.
        Returns (best_pane_id, association_confidence).
        """
        tx, ty, tw, th = text_bbox
        tcx = tx + tw / 2.0
        tcy = ty + th / 2.0

        for pane_id, (px, py, pw, ph) in pane_bboxes:
            # Check containment
            if px <= tcx <= (px + pw) and py <= tcy <= (py + ph):
                return pane_id, 0.98

        # If outside due to border gutter, find nearest pane
        best_id = None
        min_dist = float("inf")
        for pane_id, (px, py, pw, ph) in pane_bboxes:
            pcx = px + pw / 2.0
            pcy = py + ph / 2.0
            dist = (tcx - pcx) ** 2 + (tcy - pcy) ** 2
            if dist < min_dist:
                min_dist = dist
                best_id = pane_id

        return best_id, 0.75
