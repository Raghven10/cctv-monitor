"""Assisted & Manual calibration persistence for physical display monitor corners."""

import json
from pathlib import Path
from typing import List, Optional, Tuple
import numpy as np

from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.calibration")


class CalibrationStore:
    """Manages saving and loading 4-point corner calibration for perspective correction."""

    def __init__(self, calibration_file: str = "config/calibration.json"):
        self.file_path = Path(calibration_file)

    def save_corners(
        self,
        corners: np.ndarray,
        source_width: int,
        source_height: int,
        target_width: int = 3840,
        target_height: int = 2160,
    ) -> None:
        """
        Save 4 corners [TL, TR, BR, BL] as normalized (0.0 to 1.0) and absolute coordinates.
        Corners shape: (4, 2)
        """
        corners_list = corners.tolist()
        normalized_corners = [
            [float(pt[0]) / max(source_width, 1), float(pt[1]) / max(source_height, 1)]
            for pt in corners_list
        ]

        data = {
            "source_width": source_width,
            "source_height": source_height,
            "target_width": target_width,
            "target_height": target_height,
            "corners": corners_list,
            "normalized_corners": normalized_corners,
        }

        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Calibration saved to {self.file_path} for frame {source_width}x{source_height}")

    def load_corners(
        self,
        current_width: int,
        current_height: int,
    ) -> Optional[np.ndarray]:
        """
        Load calibrated corners, scaling by current resolution if source resolution differs.
        Returns shape (4, 2) float32 array in [TL, TR, BR, BL] order, or None.
        """
        if not self.file_path.exists():
            return None

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if "normalized_corners" in data:
                norm = data["normalized_corners"]
                corners = np.array(
                    [
                        [norm[i][0] * current_width, norm[i][1] * current_height]
                        for i in range(4)
                    ],
                    dtype=np.float32,
                )
                return corners
            elif "corners" in data:
                orig_w = data.get("source_width", current_width)
                orig_h = data.get("source_height", current_height)
                scale_x = current_width / orig_w
                scale_y = current_height / orig_h
                corners = np.array(data["corners"], dtype=np.float32)
                corners[:, 0] *= scale_x
                corners[:, 1] *= scale_y
                return corners
        except Exception as e:
            logger.error(f"Error loading calibration from {self.file_path}: {e}")
            return None

        return None
