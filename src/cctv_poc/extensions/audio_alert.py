"""Audio buzzer and voice announcement service for unknown person intrusion alerts."""

import os
import shutil
import subprocess
import threading
import time
from typing import Optional

from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.audio_alert")


class SystemAudioAlerter:
    """
    Manages audible siren buzzer tones and text-to-speech voice announcements
    on the host system (macOS / Linux / Windows) in non-blocking background threads.
    """

    def __init__(self, cooldown_seconds: float = 3.5):
        self.cooldown_seconds = cooldown_seconds
        self._last_trigger_time = 0.0
        self._lock = threading.Lock()
        self._is_playing = False

        # Detect platform audio tools
        self._has_say = shutil.which("say") is not None
        self._has_afplay = shutil.which("afplay") is not None

    def trigger_unknown_person_alarm(self, message: str = "Unknown person detected!") -> bool:
        """
        Trigger buzzer sound followed by spoken announcement if cooldown has elapsed.
        Returns True if alarm was scheduled, False if skipped due to cooldown or already playing.
        """
        with self._lock:
            now = time.time()
            if (now - self._last_trigger_time) < self.cooldown_seconds or self._is_playing:
                return False

            self._last_trigger_time = now
            self._is_playing = True

        threading.Thread(
            target=self._play_alarm_sequence,
            args=(message,),
            daemon=True,
            name="AudioAlarmThread",
        ).start()
        return True

    def _play_alarm_sequence(self, message: str) -> None:
        """Play buzzer beep sound and speak announcement asynchronously without blocking."""
        try:
            # 1. Play alert buzzer tone (system emergency beep)
            if self._has_afplay and os.path.exists("/System/Library/Sounds/Sosumi.aiff"):
                subprocess.Popen(
                    ["/usr/bin/afplay", "/System/Library/Sounds/Sosumi.aiff"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif self._has_afplay and os.path.exists("/System/Library/Sounds/Hero.aiff"):
                subprocess.Popen(
                    ["/usr/bin/afplay", "/System/Library/Sounds/Hero.aiff"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            # Short delay between buzzer and voice announcement
            time.sleep(0.4)

            # 2. Spoken voice announcement: "Unknown person detected!"
            if self._has_say:
                subprocess.Popen(
                    ["/usr/bin/say", message],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(1.2)

            logger.info(f"🔊 Audio Alarm Triggered: '{message}'")
        except Exception as e:
            logger.warning(f"Audio alarm sequence exception: {e}")
        finally:
            with self._lock:
                self._is_playing = False


# Global singleton instance
global_audio_alerter = SystemAudioAlerter()
