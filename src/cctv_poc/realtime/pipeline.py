"""Real-time hot path and asynchronous AI worker pipeline."""

import concurrent.futures
import threading
import time
import cv2
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

from ..config import POCConfig
from ..video.source import FrameData, WebcamSource
from ..monitor.display_detector import DisplayDetectionResult, DisplayDetector
from ..monitor.perspective import PerspectiveCorrector
from ..monitor.layout_discovery import DiscoveredLayout, LayoutDiscoverer
from ..monitor.manual_layout import ManualGridConfig, ManualLayoutStore
from ..monitor.pane_extractor import ExtractedPane, PaneExtractor
from ..monitor.zones import Zone
from ..extensions.interfaces import DetectionBox
from ..extensions.person_detector import FastLivePersonDetector, YoloV11PersonDetector
from ..monitor.event_engine import EventSeverity, PaneEvent, PaneEventEngine

from ..extensions.audio_alert import global_audio_alerter
from ..extensions.event_logger import global_event_db
from ..extensions.intrusion_tracker import IntrusionChangeTracker
from ..extensions.pane_activity_tracker import global_pane_activity_tracker
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
    events: List[Any] = None
    mode: str = "CCTV_WALL_MONITOR"  # "CCTV_WALL_MONITOR" or "DIRECT_ROOM_SURVEILLANCE"
    detected_persons: List[DetectionBox] = None
    should_announce_audio: bool = False
    intrusion_event_reason: str = ""
    pane_states: Optional[Dict[str, Dict[str, Any]]] = None
    is_finalized: bool = False


