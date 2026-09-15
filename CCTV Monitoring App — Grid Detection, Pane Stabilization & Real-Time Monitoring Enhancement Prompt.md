# CCTV Monitoring App Enhancement — Real-Time Grid-to-Pane Monitoring

## Role

You are a senior computer-vision/software architect working on an existing CCTV monitoring application.

Enhance the existing application to implement the workflow below **without breaking existing functionality**, especially the existing **Live Room Monitoring person detection, face recognition, person-tagging, identity registry, and alert logic**.

The application must be designed around a **real-time / near-real-time monitoring philosophy**.

The primary objective is:

> **Capture → AI Grid Detection → User Correction → Grid Finalization → Pane Extraction → Pane Stabilization → Person Detection/Identification → Activity/Event Description → Alert + Continuous Pane-Level Logging**

---

# CRITICAL ENGINEERING PRINCIPLES

Before making any code changes:

1. Inspect the complete existing architecture.
2. Identify:
   - Webcam initialization/capture pipeline
   - Frame processing loop
   - Grid/pane detection logic
   - Existing Live Room Monitoring implementation
   - Face detection
   - Face embedding generation
   - Identity registry
   - Identity resolver
   - Person tagging workflow
   - Alert generation
   - Event/activity logging
   - Frontend/backend communication
   - Existing configuration/model loading
3. Reuse existing implementations wherever possible.
4. Do NOT create duplicate implementations of face recognition, identity resolution, or person tagging.
5. Establish explicit **phase gates** before modifying code.
6. Do not start large-scale refactoring until the existing flow and bottlenecks are understood.

---

# PHASE 0 — ARCHITECTURE & PERFORMANCE AUDIT

Before writing code, inspect the repository and produce a concise implementation plan.

Determine:

### Video pipeline

Document:

```text
Webcam
   ↓
Frame Capture
   ↓
Frame Queue / Buffer
   ↓
Grid Detection
   ↓
Pane Extraction
   ↓
Person Detection
   ↓
Face Detection
   ↓
Embedding / Identification
   ↓
Activity/Event Detection
   ↓
Alert + Logging
   ↓
UI
```

Identify where the current application:

- captures frames
- drops frames
- blocks processing
- performs inference synchronously
- performs CPU-heavy operations
- performs GPU inference
- performs face embedding
- performs OCR/text detection
- updates the UI
- writes logs

### IMPORTANT

Do not assume that processing every captured frame is necessary.

The system must maintain:

> **real-time monitoring responsiveness rather than processing every frame sequentially.**

Use appropriate frame sampling, asynchronous workers, queues, batching, tracking, caching, and temporal stabilization where required.

---

# PHASE GATE 1

Before implementing functionality, provide:

1. Existing architecture summary
2. Relevant files/classes/functions
3. Current frame-processing bottlenecks
4. Current FPS at:
   - webcam capture
   - grid detection
   - pane processing
   - person detection
   - face recognition
5. GPU/CPU utilization if available
6. Proposed architecture
7. Exact files that will be modified
8. Potential regression risks

**Do not modify application code until this phase is complete.**

---

# STEP 1 — START WEBCAM & AI GRID SCANNING

When the user starts monitoring:

```text
Start Webcam
      ↓
Capture Combined CCTV Display
      ↓
AI analyzes combined view
      ↓
Detect CCTV pane/grid boundaries
      ↓
Display detected grid overlay
```

The webcam is pointed toward a physical/display screen containing multiple CCTV panes.

The system must therefore treat the webcam image as a **combined CCTV wall**, not as an individual CCTV camera.

For example:

```text
┌───────────┬───────────┬───────────┐
│  CCTV 1   │  CCTV 2   │  CCTV 3   │
├───────────┼───────────┼───────────┤
│  CCTV 4   │  CCTV 5   │  CCTV 6   │
└───────────┴───────────┴───────────┘
```

AI should detect:

- number of rows
- number of columns
- pane boundaries
- approximate coordinates
- pane ordering

Example:

```text
Detected Layout: 3 × 2

┌─────┬─────┬─────┐
│  1  │  2  │  3  │
├─────┼─────┼─────┤
│  4  │  5  │  6  │
└─────┴─────┴─────┘
```

