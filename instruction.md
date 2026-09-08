# AI CCTV Physical Display Monitoring POC --- Implementation Instructions

## 1. Objective

Build a **live, real-time / near-real-time POC** for an AI CCTV
monitoring system that observes an actual physical CCTV display using an
external webcam.

The system must behave like a human operator watching a CCTV
control-room monitor:

1.  Capture the physical CCTV display through a webcam.
2.  Detect the physical monitor/display boundary.
3.  Correct perspective distortion.
4.  Dynamically discover how many CCTV panes are currently visible.
5.  Segregate each pane without assuming a fixed 4/9/16-pane layout.
6.  Identify the camera label/name shown on each pane.
7.  Maintain a stable mapping between pane IDs and camera labels.
8.  Detect layout changes and re-learn pane/camera mappings.
9.  Display a live annotated view.
10. Produce structured JSONL diagnostics/results.
11. Maintain a real-time processing path whose detection and alert
    decisions are not blocked by expensive VLM/OCR operations.

**There must be no input video file. The only primary video input for
this POC is the live webcam observing the physical CCTV display.**

------------------------------------------------------------------------

# 2. Physical Setup

The intended setup is:

``` text
                 PHYSICAL CCTV DISPLAY

        ┌───────────────────────────────────┐
        │                                   │
        │ ┌────────┬────────┬────────┐      │
        │ │ CAM-01 │ CAM-02 │ CAM-03 │ ...  │
        │ ├────────┼────────┼────────┤      │
        │ │ CAM-04 │ CAM-05 │ CAM-06 │ ...  │
        │ └────────┴────────┴────────┘      │
        │                                   │
        └───────────────────────────────────┘
                         ▲
                         │
                  External Webcam
                         │
                         ▼
                  POC Application
```

The physical display is a **32-inch 4K monitor**, but the CCTV/VMS
layout is variable.

Examples of possible layouts include:

-   1 pane
-   2 panes
-   4 panes
-   6 panes
-   9 panes
-   12 panes
-   16 panes
-   other layouts supported by the VMS

The implementation **must not hard-code the number of panes**.

Do not implement the core logic using assumptions such as:

``` python
rows = 4
columns = 4
```

or:

``` python
pane_width = display_width / 4
pane_height = display_height / 4
```

The active layout must be discovered from the observed display.

------------------------------------------------------------------------

# 3. Real-Time Requirement

Real-time / near-real-time operation is a fundamental requirement.

The POC must be designed so that:

-   webcam acquisition is continuous;
-   pane segmentation does not stop the stream;
-   real-time detection and alert decisions do not wait for a VLM;
-   OCR and VLM analysis are asynchronous or selectively invoked;
-   expensive processing cannot create an unbounded frame queue;
-   stale frames must be dropped rather than allowing latency to grow
    indefinitely;
-   the application should expose processing FPS and latency;
-   end-to-end alert latency should target approximately **≤1--2
    seconds** under the POC test configuration.

The architecture should optimize for **bounded latency**, not merely
maximum offline throughput.

## Real-time architecture

``` text
                    LIVE WEBCAM
                         │
                         ▼
                 Frame Acquisition
                         │
                         ▼
              Monitor Detection/
             Perspective Correction
                         │
                         ▼
              Dynamic Layout Discovery
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
       REAL-TIME HOT PATH       ASYNC PATH
       ------------------       ----------
       Pane extraction          OCR
       Object detection         Vision model
       Tracking                 Face recognition
       Motion analysis          Evidence analysis
       Camera mapping           VLM enrichment
       Rules
       Alert decision
              │                     │
              └──────────┬──────────┘
                         ▼
                  Event Correlation
                         │
                         ▼
                       ALERT
```

The **hot path must never synchronously wait for a VLM**.

------------------------------------------------------------------------

# 4. POC Scope

## In scope

-   Live webcam capture.
-   Physical monitor detection.
-   Perspective correction/homography.
-   Dynamic CCTV pane discovery.
-   Pane extraction.
-   Camera label identification.
-   OCR as a fast path and/or validation mechanism.
-   Vision-language model as an optional semantic label
    recognizer/validator.
