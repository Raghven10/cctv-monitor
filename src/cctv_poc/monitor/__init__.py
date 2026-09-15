"""Monitor detection, perspective correction, dynamic layout discovery and pane extraction."""

from .calibration import CalibrationStore
from .display_detector import DisplayDetectionResult, DisplayDetector
from .layout_discovery import DiscoveredLayout, LayoutDiscoverer, PaneGeometry
from .manual_layout import ManualGridConfig, ManualLayoutStore, PRESET_CONFIGS
from .pane_extractor import ExtractedPane, PaneExtractor
from .perspective import PerspectiveCorrector

__all__ = [
    "CalibrationStore",
    "DisplayDetectionResult",
    "DisplayDetector",
    "DiscoveredLayout",
    "LayoutDiscoverer",
    "PaneGeometry",
    "ManualGridConfig",
    "ManualLayoutStore",
    "PRESET_CONFIGS",
    "ExtractedPane",
    "PaneExtractor",
    "PerspectiveCorrector",
]