The detected boxes must immediately be rendered as an overlay on the combined webcam view.

---

# STEP 2 — USER GRID CORRECTION & FINALIZATION

The AI-detected layout is only the initial proposal.

The user must be able to modify it.

## 2.1 Layout Dropdown

If AI detects:

```text
3 × 2
```

show:

```text
Grid Layout: [3 × 2 ▼]
```

Allow predefined layouts such as:

```text
1 × 1
1 × 2
2 × 1
2 × 2
2 × 3
3 × 2
3 × 3
3 × 4
4 × 3
4 × 4
...
```

The exact available layouts should be based on the application's existing requirements.

When the user changes the layout:

> The grid must immediately and smoothly recalculate and redraw on the combined view.

No page refresh should be required.

---

# 2.2 Manual Pane Boundary Adjustment

The user must be able to manually adjust the detected pane boundaries.

Each pane should have draggable borders.

Example:

```text
┌───────────────┬──────────────┐
│               │              │
│     Pane 1    │    Pane 2    │
│               │              │
└───────────────┴──────────────┘
                 ↑
             draggable
```

Support:

- dragging vertical borders
- dragging horizontal borders
- resizing adjacent panes
- real-time overlay updates
- mouse/touch interaction if supported by the existing UI

Do not require the user to enter pixel coordinates manually.

The displayed image must remain stable while the border is being adjusted.

---

# 2.3 Finalize Grid

Provide:

```text
[ Finalise Grid Layout ]
```

When clicked:

1. Freeze the selected grid configuration.
2. Save pane coordinates.
3. Assign permanent pane IDs:

```text
Pane-01
Pane-02
Pane-03
...
```

4. Stop repeated grid detection.
5. Transition the application to pane monitoring mode.

Grid detection must NOT continuously run after finalization unless the user explicitly requests re-scan/reconfigure.

---

# PHASE GATE 2

Before implementing pane monitoring, verify:

- AI grid detection works.
- Grid overlay is accurate.
- Dropdown changes work.
- Grid automatically recalculates.
- Manual border dragging works.
- Finalize button freezes the layout.
- Pane coordinates are stored correctly.
- Pane IDs remain stable.

Do not proceed to activity/identity monitoring until this workflow is stable.

---

# STEP 3 — SPLIT COMBINED VIEW INTO INDIVIDUAL PANES

After finalization, the combined webcam frame should be logically split into individual pane regions.

For example:

```text
Combined View

┌─────────┬─────────┬─────────┐
│ Pane 01 │ Pane 02 │ Pane 03 │
├─────────┼─────────┼─────────┤
│ Pane 04 │ Pane 05 │ Pane 06 │
└─────────┴─────────┴─────────┘
```

becomes:

```text
Pane 01
Pane 02
Pane 03
Pane 04
Pane 05
Pane 06
```

Each pane must have its own monitoring state.

---

# 3.1 Pane Dashboard

Display multiple pane views simultaneously.

Example:

```text
┌─────────────────────┬─────────────────────┐
│ Pane 01              │ Pane 02             │
│ Person: Ramesh       │ Person: Unknown     │
│ Activity: Standing   │ Activity: Walking    │
├─────────────────────┼─────────────────────┤
│ Pane 03              │ Pane 04             │
│ Person: Unknown      │ Person: Priya       │
│ Activity: Entered    │ Activity: Sitting   │
└─────────────────────┴─────────────────────┘
```

The UI must clearly identify every pane.

---

# 3.2 Pane Text / CCTV Label Detection

For every pane, attempt to identify the text label displayed on the CCTV view.

Possible examples:

```text
Entrance
Server Room
Lobby
Gate 1
Floor 2
Camera 07
```

Prefer the existing text/OCR/vision pipeline if one already exists.

If no reliable label is detected:

```text
Pane-01
Pane-02
Pane-03
...
```

Use deterministic sequential labels.

Do NOT repeatedly change a pane's label on every frame.

Once a label is confidently identified:

> Stabilize/cache the label.

---

# STEP 4 — PANE STABILIZATION

Once the grid has been finalized, stabilize each pane region.

The pane coordinates should remain fixed:

