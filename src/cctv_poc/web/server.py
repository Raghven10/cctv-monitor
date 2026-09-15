"""Web Server and Browser-Webcam Dashboard for AI CCTV Physical Display Monitor.

Architecture:
  The browser opens the webcam via getUserMedia(), captures JPEG frames,
  and streams them to the server over a WebSocket connection at /ws/stream.
  The server runs face detection & recognition on each frame and returns
  JSON detections that the browser renders as canvas overlays.
  No server-side camera hardware or /dev/video* device is required.
"""

import asyncio
import base64
import io
import json
import time
from typing import Any, Dict, List, Optional
import cv2
import numpy as np
from pathlib import Path
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import POCConfig, load_config, save_config
from ..db.config import init_db
from ..realtime.pipeline import RealtimePipeline
from ..video.source import FrameData
from ..utils.logging import setup_logger
from ..visualization.renderer import VisualRenderer

logger = setup_logger("cctv_poc.web")

# Ensure database tables and migrations are initialized
init_db()


def apply_runtime_config(new_config: POCConfig) -> None:
    """Apply configuration changes to live running pipeline components in real-time."""
    pipeline.config = new_config

    # 1. Display detector
    pipeline.display_detector.config = new_config.display
    pipeline.display_detector.min_area_fraction = new_config.display.min_area_fraction

    # 2. Layout discoverer
    pipeline.layout_discoverer.config = new_config.layout

    # 3. Person detector thresholds
    if hasattr(pipeline.person_detector, "score_threshold"):
        pipeline.person_detector.score_threshold = new_config.detection.score_threshold
        yunet = getattr(pipeline.person_detector, "yunet_detector", None)
        if yunet is None and hasattr(pipeline.person_detector, "_fallback_yunet"):
            yunet = getattr(pipeline.person_detector._fallback_yunet, "yunet_detector", None)
        if yunet is not None and hasattr(yunet, "setScoreThreshold"):
            yunet.setScoreThreshold(new_config.detection.score_threshold)
    if hasattr(pipeline.person_detector, "nms_threshold"):
        pipeline.person_detector.nms_threshold = new_config.detection.nms_threshold
        yunet = getattr(pipeline.person_detector, "yunet_detector", None)
        if yunet is None and hasattr(pipeline.person_detector, "_fallback_yunet"):
            yunet = getattr(pipeline.person_detector._fallback_yunet, "yunet_detector", None)
        if yunet is not None and hasattr(yunet, "setNMSThreshold"):
            yunet.setNMSThreshold(new_config.detection.nms_threshold)

    # 4. Biometrics matching threshold
    global_identity_registry.match_threshold = new_config.biometrics.match_threshold

    # 5. Intrusion tracker
    pipeline.intrusion_tracker.min_consecutive_frames = new_config.alert.min_consecutive_frames
    pipeline.intrusion_tracker.clear_cooldown_seconds = new_config.alert.clear_cooldown_seconds

    # 6. Audio alerter
    global_audio_alerter.voice_enabled = new_config.alert.voice_enabled
    global_audio_alerter.buzzer_enabled = new_config.alert.buzzer_enabled

    logger.info("✅ Applied runtime configuration updates to all active components")

try:
    from fastapi_standalone_docs import StandaloneDocs
    app = FastAPI(
        title="AI CCTV Physical Display Monitoring",
        description="Real-Time CCTV Wall & Intrusion Monitoring REST + Streaming API (Airgapped Ready)",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    StandaloneDocs(app)
except ImportError:
    from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
    app = FastAPI(
        title="AI CCTV Physical Display Monitoring",
        description="Real-Time CCTV Wall & Intrusion Monitoring REST + Streaming API (Airgapped Ready)",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} - Swagger UI (Offline)",
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        )

    @app.get("/redoc", include_in_schema=False)
    async def redoc_html():
        return get_redoc_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} - ReDoc (Offline)",
            redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js",
        )

# Enable CORS for external framework dev servers (React/Vite, Angular, SvelteKit, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Prevent browser caching of static assets during development/deployment."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# Global pipeline instance — no server-side camera started; browser streams frames via WebSocket
config = load_config("config/poc.yaml")
pipeline = RealtimePipeline(config)
renderer = VisualRenderer()