-   Temporal stabilization.
-   Dynamic layout-change detection.
-   Pane-to-camera association.
-   Live annotated display.
-   FPS and latency monitoring.
-   JSONL output.
-   Debug snapshots.
-   Configuration.
-   Automated tests for core geometry and mapping logic.

## Out of scope for this POC

Do not implement these yet:

-   face recognition;
-   known/unknown person classification;
-   person re-identification;
-   production alert workflows;
-   email/SMS/WhatsApp notifications;
-   Kafka;
-   Kubernetes;
-   distributed microservices;
-   production authentication/RBAC;
-   PostgreSQL/pgvector;
-   MinIO;
-   large-scale multi-GPU orchestration;
-   production deployment;
-   training custom detection models;
-   full CCTV stream ingestion from DVR/NVR;
-   processing an uploaded or prerecorded demo video.

These are future phases.

------------------------------------------------------------------------

# 5. Core Design Principle

Separate **pane geometry**, **camera identity**, and **semantic
understanding**.

A pane must have its own stable identifier:

``` text
P01
P02
P03
...
```

The camera identity is separate:

``` text
CAM-01
CAM-07
GATE-EAST
PARKING-02
```

Therefore:

``` text
P01 → CAM-01
P02 → GATE-EAST
P03 → PARKING-02
```

The pane ID represents a spatial region in the current display layout.

The camera label represents the CCTV feed assigned to that region.

If the VMS rearranges feeds:

``` text
Before:
P01 → CAM-01
P02 → CAM-02

After:
P01 → CAM-07
P02 → CAM-01
```

the system must detect the changed mapping rather than assuming that P01
always means CAM-01.

------------------------------------------------------------------------

# 6. Processing Pipeline

Implement the following logical pipeline:

``` text
Webcam
   ↓
Frame Capture
   ↓
Physical Monitor Detection
   ↓
Perspective Correction
   ↓
Dynamic Layout Discovery
   ↓
N Pane ROIs
   ↓
Camera Label Identification
   ├── OCR fast path
   └── VLM semantic path
   ↓
Temporal Stabilization
   ↓
Stable Pane → Camera Mapping
   ↓
Live Annotation
   ↓
JSONL / Diagnostics
```

Later, the production pipeline will extend to:

``` text
Pane
 ↓
Person Detection
 ↓
Tracking
 ↓
Face Recognition
 ↓
Known / Unknown
 ↓
Behavior / Zone Rules
 ↓
Event Engine
 ↓
Alert
```

------------------------------------------------------------------------

# 7. Webcam Input

The webcam is the only video input.

Support:

-   USB/UVC webcam;
-   optionally RTSP if the webcam exposes an RTSP stream.

Default configuration should use webcam device `0`.

Example:

``` yaml
video:
  source_type: webcam
  device: 0
  requested_width: 3840
  requested_height: 2160
  requested_fps: 30
```

Do not assume that the webcam can actually provide 4K/30 FPS. Query the
device and log the **actual negotiated resolution and FPS**.

If the webcam cannot provide the requested mode, the application must
fail clearly or fall back according to configuration.

------------------------------------------------------------------------

# 8. Physical Monitor Detection

The webcam sees the physical monitor rather than receiving the CCTV
display digitally.

Therefore the first computer-vision stage must identify the monitor.

Requirements:

-   detect the outer display boundary;
-   estimate four corners;
-   reject irrelevant rectangular objects where possible;
-   support perspective distortion;
-   expose detection confidence;
-   allow assisted/manual calibration when automatic detection is
    unreliable.

Example:

``` text
Webcam image

        ______________________
       /                      /
      /      CCTV DISPLAY     /
     /_______________________/
```

The detected four corners are used for homography.

------------------------------------------------------------------------

# 9. Perspective Correction

Use a homography/perspective transformation to map the physical display
into a normalized logical display coordinate system.

