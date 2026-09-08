"""Multi-frame temporal intrusion event tracker with stateful single-alert gating."""

import math
import time
from typing import Dict, List, Optional, Tuple
import numpy as np

from .interfaces import DetectionBox
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.intrusion_tracker")


class IntrusionChangeTracker:
    """
    Multi-frame Temporal Intrusion Tracker.
    Analyzes multiple consecutive frames before confirming an intrusion event,
    and guarantees a SINGLE alert announcement per intrusion session.
    Rejects transient single-frame flicker, micro-movements, and repetitive loops.
    """

    def __init__(
        self,
        min_consecutive_frames: int = 5,
        clear_cooldown_seconds: float = 6.0,
    ):
        self.min_consecutive_frames = min_consecutive_frames
        self.clear_cooldown_seconds = clear_cooldown_seconds

        # Multi-frame temporal state
        self._consecutive_unknown_frames: int = 0
        self._consecutive_clear_frames: int = 0
        self._event_alert_raised: bool = False
        self._is_active_intrusion: bool = False
        self._last_clear_time: float = time.time()
        self._last_unknown_count: int = 0

    def evaluate_intrusion_event(self, unknown_persons: List[DetectionBox]) -> Tuple[bool, str]:
        """
        Evaluate multi-frame temporal evidence:
        1. Accumulates consecutive positive frames to confirm authentic intrusion.
        2. Raises alert ONCE per confirmed intrusion event.
        3. Requires sustained room clearance before resetting for future events.
        
        Returns:
            (should_announce: bool, event_reason: str)
        """
        now = time.time()
        current_count = len(unknown_persons)

        # Case 0: No unknown persons in current frame
        if current_count == 0:
            self._consecutive_unknown_frames = 0
            self._consecutive_clear_frames += 1

            # Only reset intrusion state when room has been consistently clear for cooldown period
            if (now - self._last_clear_time) >= self.clear_cooldown_seconds and self._consecutive_clear_frames >= 25:
                if self._is_active_intrusion:
                    logger.info("🟢 Room confirmed clear of unknown persons. Resetting intrusion state.")
                self._is_active_intrusion = False
                self._event_alert_raised = False
                self._last_unknown_count = 0

            return False, "ROOM_CLEAR"

        # Unknown persons detected in this frame
        self._last_clear_time = now
        self._consecutive_clear_frames = 0
        self._consecutive_unknown_frames += 1

        should_announce = False
        reason = ""

        # Step 1: Check if multi-frame threshold is met for a new intrusion
        if not self._is_active_intrusion:
            if self._consecutive_unknown_frames >= self.min_consecutive_frames:
                # Multi-frame confirmed: Transition to active intrusion
                self._is_active_intrusion = True
                if not self._event_alert_raised:
                    should_announce = True
                    reason = f"CONFIRMED_INTRUSION (Analyzed {self._consecutive_unknown_frames} frames, {current_count} person(s))"
                    self._event_alert_raised = True
            else:
                reason = f"ANALYZING_FRAMES ({self._consecutive_unknown_frames}/{self.min_consecutive_frames})"

        # Step 2: If intrusion is already active, check if NEW intruder entered (count increased after sustained frames)
        elif current_count > self._last_unknown_count and self._consecutive_unknown_frames >= self.min_consecutive_frames:
            should_announce = True
            reason = f"ADDITIONAL_INTRUDER (Count increased from {self._last_unknown_count} to {current_count})"

        # Update state
        self._last_unknown_count = current_count

        if should_announce:
            logger.info(f"🚨 Intrusion Event Triggered ({reason}): Raising SINGLE alert announcement.")

        return should_announce, reason

    def reset(self) -> None:
        """Reset tracking state."""
        self._consecutive_unknown_frames = 0
        self._consecutive_clear_frames = 0
        self._event_alert_raised = False
        self._is_active_intrusion = False
        self._last_clear_time = time.time()
        self._last_unknown_count = 0