# Web state
web_state = {
    "view_mode": "original",
    "pipeline_mode": "CCTV_ONLY",  # Default: CCTV_ONLY | ROOM_ONLY | AUTO
    "grid_preset": "2X2",          # Default: 2X2 | 3X3 | 4X4 | 2X3 | 1X2 | AUTO
    "last_result": None,
    "frame_count": 0,
    "connected_clients": 0,
}


def _jpeg_to_frame(jpeg_bytes: bytes, frame_index: int) -> Optional[FrameData]:
    """Decode a JPEG byte blob (from browser) into a FrameData object for the pipeline."""
    try:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        return FrameData(
            frame_index=frame_index,
            timestamp=time.time(),
            image=img,
            width=img.shape[1],
            height=img.shape[0],
            capture_fps=0.0,
        )
    except Exception as e:
        logger.warning(f"Failed to decode JPEG from browser: {e}")
        return None


def _result_to_dict(result, view_mode: str) -> dict:
    """Serialize a ProcessedFrameResult to a JSON-safe dict for the browser."""
    h_raw, w_raw = result.raw_image.shape[:2] if getattr(result, "raw_image", None) is not None else (720, 1280)
    h_rect, w_rect = result.rectified_image.shape[:2] if getattr(result, "rectified_image", None) is not None else (720, 1280)

    persons_info = []
    for idx, p in enumerate(result.detected_persons or []):
        bx, by, bw, bh = p.bbox
        persons_info.append({
            "person_id": p.person_id or f"person_{idx}",
            "bbox": list(p.bbox),
            "norm_bbox": [round(bx / float(w_raw), 4), round(by / float(h_raw), 4), round(bw / float(w_raw), 4), round(bh / float(h_raw), 4)],
            "confidence": round(p.confidence, 3),
            "class_name": p.class_name,
            "is_known": p.is_known,
            "person_name": p.person_name or "Unknown Person",
            "tag": p.tag or "Intruder",
            "snapshot_base64": p.snapshot_base64 or "",
        })

    # Build pane info for CCTV grid mode
    pane_states_data = getattr(result, "pane_states", {}) or {}
    is_finalized = bool(getattr(result, "is_finalized", False))

    panes_info = []
    for p in result.panes:
        pid = p.pane_id
        ident = result.pane_identities.get(pid)
        state = result.stable_states.get(pid)
        p_state = pane_states_data.get(pid, {})
        px, py, pw, ph = p.bbox
        panes_info.append({
            "pane_id": pid,
            "bbox": list(p.bbox),
            "norm_bbox": [round(px / float(w_rect), 4), round(py / float(h_rect), 4), round(pw / float(w_rect), 4), round(ph / float(h_rect), 4)],
            "row": p.row,
            "col": p.col,
            "camera_label": p_state.get("label") or (state.stable_label if (state and state.stable_label != "UNKNOWN") else f"CAM-{p.index+1:02d}"),
            "is_stable": state.is_stable if state else True,
            "ocr_confidence": round(ident.ocr_confidence, 2) if ident else 0.95,
            "vlm_confidence": round(ident.vlm_confidence, 2) if ident else 0.95,
            "geometry_confidence": round(p.geometry_confidence, 2),
            "state": p_state.get("state", "MONITORING"),
            "person_count": p_state.get("person_count", 0),
            "has_unknown": p_state.get("has_unknown", False),
            "has_known": p_state.get("has_known", False),
            "alert_status": p_state.get("alert_status", "NO_ALERT"),
            "persons": p_state.get("persons", []),
            "summary": p_state.get("summary", "No person detected"),
            "last_event": p_state.get("last_event", ""),
            "last_event_time": p_state.get("last_event_time", ""),
        })

    events_data = []
    for ev in (getattr(result, "events", []) or []):
        events_data.append({
            "pane_id": ev.pane_id,
            "zone_id": ev.zone_id,
            "event_type": ev.event_type,
            "track_id": ev.track_id,
            "person_id": ev.person_id,
            "identity": ev.identity,
            "authorization_status": ev.authorization_status,
            "confidence": round(ev.confidence, 2),
            "description": ev.description,
            "severity": ev.severity.value if hasattr(ev.severity, "value") else str(ev.severity),
            "is_alarm": getattr(ev, "is_alarm", False),
            "timestamp": ev.timestamp,
        })

    metrics = result.metrics
    return {
        "frame_index": result.frame_index,
        "mode": result.mode,
        "layout_id": result.layout.layout_id,
        "pane_count": result.layout.pane_count,
        "layout_confidence": round(result.layout.confidence, 2),
        "is_layout_changed": result.is_layout_changed,
        "is_finalized": is_finalized,
        "view_mode": view_mode,
        "alerts": list(result.alerts or []),
        "events": events_data,
        "detected_persons": persons_info,
        "panes": panes_info,
        "pane_states": pane_states_data,
        "should_announce_audio": getattr(result, "should_announce_audio", False),
        "intrusion_event_reason": getattr(result, "intrusion_event_reason", ""),
        "metrics": {
            "processing_fps": round(metrics.processing_fps, 1),
            "processing_latency_ms": round(metrics.processing_latency_ms, 1),
            "end_to_end_latency_ms": round(metrics.end_to_end_latency_ms, 1),
        },
    }



