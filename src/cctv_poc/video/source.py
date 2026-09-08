"""Webcam frame acquisition with Latest-Frame Buffering for zero-backlog real-time processing."""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import cv2
import numpy as np

from ..config import VideoConfig
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.video")


@dataclass
class FrameData:
    """Encapsulates captured frame and timing metadata."""
    frame_index: int
    timestamp: float          # Epoch seconds when frame was grabbed
    image: np.ndarray         # BGR image
    width: int
    height: int
    capture_fps: float = 0.0

    @property
    def age_ms(self) -> float:
        """Milliseconds elapsed since frame capture."""
        return (time.time() - self.timestamp) * 1000.0


class LatestFrameBuffer:
    """Thread-safe single-slot or bounded buffer that automatically drops stale frames."""

    def __init__(self, maxsize: int = 2):
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._latest_frame: Optional[FrameData] = None
        self._dropped_count: int = 0
        self._total_produced: int = 0

    def put(self, frame: FrameData) -> None:
        """Store newest frame, dropping previous unconsumed frame if any."""
        with self._condition:
            if self._latest_frame is not None:
                self._dropped_count += 1
            self._latest_frame = frame
            self._total_produced += 1
            self._condition.notify_all()

    def get_latest(self, timeout: Optional[float] = 0.5) -> Optional[FrameData]:
        """Fetch the latest available frame without blocking if already available."""
        with self._condition:
            if self._latest_frame is None:
                if not self._condition.wait(timeout=timeout):
                    return None
            frame = self._latest_frame
            # We don't necessarily clear it, or we can leave it / copy it
            # For pure stream consumption, returning the frame allows consumers to check frame_index
            return frame

    def pop_latest(self, timeout: Optional[float] = 0.5) -> Optional[FrameData]:
        """Fetch and remove the latest frame, blocking until available or timeout."""
        with self._condition:
            if self._latest_frame is None:
                if not self._condition.wait(timeout=timeout):
                    return None
            frame = self._latest_frame
            self._latest_frame = None
            return frame

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return 1 if self._latest_frame is not None else 0


class WebcamSource:
    """Acquires frames from a live physical webcam continuously in a background thread."""

    def __init__(self, config: VideoConfig):
        self.config = config
        self.buffer = LatestFrameBuffer(maxsize=config.buffer_size)
        self._cap: Optional[cv2.VideoCapture] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._frame_count: int = 0
        
        # Negotiated stats
        self.actual_width: int = 0
        self.actual_height: int = 0
        self.actual_fps: float = 0.0
        self.measured_fps: float = 0.0

    def start(self, auto_open: bool = True) -> bool:
        """Initialize camera and start capture thread."""
        if auto_open:
            if not self.open_device():
                return False

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, name="WebcamCaptureThread", daemon=True)
        self._thread.start()
        logger.info("Webcam capture thread started successfully.")
        return True

    def open_device(self) -> bool:
        """Open VideoCapture and negotiate resolution/FPS."""
        device = self.config.device
        logger.info(f"Opening webcam device: {device} (requested: {self.config.requested_width}x{self.config.requested_height} @ {self.config.requested_fps}fps)")
        
        if isinstance(device, str) and device.isdigit():
            device_idx = int(device)
        else:
            device_idx = device

        self._cap = cv2.VideoCapture(device_idx)
        if not self._cap.isOpened():
            logger.error(f"Failed to open video capture device '{device}'.")
            return False

        # Request properties
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.requested_width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.requested_height)
        self._cap.set(cv2.CAP_PROP_FPS, self.config.requested_fps)

        # Query negotiated properties
        self.actual_width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.actual_fps = float(self._cap.get(cv2.CAP_PROP_FPS)) or float(self.config.requested_fps)

        logger.info(
            f"Webcam negotiated: {self.actual_width}x{self.actual_height} @ {self.actual_fps:.1f} FPS "
            f"(Requested: {self.config.requested_width}x{self.config.requested_height} @ {self.config.requested_fps} FPS)"
        )
        return True

    def _capture_loop(self) -> None:
        """Continuous frame grab loop."""
        fps_start_time = time.time()
        fps_frame_count = 0

        while self._running and self._cap is not None:
            grabbed, frame = self._cap.read()
            now = time.time()

            if not grabbed or frame is None:
                logger.warning("Webcam frame grab failed or empty frame received.")
                time.sleep(0.01)
                continue

            self._frame_count += 1
            fps_frame_count += 1

            # Compute measured FPS every second
            elapsed = now - fps_start_time
            if elapsed >= 1.0:
                self.measured_fps = fps_frame_count / elapsed
                fps_frame_count = 0
                fps_start_time = now

            frame_data = FrameData(
                frame_index=self._frame_count,
                timestamp=now,
                image=frame,
                width=frame.shape[1],
                height=frame.shape[0],
                capture_fps=self.measured_fps or self.actual_fps,
            )
            self.buffer.put(frame_data)

    def stop(self) -> None:
        """Stop capture loop and release resources."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

        if self._cap is not None:
            self._cap.release()
            self._cap = None
        logger.info("Webcam capture stopped and camera released.")

    def inject_synthetic_frame(self, image: np.ndarray) -> FrameData:
        """Helper for testing: injects a frame into buffer directly."""
        self._frame_count += 1
        now = time.time()
        frame_data = FrameData(
            frame_index=self._frame_count,
            timestamp=now,
            image=image,
            width=image.shape[1],
            height=image.shape[0],
            capture_fps=30.0,
        )
        self.buffer.put(frame_data)
        return frame_data
