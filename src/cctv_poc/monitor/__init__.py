"""Monitor detection, perspective correction, dynamic layout discovery and pane extraction."""

from .calibration import CalibrationStore
from .display_detector import DisplayDetectionResult, DisplayDetector
from .layout_discovery import DiscoveredLayout, LayoutDiscoverer, PaneGeometry
from .pane_extractor import ExtractedPane, PaneExtractor
from .perspective import PerspectiveCorrector

__all__ = [
    "CalibrationStore",
    "DisplayDetectionResult",
    "DisplayDetector",
    "DiscoveredLayout",
    "LayoutDiscoverer",
    "PaneGeometry",
    "ExtractedPane",
    "PaneExtractor",
    "PerspectiveCorrector",
]
