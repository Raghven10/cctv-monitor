You are working on my real-time CCTV monitoring application.

## Objective

The application receives CCTV-wall video at approximately **10 FPS**, but the current end-to-end processing throughput is only approximately **1 FPS or less**.

This is causing excessive latency and may be contributing to:

- false/inconsistent grid/pane detection
- stale frame processing
- repeated UNKNOWN face alerts
- incorrect association between detected faces and CCTV panes
- delayed response after a user manually tags an unknown person

Your task is to inspect the existing codebase and fix the processing architecture so the system operates in **real time or near real time**.

Do NOT simply increase thread counts or lower model quality without first identifying the bottleneck.

---

# 1. First inspect the complete pipeline

Trace the actual flow from:

```text
CCTV/Webcam input
    ↓
frame acquisition
    ↓
frame buffering/queue
    ↓
grid/pane detection
    ↓
pane extraction
    ↓
face detection
    ↓
face tracking
    ↓
ArcFace embedding
    ↓
identity matching
    ↓
UNKNOWN/KNOWN decision
    ↓
manual tagging
    ↓
alerts/UI
```

Inspect especially:

- frame capture code
- frame queues/buffers
- producer/consumer logic
- grid detection
- pane detection
- face detection
- person tracking
- ArcFace inference
- identity registry
- manual tagging endpoint/action
- UNKNOWN alert generation
- frontend/backend communication
- any synchronous/blocking operations
- OCR/VLM processing
- database operations inside the real-time loop

Identify exactly why 10 FPS input becomes approximately 1 FPS processing.

Do not assume the cause. Measure it.

---

# 2. Add pipeline instrumentation

Add lightweight performance instrumentation.

Measure at minimum:

```text
input FPS
grid detection FPS
pane processing FPS
face detection FPS
tracking FPS
ArcFace FPS
recognition FPS
overall processed FPS
queue depth
processing latency
frame age when processed
```

Log something similar to:

```text
INPUT FPS: 10.0
PROCESS FPS: 1.2
FRAME AGE: 3.8 sec
QUEUE DEPTH: 37
GRID FPS: 0.8
FACE FPS: 1.1
ARCFACE FPS: 4.5
```

The exact implementation is up to you.

The important requirement is to determine whether the system is:

1. CPU/model inference bound
2. queue/backlog bound
3. OCR/VLM bound
4. grid detection bound
5. database/I/O bound
6. synchronization/locking bound
7. frontend/network bound

---

# 3. Fix stale-frame processing

This is critical.

For real-time CCTV monitoring, **do not allow an unbounded queue of old frames to accumulate**.

If input is:

```text
10 FPS
```

but processing capacity is:

```text
1 FPS
```

the application must NOT process:

```text
frame 1
frame 2
frame 3
...
frame 100
```

sequentially while the camera is already showing frame 200.

Instead, implement a bounded/latest-frame strategy.

Prefer:

```text
Camera
  ↓
Latest-frame buffer
  ↓
Processor
```

where stale frames can be dropped.

For example, conceptually:

```python
latest_frame = newest_available_frame()
```

rather than forcing the processor to consume every historical frame.

The system should prioritize **freshness over processing every frame**.

Do not change this blindly; inspect the existing architecture first and preserve any required frame ordering semantics.

---

# 4. Separate grid detection from high-frequency processing

Grid/pane geometry is relatively stable compared with faces.

Do NOT run expensive grid detection independently on every incoming frame.

Implement a cached/stable grid model.

Desired architecture:

```text
10 FPS input
     │
     ├───────────────► Grid detector
     │                    ~0.5–2 FPS
     │                       │
     │                       ▼
     │                 Stable grid layout
     │                       │
     │                       ▼
     │                  Cached geometry
     │
     └───────────────► Face processing
                          5–10 FPS target
```

The grid detector should:

1. Detect the grid initially.
2. Cache pane boundaries.
3. Reuse those boundaries for subsequent frames.
4. Periodically revalidate the grid.
5. Trigger grid re-detection only when evidence indicates that the layout changed.

Avoid continuously replacing a good grid estimate with noisy estimates from individual frames.

Use temporal stability/debouncing where appropriate.

For example:

```text
Grid candidate A
Grid candidate A
Grid candidate A
Grid candidate B
Grid candidate A
```

should not immediately cause the layout to jump between A and B.

---

# 5. Preserve fresh frame data for face processing

Face detection/tracking should operate at a substantially higher frequency than grid detection.

Target architecture:

