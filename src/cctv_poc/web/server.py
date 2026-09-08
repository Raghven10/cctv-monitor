"""Web Server and Live Streaming Dashboard for AI CCTV Physical Display Monitor."""

import asyncio
import io
import json
import time
from typing import Dict, List, Optional
import cv2
import numpy as np
from pathlib import Path
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import POCConfig, load_config, save_config
from ..realtime.pipeline import RealtimePipeline
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

# Global pipeline instance
config = load_config("config/poc.yaml")
pipeline = RealtimePipeline(config)
renderer = VisualRenderer()

# Web state
web_state = {
    "view_mode": "original",  # Default to live camera view with projected pane boxes
    "is_webcam": True,
    "webcam_error": None,
    "last_result": None,
    "frame_count": 0,
}

# Auto-start live webcam on startup
try:
    if not pipeline.start(auto_open_camera=True):
        web_state["webcam_error"] = "Camera could not be opened. Check permissions or device index."
except Exception as e:
    web_state["webcam_error"] = str(e)


def frame_generator():
    """Generator for MJPEG stream from the live webcam."""
    global web_state
    
    while True:
        web_state["frame_count"] += 1

        # Continuous live frame acquisition
        frame_data = pipeline.video_source.buffer.pop_latest(timeout=0.08)
        if frame_data is None:
            # When camera is waiting or disconnected
            waiting_frame = np.full((720, 1280, 3), 15, dtype=np.uint8)
            cv2.putText(
                waiting_frame,
                "WAITING FOR LIVE WEBCAM INPUT...",
                (280, 340),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (0, 240, 255),
                2,
            )
            err_msg = web_state.get("webcam_error") or "Please ensure external webcam is connected."
            cv2.putText(
                waiting_frame,
                f"Status: {err_msg}",
                (280, 390),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 165, 255),
                1,
            )
            _, jpeg = cv2.imencode(".jpg", waiting_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
            )
            time.sleep(0.05)
            continue

        # Process frame automatically through the real-time hot path
        result = pipeline.process_frame_data(frame_data, view_mode=web_state.get("view_mode"))
        web_state["last_result"] = result

        # Render real-time live annotations & pane boxes on video
        annotated = renderer.render(result, view_mode=web_state["view_mode"])
        disp = cv2.resize(annotated, (1280, 720)) if annotated.shape[1] > 1920 else annotated
        
        _, jpeg = cv2.imencode(".jpg", disp, [cv2.IMWRITE_JPEG_QUALITY, 85])
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
        )
        time.sleep(0.03)


@app.get("/video_feed")
def video_feed():
    """MJPEG live video stream."""
    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/status")
def get_status():
    """Returns real-time telemetry and automatically discovered layout metadata."""
    res = web_state.get("last_result")
    if res is None:
        alerts = [f"WEBCAM_DISCONNECTED: {web_state['webcam_error']}"] if web_state.get("webcam_error") else ["INITIALIZING: Connecting to webcam..."]
        return {
            "status": "initializing",
            "alerts": alerts,
            "pane_count": 0,
            "layout_id": "searching...",
            "layout_confidence": 0.0,
            "metrics": {
                "processing_fps": 0.0,
                "capture_fps": 0.0,
                "frame_age_ms": 0.0,
                "processing_latency_ms": 0.0,
                "end_to_end_latency_ms": 0.0,
                "queue_depth": 0,
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
    if web_state.get("webcam_error"):
        active_alerts.append(f"WEBCAM_ERROR: {web_state['webcam_error']}")

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
            "processing_fps": metrics.processing_fps,
            "capture_fps": metrics.capture_fps,
            "frame_age_ms": metrics.frame_age_ms,
            "processing_latency_ms": metrics.processing_latency_ms,
            "end_to_end_latency_ms": metrics.end_to_end_latency_ms,
            "queue_depth": metrics.queue_depth,
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