class RealtimePipeline:
    """Coordinates real-time hot path and asynchronous AI workers."""

    def __init__(self, config: POCConfig):
        self.config = config
        self.frame_count = 0 # Track frames for interval-based detection

        # Subsystems
        self.video_source = WebcamSource(config.video)
        self.display_detector = DisplayDetector(config.display)
        self.layout_discoverer = LayoutDiscoverer(config.layout)
        manual_file = getattr(config.layout, "manual_layout_file", "config/manual_layout.json")
        self.manual_layout_store = ManualLayoutStore(config_file=manual_file)
        self.pane_extractor = PaneExtractor()
        self.label_reader = LabelReader(config.ocr)
        self.vlm_recognizer = create_vlm_recognizer(config.vlm)
        self.identity_resolver = IdentityResolver()
        self.layout_stabilizer = LayoutStabilizer(config.stabilization)
        self.camera_stabilizer = CameraStabilizer(config.stabilization)
        self.person_detector = YoloV11PersonDetector(
            score_threshold=getattr(config.detection, "score_threshold", 0.35),
            nms_threshold=getattr(config.detection, "nms_threshold", 0.40),
        )
        self.intrusion_tracker = IntrusionChangeTracker()

        self.event_engine = PaneEventEngine()
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

        # Grid & Display Detection Cache (throttles heavy layout discovery to ~1.8s intervals)
        self._cached_display_res: Optional[DisplayDetectionResult] = None
        self._cached_corrector: Optional[PerspectiveCorrector] = None
        self._cached_layout: Optional[DiscoveredLayout] = None
        self._last_layout_calc_time: float = 0.0
        self._last_layout_view_mode: str = ""

    def start(self, auto_open_camera: bool = True) -> bool:
        """Start video acquisition thread."""
        return self.video_source.start(auto_open=auto_open_camera)

    def rescan_layout(self) -> None:
        """Force an immediate fresh layout and display bezel discovery on the next frame."""
        with self._state_lock:
            self._cached_layout = None
            self._cached_display_res = None
            self._cached_corrector = None
            self._last_layout_calc_time = 0.0
            self.layout_stabilizer.reset()
            self.camera_stabilizer.reset()
            self._resolved_identities.clear()
        logger.info("🔄 Pipeline layout cache invalidated. Rescanning grid layout immediately on next frame.")

    def set_manual_preset(self, preset: str) -> ManualGridConfig:
        """Apply a named layout preset or reset to auto."""
        cfg = self.manual_layout_store.set_preset(preset)
        self.rescan_layout()
        return cfg

    def set_manual_custom_grid(
        self,
        rows: int,
        cols: int,
        x_dividers: Optional[List[float]] = None,
        y_dividers: Optional[List[float]] = None,
        preset: str = "CUSTOM",
    ) -> ManualGridConfig:
        """Set user-customized grid dividers and dimensions."""
        cfg = self.manual_layout_store.set_custom_grid(
            rows=rows,
            cols=cols,
            x_dividers=x_dividers,
            y_dividers=y_dividers,
            preset=preset,
        )
        self.rescan_layout()
        return cfg

    def reset_manual_layout(self) -> ManualGridConfig:
        """Revert back to automatic dynamic grid discovery."""
        cfg = self.manual_layout_store.reset_to_auto()
        self.rescan_layout()
        return cfg

    def finalize_grid(
        self,
        rows: Optional[int] = None,
        cols: Optional[int] = None,
        x_dividers: Optional[List[float]] = None,
        y_dividers: Optional[List[float]] = None,
        preset: Optional[str] = None,
        pane_labels: Optional[Dict[str, str]] = None,
        panes_metadata: Optional[List[Dict[str, Any]]] = None,
        mesh: Optional[List[List[List[float]]]] = None,
    ) -> ManualGridConfig:
        """Freeze grid layout configuration, persist normalized pane coordinates, and stop continuous detection."""
        cfg = self.manual_layout_store.finalize_grid(
            rows=rows,
            cols=cols,
            x_dividers=x_dividers,
            y_dividers=y_dividers,
            preset=preset,
            pane_labels=pane_labels,
            panes_metadata=panes_metadata,
            mesh=mesh,
        )
        self.rescan_layout()
        return cfg

    def get_manual_layout(self) -> ManualGridConfig:
        """Get active manual layout settings."""
        return self.manual_layout_store.config

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

    def process_frame_data(
        self,
        frame_data: FrameData,
        view_mode: Optional[str] = None,
        grid_preset: Optional[str] = None,
    ) -> ProcessedFrameResult:
        """
        Execute real-time hot path on a given frame.
        Guaranteed never to block for VLM or OCR.
        """
        proc_start = time.time()
        raw_image = frame_data.image
        h, w = raw_image.shape[:2]
        effective_view_mode = (view_mode or "AUTO").upper()
        now = proc_start

        # Layout & Bezel Discovery
        is_manual_active = self.manual_layout_store.is_active
        is_finalized = self.manual_layout_store.config.is_finalized

        if is_manual_active:
            # User defined manual grid, preset, or frozen finalized layout
            if is_finalized and self._cached_corrector is not None and self._cached_display_res is not None:
                display_res = self._cached_display_res
                corrector = self._cached_corrector
            else:
                display_res = self.display_detector.detect(raw_image)
                if display_res and display_res.is_detected:
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
                self._cached_display_res = display_res
                self._cached_corrector = corrector

            rectified_image = corrector.rectify(raw_image)
            detected_layout = self.manual_layout_store.generate_discovered_layout(
                self.config.display.target_width,
                self.config.display.target_height,
            )
            layout_lat_ms = 0.0
            self._cached_layout = detected_layout
            self._last_layout_calc_time = now
            self._last_layout_view_mode = effective_view_mode
        else:
            # Throttled Dynamic Layout & Bezel Discovery (~every 1.8 seconds or on mode change)
            layout_due = (
                self._cached_layout is None
                or (now - self._last_layout_calc_time) >= 1.8
                or effective_view_mode != self._last_layout_view_mode
            )

            if layout_due:
                t_layout_start = time.time()
                display_res = self.display_detector.detect(raw_image)

                if display_res and display_res.is_detected:
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

                detected_layout = self.layout_discoverer.discover(
                    rectified_image,
                    force_cctv_grid=(effective_view_mode == "CCTV_ONLY"),
                )
                layout_lat_ms = (time.time() - t_layout_start) * 1000.0

                self._cached_display_res = display_res
                self._cached_corrector = corrector
                self._cached_layout = detected_layout
                self._last_layout_calc_time = now
                self._last_layout_view_mode = effective_view_mode
            else:
                display_res = self._cached_display_res
                corrector = self._cached_corrector
                detected_layout = self._cached_layout
                rectified_image = corrector.rectify(raw_image) if corrector is not None else raw_image.copy()
                layout_lat_ms = 0.0

        # Mode determination:
        # 1. If explicit view_mode is set to CCTV_ONLY, force CCTV mode.
        # 2. If explicit view_mode is set to ROOM_ONLY, force Room Surveillance.
        # 3. If manual layout active, force CCTV mode.
        # 4. If AUTO: active if display bezel detected OR multi-pane grid (pane_count >= 2).
        if effective_view_mode == "CCTV_ONLY" or is_manual_active:
            is_cctv_monitor_active = True
        elif effective_view_mode == "ROOM_ONLY":
            is_cctv_monitor_active = False
        else:
            is_cctv_monitor_active = (display_res.is_detected if display_res else False) or (detected_layout.pane_count >= 2)


        alerts: List[str] = []
        detected_persons: List[DetectionBox] = []
        should_announce: bool = False
        event_reason: str = ""
        frame_events: List[Any] = []
        pane_states: Dict[str, Dict[str, Any]] = {}
        active_layout: Optional[DiscoveredLayout] = None
        is_layout_changed: bool = False
        extracted_panes: List[ExtractedPane] = []

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

            # Evaluate defined zones on live room surveillance feed
            raw_zones = getattr(self.manual_layout_store.config, "zones", {})
            room_zones_data = []
            if isinstance(raw_zones, dict):
                if "LIVE_ROOM_CAM" in raw_zones:
                    room_zones_data = raw_zones["LIVE_ROOM_CAM"]
                elif "Pane-01" in raw_zones:
                    room_zones_data = raw_zones["Pane-01"]
                elif "P01" in raw_zones:
                    room_zones_data = raw_zones["P01"]
                else:
                    for z_list in raw_zones.values():
                        if isinstance(z_list, list):
                            room_zones_data.extend(z_list)
            elif isinstance(raw_zones, list):
                room_zones_data = raw_zones

            room_zones = [
                Zone(**z) if isinstance(z, dict) else z
                for z in room_zones_data
                if isinstance(z, (dict, Zone))
            ]

            if room_zones:
                ri_h, ri_w = raw_image.shape[:2]
                pane_events = self.event_engine.process_pane(
                    pane_id="LIVE_ROOM_CAM",
                    pane_label="Room Surveillance",
                    zones=room_zones,
                    detected_persons=detected_persons,
                    pane_image=raw_image,
                    img_w=ri_w,
                    img_h=ri_h,
                    now=now,
                )
                frame_events.extend(pane_events)

                for event in pane_events:
                    if event.is_alarm or event.severity in [EventSeverity.HIGH, EventSeverity.CRITICAL]:
                        alerts.append(f"{event.event_type}: {event.description}")
                        global_audio_alerter.trigger_unknown_person_alarm(event.description)
                        try:
                            self._async_executor.submit(
                                global_event_db.log_unusual_frame,
                                event_type=event.event_type,
                                severity=event.severity.value,
                                description=event.description,
                                frame_image=raw_image,
                                mode="DIRECT_ROOM_SURVEILLANCE",
                                pane_id="LIVE_ROOM_CAM",
                                debounce_seconds=3.0,
                            )
                        except Exception:
                            pass

            with self._state_lock:
                self.layout_stabilizer.reset()
                self.camera_stabilizer.reset()
                self._resolved_identities.clear()


        else:
            # Mode B: CCTV WALL MULTI-PANE MONITOR
            pipeline_mode = "CCTV_WALL_MONITOR"
            if display_res is None or not display_res.is_detected:
                if display_res is not None:
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
            # Detect persons/faces across CCTV feeds (global frame + high-resolution per-pane inspection)

            # Optimization: Downsample only ultra-high-res (e.g. 4K) frames for neural detection
            max_neural_dim = 1280
            cur_max_dim = max(w, h)
            if cur_max_dim > max_neural_dim:
                global_downsample_scale = float(max_neural_dim) / float(cur_max_dim)
                global_img_small = cv2.resize(raw_image, None, fx=global_downsample_scale, fy=global_downsample_scale, interpolation=cv2.INTER_LINEAR)
            else:
                global_downsample_scale = 1.0
                global_img_small = raw_image

            # Use a wrapper to scale boxes back to original size
            raw_detected = self.person_detector.detect_persons(global_img_small, pane_id="CCTV_MONITOR") or []
            detected_persons = []
            for p in raw_detected:
                if global_downsample_scale < 1.0:
                    bx, by, bw, bh = p.bbox
                    p.bbox = (int(bx / global_downsample_scale), int(by / global_downsample_scale),
                              int(bw / global_downsample_scale), int(bh / global_downsample_scale))
                detected_persons.append(p)

            def detect_in_pane(pane):
                pid = pane.pane_id
                pnx, pny, pnw, pnh = pane.normalized_bbox

                # Only run per-pane detection if this pane doesn't already have a person from global pass
                pane_has_person = any(
                    pnx <= float(pbox.bbox[0] + pbox.bbox[2] / 2.0) / float(w) <= (pnx + pnw) and
                    pny <= float(pbox.bbox[1] + pbox.bbox[3] / 2.0) / float(h) <= (pny + pnh)
                    for pbox in detected_persons
                )

                if not pane_has_person and pane.image is not None and pane.image.shape[0] >= 100 and pane.image.shape[1] >= 100:
                    pane_boxes = self.person_detector.detect_persons(pane.image, pane_id=pid) or []
                    pw_img, ph_img = pane.image.shape[1], pane.image.shape[0]
                    results = []
                    for pbox in pane_boxes:
                        plx, ply, plw, plh = pbox.bbox
                        norm_cx = pnx + (float(plx) / float(pw_img)) * pnw
                        norm_cy = pny + (float(ply) / float(ph_img)) * pnh
                        norm_cw = (float(plw) / float(pw_img)) * pnw
                        norm_ch = (float(plh) / float(ph_img)) * pnh
                        gx = int(norm_cx * w)
                        gy = int(norm_cy * h)
                        gw = max(1, int(norm_cw * w))
                        gh = max(1, int(norm_ch * h))
                        pbox.bbox = (gx, gy, gw, gh)
                        pbox.pane_id = pid
                        results.append(pbox)
                    return results
                return []

            # Parallelize per-pane detection using the existing async executor
            if self.frame_count % self.config.detection.detection_interval == 0:
                pane_futures = [self._async_executor.submit(detect_in_pane, pane) for pane in extracted_panes]
                for fut in concurrent.futures.as_completed(pane_futures):
                    res = fut.result()
                    if res:
                        detected_persons.extend(res)

            # Suppress overlapping / duplicate person bounding boxes across global and per-pane passes
            if len(detected_persons) > 1:
                g_boxes = [[p.bbox[0], p.bbox[1], p.bbox[2], p.bbox[3]] for p in detected_persons]
                g_scores = [float(p.confidence) for p in detected_persons]
                g_indices = cv2.dnn.NMSBoxes(g_boxes, g_scores, score_threshold=0.60, nms_threshold=0.30)
                if len(g_indices) > 0:
                    g_indices = [int(i[0]) if isinstance(i, (list, np.ndarray)) else int(i) for i in g_indices]
                    detected_persons = [detected_persons[i] for i in g_indices]

            self.frame_count += 1

            # Map persons into their containing panes & run stateful activity tracker
            panes_with_persons: Dict[str, Tuple[str, List[DetectionBox]]] = {}
            for pane in extracted_panes:
                pid = pane.pane_id
                pnx, pny, pnw, pnh = pane.normalized_bbox

                ident = self._resolved_identities.get(pid)
                state = self.camera_stabilizer.stable_states.get(pid)
                p_label = (
                    state.stable_label if (state and state.stable_label != "UNKNOWN")
                    else (ident.final_label if (ident and not ident.is_unknown) else f"CAM-{pane.index+1:02d}")
                )

                in_pane: List[DetectionBox] = []
                for pbox in detected_persons:
                    bx, by, bw, bh = pbox.bbox
                    cx = float(bx + bw / 2.0) / float(w)
                    cy = float(by + bh / 2.0) / float(h)
                    if (pbox.pane_id == pid) or (pnx <= cx <= (pnx + pnw) and pny <= cy <= (pny + pnh)):
                        pbox.pane_id = pid
                        in_pane.append(pbox)

                panes_with_persons[pid] = (p_label, in_pane)

            # Update stateful activity & event tracker per pane
            pane_states = global_pane_activity_tracker.update_all(
                panes_with_persons,
                frame_image=rectified_image if rectified_image is not None else raw_image,
            )

            # Evaluate spatial-temporal event rules per pane
            for pane in extracted_panes:
                pid = pane.pane_id
                p_label, in_pane = panes_with_persons.get(pid, ("UNKNOWN", []))

                # Get zones for this pane from manual layout config with alias matching
                raw_zones = getattr(self.manual_layout_store.config, "zones", {})
                pane_zones_data = []
                if isinstance(raw_zones, dict):
                    possible_keys = [
                        pid,
                        f"Pane-{pane.index+1:02d}",
                        f"P{pane.index+1:02d}",
                        f"Pane-{pane.index+1}",
                        f"P{pane.index+1}",
                        pid.lower(),
                        pid.upper(),
                    ]
                    for k in possible_keys:
                        if k in raw_zones and raw_zones[k]:
                            pane_zones_data = raw_zones[k]
                            break
                elif isinstance(raw_zones, list):
                    possible_ids = {pid, f"Pane-{pane.index+1:02d}", f"P{pane.index+1:02d}", f"Pane-{pane.index+1}", f"P{pane.index+1}"}
                    pane_zones_data = [z for z in raw_zones if isinstance(z, dict) and z.get("pane_id") in possible_ids]

                zones = [
                    Zone(**z) if isinstance(z, dict) else z
                    for z in pane_zones_data
                    if isinstance(z, (dict, Zone))
                ]


                if zones:
                    pane_img = pane.image if pane.image is not None else raw_image
                    pi_h, pi_w = pane_img.shape[:2]
                    pane_events = self.event_engine.process_pane(
                        pane_id=pid,
                        pane_label=p_label,
                        zones=zones,
                        detected_persons=in_pane,
                        pane_image=pane_img,
                        img_w=pi_w,
                        img_h=pi_h,
                        now=now,
                    )
                    frame_events.extend(pane_events)

                    # Process activity events & alarms
                    for event in pane_events:
                        if event.is_alarm or event.severity in [EventSeverity.HIGH, EventSeverity.CRITICAL]:
                            alerts.append(f"{event.event_type}: {event.description}")
                            # Update pane state to alert active
                            if pid in pane_states:
                                pane_states[pid]["alert_status"] = "ALERT_ACTIVE"
                                pane_states[pid]["last_event"] = event.description
                                pane_states[pid]["last_event_time"] = time.strftime("%H:%M:%S", time.localtime(now))

                            # Trigger audio alert
                            global_audio_alerter.trigger_unknown_person_alarm(event.description)

                            # Log event to database
                            try:
                                self._async_executor.submit(
                                    global_event_db.log_unusual_frame,
                                    event_type=event.event_type,
                                    severity=event.severity.value if hasattr(event.severity, "value") else str(event.severity),
                                    description=event.description,
                                    frame_image=pane_img,
                                    mode=pipeline_mode,
                                    debounce_seconds=4.0,
                                )
                            except Exception:
                                pass

                        # VLM Verification for candidate events
                        if event.severity in [EventSeverity.HIGH, EventSeverity.CRITICAL] or "UNAUTHORIZED" in event.event_type:
                            zone_obj = next((z for z in zones if z.zone_id == event.zone_id), None)
                            self._schedule_event_verification(event, pane.image, zone_obj)


            # Alert evaluation per pane with intrusion gating
            for pid, pstate in pane_states.items():
                if pstate.get("alert_status") == "ALERT_ACTIVE":
                    unknowns = [p for p in panes_with_persons.get(pid, ("", []))[1] if not p.is_known]
                    if unknowns:
                        should_ann, event_reason = self.intrusion_tracker.evaluate_intrusion_event(unknowns)
                        if should_ann:
                            lbl = pstate.get("label", pid)
                            global_audio_alerter.trigger_unknown_person_alarm(f"Unknown person in {lbl}!")
                            alerts.append(f"INTRUSION_ALERT ({event_reason}): Unknown person in {pid} [{lbl}]")

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

        if pipeline_mode == "CCTV_WALL_MONITOR" and (not active_layout or active_layout.pane_count == 0 or len(extracted_panes) == 0):
            alerts.append("NO_PANES_DETECTED: Could not detect CCTV panes on screen")
            try:
                self._async_executor.submit(
                    global_event_db.log_unusual_frame,
                    event_type="NO_PANES_DETECTED",
                    severity="WARNING",
                    description="CCTV Screen detected but no active video feeds found",
                    frame_image=raw_image,
                    mode=pipeline_mode,
                    debounce_seconds=10.0,
                )
            except Exception:
                pass

        # 8. Record Metrics
        self.metrics_tracker.record_frame_processed(
            frame_timestamp=frame_data.timestamp,
            proc_start_time=proc_start,
            proc_end_time=proc_end,
            capture_fps=frame_data.capture_fps,
            queue_depth=self.video_source.buffer.queue_depth,
            layout_ms=layout_lat_ms if (display_res and display_res.is_detected) else 0.0,
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
            should_announce_audio=(should_announce if pipeline_mode == "DIRECT_ROOM_SURVEILLANCE" else (any(getattr(e, 'is_alarm', False) for e in frame_events) or any('ALARM' in a or 'INTRUSION' in a for a in alerts))),
            intrusion_event_reason=event_reason if pipeline_mode == "DIRECT_ROOM_SURVEILLANCE" else (frame_events[0].description if frame_events else ""),
            pane_states=pane_states if pipeline_mode == "CCTV_WALL_MONITOR" else {},
            events=frame_events,
            is_finalized=bool(self.manual_layout_store.config.is_finalized),
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

        # Schedule OCR for panes (very fast)
        vlm_scheduled = False

        for pane in panes:
            pid = pane.pane_id

            # Check if this pane already has a running async task
            if pid in self._pending_tasks and not self._pending_tasks[pid].done():
                continue

            # Only run VLM on at most 1 pane per interval cycle to prevent thread pool saturation
            pane_run_vlm = False
            if should_run_vlm and not vlm_scheduled:
                ident = self._resolved_identities.get(pid)
                if ident is None or ident.is_unknown or ident.confidence < 0.70:
                    pane_run_vlm = True
                    vlm_scheduled = True

            if not (should_run_ocr or pane_run_vlm):
                continue

            pane_img_copy = pane.image.copy()
            future = self._async_executor.submit(
                self._async_ai_worker_fn,
                pid,
                pane_img_copy,
                should_run_ocr,
                pane_run_vlm,
                now,
            )
            self._pending_tasks[pid] = future
            future.add_done_callback(lambda fut, p=pid: self._on_ai_worker_done(p, fut))

        if vlm_scheduled:
            self._last_vlm_time = now

    def _schedule_event_verification(self, event: Any, pane_image: np.ndarray, zone: Optional[Zone] = None) -> None:
        """Schedule an asynchronous VLM verification for a candidate event."""
        if not self.config.vlm.enabled:
            return

        # Create a crop of the area of interest (person + zone)
        # For simplicity, we use the pane image as the context, or a crop if bounding box is provided
        # Here we use the pane image for better semantic context
        img_copy = pane_image.copy()

        # Use a unique task ID for the event
        task_id = f"verify_{event.pane_id}_{event.track_id}_{event.timestamp}"

        future = self._async_executor.submit(
            self._async_event_verifier_fn,
            event,
            img_copy,
            zone,
        )
        self._pending_tasks[task_id] = future
        future.add_done_callback(lambda fut, e=event: self._on_event_verifier_done(e, fut))

    def _async_event_verifier_fn(self, event: Any, image: np.ndarray, zone: Optional[Zone]) -> Any:
        """Background worker for semantic event verification."""
        zone_label = zone.label if zone else "General Area"

        # Call VLM verify_event
        res = self.vlm_recognizer.verify_event(
            image=image,
            event_type=event.event_type,
            zone_label=zone_label,
            identity=event.identity
        )
        return res

    def _on_event_verifier_done(self, event: Any, future: concurrent.futures.Future) -> None:
        """Callback when event verification finishes."""
        try:
            res = future.result()
            if res.is_valid and res.is_verified:
                # Log the verified event as a high-confidence confirmed alert
                global_event_db.log_unusual_frame(
                    event_type=f"CONFIRMED_{event.event_type}",
                    severity="HIGH" if event.severity == "HIGH" else "INFO",
                    description=f"VLM Verified: {res.reason}",
                    frame_image=None, # We'd need the frame here, but let's assume the previous log has it
                    mode="CCTV_WALL_MONITOR",
                    metadata={
                        "pane_id": event.pane_id,
                        "track_id": event.track_id,
                        "vlm_confidence": res.confidence,
                        "vlm_reason": res.reason
                    },
                    debounce_seconds=5.0,
                )
                logger.info(f"✅ Event Verified: {event.event_type} in {event.pane_id} - {res.reason}")
            else:
                logger.info(f"❌ Event Rejected by VLM: {event.event_type} in {event.pane_id} - {res.reason if res.reason else 'False Positive'}")
        except Exception as e:
            logger.error(f"Error in event verifier callback: {e}")

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
                "bbox": list(display_res.bbox) if display_res else [0, 0, 0, 0],
                "confidence": round(display_res.confidence, 2) if display_res else 0.0,
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
