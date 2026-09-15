import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Set
from enum import Enum
import cv2
import numpy as np

from .zones import Zone, ZoneType


class EventSeverity(Enum):
    INFO = "INFO"
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class EventOutcome:
    pane_id: str
    zone_id: Optional[str]
    event_type: str  # DOOR_ENTRY_ALARM, DOOR_ACTIVITY, DOOR_OPEN_DETECTED, DOOR_CLOSED, OBJECT_LIFTED_ALARM, RESTRICTED_INTRUSION, etc.
    track_id: str
    person_id: Optional[str]
    identity: str
    authorization_status: str  # "AUTHORIZED", "UNAUTHORIZED", "UNKNOWN", "ALARM"
    confidence: float
    description: str
    severity: EventSeverity
    timestamp: float = field(default_factory=time.time)
    is_alarm: bool = False
    evidence_snapshot: Optional[np.ndarray] = None
    verified: bool = False


# Backward compatibility alias
PaneEvent = EventOutcome


class TrackZoneState:
    """Maintains the temporal state and dwell duration of a single track relative to a single zone."""

    def __init__(self):
        self.last_state = "OUTSIDE"  # "OUTSIDE", "INSIDE"
        self.last_transition_time = 0.0
        self.entry_time = 0.0
        self.hit_count = 0
        self.loiter_alarm_raised = False

    def update(self, is_inside: bool, now: float) -> Optional[str]:
        """Update state and return a transition event if one occurred."""
        if is_inside:
            self.hit_count += 1
        else:
            self.hit_count = max(0, self.hit_count - 1)

        # Debounce state transitions (require 2 consecutive frames of consistency)
        if is_inside and self.last_state == "OUTSIDE":
            if self.hit_count >= 2:
                self.last_state = "INSIDE"
                self.last_transition_time = now
                self.entry_time = now
                self.loiter_alarm_raised = False
                return "ENTERED"
        elif not is_inside and self.last_state == "INSIDE":
            if self.hit_count == 0:
                self.last_state = "OUTSIDE"
                self.last_transition_time = now
                self.loiter_alarm_raised = False
                return "EXITED"

        return None