@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    """Browser-side webcam streaming endpoint with non-blocking latest-frame strategy.

    Prevents frame backlog by dropping stale frames when processing capacity is busy,
    guaranteeing real-time, zero-latency feedback to the browser UI.
    """
    await websocket.accept()
    web_state["connected_clients"] += 1
    logger.info(f"WebSocket client connected. Active clients: {web_state['connected_clients']}")

    loop = asyncio.get_running_loop()
    latest_jpeg_bytes: Optional[bytes] = None
    frame_counter = 0
    client_active = True
    new_frame_event = asyncio.Event()

    async def pipeline_worker():
        nonlocal latest_jpeg_bytes, frame_counter
        while client_active:
            await new_frame_event.wait()
            new_frame_event.clear()

            curr_bytes = latest_jpeg_bytes
            latest_jpeg_bytes = None
            if curr_bytes is None:
                continue

            frame_counter += 1
            web_state["frame_count"] += 1
            idx = frame_counter

            frame_data = _jpeg_to_frame(curr_bytes, idx)
            if frame_data is None:
                continue

            try:
                # Offload CPU/model work to thread pool executor
                result = await loop.run_in_executor(
                    None,
                    pipeline.process_frame_data,
                    frame_data,
                    web_state.get("pipeline_mode", "CCTV_ONLY"),
                    web_state.get("grid_preset", "AUTO"),
                )
                web_state["last_result"] = result
                payload = _result_to_dict(result, web_state["view_mode"])
                await websocket.send_text(json.dumps(payload))
            except Exception as exc:
                logger.error(f"Pipeline processing error on frame {idx}: {exc}", exc_info=True)

    worker_task = asyncio.create_task(pipeline_worker())

    try:
        while True:
            jpeg_bytes = await websocket.receive_bytes()
            latest_jpeg_bytes = jpeg_bytes
            new_frame_event.set()
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    except Exception as exc:
        logger.error(f"WebSocket stream error: {exc}")
    finally:
        client_active = False
        new_frame_event.set()
        worker_task.cancel()
        web_state["connected_clients"] = max(0, web_state["connected_clients"] - 1)


class FrameRequest(BaseModel):
    """Single JPEG frame as base64 for HTTP-based frame processing."""
    image_base64: str
    view_mode: str = "original"


@app.post("/api/process_frame")
def process_frame_http(req: FrameRequest):
    """Process a single JPEG frame sent as base64 via HTTP POST.

    Useful for non-WebSocket clients or debugging. For live streaming,
    prefer the WebSocket endpoint /ws/stream which has lower overhead.
    """
    try:
        jpeg_bytes = base64.b64decode(req.image_base64)
    except Exception:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    frame_index = web_state["frame_count"] + 1
    frame_data = _jpeg_to_frame(jpeg_bytes, frame_index)
    if frame_data is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Could not decode JPEG image")

    web_state["frame_count"] += 1
    result = pipeline.process_frame_data(frame_data, view_mode=req.view_mode)
    web_state["last_result"] = result
    return _result_to_dict(result, req.view_mode)