```text
Frame stream
    ↓
Latest-frame buffer
    ↓
Stable pane geometry
    ↓
Face detector
    ↓
Tracker
    ↓
ArcFace only when necessary
```

Do not repeatedly perform expensive operations that can be reused.

---

# 6. Optimize ArcFace usage

The existing identity registry uses:

- InsightFace ArcFace ResNet-50
- 512-dimensional normalized embeddings
- 5-point facial alignment
- cosine similarity
- multi-vector gallery

The existing `IdentityRegistry.match_person()` already compares the query against the person's centroid and gallery. Preserve this behavior unless there is a demonstrated performance problem.

Do not run ArcFace unnecessarily on every frame for every face.

Use tracking to reduce recognition frequency.

For example:

```text
Face detected
    ↓
Track ID = 27
    ↓
ArcFace recognition
    ↓
Raghav
    ↓
Track remains stable
    ↓
Reuse identity for several frames
    ↓
Re-run ArcFace periodically or when tracking confidence drops
```

The exact recognition interval should be configurable.

Do not sacrifice recognition reliability merely to achieve an FPS number.

---

# 7. Fix manual single-frame tagging

The existing `IdentityRegistry.register_person()` already supports direct crop registration and creates an ArcFace embedding from the supplied crop.

Therefore, **single-frame tagging must remain supported**.

When the user tags an UNKNOWN face:

```text
Current frame
    ↓
Face crop
    ↓
ArcFace 512-D embedding
    ↓
register_person()
    ↓
known_persons database
```

The identity must immediately become available to the in-memory recognition registry as well as persistent storage.

Do not require the user to capture multiple frames.

The existing registry also supports optional multi-frame track history. Preserve that capability, but it must not block immediate single-frame registration.

---

# 8. Bind manual identity to the current track

This is extremely important.

When:

```text
Track ID = 27
```

is manually tagged:

```text
Track 27 → Person A
```

the current track must immediately use the manually assigned identity.

Do NOT wait for ArcFace to rediscover the identity on the next frame.

Conceptually:

```python
track.manual_identity = person_id
track.identity_source = "manual"
```

Then:

```text
if track has manual identity:
    use manual identity
else:
    perform normal recognition
```

This should immediately stop UNKNOWN alerts for the manually tagged current track.

---

# 9. Preserve identity after the track disappears

Track IDs are temporary.

Therefore:

```text
Track 27 → Person A
```

should be used for immediate continuity.

If the person disappears:

```text
Track 27 disappears
```

and later returns:

```text
Track 82
```

the system should use ArcFace against the persistent identity registry:

```text
Track 82
   ↓
ArcFace
   ↓
known_persons
   ↓
Person A
```

The existing persistent embedding database must remain the source for this.

---

# 10. Prevent UNKNOWN alert storms

UNKNOWN must not generate an alert on every frame.

Use track-level alert state.

Desired behavior:

```text
Track 27
   ↓
UNKNOWN
   ↓
confirmation/debounce
   ↓
ONE UNKNOWN alert
   ↓
suppress duplicate alerts
```

If the user then tags:

```text
Track 27 → Person A
```

the UNKNOWN alert state must be cleared/suppressed immediately.

Do not continue producing:

```text
UNKNOWN
UNKNOWN
UNKNOWN
UNKNOWN
```

for the same tracked person.

---

# 11. Do not blindly lower ArcFace threshold

The current identity registry uses a cosine threshold of approximately:

```text
0.48
```

Do not modify this simply because the application is slow.

First log recognition scores:

```text
Track 27
similarity = 0.71

Track 27
similarity = 0.68

Track 27
similarity = 0.42
```

If tagged people still become UNKNOWN, determine whether the issue is:

- identity registry not updated
- stale in-memory cache
- track identity not persisted
- inconsistent alignment
- embedding mismatch
- wrong model
- wrong dimensionality
- threshold
- stale frames

Only change the threshold if the evidence supports it.

---

# 12. Check ArcFace/SFace consistency

The primary recognition model is:

```text
ArcFace ResNet-50
512-D
```

The SFace fallback is:

```text
128-D
```

Never compare embeddings generated by different models as though they belong to the same embedding space.

Ensure the identity registry does not accidentally register an identity using one model and query it using another model.

If fallback behavior exists, make model identity explicit.

---

# 13. Fix unnecessary duplicate face alignment

Inspect the registration path carefully.

The current `register_person()` path appears to perform:

```text
align_face()
    ↓
extract_features()
    ↓
extract_features() calls align_face() again
    ↓
ArcFace
```

Remove unnecessary duplicate alignment while preserving the same ArcFace preprocessing and output.

