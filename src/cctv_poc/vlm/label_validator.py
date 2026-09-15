import abc
import base64
import os
import re
import time
from dataclasses import dataclass
from typing import Optional
import cv2
import httpx
import numpy as np

from ..config import VLMConfig
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.vlm")


@dataclass
class VLMResult:
    """Result returned by VLM semantic label identification."""
    pane_id: str
    label: str
    confidence: float
    timestamp: float
    latency_ms: float
    is_valid: bool = True
    error_message: Optional[str] = None

@dataclass
class EventVerificationResult:
    """Result of VLM event verification."""
    is_verified: bool
    reason: str
    confidence: float
    latency_ms: float
    is_valid: bool = True
    error_message: Optional[str] = None


class VisionLabelRecognizer(abc.ABC):
    """Abstract interface for VLM label recognizers."""

    @abc.abstractmethod
    def identify_label(self, pane_image: np.ndarray, pane_id: str = "") -> VLMResult:
        """
        Identify the CCTV camera identifier or location label visible in this pane.
        Must return VLMResult with label and confidence.
        """
        pass

    @abc.abstractmethod
    def verify_event(self, image: np.ndarray, event_type: str, zone_label: str, identity: str) -> EventVerificationResult:
        """
        Verify if a candidate event is actually happening in the image.
        """
        pass


class MockVLMRecognizer(VisionLabelRecognizer):
    """Deterministic Mock VLM for testing and offline execution."""

    def __init__(self, default_confidence: float = 0.94):
        self.default_confidence = default_confidence

    def identify_label(self, pane_image: np.ndarray, pane_id: str = "") -> VLMResult:
        start_t = time.time()
        simulated_label = f"CAM-{pane_id[1:]}" if pane_id.startswith("P") else "CAM-01"
        latency = (time.time() - start_t) * 1000.0 + 5.0

        return VLMResult(
            pane_id=pane_id,
            label=simulated_label,
            confidence=self.default_confidence,
            timestamp=time.time(),
            latency_ms=latency,
            is_valid=True,
        )

    def verify_event(self, image: np.ndarray, event_type: str, zone_label: str, identity: str) -> EventVerificationResult:
        # Mock verification: simply return True for common events
        return EventVerificationResult(
            is_verified=True,
            reason=f"Mock verification confirmed {event_type} for {identity} in {zone_label}",
            confidence=self.default_confidence,
            latency_ms=10.0,
            is_valid=True,
        )