class DoorZoneWatchState:
    """
    Monitors a designated Door / Entryway ROI for opening, closing, swinging, or traversal.
    Maintains background baseline, detects optical flow / pixel energy shifts,
    and correlates door motion with nearby authorized vs unauthorized persons.
    
    States: CALIBRATING -> CLOSED_IDLE -> DOOR_OPENING -> DOOR_OPEN -> DOOR_CLOSING
    """

    def __init__(self, zone_id: str, zone_label: str, alarm_on_entry: bool = True, loiter_sec: float = 10.0):
        self.zone_id = zone_id
        self.zone_label = zone_label
        self.alarm_on_entry = alarm_on_entry
        self.loiter_threshold_sec = loiter_sec

        self.state = "CALIBRATING"
        self.baseline_patch: Optional[np.ndarray] = None
        self.last_patch: Optional[np.ndarray] = None
        self.calib_samples = 0

        self.motion_streak = 0
        self.idle_streak = 0
        self.open_start_time = 0.0
        self.last_alarm_time = 0.0
        self.loiter_alarm_raised = False

    def update(
        self,
        pane_img: Optional[np.ndarray],
        zone_bbox_norm: List[float],
        nearby_persons: List[Any],
        now: float,
    ) -> Optional[Tuple[str, str, EventSeverity, bool, str, Optional[str], str]]:
        """
        Evaluate door optical state and motion changes.
        Returns: Optional (event_type, description, severity, is_alarm, identity, person_id, auth_status)
        """
        if pane_img is None or len(zone_bbox_norm) < 4:
            return None

        h, w = pane_img.shape[:2]
        zx, zy, zw, zh = zone_bbox_norm
        px = max(0, min(w - 1, int(round(zx * w))))
        py = max(0, min(h - 1, int(round(zy * h))))
        pw = max(6, min(w - px, int(round(zw * w))))
        ph = max(6, min(h - py, int(round(zh * h))))

        crop = pane_img[py : py + ph, px : px + pw]
        if crop.size == 0:
            return None

        # Standardize crop to small fixed size for fast and stable analysis
        try:
            small_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
            small_gray = cv2.resize(small_gray, (64, 64), interpolation=cv2.INTER_LINEAR)
            small_gray = cv2.GaussianBlur(small_gray, (5, 5), 0)
        except Exception:
            return None

        # Calibration
        if self.state == "CALIBRATING":
            if self.baseline_patch is None:
                self.baseline_patch = small_gray.astype(np.float32)
            else:
                self.baseline_patch = 0.7 * self.baseline_patch + 0.3 * small_gray.astype(np.float32)
            self.last_patch = small_gray.copy()
            self.calib_samples += 1
            if self.calib_samples >= 3:
                self.state = "CLOSED_IDLE"
            return None

        # Compute Frame-to-Frame Motion Delta and Baseline Delta
        frame_diff = cv2.absdiff(small_gray, self.last_patch)
        motion_energy = float(np.mean(frame_diff))
        motion_pixels = float(np.count_nonzero(frame_diff > 12)) / 4096.0  # Fraction of moving pixels

        base_diff = cv2.absdiff(small_gray, self.baseline_patch.astype(np.uint8))
        bg_delta = float(np.mean(base_diff))

        self.last_patch = small_gray.copy()

        # Motion thresholding for door activity (opening, closing, swinging)
        is_motion_active = (motion_energy > 3.2 and motion_pixels > 0.035) or (motion_energy > 7.0)

        # Correlate with nearby detected persons
        interacting_person = None
        for p in nearby_persons:
            bx, by, bw, bh = p.bbox
            # Check bounding box proximity to door zone
            nbx = bx / float(w)
            nby = by / float(h)
            nbw = bw / float(w)
            nbh = bh / float(h)

            # Check overlap or proximity within 0.08 normalized distance
            margin = 0.08
            overlaps = not (
                (nbx + nbw) < (zx - margin)
                or nbx > (zx + zw + margin)
                or (nby + nbh) < (zy - margin)
                or nby > (zy + zh + margin)
            )
            if overlaps:
                interacting_person = p
                break

        # State 1: CLOSED_IDLE -> Check for Door Opening / Motion
        if self.state == "CLOSED_IDLE":
            if is_motion_active:
                self.motion_streak += 1
                self.idle_streak = 0
            else:
                self.motion_streak = max(0, self.motion_streak - 1)
                # Adapt baseline slowly to ambient lighting changes
                self.baseline_patch = 0.98 * self.baseline_patch + 0.02 * small_gray.astype(np.float32)

            if self.motion_streak >= 2:
                # Door is OPENING / ACTIVE
                self.state = "DOOR_OPEN"
                self.open_start_time = now
                self.loiter_alarm_raised = False

                cooldown_elapsed = (now - self.last_alarm_time) > 3.0
                if cooldown_elapsed:
                    self.last_alarm_time = now

                    if interacting_person is not None:
                        is_known = getattr(interacting_person, "is_known", False)
                        pname = getattr(interacting_person, "person_name", "Unknown Person")
                        pid = getattr(interacting_person, "person_id", None)
                        tag = getattr(interacting_person, "tag", "Authorized")

                        if is_known:
                            return (
                                "DOOR_ACTIVITY",
                                f"🚪 Authorized door access: {pname} [{tag}] accessed {self.zone_label}",
                                EventSeverity.INFO,
                                False,
                                pname,
                                pid,
                                "AUTHORIZED",
                            )
                        else:
                            return (
                                "DOOR_ENTRY_ALARM",
                                f"🚨 DOOR ENTRY ALARM: Unknown person opened/entered from {self.zone_label}!",
                                EventSeverity.CRITICAL if self.alarm_on_entry else EventSeverity.WARNING,
                                self.alarm_on_entry,
                                "Unknown Person",
                                None,
                                "UNAUTHORIZED",
                            )
                    else:
                        # Door movement without identified face/person (e.g. door swinging open, push from behind)
                        is_alarm = self.alarm_on_entry
                        return (
                            "DOOR_OPEN_DETECTED",
                            f"🚪 Door activity detected: {self.zone_label} opened / moved!",
                            EventSeverity.WARNING if is_alarm else EventSeverity.INFO,
                            is_alarm,
                            "Door Activity",
                            None,
                            "UNKNOWN",
                        )
            return None

        # State 2: DOOR_OPEN -> Monitor open duration and closing transition
        if self.state == "DOOR_OPEN":
            if not is_motion_active and bg_delta < 6.0:
                self.idle_streak += 1
                self.motion_streak = 0
            elif not is_motion_active and bg_delta >= 6.0:
                # Door is stationary but propped/held OPEN
                self.idle_streak = 0
                self.motion_streak = 0
            else:
                self.motion_streak += 1
                self.idle_streak = 0

            # Door Held Open Loitering Check
            dwell = now - self.open_start_time
            if dwell >= self.loiter_threshold_sec and not self.loiter_alarm_raised and (now - self.last_alarm_time > 4.0):
                self.loiter_alarm_raised = True
                self.last_alarm_time = now
                return (
                    "DOOR_HELD_OPEN_WARNING",
                    f"⚠️ Door held open: {self.zone_label} has remained open for {int(dwell)}s!",
                    EventSeverity.WARNING,
                    True,
                    "Door Monitor",
                    None,
                    "WARNING",
                )

            # Door Closed Transition
            if self.idle_streak >= 3:
                self.state = "CLOSED_IDLE"
                self.motion_streak = 0
                self.idle_streak = 0
                # Re-calibrate baseline
                self.baseline_patch = small_gray.astype(np.float32)

                if now - self.last_alarm_time > 3.0:
                    return (
                        "DOOR_CLOSED",
                        f"🚪 Door closed: {self.zone_label} is now closed.",
                        EventSeverity.INFO,
                        False,
                        "Door Monitor",
                        None,
                        "AUTHORIZED",
                    )

        return None


