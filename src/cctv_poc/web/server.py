"""Web Server and Browser-Webcam Dashboard for AI CCTV Physical Display Monitor.

Architecture:
  The browser opens the webcam via getUserMedia(), captures JPEG frames,
  and streams them to the server over a WebSocket connection at /ws/stream.
  The server runs face detection & recognition on each frame and returns
  JSON detections that the browser renders as canvas overlays.
  No server-side camera hardware or /dev/video* device is required.
"""

import base64
import io
import json
import time
from typing import Dict, List, Optional
import cv2
import numpy as np
from pathlib import Path
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import POCConfig, load_config, save_config
from ..realtime.pipeline import RealtimePipeline
from ..video.source import FrameData
from ..utils.logging import setup_logger
from ..visualization.renderer import VisualRenderer

logger = setup_logger("cctv_poc.web")


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
        if pipeline.person_detector.yunet_detector is not None and hasattr(
            pipeline.person_detector.yunet_detector, "setScoreThreshold"
        ):
            pipeline.person_detector.yunet_detector.setScoreThreshold(new_config.detection.score_threshold)
    if hasattr(pipeline.person_detector, "nms_threshold"):
        pipeline.person_detector.nms_threshold = new_config.detection.nms_threshold
        if pipeline.person_detector.yunet_detector is not None and hasattr(
            pipeline.person_detector.yunet_detector, "setNMSThreshold"
        ):
            pipeline.person_detector.yunet_detector.setNMSThreshold(new_config.detection.nms_threshold)

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

# Global pipeline instance — no server-side camera started; browser streams frames via WebSocket
config = load_config("config/poc.yaml")
pipeline = RealtimePipeline(config)
renderer = VisualRenderer()

# Web state
web_state = {
    "view_mode": "original",
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
    persons_info = [
        {
            "person_id": p.person_id or f"person_{idx}",
            "bbox": list(p.bbox),
            "confidence": round(p.confidence, 3),
            "class_name": p.class_name,
            "is_known": p.is_known,
            "person_name": p.person_name or "Unknown Person",
            "tag": p.tag or "Intruder",
            "snapshot_base64": p.snapshot_base64 or "",
        }
        for idx, p in enumerate(result.detected_persons or [])
    ]
    metrics = result.metrics
    return {
        "frame_index": result.frame_index,
        "mode": result.mode,
        "layout_id": result.layout.layout_id,
        "pane_count": result.layout.pane_count,
        "layout_confidence": round(result.layout.confidence, 2),
        "view_mode": view_mode,
        "alerts": list(result.alerts or []),
        "detected_persons": persons_info,
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
    """Browser-side webcam streaming endpoint.

    The browser captures frames from getUserMedia(), encodes them as JPEG,
    and sends the raw bytes over this WebSocket. The server decodes each
    frame, runs face detection + recognition through the pipeline, and
    returns a JSON payload with bounding boxes and identity labels.

    Message protocol:
      Browser → Server : binary JPEG bytes (one frame per message)
      Server → Browser : UTF-8 JSON string with detection results
    """
    await websocket.accept()
    web_state["connected_clients"] += 1
    frame_index = 0
    logger.info(f"WebSocket client connected. Active clients: {web_state['connected_clients']}")

    try:
        while True:
            # Receive raw JPEG bytes from the browser
            jpeg_bytes = await websocket.receive_bytes()

            frame_index += 1
            web_state["frame_count"] += 1

            frame_data = _jpeg_to_frame(jpeg_bytes, frame_index)
            if frame_data is None:
                await websocket.send_text(json.dumps({"error": "Invalid frame", "frame_index": frame_index}))
                continue

            # Run full detection + recognition pipeline
            try:
                result = pipeline.process_frame_data(frame_data, view_mode=web_state.get("view_mode", "original"))
                web_state["last_result"] = result
                payload = _result_to_dict(result, web_state["view_mode"])
            except Exception as exc:
                logger.error(f"Pipeline error on frame {frame_index}: {exc}")
                payload = {"error": str(exc), "frame_index": frame_index, "detected_persons": [], "alerts": []}

            await websocket.send_text(json.dumps(payload))

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    except Exception as exc:
        logger.error(f"WebSocket error: {exc}")
    finally:
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
    return {"status": "ok", "deleted": success}


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

