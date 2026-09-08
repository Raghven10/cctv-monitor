# AI CCTV Physical Display Monitoring POC

A real-time, live computer vision system that observes and monitors a physical CCTV control-room display monitor using an external webcam.

## Features

- **Live Webcam Acquisition & Bounded Latency**: Continuously captures from live webcam (UVC/USB/RTSP) with a thread-safe, drop-oldest buffer ensuring zero queue backlog accumulation.
- **Physical Monitor Detection & Perspective Homography**: Automatically localizes display corners, corrects perspective distortions, and supports assisted 4-corner calibration.
- **Dynamic Layout & Pane Discovery**: Intelligently discovers 1, 2, 4, 6, 9, 12, 16, or custom CCTV pane grids without hardcoded assumptions.
- **Separation of Pane IDs & Camera Identity**: Retains spatial regions (`P01`, `P02`, ...) while dynamically tracking camera identity feeds (`CAM-01`, `GATE-EAST`, etc.).
- **Hybrid OCR + VLM Validation**: Fast text extraction via OCR and asynchronous semantic validation via Vision-Language Model interfaces (`MockVLMRecognizer`, `LocalVLMRecognizer`, `RemoteVLMRecognizer`).
- **Temporal Stabilization & Noise Rejection**: Hysteresis tracking for layout transitions and sliding observation windows for camera label stabilization.
- **Live Annotated Visualization & HUD**: Real-time overlays of pane IDs, camera identities, confidence levels, and live performance metrics.
- **Structured JSONL Diagnostic Output**: Streams machine-readable per-frame records conforming to the POC specification.
- **Extensibility**: Clean abstraction interfaces for future phases (`PersonDetector`, `Tracker`, `FaceRecognizer`, `RuleEngine`, `AlertManager`).

---

## Directory Structure

```text
cctv/
├── config/
│   ├── poc.yaml               # Application & pipeline configuration
│   └── calibration.json       # Persisted 4-point monitor calibration
├── src/
│   └── cctv_poc/
│       ├── main.py            # Main entrypoint
│       ├── config.py          # Pydantic configuration models
│       ├── video/             # Webcam capture & bounded buffer
│       ├── monitor/           # Display detection, calibration & dynamic layout
│       ├── ocr/               # Fast OCR text extraction & normalization
│       ├── vlm/               # VLM abstraction and providers
│       ├── camera/            # Spatial label association & identity fusion
│       ├── stabilization/     # Temporal layout & label hysteresis
│       ├── realtime/          # Hot path orchestrator & latency metrics
│       ├── visualization/     # Live HUD & pane overlay renderer
│       ├── extensions/        # Extension interfaces for future detection/alerts
│       └── utils/             # Logging & JSONL writer
├── scripts/
│   └── calibrate.py           # Interactive monitor corner calibration CLI
└── tests/                     # Comprehensive automated test suite
```

---

## Installation & Setup

1. **Prerequisites**: Python 3.11+
2. **Setup virtual environment**:
   ```bash
   uv venv .venv
   source .venv/bin/activate
   uv pip install -r requirements.txt
   uv pip install -e .
   ```

---

## Usage

### 1. Run Live Monitoring
```bash
# Start live webcam monitoring (device 0 by default)
.venv/bin/python -m cctv_poc.main

# Specify custom webcam index or URL
.venv/bin/python -m cctv_poc.main --device 1

# Run in headless mode (no GUI window, writes to JSONL)
.venv/bin/python -m cctv_poc.main --no-display
```

### 2. Verify with Dry-Run
```bash
.venv/bin/python -m cctv_poc.main --dry-run
```

### 3. Assisted Monitor Calibration
When automatic monitor detection requires assistance or for fixed camera setups:
```bash
.venv/bin/python scripts/calibrate.py --device 0
```
Click the 4 corners of the monitor in order:
1. Top-Left
2. Top-Right
3. Bottom-Right
4. Bottom-Left

Press `s` to save calibration to `config/calibration.json`, or `r` to reset.

### 4. Running Automated Tests
```bash
.venv/bin/pytest -v
```

---

## Configuration (`config/poc.yaml`)

```yaml
application:
  name: cctv-monitor-poc
  log_level: INFO

video:
  source_type: webcam
  device: 0
  requested_width: 3840
  requested_height: 2160
  requested_fps: 30

display:
  auto_detect: true
  calibration_mode: assisted
  calibration_file: config/calibration.json
  min_area_fraction: 0.40
  target_width: 3840
  target_height: 2160

layout:
  dynamic: true
  min_panes: 1
  max_panes: 64
  stability_frames: 10

ocr:
  enabled: true
  sample_every_n_frames: 15
  confidence_threshold: 0.60

vlm:
  enabled: true
  sample_interval_seconds: 3.0
  provider: mock # mock, local, or remote

stabilization:
  label_observations_required: 5
  layout_frames_required: 10
  change_confirmation_frames: 10

output:
  display: true
  output_directory: data/output
  jsonl: data/output/results.jsonl
```
