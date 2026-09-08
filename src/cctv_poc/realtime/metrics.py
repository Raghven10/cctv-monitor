"""Performance metrics tracking for real-time video processing."""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PerformanceMetrics:
    """Snapshot of real-time latency and throughput metrics."""
    capture_fps: float = 0.0
    processing_fps: float = 0.0
    effective_fps: float = 0.0
    queue_depth: int = 0
    frame_age_ms: float = 0.0
    processing_latency_ms: float = 0.0
    end_to_end_latency_ms: float = 0.0
    layout_detection_latency_ms: float = 0.0
    ocr_latency_ms: float = 0.0
    vlm_latency_ms: float = 0.0


class PipelineMetricsTracker:
    """Calculates rolling FPS and latency stats across pipeline stages."""

    def __init__(self, window_size: int = 30):
        self.window_size = window_size
        self._proc_timestamps: deque = deque(maxlen=window_size)
        self._processing_latencies: deque = deque(maxlen=window_size)
        self._end_to_end_latencies: deque = deque(maxlen=window_size)
        self._frame_ages: deque = deque(maxlen=window_size)
        
        self._layout_latencies: deque = deque(maxlen=window_size)
        self._ocr_latencies: deque = deque(maxlen=window_size)
        self._vlm_latencies: deque = deque(maxlen=window_size)

        self._last_capture_fps: float = 0.0
        self._last_queue_depth: int = 0

    def record_frame_processed(
        self,
        frame_timestamp: float,
        proc_start_time: float,
        proc_end_time: float,
        capture_fps: float = 0.0,
        queue_depth: int = 0,
        layout_ms: float = 0.0,
        ocr_ms: float = 0.0,
        vlm_ms: float = 0.0,
    ) -> None:
        """Record timing data for a processed frame."""
        now = proc_end_time
        proc_lat_ms = (now - proc_start_time) * 1000.0
        e2e_lat_ms = (now - frame_timestamp) * 1000.0
        frame_age_ms = (proc_start_time - frame_timestamp) * 1000.0

        self._proc_timestamps.append(now)
        self._processing_latencies.append(proc_lat_ms)
        self._end_to_end_latencies.append(e2e_lat_ms)
        self._frame_ages.append(frame_age_ms)

        if layout_ms > 0:
            self._layout_latencies.append(layout_ms)
        if ocr_ms > 0:
            self._ocr_latencies.append(ocr_ms)
        if vlm_ms > 0:
            self._vlm_latencies.append(vlm_ms)

        self._last_capture_fps = capture_fps
        self._last_queue_depth = queue_depth

    def get_metrics(self) -> PerformanceMetrics:
        """Compute rolling average metrics snapshot."""
        # Calculate processing FPS
        proc_fps = 0.0
        if len(self._proc_timestamps) >= 2:
            duration = self._proc_timestamps[-1] - self._proc_timestamps[0]
            if duration > 0:
                proc_fps = (len(self._proc_timestamps) - 1) / duration

        avg_proc_lat = float(sum(self._processing_latencies) / len(self._processing_latencies)) if self._processing_latencies else 0.0
        avg_e2e_lat = float(sum(self._end_to_end_latencies) / len(self._end_to_end_latencies)) if self._end_to_end_latencies else 0.0
        avg_frame_age = float(sum(self._frame_ages) / len(self._frame_ages)) if self._frame_ages else 0.0

        avg_layout_ms = float(sum(self._layout_latencies) / len(self._layout_latencies)) if self._layout_latencies else 0.0
        avg_ocr_ms = float(sum(self._ocr_latencies) / len(self._ocr_latencies)) if self._ocr_latencies else 0.0
        avg_vlm_ms = float(sum(self._vlm_latencies) / len(self._vlm_latencies)) if self._vlm_latencies else 0.0

        effective_fps = min(self._last_capture_fps or 30.0, proc_fps or 30.0)

        return PerformanceMetrics(
            capture_fps=round(self._last_capture_fps, 1),
            processing_fps=round(proc_fps, 1),
            effective_fps=round(effective_fps, 1),
            queue_depth=self._last_queue_depth,
            frame_age_ms=round(avg_frame_age, 1),
            processing_latency_ms=round(avg_proc_lat, 1),
            end_to_end_latency_ms=round(avg_e2e_lat, 1),
            layout_detection_latency_ms=round(avg_layout_ms, 1),
            ocr_latency_ms=round(avg_ocr_ms, 1),
            vlm_latency_ms=round(avg_vlm_ms, 1),
        )