```text
Pane ID
   ↓
Fixed ROI
   ↓
Frame extraction
   ↓
Monitoring
```

Do not repeatedly run expensive grid detection.

If minor camera/display movement occurs, investigate whether lightweight ROI stabilization is required.

However:

> Do not introduce expensive image registration on every frame unless profiling demonstrates that it is necessary.

---

# STEP 4.1 — PERSON DETECTION

Each stabilized pane must independently perform person detection.

Example:

```text
Pane 01
    ↓
Person detector
    ↓
Person bounding box
    ↓
Face detector
    ↓
Identity resolver
```

Multiple people may exist inside the same pane.

Therefore the architecture must support:

```text
Pane 01
 ├── Person Track 1
 ├── Person Track 2
 └── Person Track 3
```

Each detected person should receive a temporary tracking ID.

---

# STEP 4.2 — PERSON IDENTIFICATION

Reuse the **existing Live Room Monitoring identity recognition/tagging logic**.

Do NOT implement an independent recognition system unless absolutely necessary.

Reuse:

- face detection
- face embeddings
- embedding normalization
- identity registry
- identity resolver
- similarity calculation
- recognition thresholds
- person tagging
- identity persistence

If the current implementation already contains:

```text
identity_registry.py
identity_resolver.py
```

or equivalent modules, integrate with those modules rather than duplicating their functionality.

---

# IDENTIFIED PERSON

If a person is confidently identified:

```text
Person: Person-1
Identity: Ramesh
Confidence: 0.91
Status: IDENTIFIED
```

No alert is required merely because the person is identified.

However, activity/event logging must continue.

---

# UNKNOWN PERSON

If a person cannot be identified:

```text
Person: Unknown
Status: UNIDENTIFIED
```

Raise an alert.

The UI should provide an action such as:

```text
[ Tag Person ]
```

The tagging flow must reuse the existing **Live Room Monitoring** tagging functionality.

Example:

```text
Unknown Person Detected
        ↓
[Tag Person]
        ↓
Existing identity/tagging workflow
        ↓
Person assigned identity
        ↓
Identity registry updated
        ↓
Future detections can recognize person
```

Do not create a second tagging database.

---

# STEP 4.3 — ACTIVITY / EVENT DETECTION

Once the pane is stable and persons are detected, begin activity monitoring.

The system should maintain a continuous event history for each pane.

Example:

```text
Pane-01

10:01:03  Person-1 entered
10:01:06  Person-1 identified as Ramesh
10:01:12  Ramesh standing
10:01:20  Ramesh walking
10:01:37  Person-2 entered
10:01:40  Person-2 unidentified
10:01:40  ALERT — Unknown person
```

---

# VISION MODEL FOR EVENT DESCRIPTION

Use the existing vision model, or the most appropriate existing vision-capable model, to describe meaningful events.

Do NOT send every frame to the vision-language model.

Instead:

```text
Continuous video
       ↓
Person/object tracking
       ↓
Detect meaningful state change
       ↓
Trigger vision model
       ↓
Generate event description
       ↓
Store event
```

Examples:

```text
"Person entered the room."

"Ramesh entered the room and is standing near the door."

"Unknown person detected near the entrance."

"Ramesh moved from the left side toward the desk."

"Person left the monitored area."
```

The event-description model should be invoked **only when meaningful changes occur**.

---

# EVENT DE-DUPLICATION

Do not generate:

```text
Standing
Standing
Standing
Standing
Standing
...
```

every frame.

Use temporal state tracking.

Example:

```text
STATE:
Ramesh = Standing

No event generated until:
    state changes
    OR
    significant movement occurs
    OR
    new person appears
    OR
    person disappears
```

This dramatically reduces unnecessary inference.

---

# PANE-LEVEL STATE MACHINE

Implement a clear state machine for every pane.

Example:

```text
UNINITIALIZED
      ↓
GRID_CONFIRMED
      ↓
PANE_STABILIZED
      ↓
MONITORING
      ↓
PERSON_DETECTED
      ↓
IDENTIFIED / UNKNOWN
      ↓
ACTIVITY_TRACKING
      ↓
EVENT_GENERATED
      ↓
MONITORING
```

A pane must maintain independent state.

---

