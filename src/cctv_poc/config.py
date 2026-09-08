"""Configuration models, loader, and serializer for CCTV POC."""

from pathlib import Path
from typing import Any, Dict, Optional, Union
import yaml
from pydantic import BaseModel, Field


class ApplicationConfig(BaseModel):
    name: str = "cctv-monitor-poc"
    log_level: str = "INFO"


class VideoConfig(BaseModel):
    source_type: str = "webcam"  # "webcam", "file", "rtsp"
    device: Union[int, str] = 0
    requested_width: int = 3840
    requested_height: int = 2160
    requested_fps: int = 30
    buffer_size: int = 2


class DisplayConfig(BaseModel):
    auto_detect: bool = True
    calibration_mode: str = "assisted"  # "auto", "assisted", "manual"
    calibration_file: str = "config/calibration.json"
    min_area_fraction: float = 0.12
    target_width: int = 3840
    target_height: int = 2160


class LayoutConfig(BaseModel):
    dynamic: bool = True
    min_panes: int = 1
    max_panes: int = 64
    stability_frames: int = 10
    expected_aspect_ratio: str = "auto"
    edge_threshold: int = 30
    gutter_min_width: int = 2


class DetectionConfig(BaseModel):
    score_threshold: float = 0.70
    nms_threshold: float = 0.30
    min_face_size: int = 35


class BiometricsConfig(BaseModel):
    match_threshold: float = 0.48
    model_name: str = "InsightFace ArcFace ResNet-50 (512-d)"
    strict_landmark_alignment: bool = True


class AlertConfig(BaseModel):
    min_consecutive_frames: int = 5
    clear_cooldown_seconds: float = 6.0
    voice_enabled: bool = True
    buzzer_enabled: bool = True
    announcement_text: str = "Unknown person detected!"


class OCRConfig(BaseModel):
    enabled: bool = True
    sample_every_n_frames: int = 15
    confidence_threshold: float = 0.60
    engine: str = "auto"  # "auto", "easyocr", "tesseract"


class VLMConfig(BaseModel):
    enabled: bool = True
    mode: str = "validation"  # "validation", "discovery"
    sample_interval_seconds: float = 3.0
    trigger_on_low_ocr_confidence: bool = True
    trigger_on_layout_change: bool = True
    timeout_seconds: float = 3.0
    provider: str = "mock"  # "mock", "openai", "local", "remote"
    api_key: Optional[str] = None
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    prompt: Optional[str] = None
    temperature: float = 0.0


class StabilizationConfig(BaseModel):
    label_observations_required: int = 5
    layout_frames_required: int = 10
    change_confirmation_frames: int = 10


class OutputConfig(BaseModel):
    display: bool = True
    save_snapshots: bool = True
    output_directory: str = "data/output"
    jsonl: str = "data/output/results.jsonl"
    metrics_log_interval_frames: int = 30


class POCConfig(BaseModel):
    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    biometrics: BiometricsConfig = Field(default_factory=BiometricsConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    stabilization: StabilizationConfig = Field(default_factory=StabilizationConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)


def load_config(config_path: Union[str, Path] = "config/poc.yaml") -> POCConfig:
    """Load configuration from YAML file or return defaults if file not found."""
    path = Path(config_path)
    if not path.exists():
        return POCConfig()

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return POCConfig(**data)


def save_config(config: POCConfig, config_path: Union[str, Path] = "config/poc.yaml") -> None:
    """Save configuration object to YAML file."""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump()

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
