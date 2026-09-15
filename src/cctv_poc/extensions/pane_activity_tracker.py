"""Pane-Level State Machine, Real-Time Activity Tracking & Event De-duplication.

Maintains independent continuous state per CCTV pane:
1. Spatial Person & Multi-Person Tracking inside each pane boundary
2. Motion & Activity Analysis (Standing vs Walking / Moving)
3. Meaningful Event Generation (ENTERED, IDENTIFIED, ACTIVITY_CHANGED, EXITED)
4. Strict Event De-duplication (no frame spamming)
5. Unknown Person Single-Alert Gating with Cooldown
6. Continuous Structured Event Logging to DB
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

from .event_logger import global_event_db
from .interfaces import DetectionBox
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.pane_activity_tracker")


@dataclass
class PaneEventRecord:
    """Structured continuous event log record for an individual pane."""
    timestamp: str
    pane_id: str
    pane_label: str
    track_id: str
    person_id: str
    identity: str
    identity_confidence: float
    event_type: str        # PERSON_ENTERED, PERSON_IDENTIFIED, ACTIVITY_CHANGED, PERSON_EXITED, UNKNOWN_ALERT
    activity: str          # Standing, Walking, Sitting, Moving, Departed
    description: str       # Human-readable event description
    alert_status: str      # NO_ALERT, ALERT_ACTIVE, CLEARED
    frame_reference: str = ""


@dataclass
class TrackedPersonState:
    """State tracking for a person currently inside a specific pane."""
    track_id: str
    person_id: str
    name: str
    tag: str
    is_known: bool
    confidence: float
    bbox: Tuple[int, int, int, int]
    centroid: Tuple[float, float]
    first_seen: float
    last_seen: float
    activity: str = "Standing"
    last_activity_time: float = 0.0
    alert_raised: bool = False
    snapshot_base64: str = ""
    centroid_history: List[Tuple[float, float, float]] = field(default_factory=list)  # (x, y, t)


class PaneStateTracker:
    """Maintains state, tracking, activity detection, and event history for a single pane."""

    def __init__(self, pane_id: str, default_label: str = ""):
        self.pane_id = pane_id
        self.label = default_label or f"CAM-{pane_id[5:] if pane_id.startswith('Pane-') else pane_id}"
        self.state = "MONITORING"  # UNINITIALIZED, GRID_CONFIRMED, MONITORING, PERSON_DETECTED, ALERT_ACTIVE
        self._tracks: Dict[str, TrackedPersonState] = {}
        self._recent_events: List[PaneEventRecord] = []
        self._last_event_description: str = "Monitoring active"
        self._last_event_time: str = ""
        self._last_event_timestamp: float = time.time()
        self._lock = threading.Lock()

        # Motion analysis settings
        self.motion_speed_threshold: float = 0.018  # Normalized distance per second
        self.activity_debounce_seconds: float = 1.2

    def update(
        self,
        persons_in_pane: List[DetectionBox],
        pane_label: Optional[str] = None,
        frame_image: Optional[np.ndarray] = None,
        now: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], List[PaneEventRecord]]:
        """
        Process frame detections for this pane.
        Detects entries, identity resolutions, activity changes, and exits.
        Returns: (pane_status_dict, new_events_list)
        """
        now = now or time.time()
        if pane_label:
            self.label = pane_label

        new_events: List[PaneEventRecord] = []

        with self._lock:
            active_track_ids = set()

            for p in persons_in_pane:
                tid = p.person_id or f"trk_{p.bbox[0]}_{p.bbox[1]}"
                active_track_ids.add(tid)

                bx, by, bw, bh = p.bbox
                cx = float(bx + bw / 2.0)
                cy = float(by + bh / 2.0)
                is_known = bool(p.is_known)
                pname = p.person_name if is_known else "Unknown Person"

                if tid not in self._tracks:
                    # --- EVENT: PERSON ENTERED ---
                    activity = "Standing"
                    alert_status = "ALERT_ACTIVE" if not is_known else "NO_ALERT"
                    alert_raised = not is_known

                    if is_known:
                        desc = f"{pname} entered {self.label}."
                    else:
                        desc = f"Unknown person detected near {self.label}."

                    track_obj = TrackedPersonState(
                        track_id=tid,
                        person_id=p.person_id,
                        name=pname,
                        tag=p.tag or ("Staff" if is_known else "Intruder"),
                        is_known=is_known,
                        confidence=p.confidence,
                        bbox=p.bbox,
                        centroid=(cx, cy),
                        first_seen=now,
                        last_seen=now,
                        activity=activity,
                        last_activity_time=now,
                        alert_raised=alert_raised,
                        snapshot_base64=p.snapshot_base64 or "",
                        centroid_history=[(cx, cy, now)],
                    )
                    self._tracks[tid] = track_obj

                    evt = self._create_event(
                        event_type="PERSON_ENTERED",
                        activity=activity,
                        description=desc,
                        track_obj=track_obj,
                        alert_status=alert_status,
                        now=now,
                    )
                    new_events.append(evt)

                else:
                    # --- EXISTING TRACK UPDATE ---
                    trk = self._tracks[tid]
                    prev_is_known = trk.is_known
                    trk.last_seen = now
                    trk.bbox = p.bbox
                    if p.snapshot_base64:
                        trk.snapshot_base64 = p.snapshot_base64

                    # Check if identity got resolved from unknown to known
                    if not prev_is_known and is_known:
                        trk.is_known = True
                        trk.name = pname
                        trk.tag = p.tag or "Authorized"
                        trk.alert_raised = False
                        desc = f"Person in {self.label} identified as {pname}."
                        evt = self._create_event(
                            event_type="PERSON_IDENTIFIED",
                            activity=trk.activity,
                            description=desc,
                            track_obj=trk,
                            alert_status="NO_ALERT",
                            now=now,
                        )
                        new_events.append(evt)

                    # Update centroid history & estimate motion
                    trk.centroid_history.append((cx, cy, now))
                    if len(trk.centroid_history) > 30:
                        trk.centroid_history.pop(0)

                    # Determine motion / speed over rolling ~0.6s window
                    history_window = [pt for pt in trk.centroid_history if (now - pt[2]) <= 0.8]
                    if len(history_window) >= 2:
                        oldest = history_window[0]
                        dt = max(0.1, now - oldest[2])
                        dx = cx - oldest[0]
                        dy = cy - oldest[1]
                        dist_px = math.sqrt(dx * dx + dy * dy)
                        # Relative motion per second
                        speed = dist_px / dt

                        new_act = "Walking" if speed > 18.0 else "Standing"

                        # Debounce activity changes
                        if new_act != trk.activity and (now - trk.last_activity_time) >= self.activity_debounce_seconds:
                            trk.activity = new_act
                            trk.last_activity_time = now
                            desc = f"{trk.name} is {new_act.lower()} in {self.label}."
                            evt = self._create_event(
                                event_type="ACTIVITY_CHANGED",
                                activity=new_act,
                                description=desc,
                                track_obj=trk,
                                alert_status="ALERT_ACTIVE" if not trk.is_known else "NO_ALERT",
                                now=now,
                            )
                            new_events.append(evt)

            # --- DETECT EXITED TRACKS ---
            dead_tracks = []
            for tid, trk in self._tracks.items():
                if tid not in active_track_ids:
                    if (now - trk.last_seen) >= 2.0:
                        # Person left this pane
                        dead_tracks.append(tid)
                        desc = f"{trk.name} left {self.label}."
                        evt = self._create_event(
                            event_type="PERSON_EXITED",
                            activity="Departed",
                            description=desc,
                            track_obj=trk,
                            alert_status="NO_ALERT",
                            now=now,
                        )
                        new_events.append(evt)

            for tid in dead_tracks:
                del self._tracks[tid]

            # Determine aggregate pane status
            has_unknown = any(not t.is_known for t in self._tracks.values())
            has_known = any(t.is_known for t in self._tracks.values())

            if has_unknown:
                self.state = "ALERT_ACTIVE"
            elif has_known:
                self.state = "PERSON_DETECTED"
            else:
                self.state = "MONITORING"

            # Build status dict for UI
            persons_list = []
            for trk in self._tracks.values():
                persons_list.append({
                    "track_id": trk.track_id,
                    "person_id": trk.person_id,
                    "name": trk.name,
                    "tag": trk.tag,
                    "is_known": trk.is_known,
                    "confidence": round(trk.confidence, 2),
                    "activity": trk.activity,
                    "snapshot_base64": trk.snapshot_base64,
                })

            summary_text = "No person detected"
            if self._tracks:
                summary_parts = [f"{t.name} ({t.activity})" for t in self._tracks.values()]
                summary_text = ", ".join(summary_parts)

            pane_status = {
                "pane_id": self.pane_id,
                "label": self.label,
                "state": self.state,
                "person_count": len(self._tracks),
                "has_unknown": has_unknown,
                "has_known": has_known,
                "alert_status": "ALERT_ACTIVE" if has_unknown else "NO_ALERT",
                "persons": persons_list,
                "summary": summary_text,
                "last_event": self._last_event_description,
                "last_event_time": self._last_event_time,
            }

        # Log new events to EventDatabase asynchronously if critical/unusual
        for evt in new_events:
            if evt.event_type in {"PERSON_ENTERED", "PERSON_IDENTIFIED", "UNKNOWN_ALERT"} or evt.alert_status == "ALERT_ACTIVE":
                try:
                    global_event_db.log_unusual_frame(
                        event_type=f"PANE_{evt.event_type}",
                        severity="CRITICAL" if evt.alert_status == "ALERT_ACTIVE" else "INFO",
                        description=f"[{self.pane_id}] {evt.description}",
                        frame_image=frame_image,
                        mode="CCTV_WALL_MONITOR",
                        pane_id=self.pane_id,
                        debounce_seconds=2.0,
                    )
                except Exception as e:
                    logger.debug(f"Event DB log error: {e}")

        return pane_status, new_events

    def _create_event(
        self,
        event_type: str,
        activity: str,
        description: str,
        track_obj: TrackedPersonState,
        alert_status: str,
        now: float,
    ) -> PaneEventRecord:
        """Create and append a structured event record."""
        time_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M:%S")
        evt = PaneEventRecord(
            timestamp=time_str,
            pane_id=self.pane_id,
            pane_label=self.label,
            track_id=track_obj.track_id,
            person_id=track_obj.person_id,
            identity=track_obj.name,
            identity_confidence=round(track_obj.confidence, 2),
            event_type=event_type,
            activity=activity,
            description=description,
            alert_status=alert_status,
        )
        self._recent_events.append(evt)
        if len(self._recent_events) > 50:
            self._recent_events.pop(0)

        self._last_event_description = description
        self._last_event_time = time_str
        self._last_event_timestamp = now
        return evt

    def get_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent structured events for this pane."""
        with self._lock:
            records = self._recent_events[-limit:]
            return [asdict(r) for r in reversed(records)]


