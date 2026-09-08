"""Real-time hot path and asynchronous AI worker pipeline."""

import concurrent.futures
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import numpy as np

from ..config import POCConfig
from ..video.source import FrameData, WebcamSource
from ..monitor.display_detector import DisplayDetectionResult, DisplayDetector
from ..monitor.perspective import PerspectiveCorrector
from ..monitor.layout_discovery import DiscoveredLayout, LayoutDiscoverer
from ..monitor.pane_extractor import ExtractedPane, PaneExtractor
from ..extensions.interfaces import DetectionBox
from ..extensions.person_detector import FastLivePersonDetector
from ..extensions.audio_alert import global_audio_alerter
from ..extensions.event_logger import global_event_db
from ..extensions.intrusion_tracker import IntrusionChangeTracker
from ..ocr.label_reader import LabelReader, OCRResult
from ..vlm.label_validator import VisionLabelRecognizer, VLMResult, create_vlm_recognizer
from ..camera.identity_resolver import IdentityResolver, ResolvedPaneIdentity
from ..stabilization.temporal import CameraStabilizer, LayoutStabilizer, StablePaneState
from ..utils.jsonl_writer import JsonlWriter
from ..utils.logging import setup_logger
from .metrics import PerformanceMetrics, PipelineMetricsTracker

logger = setup_logger("cctv_poc.pipeline")


@dataclass
class ProcessedFrameResult:
    """Full outcome of processing a single live frame."""
    frame_index: int
    timestamp: float
    raw_image: np.ndarray
    rectified_image: Optional[np.ndarray]
    display_result: DisplayDetectionResult
    corrector: Optional[PerspectiveCorrector]
    layout: DiscoveredLayout
    is_layout_changed: bool
    panes: List[ExtractedPane]
    pane_identities: Dict[str, ResolvedPaneIdentity]
    stable_states: Dict[str, StablePaneState]
    metrics: PerformanceMetrics
    alerts: List[str] = None
    mode: str = "CCTV_WALL_MONITOR"  # "CCTV_WALL_MONITOR" or "DIRECT_ROOM_SURVEILLANCE"
    detected_persons: List[DetectionBox] = None
    should_announce_audio: bool = False
    intrusion_event_reason: str = ""