@app.get("/api/status")
def get_status():
    """Returns real-time telemetry and pipeline status.

    When no browser is connected (no frames received yet), returns
    'waiting' status. Once the browser opens the webcam and starts
    streaming frames via /ws/stream, status becomes 'active'.
    """
    res = web_state.get("last_result")
    connected = web_state.get("connected_clients", 0)
    if res is None:
        return {
            "status": "waiting" if connected == 0 else "initializing",
            "connected_clients": connected,
            "message": "Open the dashboard in a browser to start — webcam runs in the browser, not the server.",
            "alerts": [],
            "pane_count": 0,
            "layout_id": "pending",
            "layout_confidence": 0.0,
            "metrics": {
                "processing_fps": 0.0,
                "processing_latency_ms": 0.0,
                "end_to_end_latency_ms": 0.0,
            },
            "panes": [],
        }

    panes_info = []
    for p in res.panes:
        pid = p.pane_id
        ident = res.pane_identities.get(pid)
        state = res.stable_states.get(pid)
        panes_info.append({
            "pane_id": pid,
            "bbox": list(p.bbox),
            "camera_label": state.stable_label if state else "UNKNOWN",
            "is_stable": state.is_stable if state else False,
            "ocr_confidence": round(ident.ocr_confidence, 2) if ident else 0.0,
            "vlm_confidence": round(ident.vlm_confidence, 2) if ident else 0.0,
            "geometry_confidence": round(p.geometry_confidence, 2),
        })

    metrics = res.metrics
    active_alerts = list(res.alerts or [])

    persons_info = [
        {
            "person_id": p.person_id or f"person_{idx}",
            "bbox": list(p.bbox),
            "confidence": p.confidence,
            "class_name": p.class_name,
            "is_known": p.is_known,
            "person_name": p.person_name or "Unknown Person",
            "tag": p.tag or "Intruder",
            "snapshot_base64": p.snapshot_base64 or "",
        }
        for idx, p in enumerate(res.detected_persons or [])
    ]

    return {
        "frame_index": res.frame_index,
        "status": "active",
        "connected_clients": web_state.get("connected_clients", 0),
        "mode": res.mode,
        "layout_id": res.layout.layout_id,
        "pane_count": res.layout.pane_count,
        "layout_confidence": round(res.layout.confidence, 2),
        "is_layout_changed": res.is_layout_changed,
        "view_mode": web_state["view_mode"],
        "alerts": active_alerts,
        "detected_persons": persons_info,
        "should_announce_audio": getattr(res, "should_announce_audio", False),
        "intrusion_event_reason": getattr(res, "intrusion_event_reason", ""),
        "metrics": {
            "processing_fps": round(metrics.processing_fps, 1),
            "processing_latency_ms": round(metrics.processing_latency_ms, 1),
            "end_to_end_latency_ms": round(metrics.end_to_end_latency_ms, 1),
        },
        "panes": panes_info,
    }


import os
from dataclasses import asdict
from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db.config import get_db
from ..db.models import ActivityEventModel, KnownPersonModel
from ..db.schemas import (
    ActivityEventResponse,
    EventListResponse,
    KnownPersonCreate,
    KnownPersonListResponse,
    KnownPersonManualCreate,
    KnownPersonResponse,
    TestAlarmResponse,
    ViewModeRequest,
)
from ..extensions.audio_alert import global_audio_alerter
from ..extensions.event_logger import global_event_db
from ..extensions.identity_registry import global_identity_registry


@app.post("/api/set_view_mode")
def set_view_mode(req: ViewModeRequest):
    global web_state
    web_state["view_mode"] = req.view_mode
    logger.info(f"Switched view mode to: {req.view_mode}")
    return {"status": "ok", "view_mode": req.view_mode}


class PipelineModeRequest(BaseModel):
    mode: str  # AUTO, ROOM_ONLY, CCTV_ONLY


