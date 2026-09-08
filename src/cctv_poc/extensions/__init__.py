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
from .person_detector import FastLivePersonDetector

__all__ = [
    "AlertEvent",
    "AlertManager",
    "DetectionBox",
    "EvidenceStore",
    "FaceRecognizer",
    "FastLivePersonDetector",
    "PersonDetector",
    "RuleEngine",
    "TrackedObject",
    "Tracker",
]