Ensure that registration and live recognition use the same:

- landmark ordering
- affine transform
- 112×112 dimensions
- RGB conversion
- normalization
- ArcFace input tensor layout

---

# 14. Avoid blocking operations in the real-time loop

Identify operations such as:

```text
database commits
disk writes
JPEG/base64 encoding
OCR
VLM inference
network requests
frontend synchronization
logging
```

that are currently blocking frame processing.

Move expensive non-real-time operations to asynchronous/background workers where appropriate.

For example:

```text
Real-time path
    ↓
detect → track → recognize → alert

Background path
    ↓
DB persistence
snapshot saving
analytics
logging
optional gallery enrichment
```

Do not move an operation to a background thread if doing so would introduce race conditions or incorrect identity state.

---

# 15. Concurrency requirements

If multiple workers are introduced:

- avoid duplicate processing of the same stale frame
- protect shared identity state
- preserve thread safety
- avoid deadlocks
- avoid excessive locking around model inference
- do not create one ArcFace model session per frame
- reuse loaded ONNX Runtime sessions
- ensure database sessions are correctly scoped

Measure before and after.

---

# 16. Real-time target

The input is approximately:

```text
10 FPS
```

The target should be:

```text
Input:          ~10 FPS
Effective face processing: ideally 5–10 FPS
End-to-end latency: ideally <500 ms
```

If hardware cannot sustain 10 FPS for every expensive operation, the system should still maintain low latency by:

- dropping stale frames
- using latest-frame processing
- separating grid detection from face detection
- tracking faces
- throttling ArcFace intelligently
- running expensive OCR/VLM asynchronously

A stable 5 FPS with <500 ms latency is preferable to 1 FPS with several seconds of latency.

---

# 17. False grid detection

Investigate whether the current false grid detection is caused by:

- processing stale frames
- grid detection running too frequently
- transient CCTV content
- unstable pane boundaries
- OCR/VLM output changing between frames
- asynchronous results being applied to newer frames
- frame ordering problems

Every grid result should be associated with the frame/version from which it was calculated.

Do not allow an old grid-detection result to overwrite a newer stable grid state.

Use frame timestamps or monotonically increasing frame IDs where necessary.

---

# 18. Acceptance criteria

The implementation is complete only if all of the following are true:

### Performance

- 10 FPS input can be accepted continuously.
- The processing pipeline does not build an unbounded backlog.
- Frame age remains low.
- Effective processing FPS is substantially improved from the current ~1 FPS.
- Expensive grid detection is throttled/cached.
- Face tracking/recognition remains responsive.

### Grid

- Grid/pane geometry remains stable across frames.
- False grid changes are debounced.
- Old grid results cannot overwrite newer results.
- Grid detection is independently throttled from face processing.

### Face recognition

- ArcFace remains the primary recognition model.
- 512-D normalized embeddings remain compatible.
- Recognition uses the existing known-person registry.
- Tracking reduces unnecessary ArcFace inference.

### Manual tagging

A single captured frame can be tagged immediately:

```text
UNKNOWN → TAG PERSON → immediately KNOWN
```

The current track must immediately use the tagged identity.

### Alerts

After tagging:

```text
UNKNOWN alert → STOP
```

for the current track.

A person returning later should be recognized from the persistent ArcFace embedding.

### Reliability

- No race conditions.
- No stale-frame identity assignment.
- No stale grid result overwriting current state.
- No duplicate UNKNOWN alert storm.
- No regression in existing UI/API behavior.

---

# 19. Implementation process

Follow this process:

1. Inspect the repository.
2. Trace the entire frame-processing pipeline.
3. Identify the actual bottleneck.
4. Add instrumentation.
5. Implement the minimum architectural changes necessary.
6. Run tests.
7. Run the application if possible.
8. Measure FPS and latency again.
9. Verify manual single-frame tagging.
10. Verify that the tagged person does not revert to UNKNOWN.
11. Verify grid stability.
12. Verify that returning people can be recognized.
13. Report exactly what files were changed and why.

Do not make speculative large-scale rewrites.

Preserve existing functionality and APIs wherever possible.

At the end, provide a concise report containing:

```text
1. Root cause of ~1 FPS processing
2. Root cause of false grid detection
3. Changes made
4. Files modified
5. Before/after FPS
6. Before/after latency
7. Manual tagging behavior
8. UNKNOWN alert behavior
9. Any remaining bottlenecks
```

Most importantly: **optimize for fresh, low-latency CCTV processing rather than processing every historical frame.**