@app.post("/api/set_pipeline_mode")
def set_pipeline_mode(req: PipelineModeRequest):
    """Switch between Live Room Surveillance, CCTV Grid Monitoring, or Auto-detect."""
    allowed = {"AUTO", "ROOM_ONLY", "CCTV_ONLY"}
    mode = req.mode.upper()
    if mode not in allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid mode. Must be one of: {', '.join(allowed)}")
    web_state["pipeline_mode"] = mode
    logger.info(f"Pipeline mode switched to: {mode}")
    return {"status": "ok", "pipeline_mode": mode}


class SetManualLayoutRequest(BaseModel):
    preset: Optional[str] = "AUTO"  # AUTO, 1X1, 1X2, 2X1, 2X2, 2X3, 3X2, 3X3, 4X4, CUSTOM
    rows: Optional[int] = 2
    cols: Optional[int] = 2
    x_dividers: Optional[List[float]] = None
    y_dividers: Optional[List[float]] = None


@app.post("/api/set_manual_layout")
def set_manual_layout(req: SetManualLayoutRequest):
    """Set custom grid layout with arbitrary rows/cols and draggable divider positions."""
    preset = (req.preset or "CUSTOM").upper().strip()
    if preset == "AUTO":
        cfg = pipeline.reset_manual_layout()
    elif req.x_dividers is not None or req.y_dividers is not None:
        cfg = pipeline.set_manual_custom_grid(
            rows=req.rows or 2,
            cols=req.cols or 2,
            x_dividers=req.x_dividers,
            y_dividers=req.y_dividers,
            preset=preset if preset != "AUTO" else "CUSTOM",
        )
    elif preset in {"1X1", "1X2", "2X1", "2X2", "2X3", "3X2", "3X3", "4X4"}:
        cfg = pipeline.set_manual_preset(preset)
    else:
        cfg = pipeline.set_manual_custom_grid(
            rows=req.rows or 2,
            cols=req.cols or 2,
            x_dividers=None,
            y_dividers=None,
            preset=preset,
        )

    web_state["grid_preset"] = cfg.preset
    return {"status": "ok", "config": asdict(cfg)}


@app.get("/api/get_manual_layout")
def get_manual_layout():
    """Retrieve active manual grid configuration and divider positions."""
    cfg = pipeline.get_manual_layout()
    return {"status": "ok", "config": asdict(cfg)}


@app.post("/api/reset_manual_layout")
def reset_manual_layout():
    """Revert grid layout to dynamic AI auto-discovery."""
    cfg = pipeline.reset_manual_layout()
    web_state["grid_preset"] = "AUTO"
    return {"status": "ok", "config": asdict(cfg)}


class GridPresetRequest(BaseModel):
    grid_preset: str  # AUTO, 2X2, 3X3, 4X4, 2X3, 1X2


@app.post("/api/set_grid_preset")
def set_grid_preset(req: GridPresetRequest):
    """Select active CCTV grid layout preset."""
    allowed = {"AUTO", "2X2", "3X3", "4X4", "2X3", "3X2", "1X2", "2X1", "1X1", "3X4", "4X3"}
    preset = req.grid_preset.upper().strip()
    if preset not in allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid grid preset. Must be one of: {', '.join(allowed)}")
    web_state["grid_preset"] = preset
    if preset == "AUTO":
        pipeline.reset_manual_layout()
    else:
        pipeline.set_manual_preset(preset)
    logger.info(f"CCTV Grid layout preset set to: {preset}")
    return {"status": "ok", "grid_preset": preset}


class FinalizeGridRequest(BaseModel):
    rows: Optional[int] = None
    cols: Optional[int] = None
    x_dividers: Optional[List[float]] = None
    y_dividers: Optional[List[float]] = None
    preset: Optional[str] = None
    pane_labels: Optional[Dict[str, str]] = None
    panes_metadata: Optional[List[Dict[str, Any]]] = None
    mesh: Optional[List[List[List[float]]]] = None


class SaveZonesRequest(BaseModel):
    pane_id: str
    zones: List[Dict[str, Any]]


class SaveRulesRequest(BaseModel):
    pane_id: str
    rules: Dict[str, Any]