class OpenAIVLMRecognizer(VisionLabelRecognizer):
    """
    Universal OpenAI-Compatible Vision-Language Model Recognizer.
    Supports official OpenAI (gpt-4o, gpt-4o-mini), Ollama (llava, qwen2.5-vl),
    vLLM, LM Studio, LocalAI, OpenRouter, and LiteLLM endpoints.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 4.0,
        temperature: float = 0.0,
        prompt: Optional[str] = None,
    ):
        env_base_url = os.getenv("VLM_BASE_URL")
        self.base_url = (env_base_url or base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = os.getenv("OPENAI_API_KEY") or os.getenv("VLM_API_KEY") or api_key or "EMPTY"
        self.model = os.getenv("VLM_MODEL") or model or "gpt-4o-mini"
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.system_prompt = (
            "You are an automated CCTV stream camera label and location recognizer. "
            "Examine this CCTV pane image and extract the camera identifier, channel tag, or physical location label "
            "(e.g. CAM-01, ENTRANCE, GATE-EAST, LOBBY-NORTH, PARKING-B2). "
            "Reply strictly with ONLY the uppercase alphanumeric camera label (or NONE if no label is visible). Do not include any explanations or punctuation."
        )
        self.user_prompt = prompt or "Read the camera label text shown in this CCTV pane."

    def _image_to_base64(self, image: np.ndarray) -> str:
        """Convert image to JPEG base64 string."""
        if image is None or image.size == 0:
            return ""
        h, w = image.shape[:2]
        if max(h, w) > 768:
            scale = 768.0 / max(h, w)
            image = cv2.resize(image, (int(w * scale), int(h * scale)))
        _, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return base64.b64encode(buffer).decode("utf-8")

    def identify_label(self, pane_image: np.ndarray, pane_id: str = "") -> VLMResult:
        """Send pane crop to OpenAI-compatible vision chat completions endpoint."""
        start_t = time.time()
        if pane_image is None or pane_image.size == 0:
            return VLMResult(
                pane_id=pane_id,
                label="",
                confidence=0.0,
                timestamp=time.time(),
                latency_ms=0.0,
                is_valid=False,
                error_message="Empty image provided",
            )

        b64_img = self._image_to_base64(pane_image)
        endpoint = f"{self.base_url}/chat/completions"

        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key and self.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
                        },
                    ],
                },
            ],
            "temperature": self.temperature,
            "max_tokens": 25,
        }

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                latency = (time.time() - start_t) * 1000.0

                if resp.status_code != 200:
                    logger.warning(f"VLM API error {resp.status_code}: {resp.text[:200]}")
                    return VLMResult(
                        pane_id=pane_id,
                        label="",
                        confidence=0.0,
                        timestamp=time.time(),
                        latency_ms=latency,
                        is_valid=False,
                        error_message=f"HTTP {resp.status_code}: {resp.text[:100]}",
                    )

                data = resp.json()
                raw_text = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )

                # Extract camera label from VLM output
                cleaned_label = ""
                # 1. Try extracting quoted string like "CAM-05" or 'CAM-01'
                quote_match = re.search(r'["\']([A-Za-z0-9_\-\s]{2,20})["\']', raw_text)
                if quote_match:
                    cleaned_label = quote_match.group(1).strip().upper()
                else:
                    # 2. Try matching common camera label formats
                    cam_match = re.search(r'\b(CAM(?:ERA)?[-_\s]?\d+|CH(?:ANNEL)?[-_\s]?\d+|[A-Z]{2,10}[-_\s]\d{1,4}|GATE[-_\s]?[A-Z0-9]+|LOBBY[-_\s]?[A-Z0-9]*|ENTRANCE|PARKING[-_\s]?[A-Z0-9]*)\b', raw_text, re.IGNORECASE)
                    if cam_match:
                        cleaned_label = cam_match.group(1).strip().upper()
                    elif len(raw_text) <= 25:
                        cleaned_label = re.sub(r'[^A-Za-z0-9_\-\s]', '', raw_text).strip().upper()

                if cleaned_label in ["NONE", "NO LABEL", "UNKNOWN", "N/A", ""]:
                    return VLMResult(
                        pane_id=pane_id,
                        label="",
                        confidence=0.0,
                        timestamp=time.time(),
                        latency_ms=latency,
                        is_valid=False,
                    )

                return VLMResult(
                    pane_id=pane_id,
                    label=cleaned_label,
                    confidence=0.92,
                    timestamp=time.time(),
                    latency_ms=latency,
                    is_valid=True,
                )
        except Exception as e:
            latency = (time.time() - start_t) * 1000.0
            try:
                logger.warning(f"VLM inference request failed: {e}")
            except Exception:
                pass
            return VLMResult(
                pane_id=pane_id,
                label="",
                confidence=0.0,
                timestamp=time.time(),
                latency_ms=latency,
                is_valid=False,
                error_message=str(e),
            )

    def verify_event(self, image: np.ndarray, event_type: str, zone_label: str, identity: str) -> EventVerificationResult:
        """
        Semantically verify if a candidate event is actually happening using VLM.
        """
        start_t = time.time()
        if image is None or image.size == 0:
            return EventVerificationResult(
                is_verified=False,
                reason="Empty image provided",
                confidence=0.0,
                latency_ms=0.0,
                is_valid=False,
                error_message="Empty image provided",
            )

        b64_img = self._image_to_base64(image)
        endpoint = f"{self.base_url}/chat/completions"

        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key and self.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self.api_key}"

        prompt = (
            f"Analyze this image. Does it show {identity} performing the action '{event_type}' "
            f"in the area labeled '{zone_label}'? "
            "Reply strictly with 'YES' or 'NO' followed by a short one-sentence reason."
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a security event verification AI. Your task is to confirm if a detected event is actually happening based on visual evidence."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
                        },
                    ],
                },
            ],
            "temperature": self.temperature,
            "max_tokens": 50,
        }

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                latency = (time.time() - start_t) * 1000.0

                if resp.status_code != 200:
                    return EventVerificationResult(
                        is_verified=False,
                        reason=f"API Error {resp.status_code}",
                        confidence=0.0,
                        latency_ms=latency,
                        is_valid=False,
                        error_message=resp.text[:100],
                    )

                data = resp.json()
                raw_text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                is_verified = raw_text.upper().startswith("YES")
                reason = raw_text
                confidence = 0.90 if is_verified else 0.85

                return EventVerificationResult(
                    is_verified=is_verified,
                    reason=reason,
                    confidence=confidence,
                    latency_ms=latency,
                    is_valid=True,
                )
        except Exception as e:
            latency = (time.time() - start_t) * 1000.0
            return EventVerificationResult(
                is_verified=False,
                reason=str(e),
                confidence=0.0,
                latency_ms=latency,
                is_valid=False,
                error_message=str(e),
            )


class LocalVLMRecognizer(VisionLabelRecognizer):
    """Local VLM integration."""

    def __init__(self, model_path: str = ""):
        self.model_path = model_path

    def identify_label(self, pane_image: np.ndarray, pane_id: str = "") -> VLMResult:
        return MockVLMRecognizer().identify_label(pane_image, pane_id=pane_id)


class RemoteVLMRecognizer(VisionLabelRecognizer):
    """Remote HTTP REST endpoint VLM integration."""

    def __init__(self, endpoint_url: str = "http://localhost:8000/v1/vlm"):
        self.endpoint_url = endpoint_url

    def identify_label(self, pane_image: np.ndarray, pane_id: str = "") -> VLMResult:
        return MockVLMRecognizer().identify_label(pane_image, pane_id=pane_id)


def create_vlm_recognizer(config: VLMConfig) -> VisionLabelRecognizer:
    """Factory to instantiate the appropriate VLM recognizer."""
    provider = os.getenv("VLM_PROVIDER", config.provider).lower()
    if provider in ["openai", "openai-compatible", "ollama", "vllm", "openrouter"]:
        return OpenAIVLMRecognizer(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
            temperature=config.temperature,
            prompt=config.prompt,
        )
    elif provider == "local":
        return LocalVLMRecognizer()
    elif provider == "remote":
        return RemoteVLMRecognizer()
    return MockVLMRecognizer()