class ObjectZoneWatchState:
    """
    Monitors a designated box/object ROI for lifting, displacement, or tampering.
    States: CALIBRATING -> ARMED -> INTERACTING -> LIFTED_ALARM
    """

    def __init__(self, zone_id: str, zone_label: str):
        self.zone_id = zone_id
        self.zone_label = zone_label
        self.state = "CALIBRATING"
        self.baseline_patch: Optional[np.ndarray] = None
        self.baseline_mean: float = 0.0
        self.baseline_std: float = 0.0
        self.last_interaction_time: float = 0.0
        self.last_alarm_time: float = 0.0
        self.calib_samples = 0
        self.arming_cooldown = 1.5

    def update(
        self,
        pane_img: Optional[np.ndarray],
        zone_bbox_norm: List[float],
        nearby_persons: List[Any],
        now: float,
    ) -> Optional[Tuple[str, str, EventSeverity]]:
        """
        Evaluate if object was lifted or tampered with.
        Returns: Optional (event_type, description, severity)
        """
        if pane_img is None or len(zone_bbox_norm) < 4:
            return None

        h, w = pane_img.shape[:2]
        zx, zy, zw, zh = zone_bbox_norm
        px = max(0, min(w - 1, int(round(zx * w))))
        py = max(0, min(h - 1, int(round(zy * h))))
        pw = max(4, min(w - px, int(round(zw * w))))
        ph = max(4, min(h - py, int(round(zh * h))))

        crop = pane_img[py : py + ph, px : px + pw]
        if crop.size == 0:
            return None

        gray = np.mean(crop, axis=2) if len(crop.shape) == 3 else crop
        curr_mean = float(np.mean(gray))
        curr_std = float(np.std(gray))

        # Check person overlap with the object zone
        person_overlapping = False
        interacting_person_name = "Person"
        for p in nearby_persons:
            bx, by, bw, bh = p.bbox
            nbx, nby, nbw, nbh = bx / float(w), by / float(h), bw / float(w), bh / float(h)
            if not (nbx + nbw < zx or nbx > zx + zw or nby + nbh < zy or nby > zy + zh):
                person_overlapping = True
                pname = getattr(p, "person_name", None)
                if pname and pname != "Unknown Person":
                    interacting_person_name = pname
                break

        # State 1: Calibrate baseline when no person is present
        if self.state == "CALIBRATING":
            if not person_overlapping:
                self.calib_samples += 1
                self.baseline_mean = 0.8 * self.baseline_mean + 0.2 * curr_mean if self.calib_samples > 1 else curr_mean
                self.baseline_std = 0.8 * self.baseline_std + 0.2 * curr_std if self.calib_samples > 1 else curr_std
                if self.calib_samples >= 3:
                    self.state = "ARMED"
            return None

        # State 2: ARMED - Watching for person approaching / lifting
        if self.state == "ARMED":
            if person_overlapping:
                self.state = "INTERACTING"
                self.last_interaction_time = now
            else:
                self.baseline_mean = 0.98 * self.baseline_mean + 0.02 * curr_mean
                self.baseline_std = 0.98 * self.baseline_std + 0.02 * curr_std
            return None

        # State 3: INTERACTING - Person in contact with object zone
        if self.state == "INTERACTING":
            diff = abs(curr_mean - self.baseline_mean) + abs(curr_std - self.baseline_std)
            if not person_overlapping:
                if diff > 14.0 and (now - self.last_alarm_time > 8.0):
                    self.state = "LIFTED_ALARM"
                    self.last_alarm_time = now
                    desc = f"🚨 Object / Box lifted from {self.zone_label} by {interacting_person_name}!"
                    return ("OBJECT_LIFTED_ALARM", desc, EventSeverity.CRITICAL)
                else:
                    self.state = "ARMED"
            else:
                if diff > 28.0 and (now - self.last_interaction_time > 0.8) and (now - self.last_alarm_time > 8.0):
                    self.state = "LIFTED_ALARM"
                    self.last_alarm_time = now
                    desc = f"🚨 Active Box / Object lifting detected in {self.zone_label} by {interacting_person_name}!"
                    return ("OBJECT_LIFTED_ALARM", desc, EventSeverity.CRITICAL)

        # State 4: LIFTED_ALARM - Cooldown before re-arming
        if self.state == "LIFTED_ALARM":
            if not person_overlapping and (now - self.last_alarm_time > 6.0):
                self.state = "CALIBRATING"
                self.calib_samples = 0

        return None