# REAL-TIME PERFORMANCE — CRITICAL

The system must NOT process the webcam sequentially through every expensive model.

Bad architecture:

```text
Frame
 ↓
Grid detection
 ↓
OCR
 ↓
Person detection
 ↓
Face detection
 ↓
Embedding
 ↓
Vision model
 ↓
UI
 ↓
Next frame
```

This will cause severe FPS degradation.

Instead use an asynchronous architecture.

Preferred design:

```text
                    ┌── Grid / UI
                    │
Webcam ──→ Capture ─┼── Person Detection
                    │
                    ├── Face Recognition
                    │
                    ├── Activity Tracking
                    │
                    └── Event/Vision Model
```

Use:

- producer/consumer queues
- asynchronous processing
- bounded queues
- latest-frame semantics where appropriate
- frame skipping for expensive inference
- object tracking between detector runs
- temporal caching
- batched inference where beneficial
- GPU acceleration
- separate UI and inference threads/processes where appropriate

---

# LATEST-FRAME PRIORITY

For real-time monitoring, stale frames are worse than dropped frames.

If processing falls behind:

```text
Frame 100
Frame 101
Frame 102
Frame 103
...
Frame 130
```

Do NOT process all 30 stale frames sequentially.

Prefer:

```text
Process Frame 100

Queue becomes overloaded

Discard stale frames

Process latest available frame
```

This ensures the UI and detection remain close to real time.

---

# FRAME-RATE ARCHITECTURE

Separate capture FPS from inference FPS.

Example:

```text
Webcam capture:       15–30 FPS
Pane display:         15–30 FPS
Person detection:     5–15 FPS
Face recognition:     event/track based
Activity detection:  event/state based
Vision model:         only on meaningful events
```

The exact values must be determined through profiling rather than hard-coded blindly.

---

# FACE RECOGNITION OPTIMIZATION

Do NOT generate a new embedding for the same tracked person on every frame.

Use:

```text
New person detected
       ↓
Face recognition
       ↓
Identity cached against Track ID
       ↓
Track continues
       ↓
Recognition periodically refreshed only when needed
```

Re-run recognition when:

- a new person appears
- face quality improves
- identity confidence is insufficient
- tracking is lost
- person changes
- configurable timeout expires

---

# PANE PROCESSING OPTIMIZATION

If there are N panes:

```text
Pane 1
Pane 2
...
Pane N
```

Do not create N completely independent expensive pipelines if the GPU can process them more efficiently as a batch.

Evaluate:

```text
N pane ROIs
      ↓
Batch person detection
      ↓
Batch face detection
      ↓
Batch embedding
```

Use batching where it improves throughput without introducing unacceptable latency.

---

# UI REQUIREMENT — MINIMIZE / MAXIMIZE

Every pane must support:

```text
[ − ] Minimize
[ ⛶ ] Maximize
```

## Minimize

Collapse the pane into a compact representation while continuing monitoring in the background.

Example:

```text
┌─────────────────────────────┐
│ Pane-01       [Restore]     │
│ Ramesh — Monitoring         │
└─────────────────────────────┘
```

Monitoring must NOT stop simply because the pane is minimized.

---

# MAXIMIZE

When the user maximizes a pane:

```text
┌──────────────────────────────────────────────┐
│ Pane-03                         [Restore]     │
│                                              │
│              FULL PANE VIEW                  │
│                                              │
│ Person: Ramesh                               │
│ Activity: Walking                            │
│ Status: IDENTIFIED                           │
│                                              │
└──────────────────────────────────────────────┘
```

The maximized pane should receive the available display area while background monitoring of all other panes continues.

---

# PANE INFORMATION PANEL

Each pane should expose, at minimum:

```text
Pane ID
CCTV Label
Person(s)
Identity
Recognition confidence
Activity
Monitoring status
Last event
Alert status
```

Example:

```text
Pane-03
──────────────
Location: Main Gate
Person: Ramesh
Identity: Identified
Confidence: 94%
Activity: Walking
Last Event: Entered at 10:02:13
Alert: None
```

---

# ALERT MANAGEMENT

Unknown-person detection should generate an alert.

Avoid generating repeated alerts for the same unknown tracked person.

