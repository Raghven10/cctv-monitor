from dataclasses import dataclass, field
from typing import List, Optional, Set, Dict, Any, Tuple
from enum import Enum


class ZoneType(Enum):
    DOOR = "door"
    ENTRY = "entry"
    EXIT = "exit"
    OBJECT_BOX = "object_box"
    BOX = "box"
    RESTRICTED = "restricted"
    WORKSTATION = "workstation"
    COMPUTER = "computer"
    DESK = "desk"
    WAITING = "waiting"
    TRIPWIRE = "tripwire"
    GENERAL = "general"


@dataclass
class Zone:
    zone_id: str
    label: str
    zone_type: ZoneType
    # Normalized coordinates [x, y, w, h] (0.0 to 1.0)
    bbox: List[float]
    # List of person_ids authorized for this zone
    authorized_persons: Set[str] = field(default_factory=set)
    # Rule configurations
    alarm_on_entry: bool = True
    alarm_on_lift: bool = True
    loiter_threshold_sec: float = 10.0
    # Additional metadata for the zone
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.zone_type, str):
            val = self.zone_type.lower().strip()
            try:
                self.zone_type = ZoneType(val)
            except Exception:
                try:
                    self.zone_type = ZoneType[self.zone_type.upper()]
                except Exception:
                    self.zone_type = ZoneType.GENERAL
        if isinstance(self.authorized_persons, list):
            self.authorized_persons = set(self.authorized_persons)

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        """Check if a normalized point (x, y) is inside the zone bbox (with optional margin)."""
        if not self.bbox or len(self.bbox) < 4:
            return False
        zx, zy, zw, zh = self.bbox
        return (zx - margin) <= x <= (zx + zw + margin) and (zy - margin) <= y <= (zy + zh + margin)

    def overlaps(self, bx: float, by: float, bw: float, bh: float, margin: float = 0.0) -> bool:
        """Check if normalized bbox [bx, by, bw, bh] overlaps with zone bbox."""
        if not self.bbox or len(self.bbox) < 4:
            return False
        zx, zy, zw, zh = self.bbox
        return not (
            (bx + bw) < (zx - margin)
            or bx > (zx + zw + margin)
            or (by + bh) < (zy - margin)
            or by > (zy + zh + margin)
        )

    def overlaps_person(
        self,
        person_bbox: Tuple[float, float, float, float],
        img_w: int,
        img_h: int,
        margin: float = 0.04,
    ) -> bool:
        """Robust multi-point and spatial bounding box overlap check for a detected person."""
        if not person_bbox or len(person_bbox) < 4 or img_w <= 0 or img_h <= 0:
            return False

        px, py, pw, ph = person_bbox
        nbx = px / float(img_w)
        nby = py / float(img_h)
        nbw = pw / float(img_w)
        nbh = ph / float(img_h)

        # 1. Bounding box intersection check (with margin)
        if not self.overlaps(nbx, nby, nbw, nbh, margin=margin):
            return False

        # 2. Key point checks: Center, Feet/Base, Torso, and Head
        cx = nbx + nbw / 2.0
        cy = nby + nbh / 2.0
        feet_y = nby + nbh
        torso_y = nby + nbh * 0.4

        if self.contains(cx, cy, margin=margin):
            return True
        if self.contains(cx, feet_y, margin=margin):
            return True
        if self.contains(cx, torso_y, margin=margin):
            return True

        # 3. Intersection Area vs Zone Area ratio
        if not self.bbox or len(self.bbox) < 4:
            return False
        zx, zy, zw, zh = self.bbox
        ix1 = max(zx, nbx)
        iy1 = max(zy, nby)
        ix2 = min(zx + zw, nbx + nbw)
        iy2 = min(zy + zh, nby + nbh)

        if ix2 > ix1 and iy2 > iy1:
            inter_area = (ix2 - ix1) * (iy2 - iy1)
            zone_area = max(1e-6, zw * zh)
            person_area = max(1e-6, nbw * nbh)
            if (inter_area / zone_area > 0.08) or (inter_area / person_area > 0.15):
                return True

        return True