For a 4K monitor, the nominal logical target is:

``` text
3840 × 2160
```

However, implementation should not assume that the webcam actually
captures at 3840×2160.

The pipeline should:

1.  detect monitor corners;
2.  compute homography;
3.  rectify the display;
4.  operate on the rectified image;
5.  preserve the transformation needed to map annotations back to the
    original webcam image.

------------------------------------------------------------------------

# 10. Dynamic Pane Discovery

This is one of the most important components.

The application must dynamically infer the current pane structure.

Do not use a fixed grid.

Prefer deterministic computer-vision techniques first:

1.  edge/gradient extraction;
2.  vertical and horizontal projection analysis;
3.  Hough/line detection;
4.  repeated rectangular-region detection;
5.  divider/gutter detection;
6.  x/y separator clustering;
7.  geometric consistency checks;
8.  grid/layout inference.

If the VMS has clear pane borders:

``` text
┌──────────┬──────────┬──────────┐
│          │          │          │
│   P01    │   P02    │   P03    │
│          │          │          │
├──────────┼──────────┼──────────┤
│   P04    │   P05    │   P06    │
└──────────┴──────────┴──────────┘
```

detect the separator geometry.

If borders are weak or absent, infer repeated rectangular regions using
multiple visual cues.

The implementation should produce:

-   pane bounding box;
-   pane geometry confidence;
-   layout ID;
-   pane count;
-   overall layout confidence.

Example:

``` json
{
  "layout_id": "layout-a91f",
  "pane_count": 6,
  "confidence": 0.94
}
```

------------------------------------------------------------------------

# 11. Do Not Depend on Fixed Label Position

Do not assume:

``` text
label = top 10% of pane
```

The label may be:

-   top-left;
-   top-right;
-   bottom-left;
-   bottom-right;
-   centered;
-   overlaid on the image;
-   outside the pane boundary;
-   accompanied by icons or status indicators.

Therefore label detection should be based on **text/visual-region
geometry and spatial association**, not only a fixed crop.

OCR text bounding boxes should be associated with the pane whose
geometry contains or is nearest to the text.

A configurable header/label ROI may be supported as an optimization, but
it must not be the only mechanism.

------------------------------------------------------------------------

# 12. Camera Label Identification

Use a hybrid approach.

## 12.1 OCR

OCR should be the fast, deterministic text extraction path.

For each pane:

``` text
Pane
 ↓
Candidate text regions
 ↓
OCR
 ↓
raw_text
 ↓
normalized_text
 ↓
confidence
```

Store both:

``` text
raw_text
normalized_text
```

Example:

``` json
{
  "raw_text": "CAM- 01",
  "normalized_text": "CAM-01",
  "ocr_confidence": 0.96
}
```

OCR should run periodically rather than on every frame.

## 12.2 Vision-language model

A VLM may be used to interpret or validate the camera label.

Example task:

> Identify the CCTV camera identifier or location label visible in this
> pane. Return the most likely label and confidence. Do not infer an
> identifier that is not visually supported.

The VLM should be invoked:

-   periodically;
-   when OCR confidence is low;
-   when OCR outputs disagree;
-   after a layout change;
-   when label association is ambiguous;
-   when semantic interpretation is needed.

Do not run the VLM synchronously on every frame.

------------------------------------------------------------------------

# 13. OCR + VLM Fusion

The system should support:

``` text
             Pane
               │
        ┌──────┴──────┐
        ▼             ▼
       OCR           VLM
        │             │
        └──────┬──────┘
               ▼
        Identity Resolver
               │
               ▼
        Temporal Stabilizer
               │
               ▼
         Stable Label
```

Example:

``` text
OCR: CAM-01   confidence 0.97
VLM: CAM-01   confidence 0.92
→ stable = CAM-01
```

If:

``` text
OCR: CAM-01
VLM: CAM-07
```

do not immediately change the mapping.

Keep the previous stable identity until sufficient evidence supports the
change.

------------------------------------------------------------------------

