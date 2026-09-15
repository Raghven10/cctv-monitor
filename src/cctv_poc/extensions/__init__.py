"""Future expansion interfaces for person detection, tracking, face recognition, and rule alerts."""

from .interfaces import (
    AlertEvent,
    AlertManager,
    DetectionBox,
    EvidenceStore,
    FaceRecognizer,
    PersonDetector,
    RuleEngine,
    TrackedObject,
    Tracker,
)
from .person_detector import FastLivePersonDetector, YoloV11PersonDetector
from .pane_activity_tracker import PaneActivityTrackerRegistry, global_pane_activity_tracker

__all__ = [
    "AlertEvent",
    "AlertManager",
    "DetectionBox",
    "EvidenceStore",
    "FaceRecognizer",
    "FastLivePersonDetector",
    "YoloV11PersonDetector",
    "PaneActivityTrackerRegistry",
    "PersonDetector",
    "RuleEngine",
    "TrackedObject",
    "Tracker",
    "global_pane_activity_tracker",
]

