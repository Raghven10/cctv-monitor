"""Tests for OCR text normalization, spatial association, and OCR+VLM identity fusion."""

import pytest

from cctv_poc.camera.identity_resolver import IdentityResolver
from cctv_poc.ocr.label_reader import OCRResult, normalize_camera_label
from cctv_poc.vlm.label_validator import VLMResult


def test_normalize_camera_label():
    """Verify camera label normalization rules."""
    assert normalize_camera_label("CAM- 01") == "CAM-01"
    assert normalize_camera_label("  cam 07  ") == "CAM-07"
    assert normalize_camera_label("gate east") == "GATE-EAST"
    assert normalize_camera_label("PARKING - 02") == "PARKING-02"
    assert normalize_camera_label("#CAM-12*") == "CAM-12"


def test_spatial_association_to_pane():
    """Verify text bounding boxes are mapped to containing or closest pane."""
    pane_bboxes = [
        ("P01", (0, 0, 600, 400)),
        ("P02", (600, 0, 600, 400)),
        ("P03", (0, 400, 600, 400)),
        ("P04", (600, 400, 600, 400)),
    ]

    # Text box inside P01 (top-left)
    assigned_id, conf = IdentityResolver.associate_text_box_to_pane((20, 20, 100, 30), pane_bboxes)
    assert assigned_id == "P01"
    assert conf >= 0.95

    # Text box inside P04 (bottom-right)
    assigned_id, conf = IdentityResolver.associate_text_box_to_pane((700, 500, 120, 30), pane_bboxes)
    assert assigned_id == "P04"
    assert conf >= 0.95


def test_identity_fusion_ocr_and_vlm():
    """Verify fusion behavior for agreeing, high-confidence, and conflicting OCR/VLM."""
    resolver = IdentityResolver()

    # Case 1: OCR and VLM agree
    ocr1 = OCRResult("CAM-01", "CAM-01", 0.92)
    vlm1 = VLMResult("P01", "CAM-01", 0.95, timestamp=0.0, latency_ms=10.0)
    res1 = resolver.resolve_pane_identity("P01", ocr1, vlm1)
    assert res1.final_label == "CAM-01"
    assert res1.confidence >= 0.95
    assert not res1.is_unknown

    # Case 2: High OCR confidence
    ocr2 = OCRResult("GATE-EAST", "GATE-EAST", 0.88)
    res2 = resolver.resolve_pane_identity("P02", ocr2, None)
    assert res2.final_label == "GATE-EAST"
    assert not res2.is_unknown

    # Case 3: Low confidence OCR, high confidence VLM validation
    ocr3 = OCRResult("C4M-O3", "C4M-O3", 0.35)
    vlm3 = VLMResult("P03", "CAM-03", 0.90, timestamp=0.0, latency_ms=10.0)
    res3 = resolver.resolve_pane_identity("P03", ocr3, vlm3)
    assert res3.final_label == "CAM-03"
    assert res3.confidence == 0.90

    # Case 4: Neither OCR nor VLM have confidence -> UNKNOWN
    res4 = resolver.resolve_pane_identity("P04", None, None)
    assert res4.final_label == "UNKNOWN"
    assert res4.is_unknown