# 14. Temporal Stabilization

Single-frame OCR/VLM output must not immediately become the system's
camera identity.

Maintain a temporal history.

Example:

``` text
Frame 100   CAM-01
Frame 105   CAM-01
Frame 110   CAM-01
Frame 115   CAM-01
Frame 120   CAM-07
```

The mapping should remain CAM-01 until a configurable stability
criterion is satisfied.

Configuration example:

``` yaml
stabilization:
  label_observations_required: 5
  layout_frames_required: 10
  change_confirmation_frames: 10
```

Use configurable thresholds rather than hard-coded constants.

------------------------------------------------------------------------

# 15. Layout Change Detection

The CCTV/VMS may change its layout while the application is running.

Examples:

``` text
4 panes → 9 panes
9 panes → 16 panes
16 panes → 6 panes
```

or feeds may be rearranged without changing pane count.

The system must detect:

-   pane count changes;
-   pane geometry changes;
-   major divider changes;
-   camera-label mapping changes.

Do not rebuild the layout based on a single noisy frame.

Use temporal confirmation.

Example:

``` text
Detected new layout
        ↓
Candidate layout
        ↓
Observe N frames
        ↓
Consistent?
   ┌────┴────┐
  NO        YES
   │          │
discard     activate
```

Each activated layout should have a new `layout_id`.

------------------------------------------------------------------------

# 16. Real-Time Processing Architecture

Use a producer/consumer design.

Recommended logical components:

``` text
Capture Thread
     │
     ▼
Latest-Frame Buffer
     │
     ├───────────────► Layout/Geometry Worker
     │
     ├───────────────► Real-Time Detection Worker
     │
     └───────────────► Periodic OCR/VLM Worker
```

Important requirement:

**Prefer a bounded queue or latest-frame buffer.**

If processing falls behind, do not process old frames indefinitely.

For real-time operation:

``` text
BAD:
Frame 1 → waiting
Frame 2 → waiting
Frame 3 → waiting
...
latency grows continuously

GOOD:
Frame N-1
Frame N
Frame N+1
        ↓
process latest useful frame
```

------------------------------------------------------------------------

# 17. Real-Time Metrics

The application must report at least:

``` text
capture_fps
processing_fps
effective_fps
queue_depth
frame_age_ms
processing_latency_ms
end_to_end_latency_ms
layout_detection_latency_ms
ocr_latency_ms
vlm_latency_ms
```

Display these metrics in the live UI.

Example:

``` text
FPS: 28.7
Frame age: 42 ms
Processing: 71 ms
End-to-end: 180 ms
Panes: 9
Layout confidence: 0.96
```

------------------------------------------------------------------------

# 18. Future Detection and Alert Architecture

Although person detection/alerts are not required for the first POC, the
architecture must be designed so they can be added without redesigning
the entire pipeline.

Future hot path:

``` text
Pane
 ↓
Person detector
 ↓
Tracker
 ↓
Zone/rule engine
 ↓
Known/unknown identity
 ↓
Event decision
 ↓
Alert
```

The alert path must remain independent from slow semantic enrichment.

For example:

``` text
Person enters restricted zone
        │
        ▼
REAL-TIME RULE ENGINE
        │
        ▼
ALERT
        │
        └───────────────┐
                        ▼
                  VLM analysis
                  evidence enrichment
```

The VLM may add context such as:

-   carrying object;
-   crowding;
-   unusual activity;
-   contextual scene description.

But it must not block the initial alert.

------------------------------------------------------------------------

# 19. Live Visualization

Provide a live window showing the webcam/rectified monitor with
annotations.

Each pane should show:

``` text
┌──────────────────────────────┐
│ P03                          │
│ CAM-GATE-EAST                │
│ Label conf: 0.94             │
│ Geometry: 0.97               │
│                              │
│        CCTV IMAGE            │
│                              │
└──────────────────────────────┘
```

Global overlay:

``` text
Layout: 6 panes
Layout confidence: 0.95
FPS: 29.1
Latency: 164 ms
```

