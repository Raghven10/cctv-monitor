"""Integration tests for RealtimePipeline end-to-end."""

import json
import time
import cv2
import numpy as np
import pytest

from cctv_poc.config import POCConfig
from cctv_poc.realtime.pipeline import RealtimePipeline


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


def test_pipeline_synthetic_processing(tmp_path):
    """Verify processing multiple synthetic frames through the full pipeline."""
    jsonl_file = str(tmp_path / "test_results.jsonl")
    
    config = POCConfig()
    config.output.jsonl = jsonl_file
    config.stabilization.layout_frames_required = 2
    config.stabilization.label_observations_required = 2

    pipeline = RealtimePipeline(config)

    try:
        # Generate synthetic 4-pane display (2x2)
        synth_4p = create_synthetic_cctv_layout(2, 2, width=1280, height=720)

        # Process 3 frames
        for i in range(3):
            frame_data = pipeline.video_source.inject_synthetic_frame(synth_4p)
            res = pipeline.process_frame_data(frame_data)
            assert res.layout.pane_count == 4
            assert len(res.panes) == 4
            assert res.metrics is not None

        # Verify JSONL lines written
        with open(jsonl_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 3

        record = json.loads(lines[-1])
        assert record["schema_version"] == "1.0"
        assert record["layout"]["pane_count"] == 4
        assert len(record["panes"]) == 4

    finally:
        pipeline.stop()