@app.post("/api/save_zones")
def save_zones(req: SaveZonesRequest):
    """Save ROI/Zone definitions for a specific CCTV pane."""
    try:
        cfg = pipeline.manual_layout_store.config
        # Update the zones mapping: {pane_id: [zones]}
        if cfg.zones is None:
            cfg.zones = {}
        cfg.zones[req.pane_id] = req.zones
        pipeline.manual_layout_store.save(cfg)
        logger.info(f"✅ Saved {len(req.zones)} zones for pane {req.pane_id}")
        return {"status": "ok", "message": f"Zones for {req.pane_id} saved successfully"}
    except Exception as e:
        logger.error(f"Failed to save zones for {req.pane_id}: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/save_rules")
def save_rules(req: SaveRulesRequest):
    """Save event rule configurations for a specific CCTV pane."""
    try:
        cfg = pipeline.manual_layout_store.config
        if cfg.rules is None:
            cfg.rules = {}
        cfg.rules[req.pane_id] = req.rules

        pipeline.manual_layout_store.save(cfg)
        logger.info(f"✅ Saved rules for pane {req.pane_id}: {req.rules}")
        return {"status": "ok", "message": f"Rules for {req.pane_id} saved successfully"}
    except Exception as e:
        logger.error(f"Failed to save rules for {req.pane_id}: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/api/finalize_grid")
def finalize_grid(req: FinalizeGridRequest):
    """Freeze the selected grid configuration, save normalized coordinates, and enter pane monitoring."""
    cfg = pipeline.finalize_grid(
        rows=req.rows,
        cols=req.cols,
        x_dividers=req.x_dividers,
        y_dividers=req.y_dividers,
        preset=req.preset,
        pane_labels=req.pane_labels,
        panes_metadata=req.panes_metadata,
        mesh=req.mesh,
    )
    web_state["grid_preset"] = cfg.preset
    logger.info(f"🔒 Grid layout finalized via API: {cfg.rows}x{cfg.cols}, preset={cfg.preset}")
    return {"status": "ok", "config": asdict(cfg)}


@app.get("/api/pane_events/{pane_id}")
def get_pane_events(pane_id: str, limit: int = 25):
    """Retrieve structured recent activity/event history for a specific CCTV pane."""
    from ..extensions.pane_activity_tracker import global_pane_activity_tracker
    events = global_pane_activity_tracker.get_pane_events(pane_id, limit=limit)
    return {"status": "ok", "pane_id": pane_id, "events": events}



class CalibrateDisplayRequest(BaseModel):
    corners: List[List[float]]  # [[x0, y0], [x1, y1], [x2, y2], [x3, y3]]
    width: int
    height: int


@app.post("/api/calibrate_display")
def calibrate_display(req: CalibrateDisplayRequest):
    """Save calibrated 4 corners for physical display monitor perspective correction."""
    if len(req.corners) != 4:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Must provide exactly 4 corners [TL, TR, BR, BL]")
    arr = np.array(req.corners, dtype=np.float32)
    pipeline.display_detector.calibration_store.save_corners(
        arr,
        source_width=req.width,
        source_height=req.height,
        target_width=pipeline.config.display.target_width,
        target_height=pipeline.config.display.target_height,
    )
    pipeline.rescan_layout()
    logger.info(f"✅ Display 4-corner calibration saved for frame {req.width}x{req.height}")
    return {"status": "ok", "message": "Display calibration saved and applied"}