When uncertain, explicitly show:

``` text
UNKNOWN
UNSTABLE
LOW CONFIDENCE
```

Do not silently invent a camera identity.

------------------------------------------------------------------------

# 20. JSONL Output

Write one structured record per relevant frame/state change, or
according to a configurable sampling rate.

Suggested schema:

``` json
{
  "schema_version": "1.0",
  "timestamp": "2026-09-08T12:00:00.123Z",
  "frame_index": 12345,
  "capture": {
    "width": 3840,
    "height": 2160,
    "fps": 30
  },
  "display": {
    "bbox": [100, 50, 3700, 2050],
    "confidence": 0.98
  },
  "layout": {
    "layout_id": "layout-a91f",
    "pane_count": 6,
    "confidence": 0.95,
    "stable": true
  },
  "panes": [
    {
      "pane_id": "P01",
      "bbox": [0, 0, 1280, 1080],
      "geometry_confidence": 0.97,
      "label": {
        "raw_text": "CAM-01",
        "normalized_text": "CAM-01",
        "ocr_confidence": 0.96,
        "vlm_confidence": 0.92,
        "association_confidence": 0.98,
        "stable": true
      }
    }
  ],
  "performance": {
    "processing_fps": 28.7,
    "frame_age_ms": 42,
    "end_to_end_latency_ms": 180
  }
}
```

------------------------------------------------------------------------

# 21. Configuration

Create:

``` text
config/poc.yaml
```

Suggested configuration:

``` yaml
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
  min_area_fraction: 0.40

layout:
  dynamic: true
  min_panes: 1
  max_panes: 64
  stability_frames: 10
  expected_aspect_ratio: auto

ocr:
  enabled: true
  sample_every_n_frames: 15
  confidence_threshold: 0.60

vlm:
  enabled: true
  mode: validation
  sample_interval_seconds: 3
  trigger_on_low_ocr_confidence: true
  trigger_on_layout_change: true

stabilization:
  label_observations_required: 5
  change_confirmation_frames: 10

output:
  display: true
  save_snapshots: true
  output_directory: data/output
  jsonl: data/output/results.jsonl
```

All important thresholds must be configurable.

------------------------------------------------------------------------

# 22. Recommended Repository Structure

``` text
cctv-monitor-poc/
├── README.md
├── instruction.md
├── pyproject.toml
├── requirements.txt
├── .gitignore
│
├── config/
│   └── poc.yaml
│
├── src/
│   └── cctv_poc/
│       ├── main.py
│       │
│       ├── video/
│       │   └── source.py
│       │
│       ├── monitor/
│       │   ├── display_detector.py
│       │   ├── calibration.py
│       │   ├── layout_discovery.py
│       │   └── pane_extractor.py
│       │
│       ├── ocr/
│       │   └── label_reader.py
│       │
│       ├── vlm/
│       │   └── label_validator.py
│       │
│       ├── camera/
│       │   └── identity_resolver.py
│       │
│       ├── stabilization/
│       │   └── temporal.py
│       │
│       ├── realtime/
│       │   ├── pipeline.py
│       │   └── metrics.py
│       │
│       ├── visualization/
│       │   └── renderer.py
│       │
│       └── utils/
│           └── logging.py
│
├── tests/
│   ├── test_display_detection.py
│   ├── test_layout_discovery.py
│   ├── test_pane_extraction.py
│   ├── test_label_association.py
│   ├── test_temporal_stability.py
│   └── test_webcam_source.py
│
├── scripts/
│   └── calibrate.py
│
└── data/
    └── output/
```

Do **not** create:

``` text
data/input/demo.mp4
```

and do not require any prerecorded input file.

------------------------------------------------------------------------

# 23. Technology Guidance

Recommended initial technologies:

-   Python 3.11+
-   OpenCV
-   NumPy
-   PyYAML
-   GStreamer where useful for reliable camera ingestion
-   PaddleOCR or equivalent OCR engine
-   A locally deployable VLM where practical
-   PyTorch for future neural inference
-   structured Python logging

