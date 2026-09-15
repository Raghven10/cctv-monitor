"""Unit tests for manual grid layout store and custom pane geometry adjustment."""

import os
import tempfile
import pytest
import numpy as np
from fastapi.testclient import TestClient

from cctv_poc.monitor.manual_layout import (
    ManualGridConfig,
    ManualLayoutStore,
    PRESET_CONFIGS,
    create_even_dividers,
)
from cctv_poc.web.server import app


def test_create_even_dividers():
    """Verify even divider generation across dimensions."""
    assert create_even_dividers(1) == []
    assert create_even_dividers(2) == [0.5]
    divs3 = create_even_dividers(3)
    assert len(divs3) == 2
    assert pytest.approx(divs3[0], 0.01) == 0.3333
    assert pytest.approx(divs3[1], 0.01) == 0.6667

    divs4 = create_even_dividers(4)
    assert len(divs4) == 3
    assert divs4 == [0.25, 0.5, 0.75]


def test_manual_layout_presets():
    """Verify standard grid presets generate expected rows, cols, and pane counts."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name

    try:
        store = ManualLayoutStore(config_file=tmp_path)
        assert store.is_active is False

        for preset, (expected_r, expected_c) in PRESET_CONFIGS.items():
            cfg = store.set_preset(preset)
            assert cfg.is_active is True
            assert cfg.preset == preset
            assert cfg.rows == expected_r
            assert cfg.cols == expected_c

            layout = store.generate_discovered_layout(1920, 1080)
            assert layout.pane_count == expected_r * expected_c
            assert len(layout.panes) == expected_r * expected_c

            # Verify pane index sequencing and bounding boxes
            for idx, pane in enumerate(layout.panes):
                assert pane.index == idx
                x, y, w, h = pane.bbox
                assert x >= 0 and y >= 0
                assert w > 0 and h > 0
                assert x + w <= 1920
                assert y + h <= 1080
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_manual_layout_custom_grid_and_readjustment():
    """Verify custom rows/cols and custom dragged divider positions."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = f.name

    try:
        store = ManualLayoutStore(config_file=tmp_path)

        # Custom 2x3 with non-equal divider positions
        custom_x = [0.25, 0.70]  # 3 columns with widths ~25%, 45%, 30%
        custom_y = [0.40]        # 2 rows with heights ~40%, 60%

        cfg = store.set_custom_grid(
            rows=2,
            cols=3,
            x_dividers=custom_x,
            y_dividers=custom_y,
            preset="CUSTOM",
        )

        assert cfg.is_active is True
        assert cfg.rows == 2
        assert cfg.cols == 3
        assert cfg.x_dividers == custom_x
        assert cfg.y_dividers == custom_y

        layout = store.generate_discovered_layout(1000, 1000)
        assert layout.pane_count == 6

        # Check column 0 width = 250, col 1 width = 450, col 2 width = 300
        pane0 = layout.panes[0]  # row 0, col 0
        assert pane0.bbox == (0, 0, 250, 400)

        pane1 = layout.panes[1]  # row 0, col 1
        assert pane1.bbox == (250, 0, 450, 400)

        pane2 = layout.panes[2]  # row 0, col 2
        assert pane2.bbox == (700, 0, 300, 400)

        pane3 = layout.panes[3]  # row 1, col 0
        assert pane3.bbox == (0, 400, 250, 600)

        # Reload store from disk
        store_reloaded = ManualLayoutStore(config_file=tmp_path)
        assert store_reloaded.is_active is True
        assert store_reloaded.config.x_dividers == custom_x
        assert store_reloaded.config.y_dividers == custom_y

        # Reset to auto
        reset_cfg = store_reloaded.reset_to_auto()
        assert reset_cfg.is_active is False
        assert reset_cfg.preset == "AUTO"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_manual_layout_api():
    """Verify REST API endpoints for setting, querying, and resetting manual layouts."""
    client = TestClient(app)

    try:
        # 1. Set 2x2 Preset
        resp = client.post("/api/set_manual_layout", json={"preset": "2X2"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["config"]["is_active"] is True
        assert data["config"]["preset"] == "2X2"
        assert data["config"]["rows"] == 2
        assert data["config"]["cols"] == 2

        # 2. Get manual layout
        resp_get = client.get("/api/get_manual_layout")
        assert resp_get.status_code == 200
        assert resp_get.json()["config"]["is_active"] is True

        # 3. Set custom dragged dividers
        resp_custom = client.post(
            "/api/set_manual_layout",
            json={
                "preset": "CUSTOM",
                "rows": 2,
                "cols": 2,
                "x_dividers": [0.45],
                "y_dividers": [0.55],
            },
        )
        assert resp_custom.status_code == 200
        cfg = resp_custom.json()["config"]
        assert cfg["x_dividers"] == [0.45]
        assert cfg["y_dividers"] == [0.55]

        # 4. Reset to auto
        resp_reset = client.post("/api/reset_manual_layout")
        assert resp_reset.status_code == 200
        assert resp_reset.json()["config"]["is_active"] is False
        assert resp_reset.json()["config"]["preset"] == "AUTO"
    finally:
        client.post("/api/reset_manual_layout")