Bad:

```text
10:01 Unknown
10:02 Unknown
10:03 Unknown
10:04 Unknown
```

Instead:

```text
10:01 Unknown person detected
      ↓
Alert generated
      ↓
Track maintained
      ↓
No duplicate alerts while same person remains
```

Generate another alert only when the person:

- leaves and re-enters
- tracking is lost and a new person is detected
- identity status changes
- configurable alert cooldown expires

---

# CONTINUOUS EVENT LOG

Maintain a structured log per pane.

Suggested structure:

```text
timestamp
pane_id
pane_label
track_id
person_id
identity
identity_confidence
event_type
activity
description
alert_status
frame_reference
```

Example:

```text
10:02:13
Pane-03
Main Gate
Track-17
Person-04
Ramesh
0.94
PERSON_ENTERED
Walking
"Ramesh entered the main gate area."
NO_ALERT
```

The log must be persistent according to the application's existing storage architecture.

---

# GRID CONFIGURATION PERSISTENCE

Once finalized, persist:

```text
grid_rows
grid_columns

pane_id
x
y
width
height
label
```

Example:

```json
{
  "layout": "3x2",
  "panes": [
    {
      "id": "pane-01",
      "x": 0.0,
      "y": 0.0,
      "width": 0.33,
      "height": 0.50,
      "label": "Main Gate"
    }
  ]
}
```

Prefer normalized coordinates rather than hard-coded pixels so the configuration remains usable if webcam resolution changes.

---

# ERROR HANDLING

Handle:

### Webcam disconnected

Display:

```text
Webcam disconnected
Attempting to reconnect...
```

Do not crash the application.

### Webcam reconnect

Automatically restore the capture pipeline.

### Grid detection failure

Display:

```text
Unable to confidently detect CCTV grid.
Please select or adjust the layout manually.
```

Allow manual configuration.

### Person recognition failure

Treat as:

```text
Unknown
```

rather than crashing or blocking monitoring.

### Vision model unavailable

Monitoring and person detection must continue.

Event description should degrade gracefully:

```text
Person entered
```

instead of stopping the pipeline.

---

# IMPORTANT: NO BLOCKING DEPENDENCIES

The following must NEVER block the primary video stream:

- OCR
- vision-language model inference
- face embedding
- database writes
- event description
- UI updates
- logging
- alert delivery

Use asynchronous/background processing where appropriate.

---

# PERFORMANCE ACCEPTANCE CRITERIA

After implementation, benchmark:

```text
Capture FPS
Displayed FPS
Effective monitoring FPS
Person detection FPS
Face recognition latency
Vision model latency
End-to-end event latency
GPU utilization
CPU utilization
Memory usage
Queue depth
Dropped-frame count
```

The system should remain responsive even when:

- multiple panes contain people
- multiple unknown people are present
- vision-event generation is triggered
- one pane is maximized
- one or more panes are minimized

---

# FRAME-LAG MONITORING

Add internal metrics for:

```text
capture_timestamp
processing_timestamp
display_timestamp
inference_latency
queue_latency
end_to_end_latency
```

Expose these metrics in debug mode.

The objective is to identify:

```text
Captured FPS:       20 FPS
Displayed FPS:      19 FPS
Detection FPS:      10 FPS
Average latency:    85 ms
Dropped frames:     3%
```

rather than blindly assuming the system is real-time.

---

# UI FLOW

The final user experience should be:

## SCREEN 1 — GRID SCANNING

```text
┌───────────────────────────────────────────────┐
│             CCTV MONITORING                   │
├───────────────────────────────────────────────┤
│                                               │
│       Combined Webcam CCTV Display           │
│                                               │
│       AI detected grid overlay                │
│                                               │
├───────────────────────────────────────────────┤
│ Detected Layout: [ 3 × 2 ▼ ]                  │
│                                               │
│ [ Scan Again ]       [ Finalise Grid Layout ] │
└───────────────────────────────────────────────┘
```

---

## SCREEN 2 — GRID ADJUSTMENT