For future high-throughput deployment, evaluate NVIDIA
DeepStream/TensorRT.

Do not introduce DeepStream, Kubernetes, Kafka, PostgreSQL, Redis, etc.
merely for the first POC unless they are genuinely required for the
measured workload.

The POC should remain simple enough to debug.

------------------------------------------------------------------------

# 24. VLM Integration Requirements

The VLM interface must be abstracted.

Do not hard-code the rest of the application to one specific VLM.

Create an interface similar to:

``` python
class VisionLabelRecognizer:
    def identify_label(self, image) -> LabelResult:
        ...
```

Possible implementations:

``` text
LocalVLMRecognizer
RemoteVLMRecognizer
MockVLMRecognizer
```

This allows the VLM to be changed later without changing pane detection
or the real-time pipeline.

The VLM worker should be asynchronous and should have:

-   timeout;
-   bounded concurrency;
-   result timestamp;
-   confidence;
-   error handling;
-   stale-result rejection.

A delayed VLM result must not overwrite a newer camera mapping without
temporal validation.

------------------------------------------------------------------------

# 25. Error Handling

The application must explicitly handle:

-   webcam unavailable;
-   webcam resolution mismatch;
-   monitor not detected;
-   monitor partially occluded;
-   poor perspective;
-   pane boundaries unclear;
-   pane count uncertain;
-   OCR failure;
-   VLM timeout;
-   OCR/VLM disagreement;
-   layout change;
-   temporary blank pane;
-   CCTV feed unavailable;
-   camera label missing.

Never silently produce a confident camera identity when evidence is
insufficient.

------------------------------------------------------------------------

# 26. Acceptance Criteria

## AC-01 --- Live input

The system starts from a physical webcam and does not require a video
file.

## AC-02 --- Monitor detection

The physical CCTV monitor can be detected and rectified.

## AC-03 --- Dynamic pane count

The application correctly discovers different pane counts without
changing source code.

## AC-04 --- Dynamic geometry

Pane bounding boxes are derived from the observed display rather than
fixed coordinates.

## AC-05 --- Label identification

Each pane can be associated with the camera label shown by the CCTV/VMS
interface.

## AC-06 --- OCR/VLM hybrid

OCR can provide fast label recognition and VLM can validate or resolve
ambiguous cases.

## AC-07 --- Temporal stability

Transient OCR/VLM errors do not immediately change the stable camera
mapping.

## AC-08 --- Layout changes

When the CCTV operator changes the layout, the system detects and
stabilizes the new layout.

## AC-09 --- No cross-pane swaps

The system must not persistently associate a camera label with the wrong
pane.

## AC-10 --- Real-time behavior

The application processes the live webcam without an ever-growing
backlog.

## AC-11 --- Latency visibility

FPS, frame age and processing/end-to-end latency are visible.

## AC-12 --- Uncertainty

Low-confidence results are explicitly marked as uncertain.

## AC-13 --- Structured output

The application writes JSONL containing layout, pane, label and
performance information.

## AC-14 --- Extensibility

The architecture can later add person detection, tracking, face
recognition and event/alert processing without replacing the
display-analysis pipeline.

------------------------------------------------------------------------

# 27. POC Performance Target

The initial target is:

-   webcam acquisition: approximately 30 FPS where hardware permits;
-   real-time pipeline: approximately 15--30 FPS depending on workload;
-   no unbounded frame queue;
-   stale frames may be dropped;
-   initial alert decision target: approximately ≤1--2 seconds;
-   OCR/VLM processing must be asynchronous;
-   VLM latency must not block real-time processing.

These are **engineering targets for the POC**, not assumptions about
guaranteed hardware performance.

Measure actual performance and record it.

------------------------------------------------------------------------

# 28. Development Sequence

Implement in this order.

### Phase 1 --- Webcam

1.  Open webcam.
2.  Query actual resolution/FPS.
3.  Display live frames.
4.  Measure capture FPS.