class RealtimePipeline:
    """Coordinates real-time hot path and asynchronous AI workers."""

    def __init__(self, config: POCConfig):
        self.config = config
        
        # Subsystems
        self.video_source = WebcamSource(config.video)
        self.display_detector = DisplayDetector(config.display)
        self.layout_discoverer = LayoutDiscoverer(config.layout)
        self.pane_extractor = PaneExtractor()
        self.label_reader = LabelReader(config.ocr)
        self.vlm_recognizer = create_vlm_recognizer(config.vlm)
        self.identity_resolver = IdentityResolver()
        self.layout_stabilizer = LayoutStabilizer(config.stabilization)
        self.camera_stabilizer = CameraStabilizer(config.stabilization)
        self.person_detector = FastLivePersonDetector()
        self.intrusion_tracker = IntrusionChangeTracker()
        self.metrics_tracker = PipelineMetricsTracker()
        self.jsonl_writer = JsonlWriter(config.output.jsonl)

        # Async AI workers
        self._async_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="AsyncAIWorker")
        self._pending_tasks: Dict[str, concurrent.futures.Future] = {}
        self._state_lock = threading.Lock()
        
        # Identity cache
        self._resolved_identities: Dict[str, ResolvedPaneIdentity] = {}
        self._last_vlm_time = 0.0
        self._last_ocr_time_per_pane: Dict[str, float] = {}

    def start(self, auto_open_camera: bool = True) -> bool:
        """Start video acquisition thread."""
        return self.video_source.start(auto_open=auto_open_camera)

    def stop(self) -> None:
        """Stop video acquisition and async workers."""
        self.video_source.stop()
        self._async_executor.shutdown(wait=False, cancel_futures=True)
        self.jsonl_writer.close()
        logger.info("Realtime pipeline stopped cleanly.")

    def process_latest(self) -> Optional[ProcessedFrameResult]:
        """Fetch latest frame from buffer and process through hot path."""
        frame_data = self.video_source.buffer.pop_latest(timeout=0.1)
        if frame_data is None:
            return None

        return self.process_frame_data(frame_data)

    def process_frame_data(self, frame_data: FrameData, view_mode: Optional[str] = None) -> ProcessedFrameResult:
        """
        Execute real-time hot path on a given frame.
        Guaranteed never to block for VLM or OCR.
        """
        proc_start = time.time()
        raw_image = frame_data.image
        
        # 1. Physical Monitor Detection
        display_res = self.display_detector.detect(raw_image)
        h, w = raw_image.shape[:2]

        # 2. Rectification & Dynamic Layout Discovery
        if display_res.is_detected:
            rectifier_corners = display_res.corners
        else:
            rectifier_corners = np.array(
                [[0.0, 0.0], [float(w - 1), 0.0], [float(w - 1), float(h - 1)], [0.0, float(h - 1)]],
                dtype=np.float32,
            )

        corrector = PerspectiveCorrector(
            rectifier_corners,
            target_width=self.config.display.target_width,
            target_height=self.config.display.target_height,
        )
        rectified_image = corrector.rectify(raw_image)

        t_layout_start = time.time()
        detected_layout = self.layout_discoverer.discover(rectified_image)
        layout_lat_ms = (time.time() - t_layout_start) * 1000.0

        # Mode determination:
        # 1. If explicit view_mode is set to CCTV_ONLY, force CCTV mode.
        # 2. If explicit view_mode is set to ROOM_ONLY, force Room Surveillance.
        # 3. If AUTO / None: active if display bezel detected OR multi-pane grid (pane_count >= 2).
        effective_view_mode = (view_mode or "AUTO").upper()
        if effective_view_mode == "CCTV_ONLY":
            is_cctv_monitor_active = True
        elif effective_view_mode == "ROOM_ONLY":
            is_cctv_monitor_active = False
        else:
            is_cctv_monitor_active = display_res.is_detected or (detected_layout.pane_count >= 2)

        alerts: List[str] = []
        detected_persons: List[DetectionBox] = []
        should_announce: bool = False
        event_reason: str = ""

        if not is_cctv_monitor_active:
            # Mode A: DIRECT LIVE ROOM SURVEILLANCE
            pipeline_mode = "DIRECT_ROOM_SURVEILLANCE"
            corrector = None
            rectified_image = None
            active_layout = DiscoveredLayout(
                layout_id="live-room-monitoring",
                pane_count=1,
                confidence=0.90,
                panes=[],
            )
            is_layout_changed = False
            extracted_panes = []

            # Run Real-Time Person / Face Detection & Identification on the live frame
            detected_persons = self.person_detector.detect_persons(raw_image, pane_id="LIVE_ROOM_CAM")
            unknown_persons = [p for p in detected_persons if not p.is_known]
            known_persons = [p for p in detected_persons if p.is_known]

            should_announce, event_reason = self.intrusion_tracker.evaluate_intrusion_event(unknown_persons)

            if should_announce:
                # Trigger host system buzzer tone + spoken voice announcement ONCE on intrusion or significant change
                global_audio_alerter.trigger_unknown_person_alarm("Unknown person detected!")
                alerts.append(f"INTRUSION_ALERT ({event_reason}): Unknown person detected ({len(unknown_persons)} intruder(s))")

            if unknown_persons:
                for p in unknown_persons:
                    bx, by, bw, bh = p.bbox
                    alerts.append(f"INTRUSION_ALERT: Unknown person at [{bx},{by},{bw},{bh}] (Conf: {p.confidence:.2f})")

                # Save unusual intruder frame to SQLite DB with activity log
                global_event_db.log_unusual_frame(
                    event_type="UNKNOWN_PERSON_INTRUSION",
                    severity="CRITICAL",
                    description=f"Unknown person ({len(unknown_persons)} intruder(s)) detected in Live Room Surveillance",
                    frame_image=raw_image,
                    mode="DIRECT_ROOM_SURVEILLANCE",
                    pane_id="LIVE_ROOM_CAM",
                    bounding_boxes=[asdict(p) for p in unknown_persons],
                    debounce_seconds=2.5,
                )

            # Note: Authorized/Identified persons are tracked in detected_persons and rendered GREEN,
            # with ZERO alerts or alarms triggered.

            with self._state_lock:
                self.layout_stabilizer.reset()
                self.camera_stabilizer.reset()
                self._resolved_identities.clear()

        else:
            # Mode B: CCTV WALL MULTI-PANE MONITOR
            pipeline_mode = "CCTV_WALL_MONITOR"
            if not display_res.is_detected:
                display_res.is_detected = True
                display_res.confidence = max(display_res.confidence, detected_layout.confidence)

            # Temporal Layout Stabilization
            active_layout, is_layout_changed = self.layout_stabilizer.update(detected_layout)
            if is_layout_changed:
                with self._state_lock:
                    self.camera_stabilizer.reset()
                    self._resolved_identities.clear()

                if active_layout.pane_count > 1:
                    global_event_db.log_unusual_frame(
                        event_type="LAYOUT_CHANGED",
                        severity="INFO",
                        description=f"CCTV monitor layout changed to {active_layout.layout_id} ({active_layout.pane_count} panes)",
                        frame_image=raw_image,
                        mode="CCTV_WALL_MONITOR",
                        metadata={"pane_count": active_layout.pane_count, "layout_id": active_layout.layout_id},
                        debounce_seconds=4.0,
                    )

            # Extract Panes
            extracted_panes = self.pane_extractor.extract_panes(rectified_image, active_layout)

            # Detect persons/faces across CCTV feeds
            detected_persons = self.person_detector.detect_persons(raw_image, pane_id="CCTV_MONITOR")
            unknown_persons = [p for p in detected_persons if not p.is_known]
            if unknown_persons:
                should_announce, event_reason = self.intrusion_tracker.evaluate_intrusion_event(unknown_persons)
                if should_announce:
                    global_audio_alerter.trigger_unknown_person_alarm("Unknown person detected!")
                    alerts.append(f"INTRUSION_ALERT ({event_reason}): Unknown person detected ({len(unknown_persons)} intruder(s))")

            # Schedule Async AI (OCR / VLM)
            self._schedule_async_ai_tasks(extracted_panes, frame_data.frame_index, is_layout_changed)

        # 7. Collect Stabilized Camera Identities
        pane_identities: Dict[str, ResolvedPaneIdentity] = {}
        stable_states: Dict[str, StablePaneState] = {}

        with self._state_lock:
            for pane in extracted_panes:
                pid = pane.pane_id
                ident = self._resolved_identities.get(
                    pid,
                    ResolvedPaneIdentity(
                        pane_id=pid,
                        raw_text="",
                        normalized_text="",
                        ocr_confidence=0.0,
                        vlm_confidence=0.0,
                        association_confidence=0.98,
                        final_label="UNKNOWN",
                        confidence=0.0,
                        is_unknown=True,
                    ),
                )
                pane_identities[pid] = ident
                stable_state = self.camera_stabilizer.update_observation(ident)
                stable_states[pid] = stable_state

        proc_end = time.time()

        if display_res.is_detected and (active_layout.pane_count == 0 or len(extracted_panes) == 0):
            alerts.append("NO_PANES_DETECTED: No active CCTV feeds/panes on display")
            global_event_db.log_unusual_frame(
                event_type="NO_PANES_DETECTED",
                severity="WARNING",
                description="CCTV Screen detected but no active video feeds found",
                frame_image=raw_image,
                mode=pipeline_mode,
                debounce_seconds=10.0,
            )

        # 8. Record Metrics
        self.metrics_tracker.record_frame_processed(
            frame_timestamp=frame_data.timestamp,
            proc_start_time=proc_start,
            proc_end_time=proc_end,
            capture_fps=frame_data.capture_fps,
            queue_depth=self.video_source.buffer.queue_depth,
            layout_ms=layout_lat_ms if display_res.is_detected else 0.0,
        )
        metrics = self.metrics_tracker.get_metrics()

        # 9. Build and Write Structured JSONL Record
        self._write_jsonl_record(frame_data, display_res, active_layout, extracted_panes, pane_identities, stable_states, metrics, alerts)

        return ProcessedFrameResult(
            frame_index=frame_data.frame_index,
            timestamp=frame_data.timestamp,
            raw_image=raw_image,
            rectified_image=rectified_image,
            display_result=display_res,
            corrector=corrector,
            layout=active_layout,
            is_layout_changed=is_layout_changed,
            panes=extracted_panes,
            pane_identities=pane_identities,
            stable_states=stable_states,
            metrics=metrics,
            alerts=alerts,
            mode=pipeline_mode,
            detected_persons=detected_persons,
            should_announce_audio=should_announce if pipeline_mode == "DIRECT_ROOM_SURVEILLANCE" else False,
            intrusion_event_reason=event_reason if pipeline_mode == "DIRECT_ROOM_SURVEILLANCE" else "",
        )

    def _schedule_async_ai_tasks(
        self, panes: List[ExtractedPane], frame_index: int, is_layout_changed: bool
    ) -> None:
        """Schedule non-blocking OCR / VLM tasks on background worker pool."""
        now = time.time()
        
        # Check if periodic OCR is triggered for any pane
        should_run_ocr = (frame_index % self.config.ocr.sample_every_n_frames == 0) or is_layout_changed
        
        # Check if VLM should be triggered
        vlm_due = (now - self._last_vlm_time) >= self.config.vlm.sample_interval_seconds
        should_run_vlm = (
            self.config.vlm.enabled
            and (vlm_due or (is_layout_changed and self.config.vlm.trigger_on_layout_change))
        )

        if not (should_run_ocr or should_run_vlm):
            return

        for pane in panes:
            pid = pane.pane_id
            
            # Check if this pane already has a running async task
            if pid in self._pending_tasks and not self._pending_tasks[pid].done():
                continue

            pane_img_copy = pane.image.copy()
            future = self._async_executor.submit(
                self._async_ai_worker_fn,
                pid,
                pane_img_copy,
                should_run_ocr,
                should_run_vlm,
                now,
            )
            self._pending_tasks[pid] = future
            future.add_done_callback(lambda fut, p=pid: self._on_ai_worker_done(p, fut))

        if should_run_vlm:
            self._last_vlm_time = now

    def _async_ai_worker_fn(
        self,
        pane_id: str,
        pane_img: np.ndarray,
        run_ocr: bool,
        run_vlm: bool,
        scheduled_time: float,
    ) -> ResolvedPaneIdentity:
        """Background worker executing OCR and/or VLM."""
        ocr_res: Optional[OCRResult] = None
        vlm_res: Optional[VLMResult] = None

        if run_ocr:
            ocr_res = self.label_reader.read_label(pane_img)

        # Trigger VLM if OCR confidence is low or periodic
        need_vlm = run_vlm
        if not need_vlm and self.config.vlm.trigger_on_low_ocr_confidence:
            if ocr_res is None or ocr_res.confidence < self.config.ocr.confidence_threshold:
                need_vlm = True

        if need_vlm:
            try:
                vlm_res = self.vlm_recognizer.identify_label(pane_img, pane_id=pane_id)
            except Exception as e:
                logger.warning(f"Async VLM execution failed for {pane_id}: {e}")

        # Resolve candidate identity
        return self.identity_resolver.resolve_pane_identity(
            pane_id=pane_id,
            ocr_result=ocr_res,
            vlm_result=vlm_res,
            association_confidence=0.98,
        )

    def _on_ai_worker_done(self, pane_id: str, future: concurrent.futures.Future) -> None:
        """Callback when async AI worker finishes."""
        try:
            resolved = future.result()
            with self._state_lock:
                self._resolved_identities[pane_id] = resolved
        except concurrent.futures.CancelledError:
            # Task cancelled during shutdown
            pass
        except Exception as e:
            logger.error(f"Error in async AI task for {pane_id}: {e}")

    def _write_jsonl_record(
        self,
        frame_data: FrameData,
        display_res: DisplayDetectionResult,
        layout: DiscoveredLayout,
        panes: List[ExtractedPane],
        pane_identities: Dict[str, ResolvedPaneIdentity],
        stable_states: Dict[str, StablePaneState],
        metrics: PerformanceMetrics,
        alerts: Optional[List[str]] = None,
    ) -> None:
        """Format and write structured diagnostic JSONL record."""
        iso_ts = datetime.fromtimestamp(frame_data.timestamp, tz=timezone.utc).isoformat()
        
        pane_records = []
        for pane in panes:
            pid = pane.pane_id
            ident = pane_identities.get(pid)
            state = stable_states.get(pid)
            
            pane_records.append({
                "pane_id": pid,
                "bbox": list(pane.bbox),
                "geometry_confidence": round(pane.geometry_confidence, 2),
                "label": {
                    "raw_text": ident.raw_text if ident else "",
                    "normalized_text": ident.normalized_text if ident else "",
                    "ocr_confidence": round(ident.ocr_confidence, 2) if ident else 0.0,
                    "vlm_confidence": round(ident.vlm_confidence, 2) if ident else 0.0,
                    "association_confidence": round(ident.association_confidence, 2) if ident else 0.0,
                    "stable_label": state.stable_label if state else "UNKNOWN",
                    "stable": state.is_stable if state else False,
                }
            })

        record = {
            "schema_version": "1.0",
            "timestamp": iso_ts,
            "frame_index": frame_data.frame_index,
            "capture": {
                "width": frame_data.width,
                "height": frame_data.height,
                "fps": metrics.capture_fps,
            },
            "display": {
                "bbox": list(display_res.bbox),
                "confidence": round(display_res.confidence, 2),
            },
            "layout": {
                "layout_id": layout.layout_id,
                "pane_count": layout.pane_count,
                "confidence": round(layout.confidence, 2),
                "stable": not self.layout_stabilizer.is_candidate_pending,
            },
            "alerts": alerts or [],
            "panes": pane_records,
            "performance": {
                "processing_fps": metrics.processing_fps,
                "frame_age_ms": metrics.frame_age_ms,
                "end_to_end_latency_ms": metrics.end_to_end_latency_ms,
            }
        }
        self.jsonl_writer.write_record(record)