@app.get("/api/get_calibration")
def get_calibration():
    """Retrieve existing display corner calibration if available."""
    store = pipeline.display_detector.calibration_store
    if store.file_path.exists():
        try:
            with open(store.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {"status": "ok", "is_calibrated": True, "data": data}
        except Exception as e:
            logger.error(f"Failed to read calibration file: {e}")
    return {"status": "ok", "is_calibrated": False, "data": None}


@app.post("/api/reset_calibration")
def reset_calibration():
    """Reset physical display monitor calibration and return to auto-detection."""
    store = pipeline.display_detector.calibration_store
    if store.file_path.exists():
        try:
            store.file_path.unlink()
        except Exception:
            pass
    pipeline.rescan_layout()
    logger.info("Display calibration reset to auto-detection.")
    return {"status": "ok", "message": "Display calibration reset to auto-detect"}


@app.post("/api/rescan_layout")
@app.post("/api/rescan_grid")
def rescan_layout():
    """Trigger an immediate re-scan and discovery of the CCTV TV display and grid panes."""
    pipeline.rescan_layout()
    return {"status": "ok", "message": "Grid layout rescan initiated"}


@app.post("/api/test_alarm", response_model=TestAlarmResponse)
def test_alarm():
    """Trigger an immediate host audio buzzer alarm and voice announcement."""
    triggered = global_audio_alerter.trigger_unknown_person_alarm("Unknown person detected!")
    return {"status": "ok", "triggered": triggered, "message": "Unknown person detected!"}


@app.post("/api/register_person")
def register_person(req: KnownPersonCreate, db: Session = Depends(get_db)):
    """Register and tag a detected person as known/authorized with database persistence and multi-frame support."""
    # Check if multiple images were supplied
    if req.images_base64 and len(req.images_base64) > 0:
        images = []
        for b64 in req.images_base64:
            decoded = global_identity_registry.base64_to_image(b64)
            if decoded is not None:
                images.append(decoded)
        if not images:
            raise HTTPException(status_code=400, detail="Could not decode any valid face images")
        record = global_identity_registry.register_person_from_images(
            name=req.name,
            images=images,
            tag=req.tag,
            person_id=req.person_id,
        )
        if record is None:
            raise HTTPException(status_code=400, detail="Failed to extract facial features from provided images")
        return {"status": "ok", "person": asdict(record)}

    img = global_identity_registry.base64_to_image(req.snapshot_base64) if req.snapshot_base64 else None

    # Track ID could be in req.track_id or req.person_id (e.g. trk_xxxx)
    track_id = req.track_id
    if not track_id and req.person_id and req.person_id.startswith("trk_"):
        track_id = req.person_id

    if img is None and not track_id:
        raise HTTPException(status_code=400, detail="Invalid snapshot image data or track ID")

    record = global_identity_registry.register_person(
        name=req.name,
        crop=img,
        tag=req.tag,
        person_id=req.person_id,
        track_id=track_id,
    )
    return {"status": "ok", "person": asdict(record)}


@app.post("/api/register_person_manual")
def register_person_manual(req: KnownPersonManualCreate, db: Session = Depends(get_db)):
    """Register a person manually using one or more uploaded photos with multi-pose biometric enrollment."""
    images = []
    for b64 in req.images_base64:
        decoded = global_identity_registry.base64_to_image(b64)
        if decoded is not None:
            images.append(decoded)

    if not images:
        raise HTTPException(status_code=400, detail="No valid images provided for manual registration")

    record = global_identity_registry.register_person_from_images(
        name=req.name,
        images=images,
        tag=req.tag,
        person_id=req.person_id,
    )
    if record is None:
        raise HTTPException(
            status_code=400,
            detail="Could not detect or extract facial features from the uploaded image(s). Please use clear front-facing face photos."
        )

    return {"status": "ok", "person": asdict(record)}


@app.get("/api/known_persons")
def get_known_persons(db: Session = Depends(get_db)):
    """Get list of all registered known persons."""
    persons = global_identity_registry.list_persons()
    return {"status": "ok", "count": len(persons), "persons": persons}


@app.delete("/api/known_persons/{person_id}")
def delete_known_person(person_id: str, db: Session = Depends(get_db)):
    """Delete a registered known person identity."""
    success = global_identity_registry.delete_person(person_id)
    if success:
        from cctv_poc.extensions.person_detector import global_face_track_buffer
        global_face_track_buffer.clear_person_binding(person_id)
    return {"status": "ok", "deleted": success}

    @app.put("/api/known_persons/{person_id}/poi")
    def toggle_person_poi(person_id: str, is_poi: bool, db: Session = Depends(get_db)):
        """Toggle 'Person of Interest' status for a known identity."""
        success = global_identity_registry.set_poi_status(person_id, is_poi)
        if not success:
            raise HTTPException(status_code=404, detail="Person not found in registry")
        return {"status": "ok", "person_id": person_id, "is_poi": is_poi}

    @app.get("/api/persons/search")
    def search_person(name: str, db: Session = Depends(get_db)):
        """Search for a person by name and retrieve their identity and last seen location."""
        person = global_identity_registry.get_person_by_name(name)
        if not person:
            raise HTTPException(status_code=404, detail=f"Person '{name}' not found in registry")

        last_seen = global_identity_registry.get_person_last_seen(person.person_id)

        return {
            "status": "ok",
            "person": {
                "person_id": person.person_id,
                "name": person.name,
                "tag": person.tag,
                "is_poi": person.is_poi,
                "snapshot_base64": person.snapshot_base64,
            },
            "last_seen": last_seen
        }


@app.post("/api/known_persons/clear")
def clear_known_persons(db: Session = Depends(get_db)):
    """Clear all registered known person profiles and reset active face track bindings."""
    global_identity_registry.clear_all()
    from cctv_poc.extensions.person_detector import global_face_track_buffer
    global_face_track_buffer.clear()
    return {"status": "ok", "message": "All registered identities cleared"}


@app.get("/api/events")
def get_events(
    limit: int = 60,
    severity: Optional[str] = None,
    event_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Retrieve recorded unusual events and activity logs from database."""
    events = global_event_db.get_events(limit=limit, severity=severity, event_type=event_type)
    return {"status": "ok", "count": len(events), "events": events}


@app.get("/api/events/{event_id}")
def get_single_event(event_id: str, db: Session = Depends(get_db)):
    """Get details of a specific unusual event."""
    event = global_event_db.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"status": "ok", "event": event}


@app.get("/api/events/{event_id}/snapshot")
def get_event_snapshot(event_id: str, db: Session = Depends(get_db)):
    """Serve full-resolution JPEG image for an unusual event frame."""
    event = global_event_db.get_event(event_id)
    if not event or not event.get("image_path") or not os.path.exists(event["image_path"]):
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return FileResponse(event["image_path"], media_type="image/jpeg")


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str, db: Session = Depends(get_db)):
    """Delete a specific unusual event and its snapshot image."""
    success = global_event_db.delete_event(event_id)
    return {"status": "ok", "deleted": success}


@app.post("/api/events/clear")
def clear_events(db: Session = Depends(get_db)):
    """Clear all unusual event logs and images from DB."""
    global_event_db.clear_all()
    return {"status": "ok", "message": "All events cleared"}


@app.get("/api/config")
def get_configuration():
    """Retrieve full active system configuration."""
    return {"status": "ok", "config": pipeline.config.model_dump()}


@app.put("/api/config")
def update_configuration(new_config: POCConfig, db: Session = Depends(get_db)):
    """Update system configuration live in runtime memory and persist to poc.yaml."""
    try:
        # 1. Apply changes to active in-memory components
        apply_runtime_config(new_config)

        # 2. Save to YAML file
        save_config(new_config, "config/poc.yaml")

        # 3. Log audit event to database
        global_event_db.log_unusual_frame(
            event_type="CONFIG_UPDATED",
            severity="INFO",
            description="System configuration updated via web dashboard settings",
            frame_image=None,
            debounce_seconds=1.0,
            metadata={"source": "settings_ui"},
        )

        return {"status": "ok", "message": "Configuration saved and applied live", "config": new_config.model_dump()}
    except Exception as e:
        logger.error(f"Failed to update configuration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/reset")
def reset_configuration(db: Session = Depends(get_db)):
    """Reset system configuration to default values and apply live."""
    try:
        default_config = POCConfig()
        apply_runtime_config(default_config)
        save_config(default_config, "config/poc.yaml")

        global_event_db.log_unusual_frame(
            event_type="CONFIG_RESET",
            severity="WARNING",
            description="System configuration reset to default factory settings",
            frame_image=None,
            debounce_seconds=1.0,
        )

        return {"status": "ok", "message": "Configuration reset to defaults", "config": default_config.model_dump()}
    except Exception as e:
        logger.error(f"Failed to reset configuration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=FileResponse)
def index_page():
    """Main dashboard entrypoint serving decoupled static HTML."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse("<h1>AI CCTV Dashboard Static Files Not Found</h1>", status_code=404)

