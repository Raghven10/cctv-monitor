"""Perspective correction and coordinate mapping."""

from typing import List, Optional, Tuple, Union
import cv2
import numpy as np

from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.perspective")


class PerspectiveCorrector:
    """Computes homography and rectifies display to canonical 2D plane."""

    def __init__(
        self,
        corners: np.ndarray,
        target_width: int = 3840,
        target_height: int = 2160,
    ):
        """
        corners: (4, 2) array of [TL, TR, BR, BL] in original frame coordinates.
        target_width, target_height: Dimensions of rectified canonical display.
        """
        self.corners = corners.astype(np.float32)
        self.target_width = int(target_width)
        self.target_height = int(target_height)

        self.dst_corners = np.array(
            [
                [0.0, 0.0],
                [float(self.target_width - 1), 0.0],
                [float(self.target_width - 1), float(self.target_height - 1)],
                [0.0, float(self.target_height - 1)],
            ],
            dtype=np.float32,
        )

        # Forward homography: original -> rectified
        self.H = cv2.getPerspectiveTransform(self.corners, self.dst_corners)
        # Inverse homography: rectified -> original
        self.H_inv = cv2.getPerspectiveTransform(self.dst_corners, self.corners)

    def rectify(self, image: np.ndarray) -> np.ndarray:
        """Warp input webcam image to rectified display."""
        return cv2.warpPerspective(
            image,
            self.H,
            (self.target_width, self.target_height),
            flags=cv2.INTER_LINEAR,
        )

    def map_rectified_to_original(self, points: Union[np.ndarray, List[Tuple[float, float]]]) -> np.ndarray:
        """
        Transform points from rectified space (3840x2160) back to original webcam coordinates.
        Input shape: (N, 2) or list of (x, y)
        Returns shape: (N, 2)
        """
        pts = np.array(points, dtype=np.float32).reshape(-1, 1, 2)
        transformed = cv2.perspectiveTransform(pts, self.H_inv)
        return transformed.reshape(-1, 2)

    def map_original_to_rectified(self, points: Union[np.ndarray, List[Tuple[float, float]]]) -> np.ndarray:
        """
        Transform points from original webcam coordinates to rectified space.
        Input shape: (N, 2) or list of (x, y)
        Returns shape: (N, 2)
        """
        pts = np.array(points, dtype=np.float32).reshape(-1, 1, 2)
        transformed = cv2.perspectiveTransform(pts, self.H)
        return transformed.reshape(-1, 2)

    def map_rectified_bbox_to_original_polygon(
        self, bbox: Tuple[int, int, int, int]
    ) -> np.ndarray:
        """
        Transform a rectified bounding box (x, y, w, h) into a 4-point polygon in the original frame.
        Returns shape (4, 2) integer array.
        """
        x, y, w, h = bbox
        rect_pts = np.array(
            [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32
        )
        orig_pts = self.map_rectified_to_original(rect_pts)
        return np.int32(np.round(orig_pts))