### Phase 2 --- Monitor detection

1.  Detect display.
2.  Implement assisted calibration.
3.  Rectify perspective.
4.  Display rectified monitor.

### Phase 3 --- Dynamic layout

1.  Detect candidate horizontal/vertical separators.
2.  Infer rectangular panes.
3.  Validate geometry.
4.  Generate pane IDs.
5.  Stabilize layout.
6.  Test multiple layouts.

### Phase 4 --- Camera labels

1.  Implement OCR.
2.  Detect candidate text regions.
3.  Associate text with panes.
4.  Normalize labels.
5.  Add temporal stabilization.

### Phase 5 --- VLM

1.  Implement VLM abstraction.
2.  Run VLM only on selected panes.
3.  Trigger on low OCR confidence/layout changes/disagreement.
4.  Fuse OCR and VLM results.
5.  Reject stale asynchronous results.

### Phase 6 --- Real-time optimization

1.  Introduce bounded/latest-frame buffering.
2.  Separate hot path and asynchronous workers.
3.  Measure latency.
4.  Drop stale frames.
5.  Optimize CPU/GPU usage.

### Phase 7 --- Visualization and output

1.  Add live pane annotations.
2.  Add FPS/latency metrics.
3.  Add JSONL.
4.  Add debug snapshots.
5.  Add structured logging.

### Phase 8 --- Future extension point

Prepare clean interfaces for:

``` text
PersonDetector
Tracker
FaceRecognizer
RuleEngine
AlertManager
EvidenceStore
```

Do not implement those components in this POC unless explicitly
requested.

------------------------------------------------------------------------

# 29. Important Implementation Rules

1.  **Never assume a fixed number of CCTV panes.**
2.  **Never require a prerecorded input video.**
3.  **Use the physical webcam as the live input.**
4.  **Do not make VLM inference part of the blocking real-time hot
    path.**
5.  **Use OCR as a fast text extraction mechanism where appropriate.**
6.  **Use VLM selectively for semantic interpretation and validation.**
7.  **Keep pane ID separate from camera identity.**
8.  **Associate labels spatially with panes.**
9.  **Use temporal stabilization for both layouts and labels.**
10. **Drop stale frames rather than accumulating latency.**
11. **Expose real-time FPS and latency.**
12. **Never fabricate a camera label when confidence is insufficient.**
13. **Keep all major thresholds configurable.**
14. **Design interfaces so future person detection/tracking/face
    recognition can be added without replacing the display-analysis
    layer.**
15. **Optimize for bounded end-to-end latency, not offline batch
    accuracy.**

------------------------------------------------------------------------

# 30. Final Target Architecture

``` text
                         ┌──────────────────────┐
                         │ PHYSICAL CCTV WALL   │
                         │ 1..N VARIABLE PANES  │
                         └──────────┬───────────┘
                                    ▲
                                    │
                              External Webcam
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Frame Acquisition    │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Monitor Detection    │
                         │ + Calibration        │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Perspective          │
                         │ Correction            │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Dynamic Layout       │
                         │ Discovery             │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ N Pane ROIs          │
                         └──────────┬───────────┘
                                    │
                   ┌────────────────┴────────────────┐
                   │                                 │
                   ▼                                 ▼
          ┌──────────────────┐              ┌──────────────────┐
          │ REAL-TIME PATH   │              │ ASYNC AI PATH   │
          │                  │              │                  │
          │ Fast CV          │              │ OCR              │
          │ Detection        │              │ VLM              │
          │ Tracking         │              │ Face recognition │
          │ Rules            │              │ Scene analysis   │
          │ Alert decision   │              │ Evidence         │
          └────────┬─────────┘              └────────┬─────────┘
                   │                                 │
                   └────────────────┬────────────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Temporal / Event     │
                         │ Correlation          │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Alert + Evidence     │
                         └──────────────────────┘
```

The POC should prove the **first half of this architecture** while
enforcing the real-time architectural constraints needed for the second
half.