class ZoneMotionWatchState:
    """
    General optical motion monitor for any designated zone (Restricted Area, Tripwire, General).
    Triggers motion-based security alarms when unexpected physical activity happens in the ROI.
    """

    def __init__(self, zone_id: str, zone_label: str, zone_type: ZoneType, alarm_on_entry: bool = True):
        self.zone_id = zone_id
        self.zone_label = zone_label
        self.zone_type = zone_type
        self.alarm_on_entry = alarm_on_entry
        self.last_patch: Optional[np.ndarray] = None
        self.last_alarm_time = 0.0
        self.motion_count = 0

    def update(
        self,
        pane_img: Optional[np.ndarray],
        zone_bbox_norm: List[float],
        nearby_persons: List[Any],
        now: float,
    ) -> Optional[Tuple[str, str, EventSeverity, bool]]:
        if pane_img is None or len(zone_bbox_norm) < 4:
            return None

        h, w = pane_img.shape[:2]
        zx, zy, zw, zh = zone_bbox_norm
        px = max(0, min(w - 1, int(round(zx * w))))
        py = max(0, min(h - 1, int(round(zy * h))))
        pw = max(6, min(w - px, int(round(zw * w))))
        ph = max(6, min(h - py, int(round(zh * h))))

        crop = pane_img[py : py + ph, px : px + pw]
        if crop.size == 0:
            return None

        try:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
            gray = cv2.resize(gray, (48, 48), interpolation=cv2.INTER_LINEAR)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
        except Exception:
            return None

        if self.last_patch is None:
            self.last_patch = gray
            return None

        diff = cv2.absdiff(gray, self.last_patch)
        motion_energy = float(np.mean(diff))
        motion_ratio = float(np.count_nonzero(diff > 14)) / (48.0 * 48.0)
        self.last_patch = gray

        is_motion = (motion_energy > 4.0 and motion_ratio > 0.04) or (motion_energy > 8.0)

        if is_motion:
            self.motion_count += 1
        else:
            self.motion_count = max(0, self.motion_count - 1)

        if self.motion_count >= 2 and (now - self.last_alarm_time > 4.0):
            self.last_alarm_time = now

            if self.zone_type == ZoneType.RESTRICTED:
                return (
                    "RESTRICTED_MOTION_ALARM",
                    f"🚨 Motion detected inside restricted zone {self.zone_label}!",
                    EventSeverity.CRITICAL,
                    True,
                )
            elif self.zone_type == ZoneType.TRIPWIRE:
                return (
                    "TRIPWIRE_TRIGGERED",
                    f"🚨 Tripwire crossed / motion triggered in {self.zone_label}!",
                    EventSeverity.CRITICAL,
                    True,
                )
            elif self.zone_type in (ZoneType.ENTRY, ZoneType.EXIT):
                return (
                    "ZONE_ENTRY_ACTIVITY",
                    f"🏃 Activity detected in entry zone {self.zone_label}",
                    EventSeverity.WARNING if self.alarm_on_entry else EventSeverity.INFO,
                    self.alarm_on_entry,
                )
            elif self.zone_type in (ZoneType.WORKSTATION, ZoneType.DESK, ZoneType.GENERAL):
                return (
                    "ZONE_ACTIVITY_DETECTED",
                    f"⚡ Activity detected in {self.zone_label}",
                    EventSeverity.INFO,
                    False,
                )

        return None


