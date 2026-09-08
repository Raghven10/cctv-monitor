"""Temporal stabilization with hysteresis for layouts and camera labels."""

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np

from ..config import StabilizationConfig
from ..monitor.layout_discovery import DiscoveredLayout
from ..camera.identity_resolver import ResolvedPaneIdentity
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.temporal")


@dataclass
class StablePaneState:
    """Stabilized camera state for a specific pane."""
    pane_id: str
    stable_label: str
    confidence: float
    is_stable: bool
    is_unknown: bool
    observation_count: int


class LayoutStabilizer:
    """Applies temporal hysteresis to prevent layout flicker on noisy frames."""

    def __init__(self, config: StabilizationConfig):
        self.config = config
        self._active_layout: Optional[DiscoveredLayout] = None
        self._candidate_layout: Optional[DiscoveredLayout] = None
        self._candidate_consecutive_frames: int = 0
        self._layout_change_count: int = 0

    def update(self, detected_layout: DiscoveredLayout) -> Tuple[DiscoveredLayout, bool]:
        """
        Process a new layout observation.
        Returns: (active_layout, is_layout_changed_this_frame)
        """
        if self._active_layout is None:
            # First layout becomes active immediately
            self._active_layout = detected_layout
            logger.info(f"Initial active layout established: {detected_layout.layout_id} ({detected_layout.pane_count} panes)")
            return self._active_layout, True

        # Check if detected layout matches active layout
        if detected_layout.layout_id == self._active_layout.layout_id:
            self._candidate_layout = None
            self._candidate_consecutive_frames = 0
            return self._active_layout, False

        # Candidate layout detected
        if self._candidate_layout is None or self._candidate_layout.layout_id != detected_layout.layout_id:
            self._candidate_layout = detected_layout
            self._candidate_consecutive_frames = 1
        else:
            self._candidate_consecutive_frames += 1

        # Check if candidate layout has reached required confirmation frames
        if self._candidate_consecutive_frames >= self.config.layout_frames_required:
            old_id = self._active_layout.layout_id
            self._active_layout = self._candidate_layout
            self._candidate_layout = None
            self._candidate_consecutive_frames = 0
            self._layout_change_count += 1
            logger.info(
                f"Layout changed from {old_id} to {self._active_layout.layout_id} "
                f"({self._active_layout.pane_count} panes) after {self.config.layout_frames_required} confirmed frames."
            )
            return self._active_layout, True

        # Retain previous active layout while candidate is being confirmed
        return self._active_layout, False

    def reset(self) -> None:
        """Reset layout stabilizer state."""
        self._active_layout = None
        self._candidate_layout = None
        self._candidate_consecutive_frames = 0
        self._layout_change_count = 0

    @property
    def active_layout(self) -> Optional[DiscoveredLayout]:
        return self._active_layout

    @property
    def is_candidate_pending(self) -> bool:
        return self._candidate_layout is not None


class CameraStabilizer:
    """Maintains sliding observation window per pane to stabilize camera labels."""

    def __init__(self, config: StabilizationConfig):
        self.config = config
        self._pane_histories: Dict[str, deque] = {}
        self._stable_states: Dict[str, StablePaneState] = {}

    def reset(self) -> None:
        """Reset all histories (e.g. on layout change)."""
        self._pane_histories.clear()
        self._stable_states.clear()

    def update_observation(self, identity: ResolvedPaneIdentity) -> StablePaneState:
        """
        Record a new observation for a pane and return its stabilized state.
        """
        pane_id = identity.pane_id
        if pane_id not in self._pane_histories:
            self._pane_histories[pane_id] = deque(maxlen=self.config.change_confirmation_frames * 2)

        history = self._pane_histories[pane_id]
        if not identity.is_unknown and identity.final_label:
            history.append((identity.final_label, identity.confidence))

        # Check valid observations
        if not history:
            state = StablePaneState(
                pane_id=pane_id,
                stable_label="UNKNOWN",
                confidence=0.0,
                is_stable=False,
                is_unknown=True,
                observation_count=0,
            )
            self._stable_states[pane_id] = state
            return state

        # Count frequencies in recent window
        recent_window = list(history)[-self.config.change_confirmation_frames:]
        labels = [item[0] for item in recent_window]
        counts = Counter(labels)
        most_common_label, count = counts.most_common(1)[0]

        # Average confidence for most common label
        confs = [item[1] for item in recent_window if item[0] == most_common_label]
        avg_conf = float(np.mean(confs)) if confs else 0.0

        is_stable = count >= self.config.label_observations_required

        # If previous stable state exists, apply change confirmation threshold
        prev_state = self._stable_states.get(pane_id)
        if prev_state and prev_state.is_stable and prev_state.stable_label != most_common_label:
            if count < self.config.change_confirmation_frames:
                # Keep previous stable label until change is fully confirmed
                return prev_state

        state = StablePaneState(
            pane_id=pane_id,
            stable_label=most_common_label if is_stable else f"({most_common_label})",
            confidence=avg_conf,
            is_stable=is_stable,
            is_unknown=False,
            observation_count=count,
        )
        self._stable_states[pane_id] = state
        return state

    def get_stable_state(self, pane_id: str) -> Optional[StablePaneState]:
        return self._stable_states.get(pane_id)
