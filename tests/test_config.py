"""Tests for system configuration loading, serialization, and REST API management."""

import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from cctv_poc.config import POCConfig, load_config, save_config
from cctv_poc.web.server import app


@pytest.fixture
def client():
    return TestClient(app)


def test_config_models_defaults_and_serialization(tmp_path):
    """Verify POCConfig loads defaults, serializes, and deserializes via YAML."""
    config = POCConfig()
    assert config.application.name == "cctv-monitor-poc"
    assert config.biometrics.match_threshold == 0.48
    assert config.detection.score_threshold == 0.70
    assert config.alert.min_consecutive_frames == 5
    assert config.display.min_area_fraction == 0.12

    yaml_file = tmp_path / "test_poc.yaml"
    save_config(config, yaml_file)
    assert yaml_file.exists()

    loaded = load_config(yaml_file)
    assert loaded.biometrics.match_threshold == 0.48
    assert loaded.alert.announcement_text == "Unknown person detected!"
    assert loaded.video.requested_fps == 30


def test_get_and_update_configuration_api(client):
    """Verify GET /api/config, PUT /api/config, and POST /api/config/reset."""
    # 1. GET /api/config
    res = client.get("/api/config")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "config" in data
    cfg = data["config"]
    assert "biometrics" in cfg
    assert "detection" in cfg
    assert "alert" in cfg

    # 2. PUT /api/config with modified parameters
    cfg["biometrics"]["match_threshold"] = 0.55
    cfg["alert"]["min_consecutive_frames"] = 4
    cfg["alert"]["announcement_text"] = "Security alert! Intruder detected."

    put_res = client.put("/api/config", json=cfg)
    assert put_res.status_code == 200
    put_data = put_res.json()
    assert put_data["status"] == "ok"
    assert put_data["config"]["biometrics"]["match_threshold"] == 0.55
    assert put_data["config"]["alert"]["min_consecutive_frames"] == 4

    # Verify GET returns updated configuration
    check_res = client.get("/api/config")
    assert check_res.json()["config"]["biometrics"]["match_threshold"] == 0.55

    # 3. POST /api/config/reset to restore factory defaults
    reset_res = client.post("/api/config/reset")
    assert reset_res.status_code == 200
    reset_data = reset_res.json()
    assert reset_data["status"] == "ok"
    assert reset_data["config"]["biometrics"]["match_threshold"] == 0.48