class WorkstationActivityState:
    """
    Temporal state machine for workstation activity.
    States: AWAY -> APPROACHING -> SITTING -> INTERACTING
    """

    def __init__(self):
        self.state = "AWAY"
        self.last_transition_time = 0.0
        self.entry_time = 0.0

    def update(self, is_approaching: bool, is_sitting: bool, now: float) -> Optional[str]:
        prev_state = self.state

        if self.state in ["SITTING", "INTERACTING"] and not is_sitting and not is_approaching:
            if now - self.last_transition_time < 2.0:
                return None

        if is_sitting:
            if self.state == "SITTING":
                if now - self.entry_time >= 5.0 and self.state != "INTERACTING":
                    self.state = "INTERACTING"
                    self.last_transition_time = now
                    return "STARTED_INTERACTING"
            elif self.state != "INTERACTING":
                self.state = "SITTING"
                self.entry_time = now
                self.last_transition_time = now
        elif is_approaching:
            if self.state not in ["SITTING", "INTERACTING"]:
                self.state = "APPROACHING"
                self.last_transition_time = now
        else:
            if self.state != "AWAY":
                self.state = "AWAY"
                self.last_transition_time = now

        if prev_state != self.state:
            return f"TRANSITIONED_TO_{self.state}"

        return None


class PaneEventEngine:
    """
    Evaluates spatial and temporal rules to detect frame-by-frame activities and alarms in a CCTV pane.
    Processes tracked persons and optical motion relative to defined zones (Doors, Objects, Restricted Areas).
    """

    def __init__(self):
        # Track state: {pane_id: {track_id: {zone_id: TrackZoneState}}}
        self._track_states: Dict[str, Dict[str, Dict[str, TrackZoneState]]] = {}
        # Door watch state: {pane_id: {zone_id: DoorZoneWatchState}}
        self._door_watch_states: Dict[str, Dict[str, DoorZoneWatchState]] = {}
        # Object watch state: {pane_id: {zone_id: ObjectZoneWatchState}}
        self._object_watch_states: Dict[str, Dict[str, ObjectZoneWatchState]] = {}
        # General zone motion watch state: {pane_id: {zone_id: ZoneMotionWatchState}}
        self._zone_motion_states: Dict[str, Dict[str, ZoneMotionWatchState]] = {}
        # Activity state: {pane_id: {track_id: WorkstationActivityState}}
        self._activity_states: Dict[str, Dict[str, WorkstationActivityState]] = {}

    def process_pane(
        self,
        pane_id: str,
        pane_label: str,
        zones: List[Zone],
        detected_persons: List[Any],
        pane_image: Optional[np.ndarray] = None,
        img_w: int = 640,
        img_h: int = 360,
        now: Optional[float] = None,
    ) -> List[EventOutcome]:
        now = now or time.time()
        outcomes: List[EventOutcome] = []

        if pane_id not in self._track_states:
            self._track_states[pane_id] = {}
        if pane_id not in self._door_watch_states:
            self._door_watch_states[pane_id] = {}
        if pane_id not in self._object_watch_states:
            self._object_watch_states[pane_id] = {}
        if pane_id not in self._zone_motion_states:
            self._zone_motion_states[pane_id] = {}
        if pane_id not in self._activity_states:
            self._activity_states[pane_id] = {}

        pane_tracks = self._track_states[pane_id]
        pane_doors = self._door_watch_states[pane_id]
        pane_objects = self._object_watch_states[pane_id]
        pane_zone_motions = self._zone_motion_states[pane_id]
        pane_activity = self._activity_states[pane_id]

        # ---------------------------------------------------------
        # 1. EVALUATE PERSON SPATIAL OVERLAPS AGAINST ZONES
        # ---------------------------------------------------------
        for person in detected_persons:
            track_id = getattr(person, "person_id", None) or f"trk_{person.bbox[0]}_{person.bbox[1]}"
            if not track_id:
                continue

            if track_id not in pane_tracks:
                pane_tracks[track_id] = {}

            person_zones = pane_tracks[track_id]
            is_approaching = False
            is_sitting = False

            person_id = getattr(person, "person_id", None)
            identity = getattr(person, "person_name", "Unknown Person")
            is_known = getattr(person, "is_known", False)

            for zone in zones:
                if zone.zone_id not in person_zones:
                    person_zones[zone.zone_id] = TrackZoneState()

                state = person_zones[zone.zone_id]
                # Use robust multi-point / bounding box overlap
                is_inside = zone.overlaps_person(person.bbox, img_w=img_w, img_h=img_h)

                if zone.zone_type in (ZoneType.WORKSTATION, ZoneType.COMPUTER, ZoneType.DESK):
                    is_sitting = is_inside
                if zone.zone_type in (ZoneType.ENTRY, ZoneType.WAITING, ZoneType.DOOR):
                    is_approaching = is_inside

                transition = state.update(is_inside, now)

                # --- Handle Transitions: ENTERED / EXITED ---
                if transition == "ENTERED":
                    auth_status = "UNKNOWN"
                    severity = EventSeverity.INFO
                    is_alarm = False
                    event_type = "PERSON_ENTERED"

                    if is_known:
                        if zone.authorized_persons and person_id in zone.authorized_persons:
                            auth_status = "AUTHORIZED"
                            severity = EventSeverity.INFO
                            desc = f"✅ Authorized entry: {identity} entered {zone.label} in {pane_label}."
                        elif zone.authorized_persons:
                            auth_status = "UNAUTHORIZED"
                            severity = EventSeverity.CRITICAL
                            is_alarm = True
                            event_type = "UNAUTHORIZED_INTRUSION"
                            desc = f"🚨 Unauthorized entry: {identity} entered restricted {zone.label} in {pane_label}!"
                        else:
                            auth_status = "AUTHORIZED"
                            severity = EventSeverity.INFO
                            desc = f"{identity} entered {zone.label} in {pane_label}."
                    else:
                        # Unknown Person
                        if zone.zone_type in (ZoneType.DOOR, ZoneType.ENTRY):
                            severity = EventSeverity.CRITICAL if zone.alarm_on_entry else EventSeverity.WARNING
                            is_alarm = zone.alarm_on_entry
                            event_type = "DOOR_ENTRY_ALARM" if is_alarm else "PERSON_ENTERED"
                            desc = f"🚨 DOOR ENTRY ALARM: Unknown person entered from {zone.label} in {pane_label}!"
                        elif zone.zone_type == ZoneType.RESTRICTED:
                            severity = EventSeverity.CRITICAL
                            is_alarm = True
                            event_type = "RESTRICTED_INTRUSION"
                            desc = f"🚨 INTRUSION ALARM: Unknown person entered restricted zone {zone.label} in {pane_label}!"
                        else:
                            severity = EventSeverity.WARNING
                            desc = f"Unknown person entered {zone.label} in {pane_label}."

                    outcomes.append(
                        EventOutcome(
                            pane_id=pane_id,
                            zone_id=zone.zone_id,
                            event_type=event_type,
                            track_id=track_id,
                            person_id=person_id,
                            identity=identity,
                            authorization_status=auth_status,
                            confidence=getattr(person, "confidence", 1.0),
                            description=desc,
                            severity=severity,
                            timestamp=now,
                            is_alarm=is_alarm,
                        )
                    )

                elif transition == "EXITED":
                    desc = f"{identity} exited {zone.label} in {pane_label}."
                    outcomes.append(
                        EventOutcome(
                            pane_id=pane_id,
                            zone_id=zone.zone_id,
                            event_type="PERSON_EXITED",
                            track_id=track_id,
                            person_id=person_id,
                            identity=identity,
                            authorization_status="AUTHORIZED" if is_known else "UNKNOWN",
                            confidence=getattr(person, "confidence", 1.0),
                            description=desc,
                            severity=EventSeverity.INFO,
                            timestamp=now,
                            is_alarm=False,
                        )
                    )

                # --- Loitering / Dwell Time Check ---
                if is_inside and not state.loiter_alarm_raised:
                    dwell = now - state.entry_time
                    if dwell >= zone.loiter_threshold_sec and zone.zone_type in (ZoneType.RESTRICTED, ZoneType.DOOR, ZoneType.ENTRY):
                        state.loiter_alarm_raised = True
                        desc = f"⚠️ Loitering Alert: {identity} has remained in {zone.label} for {int(dwell)}s in {pane_label}."
                        outcomes.append(
                            EventOutcome(
                                pane_id=pane_id,
                                zone_id=zone.zone_id,
                                event_type="LOITERING_WARNING",
                                track_id=track_id,
                                person_id=person_id,
                                identity=identity,
                                authorization_status="AUTHORIZED" if is_known else "UNKNOWN",
                                confidence=getattr(person, "confidence", 1.0),
                                description=desc,
                                severity=EventSeverity.WARNING,
                                timestamp=now,
                                is_alarm=True,
                            )
                        )

            # Update Workstation Activity State
            if track_id not in pane_activity:
                pane_activity[track_id] = WorkstationActivityState()

            activity_transition = pane_activity[track_id].update(is_approaching, is_sitting, now)
            if activity_transition:
                outcomes.append(
                    EventOutcome(
                        pane_id=pane_id,
                        zone_id=None,
                        event_type=activity_transition,
                        track_id=track_id,
                        person_id=person_id,
                        identity=identity,
                        authorization_status="AUTHORIZED" if is_known else "UNKNOWN",
                        confidence=getattr(person, "confidence", 1.0),
                        description=f"{identity} activity: {activity_transition} in {pane_label}",
                        severity=EventSeverity.INFO,
                        timestamp=now,
                        is_alarm=False,
                    )
                )

        # ---------------------------------------------------------
        # 2. EVALUATE DOOR / ENTRYWAY OPTICAL & MOTION ACTIVITY
        # ---------------------------------------------------------
        for zone in zones:
            if zone.zone_type in (ZoneType.DOOR, ZoneType.ENTRY, ZoneType.EXIT):
                if zone.zone_id not in pane_doors:
                    pane_doors[zone.zone_id] = DoorZoneWatchState(
                        zone_id=zone.zone_id,
                        zone_label=zone.label,
                        alarm_on_entry=zone.alarm_on_entry,
                        loiter_sec=zone.loiter_threshold_sec,
                    )

                door_watch = pane_doors[zone.zone_id]
                door_res = door_watch.update(
                    pane_img=pane_image,
                    zone_bbox_norm=zone.bbox,
                    nearby_persons=detected_persons,
                    now=now,
                )
                if door_res:
                    evt_type, desc, sev, is_alarm, ident, pid, auth_st = door_res
                    outcomes.append(
                        EventOutcome(
                            pane_id=pane_id,
                            zone_id=zone.zone_id,
                            event_type=evt_type,
                            track_id=f"door_{zone.zone_id}",
                            person_id=pid,
                            identity=ident,
                            authorization_status=auth_st,
                            confidence=0.98,
                            description=f"[{pane_label}] {desc}",
                            severity=sev,
                            timestamp=now,
                            is_alarm=is_alarm,
                        )
                    )

        # ---------------------------------------------------------
        # 3. EVALUATE OBJECT / BOX WATCH ZONES (LIFTING / TAMPERING)
        # ---------------------------------------------------------
        for zone in zones:
            if zone.zone_type in (ZoneType.OBJECT_BOX, ZoneType.BOX):
                if zone.zone_id not in pane_objects:
                    pane_objects[zone.zone_id] = ObjectZoneWatchState(zone.zone_id, zone.label)

                obj_state = pane_objects[zone.zone_id]
                res = obj_state.update(
                    pane_img=pane_image,
                    zone_bbox_norm=zone.bbox,
                    nearby_persons=detected_persons,
                    now=now,
                )
                if res:
                    evt_type, desc, sev = res
                    outcomes.append(
                        EventOutcome(
                            pane_id=pane_id,
                            zone_id=zone.zone_id,
                            event_type=evt_type,
                            track_id=f"obj_{zone.zone_id}",
                            person_id=None,
                            identity="Object Watch",
                            authorization_status="ALARM",
                            confidence=0.96,
                            description=f"[{pane_label}] {desc}",
                            severity=sev,
                            timestamp=now,
                            is_alarm=True,
                        )
                    )

        # ---------------------------------------------------------
        # 4. EVALUATE GENERAL ZONE MOTION (RESTRICTED, TRIPWIRE, ETC.)
        # ---------------------------------------------------------
        for zone in zones:
            if zone.zone_type in (ZoneType.RESTRICTED, ZoneType.TRIPWIRE, ZoneType.GENERAL):
                if zone.zone_id not in pane_zone_motions:
                    pane_zone_motions[zone.zone_id] = ZoneMotionWatchState(
                        zone_id=zone.zone_id,
                        zone_label=zone.label,
                        zone_type=zone.zone_type,
                        alarm_on_entry=zone.alarm_on_entry,
                    )

                mot_state = pane_zone_motions[zone.zone_id]
                mot_res = mot_state.update(
                    pane_img=pane_image,
                    zone_bbox_norm=zone.bbox,
                    nearby_persons=detected_persons,
                    now=now,
                )
                if mot_res:
                    evt_type, desc, sev, is_alarm = mot_res
                    outcomes.append(
                        EventOutcome(
                            pane_id=pane_id,
                            zone_id=zone.zone_id,
                            event_type=evt_type,
                            track_id=f"motion_{zone.zone_id}",
                            person_id=None,
                            identity="Zone Motion",
                            authorization_status="ALARM" if is_alarm else "NORMAL",
                            confidence=0.95,
                            description=f"[{pane_label}] {desc}",
                            severity=sev,
                            timestamp=now,
                            is_alarm=is_alarm,
                        )
                    )

        return outcomes

    def clear_pane(self, pane_id: str):
        """Clear cached state for a specific pane."""
        self._track_states.pop(pane_id, None)
        self._door_watch_states.pop(pane_id, None)
        self._object_watch_states.pop(pane_id, None)
        self._zone_motion_states.pop(pane_id, None)
        self._activity_states.pop(pane_id, None)