class PaneActivityTrackerRegistry:
    """Registry coordinating all pane state machines across the CCTV wall."""

    def __init__(self):
        self._panes: Dict[str, PaneStateTracker] = {}
        self._lock = threading.Lock()

    def get_or_create(self, pane_id: str, default_label: str = "") -> PaneStateTracker:
        """Get or initialize tracker for a pane ID."""
        with self._lock:
            if pane_id not in self._panes:
                self._panes[pane_id] = PaneStateTracker(pane_id, default_label=default_label)
            return self._panes[pane_id]

    def update_all(
        self,
        panes_with_persons: Dict[str, Tuple[str, List[DetectionBox]]],  # pid -> (label, persons)
        frame_image: Optional[np.ndarray] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Update states for all active panes simultaneously.
        Returns map of pane_id -> pane_status dict.
        """
        now = time.time()
        results: Dict[str, Dict[str, Any]] = {}

        for pid, (label, persons) in panes_with_persons.items():
            tracker = self.get_or_create(pid, default_label=label)
            status, _ = tracker.update(
                persons_in_pane=persons,
                pane_label=label,
                frame_image=frame_image,
                now=now,
            )
            results[pid] = status

        return results

    def get_pane_events(self, pane_id: str, limit: int = 25) -> List[Dict[str, Any]]:
        """Retrieve events for a given pane."""
        with self._lock:
            if pane_id in self._panes:
                return self._panes[pane_id].get_recent_events(limit=limit)
        return []

    def reset(self) -> None:
        """Reset all pane trackers."""
        with self._lock:
            self._panes.clear()


# Global singleton pane activity tracker
global_pane_activity_tracker = PaneActivityTrackerRegistry()
