"""Manual Grid Layout Persistence & Geometry Generator.

Allows users to:
1. Select preset layouts (1x1, 1x2, 2x1, 2x2, 2x3, 3x2, 3x3, 4x4, etc.)
2. Define custom rows and columns (e.g. 2x5, 3x4)
3. Interactively adjust individual vertical and horizontal divider positions
4. Persist and restore custom configurations
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .layout_discovery import DiscoveredLayout, PaneGeometry
from .zones import Zone, ZoneType
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.manual_layout")


PRESET_CONFIGS: Dict[str, Tuple[int, int]] = {
    "1X1": (1, 1),
    "1X2": (1, 2),
    "2X1": (2, 1),
    "2X2": (2, 2),
    "2X3": (2, 3),
    "3X2": (3, 2),
    "3X3": (3, 3),
    "3X4": (3, 4),
    "4X3": (4, 3),
    "4X4": (4, 4),
}


@dataclass
class ManualGridConfig:
    """Configuration for user-defined grid layout."""
    is_active: bool = False
    is_finalized: bool = False  # True when user finalizes grid to freeze layout & stop repeated detection
    preset: str = "AUTO"  # "AUTO", "1X1", "2X2", "CUSTOM", etc.
    rows: int = 2
    cols: int = 2
    # Normalized divider positions (0.0 to 1.0)
    # len(x_dividers) == cols - 1
    # len(y_dividers) == rows - 1
    x_dividers: List[float] = field(default_factory=list)
    y_dividers: List[float] = field(default_factory=list)
    panes_metadata: List[Dict[str, Any]] = field(default_factory=list)
    mesh: List[List[List[float]]] = field(default_factory=list)
    # Map of pane_id to list of Zone definitions
    zones: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # Map of pane_id to rule configurations (e.g. {"intrusion_enabled": True})
    rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def create_even_dividers(count: int) -> List[float]:
    """Generate equally spaced normalized divider coordinates for (count) segments.
    
    If count = 2 (2 columns), returns [0.5] (1 divider).
    If count = 3 (3 columns), returns [0.3333, 0.6667] (2 dividers).
    """
    if count <= 1:
        return []
    return [round(float(i) / float(count), 4) for i in range(1, count)]


class ManualLayoutStore:
    """Manages saving, loading, and generating geometry for user-customized CCTV grids."""

    def __init__(self, config_file: str = "config/manual_layout.json"):
        self.file_path = Path(config_file)
        self._current_config = self.load()

    @property
    def config(self) -> ManualGridConfig:
        return self._current_config

    @property
    def is_active(self) -> bool:
        return self._current_config.is_active

    def set_preset(self, preset: str) -> ManualGridConfig:
        """Configure layout from a named preset (or AUTO)."""
        preset_upper = preset.upper().strip()
        if preset_upper == "AUTO":
            return self.reset_to_auto()

        if preset_upper not in PRESET_CONFIGS:
            raise ValueError(f"Unknown preset: {preset}. Allowed: {list(PRESET_CONFIGS.keys())}")

        rows, cols = PRESET_CONFIGS[preset_upper]
        xd = create_even_dividers(cols)
        yd = create_even_dividers(rows)
        x_cuts = [0.0] + sorted(xd) + [1.0]
        y_cuts = [0.0] + sorted(yd) + [1.0]

        panes_meta = []
        idx = 0
        for row_i in range(rows):
            for col_i in range(cols):
                idx += 1
                pid = f"Pane-{idx:02d}"
                x = x_cuts[col_i]
                y = y_cuts[row_i]
                w = max(0.01, x_cuts[col_i + 1] - x)
                h = max(0.01, y_cuts[row_i + 1] - y)
                panes_meta.append({
                    "id": pid,
                    "x": round(x, 4),
                    "y": round(y, 4),
                    "width": round(w, 4),
                    "height": round(h, 4),
                    "label": f"CAM-{idx:02d}",
                    "row": row_i,
                    "col": col_i,
                    "corners": [
                        [round(x, 4), round(y, 4)],
                        [round(x + w, 4), round(y, 4)],
                        [round(x + w, 4), round(y + h, 4)],
                        [round(x, 4), round(y + h, 4)],
                    ],
                })

        cfg = ManualGridConfig(
            is_active=True,
            is_finalized=True,
            preset=preset_upper,
            rows=rows,
            cols=cols,
            x_dividers=xd,
            y_dividers=yd,
            panes_metadata=panes_meta,
            mesh=[],
            zones=self._current_config.zones or {},
            rules=self._current_config.rules or {},
        )
        self.save(cfg)
        return cfg

    def set_custom_grid(
        self,
        rows: int,
        cols: int,
        x_dividers: Optional[List[float]] = None,
        y_dividers: Optional[List[float]] = None,
        preset: str = "CUSTOM",
    ) -> ManualGridConfig:
        """Set custom grid with arbitrary rows, cols, and optional custom divider coordinates."""
        rows = max(1, min(int(rows), 8))
        cols = max(1, min(int(cols), 8))

        if x_dividers is None or len(x_dividers) != cols - 1:
            x_dividers = create_even_dividers(cols)
        else:
            # Validate and clamp dividers strictly in ascending order between 0.02 and 0.98
            x_dividers = sorted([max(0.02, min(float(x), 0.98)) for x in x_dividers])

        if y_dividers is None or len(y_dividers) != rows - 1:
            y_dividers = create_even_dividers(rows)
        else:
            y_dividers = sorted([max(0.02, min(float(y), 0.98)) for y in y_dividers])

        x_cuts = [0.0] + sorted(x_dividers) + [1.0]
        y_cuts = [0.0] + sorted(y_dividers) + [1.0]

        panes_meta = []
        idx = 0
        for row_i in range(rows):
            for col_i in range(cols):
                idx += 1
                pid = f"Pane-{idx:02d}"
                x = x_cuts[col_i]
                y = y_cuts[row_i]
                w = max(0.01, x_cuts[col_i + 1] - x)
                h = max(0.01, y_cuts[row_i + 1] - y)
                panes_meta.append({
                    "id": pid,
                    "x": round(x, 4),
                    "y": round(y, 4),
                    "width": round(w, 4),
                    "height": round(h, 4),
                    "label": f"CAM-{idx:02d}",
                    "row": row_i,
                    "col": col_i,
                    "corners": [
                        [round(x, 4), round(y, 4)],
                        [round(x + w, 4), round(y, 4)],
                        [round(x + w, 4), round(y + h, 4)],
                        [round(x, 4), round(y + h, 4)],
                    ],
                })

        cfg = ManualGridConfig(
            is_active=True,
            is_finalized=True,
            preset=preset,
            rows=rows,
            cols=cols,
            x_dividers=x_dividers,
            y_dividers=y_dividers,
            panes_metadata=panes_meta,
            mesh=[],
            zones=self._current_config.zones or {},
            rules=self._current_config.rules or {},
        )
        self.save(cfg)
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
        """
        Freeze selected grid configuration, assign permanent Pane-01.. IDs,
        store normalized pane ROIs/corners, and stop repeated grid detection.
        """
        r = rows if rows is not None else self._current_config.rows
        c = cols if cols is not None else self._current_config.cols
        p = preset or self._current_config.preset or "CUSTOM"
        xd = x_dividers if x_dividers is not None else self._current_config.x_dividers
        yd = y_dividers if y_dividers is not None else self._current_config.y_dividers

        if panes_metadata and len(panes_metadata) == (r * c):
            # Direct custom mesh/quad panes matching exact dimension count
            panes_meta = panes_metadata
        else:
            if not xd or len(xd) != c - 1:
                xd = create_even_dividers(c)
            if not yd or len(yd) != r - 1:
                yd = create_even_dividers(r)

            x_cuts = [0.0] + sorted(xd) + [1.0]
            y_cuts = [0.0] + sorted(yd) + [1.0]

            panes_meta = []
            idx = 0
            for row_i in range(r):
                for col_i in range(c):
                    idx += 1
                    pid = f"Pane-{idx:02d}"
                    x = x_cuts[col_i]
                    y = y_cuts[row_i]
                    w = max(0.01, x_cuts[col_i + 1] - x)
                    h = max(0.01, y_cuts[row_i + 1] - y)
                    custom_lbl = (pane_labels or {}).get(pid) or f"CAM-{idx:02d}"
                    panes_meta.append({
                        "id": pid,
                        "x": round(x, 4),
                        "y": round(y, 4),
                        "width": round(w, 4),
                        "height": round(h, 4),
                        "label": custom_lbl,
                        "row": row_i,
                        "col": col_i,
                        "corners": [
                            [round(x, 4), round(y, 4)],
                            [round(x + w, 4), round(y, 4)],
                            [round(x + w, 4), round(y + h, 4)],
                            [round(x, 4), round(y + h, 4)],
                        ],
                    })

        cfg = ManualGridConfig(
            is_active=True,
            is_finalized=True,
            preset=p,
            rows=r,
            cols=c,
            x_dividers=xd or [],
            y_dividers=yd or [],
            panes_metadata=panes_meta,
            mesh=mesh or [],
            zones=self._current_config.zones or {},
            rules=self._current_config.rules or {},
        )
        self.save(cfg)
        logger.info(f"🔒 Grid Layout Finalized: {r}x{c} ({len(panes_meta)} panes frozen with mesh support).")
        return cfg

    def reset_to_auto(self) -> ManualGridConfig:
        """Disable manual layout and revert to automatic dynamic discovery."""
        cfg = ManualGridConfig(is_active=False, is_finalized=False, preset="AUTO", rows=2, cols=2, x_dividers=[], y_dividers=[], panes_metadata=[], mesh=[], zones=self._current_config.zones or {}, rules=self._current_config.rules or {})
        self.save(cfg)
        return cfg

    def save(self, config: ManualGridConfig) -> None:
        """Persist config to JSON file."""
        self._current_config = config
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(asdict(config), f, indent=2)
            logger.info(f"Saved manual grid config to {self.file_path}: active={config.is_active}, finalized={config.is_finalized}, preset={config.preset}, {config.rows}x{config.cols}")
        except Exception as e:
            logger.error(f"Failed to write manual layout config to {self.file_path}: {e}")

    def load(self) -> ManualGridConfig:
        """Load config from JSON file if present."""
        if not self.file_path.exists():
            return ManualGridConfig(is_active=False, is_finalized=False, preset="AUTO", rows=2, cols=2, x_dividers=[], y_dividers=[], panes_metadata=[], mesh=[])

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_zones = data.get("zones", {})
            zones_dict: Dict[str, List[Dict[str, Any]]] = {}
            if isinstance(raw_zones, dict):
                zones_dict = {str(k): list(v) if isinstance(v, list) else [] for k, v in raw_zones.items()}
            elif isinstance(raw_zones, list):
                for z in raw_zones:
                    if isinstance(z, dict):
                        pid = z.get("pane_id", "default")
                        zones_dict.setdefault(pid, []).append(z)

            raw_rules = data.get("rules", {})
            rules_dict = dict(raw_rules) if isinstance(raw_rules, dict) else {}

            cfg = ManualGridConfig(
                is_active=bool(data.get("is_active", False)),
                is_finalized=bool(data.get("is_finalized", False)),
                preset=str(data.get("preset", "AUTO")),
                rows=int(data.get("rows", 2)),
                cols=int(data.get("cols", 2)),
                x_dividers=list(data.get("x_dividers", [])),
                y_dividers=list(data.get("y_dividers", [])),
                panes_metadata=list(data.get("panes_metadata", [])),
                mesh=list(data.get("mesh", [])),
                zones=zones_dict,
                rules=rules_dict,
            )
            return cfg
        except Exception as e:
            logger.error(f"Failed to load manual layout from {self.file_path}: {e}")
            return ManualGridConfig(is_active=False, is_finalized=False, preset="AUTO", rows=2, cols=2, x_dividers=[], y_dividers=[], panes_metadata=[], mesh=[], zones={}, rules={})

    def generate_discovered_layout(self, width: int, height: int) -> DiscoveredLayout:
        """Generate DiscoveredLayout with exact pixel bounding boxes for the given canvas dimensions."""
        cfg = self._current_config
        rows = max(1, cfg.rows)
        cols = max(1, cfg.cols)

        # If custom panes_metadata exists with exact matching count, use directly
        if cfg.panes_metadata and len(cfg.panes_metadata) == (rows * cols):
            panes: List[PaneGeometry] = []
            for idx, pm in enumerate(cfg.panes_metadata):
                px = int(round(pm.get("x", 0.0) * width))
                py = int(round(pm.get("y", 0.0) * height))
                pw = max(1, int(round(pm.get("width", 0.5) * width)))
                ph = max(1, int(round(pm.get("height", 0.5) * height)))
                panes.append(
                    PaneGeometry(
                        index=idx,
                        bbox=(px, py, pw, ph),
                        row=pm.get("row", idx // cols),
                        col=pm.get("col", idx % cols),
                        geometry_confidence=1.0,
                    )
                )
            x_seps = [int(round(x * width)) for x in cfg.x_dividers]
            y_seps = [int(round(y * height)) for y in cfg.y_dividers]
            layout_id = f"manual_{cfg.preset.lower()}_{rows}x{cols}_{len(panes)}"
            return DiscoveredLayout(
                layout_id=layout_id,
                pane_count=len(panes),
                confidence=1.0,
                panes=panes,
                x_separators=x_seps,
                y_separators=y_seps,
            )

        # Full sequence of horizontal boundary cuts (0 to height)
        y_cuts = [0] + [int(round(y * height)) for y in cfg.y_dividers] + [height]
        for i in range(1, len(y_cuts)):
            y_cuts[i] = max(y_cuts[i], y_cuts[i - 1] + 1)
        y_cuts[-1] = height

        # Full sequence of vertical boundary cuts (0 to width)
        x_cuts = [0] + [int(round(x * width)) for x in cfg.x_dividers] + [width]
        for i in range(1, len(x_cuts)):
            x_cuts[i] = max(x_cuts[i], x_cuts[i - 1] + 1)
        x_cuts[-1] = width

        panes: List[PaneGeometry] = []
        idx = 0
        for r in range(rows):
            y_top = y_cuts[r]
            y_bot = y_cuts[r + 1]
            pane_h = max(1, y_bot - y_top)

            for c in range(cols):
                x_left = x_cuts[c]
                x_right = x_cuts[c + 1]
                pane_w = max(1, x_right - x_left)

                panes.append(
                    PaneGeometry(
                        index=idx,
                        bbox=(x_left, y_top, pane_w, pane_h),
                        row=r,
                        col=c,
                        geometry_confidence=1.0,
                    )
                )
                idx += 1

        x_seps = [int(round(x * width)) for x in cfg.x_dividers]
        y_seps = [int(round(y * height)) for y in cfg.y_dividers]
        layout_id = f"manual_{cfg.preset.lower()}_{rows}x{cols}_{len(panes)}"

        return DiscoveredLayout(
            layout_id=layout_id,
            pane_count=len(panes),
            confidence=1.0,
            panes=panes,
            x_separators=x_seps,
            y_separators=y_seps,
        )