```text
Combined CCTV View

┌──────────────┬──────────────┬──────────────┐
│              │              │              │
│   Pane 01    │   Pane 02    │   Pane 03    │
│              │              │              │
├──────────────┼──────────────┼──────────────┤
│   Pane 04    │   Pane 05    │   Pane 06    │
│              │              │              │
└──────────────┴──────────────┴──────────────┘

Drag borders to adjust pane boundaries.

                 [ Finalise Grid Layout ]
```

---

## SCREEN 3 — LIVE MONITORING

```text
┌────────────────────────┬────────────────────────┐
│ Pane 01        [⛶][−] │ Pane 02        [⛶][−] │
│ Main Gate              │ Lobby                  │
│ Ramesh                 │ Unknown                │
│ Walking                │ Standing               │
│ No Alert               │ ⚠ ALERT               │
├────────────────────────┼────────────────────────┤
│ Pane 03        [⛶][−] │ Pane 04        [⛶][−] │
│ Server Room            │ Floor 2                │
│ Priya                  │ Ramesh                 │
│ Sitting                │ Walking                │
│ No Alert               │ No Alert               │
└────────────────────────┴────────────────────────┘
```

---

# ARCHITECTURAL PRIORITY

Prioritize the following in this exact order:

1. **Real-time video continuity**
2. **Stable pane/grid extraction**
3. **Reliable person detection**
4. **Reliable person tracking**
5. **Reliable identity recognition**
6. **Unknown-person alerting**
7. **Continuous event logging**
8. **Meaningful activity description**
9. **UI richness**

Do not sacrifice video responsiveness to generate more detailed AI descriptions.

---

# DO NOT OVER-ENGINEER

Reuse the existing project architecture.

Do not introduce:

- unnecessary frameworks
- duplicate databases
- duplicate face-recognition pipelines
- unnecessary microservices
- expensive per-frame VLM calls
- unnecessary OCR calls
- unnecessary image processing
- unnecessary network calls

Prefer incremental changes that fit the current application.

---

# IMPLEMENTATION PHASE GATES

Use these gates:

### Gate 1
Architecture + performance audit complete.

### Gate 2
Grid detection + user adjustment complete.

### Gate 3
Finalized grid → stable pane extraction complete.

### Gate 4
Pane person detection + tracking complete.

### Gate 5
Existing identity recognition integrated.

### Gate 6
Unknown-person alert + existing tagging workflow integrated.

### Gate 7
Activity/event detection + VLM descriptions integrated.

### Gate 8
Minimize/maximize UI integrated.

### Gate 9
Full performance testing and optimization.

Do not move to the next gate until the current gate passes its acceptance criteria.

---

# FINAL VALIDATION SCENARIO

Test with a real CCTV display containing at least 6 panes.

Example:

```text
3 × 2 CCTV wall
```

Test:

1. Start webcam.
2. AI detects 3×2.
3. Verify grid overlay.
4. Change to 2×2.
5. Verify grid automatically adjusts.
6. Return to 3×2.
7. Drag horizontal/vertical borders.
8. Finalize.
9. Verify six stable panes.
10. Verify labels.
11. Verify fallback sequential labels where text is unavailable.
12. Verify person detection.
13. Verify known-person recognition.
14. Verify unknown-person alert.
15. Tag unknown person using existing Live Room Monitoring workflow.
16. Verify subsequent recognition.
17. Verify activity changes.
18. Verify event descriptions.
19. Verify duplicate-event suppression.
20. Minimize a pane.
21. Verify background monitoring continues.
22. Maximize another pane.
23. Verify all other panes continue monitoring.
24. Temporarily disconnect/reconnect webcam.
25. Verify recovery.
26. Measure FPS, latency, queue depth, and dropped frames.

---

# FINAL DELIVERABLE

After implementation provide:

1. Files modified
2. Files added
3. Architecture changes
4. Grid detection approach
5. Pane extraction approach
6. Tracking approach
7. Identity-recognition integration details
8. Alert logic
9. Event logging design
10. VLM invocation strategy
11. Real-time optimization strategy
12. Before/after FPS measurements
13. Known limitations
14. Tests performed
15. Any remaining bottlenecks

Most importantly:

> **Do not consider the implementation successful merely because all UI features work. The system is successful only if the complete pipeline remains responsive and suitable for real-time CCTV monitoring under multi-pane and multi-person load.**