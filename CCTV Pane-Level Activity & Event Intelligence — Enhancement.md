# PANE-LEVEL ACTIVITY & EVENT INTELLIGENCE

After the user finalizes the grid and each CCTV pane becomes a stable ROI, the system must transition from **pane detection mode** into **continuous activity/event monitoring mode**.

The objective is not merely to detect people.

The system must understand meaningful activities occurring inside each pane, such as:

- person entering a door
- person exiting a door
- door opening
- door closing
- person approaching a restricted door
- person entering a restricted area
- authorized person entering an authorized door
- unauthorized person entering a restricted door
- person sitting at a workstation
- person starting to work on a computer
- person leaving a workstation
- person remaining inactive for a configurable duration
- multiple people entering together
- person carrying an object
- person falling
- prolonged presence in a restricted area
- other configurable security/workplace events

---

# 1. ACTIVITY MONITORING MUST BE PANE-SPECIFIC

Every finalized pane must maintain its own monitoring configuration and state.

Example:

```text
Pane-01
Location: Main Entrance

Configured Events:
    ✓ Person Entry
    ✓ Person Exit
    ✓ Door Open
    ✓ Door Close
    ✓ Unauthorized Entry

Pane-02
Location: Control Room

Configured Events:
    ✓ Person Entry
    ✓ Computer Activity
    ✓ Unauthorized Person

Pane-03
Location: Server Room

Configured Events:
    ✓ Door Open
    ✓ Authorized Entry
    ✓ Unauthorized Entry
    ✓ Prolonged Presence
```

Do not apply identical event rules blindly to every pane.

---

# 2. REGION OF INTEREST (ROI) / ZONE CONFIGURATION

After finalizing the grid, allow the user to define meaningful regions inside a pane.

For example:

```text
Pane-01 — Main Entrance

┌──────────────────────────────┐
│                              │
│          ENTRY ZONE          │
│                              │
│       ┌──────────────┐       │
│       │    DOOR      │       │
│       │     ROI      │       │
│       └──────────────┘       │
│                              │
└──────────────────────────────┘
```

Allow configurable zones such as:

```text
Door Zone
Entry Zone
Exit Zone
Restricted Zone
Workstation Zone
Computer Zone
Desk Zone
Waiting Zone
```

The user should be able to draw/edit these zones using the same interactive UI concept used for pane boundaries.

Store zones using normalized coordinates.

---

# 3. EVENT RULE ENGINE

Implement a configurable event/rule engine.

Conceptually:

```text
Raw Frames
     ↓
Person/Object Detection
     ↓
Tracking
     ↓
Zone Interaction
     ↓
Temporal State
     ↓
Event Rule Engine
     ↓
Meaningful Event
     ↓
Vision Model Confirmation/Description
     ↓
Alert + Event Log
```

Do NOT invoke the vision model for every frame.

---

# 4. DOOR EVENTS

For a configured door ROI, detect:

### Door opening

Example:

```text
Door state:
CLOSED
   ↓
OPENING
   ↓
OPEN
```

Generate:

```text
DOOR_OPENED
```

Example event:

```text
10:21:34
Pane-01 / Main Entrance

Door opened.
```

### Door closing

```text
OPEN
 ↓
CLOSING
 ↓
CLOSED
```

Generate:

```text
DOOR_CLOSED
```

Use temporal smoothing/debouncing so small visual fluctuations do not create repeated events.

---

# 5. PERSON ENTERING THROUGH A DOOR

This is more important than simply detecting that a person exists.

Track the person's movement relative to the door/entry zone.

Example:

```text
Outside Zone
     ↓
Door Zone
     ↓
Inside Zone
```

Generate:

```text
PERSON_ENTERED
```

Example:

```text
10:22:10
Ramesh entered through Main Entrance.
```

Likewise:

```text
Inside Zone
     ↓
Door Zone
     ↓
Outside Zone
```

Generate:

```text
PERSON_EXITED
```

---

# 6. AUTHORIZED VS UNAUTHORIZED ENTRY

Each door/zone should optionally have an authorization policy.

Example:

```text
Door: Server Room

Authorized Personnel:
    Ramesh
    Priya
    Amit
```

When a person crosses the door boundary:

```text
Person detected
      ↓
Track person
      ↓
Identify person
      ↓
Check door authorization policy
      ↓
AUTHORIZED / UNAUTHORIZED
```

### Authorized

```text
Ramesh
Server Room
AUTHORIZED ENTRY

No security alert.
```

### Unauthorized

```text
Unknown Person
Server Room
UNAUTHORIZED ENTRY

⚠ SECURITY ALERT
```

If the person is known but not authorized:

```text
Person: Amit
Identity: CONFIRMED
Authorization: NOT PERMITTED

⚠ UNAUTHORIZED ENTRY
```

This distinction is critical.

**Unknown ≠ unauthorized known person.**

Maintain separate states:

```text
IDENTIFIED + AUTHORIZED
IDENTIFIED + UNAUTHORIZED
UNKNOWN
```

---

# 7. AUTHORIZATION POLICY MODEL

Authorization should be configurable rather than hard-coded.

Example:

```json
{
  "zone": "server_room",
  "authorized_persons": [
    "person_001",
    "person_007"
  ],
  "events": {
    "entry": true,
    "exit": true
  }
}
```

The architecture should eventually support policies such as:

```text
Person X:
    Allowed:
        Main Gate
        Office

    Restricted:
        Server Room

Person Y:
    Allowed:
        Server Room
        Control Room
```

Where practical, also allow:

```text
Time-based authorization
```

For example:

```text
Ramesh
Server Room
08:00–18:00
```

An entry at 22:30 should generate:

```text
AUTHORIZED PERSON
BUT
OUTSIDE AUTHORIZED TIME
```

and therefore:

```text
⚠ POLICY VIOLATION
```

---

# 8. COMPUTER/WORKSTATION ACTIVITY

The system must detect meaningful interaction with a computer rather than simply detecting that a person is sitting near a computer.

Example:

```text
Person enters workstation zone
          ↓
Person approaches desk
          ↓
Person sits
          ↓
Person interacts with computer
```

Potential events:

```text
WORKSTATION_APPROACHED
PERSON_SAT_DOWN
COMPUTER_INTERACTION_STARTED
COMPUTER_INTERACTION_CONTINUING
COMPUTER_INTERACTION_STOPPED
PERSON_LEFT_WORKSTATION
```

---

# 9. COMPUTER ACTIVITY DETECTION

For a configured workstation:

```text
┌──────────────────────────────┐
│                              │
│       Person Zone            │
│                              │
│        ┌──────────┐          │
│        │ Monitor  │          │
│        └──────────┘          │
│           Desk               │
│                              │
└──────────────────────────────┘
```

Use multiple signals where possible:

- person position
- person pose/orientation
- hand/arm movement
- proximity to workstation
- monitor/keyboard region
- temporal movement patterns
- optional vision-model confirmation

Do not rely solely on a single frame.

---

# 10. COMPUTER ACTIVITY STATE MACHINE

Maintain temporal state:

```text
AWAY
  ↓
APPROACHING
  ↓
AT_WORKSTATION
  ↓
INTERACTING
  ↓
INACTIVE
  ↓
LEFT
```

Example:

```text
10:30:01
Ramesh approached workstation.

10:30:08
Ramesh sat at workstation.

10:30:15
Computer interaction detected.

10:42:33
No significant interaction detected.

10:45:10
Ramesh left workstation.
```

The exact event timing must be configurable.

---

# 11. VISION MODEL SHOULD ACT AS A SEMANTIC LAYER

Do not use the VLM as the primary high-frequency detector.

Instead:

```text
Fast CV models
     ↓
Detect movement/change
     ↓
Trigger candidate event
     ↓
VLM verifies/interprets
     ↓
Generate semantic description
```

For example:

```text
Person track crosses door ROI
            ↓
Candidate:
PERSON_ENTERED
            ↓
VLM receives selected frame(s)
            ↓
"Ramesh entered the server room through the main door."
```

This provides semantic understanding without sacrificing real-time FPS.

---

# 12. TEMPORAL EVENT CONFIRMATION

Never trigger important events from a single noisy frame.

For example:

```text
Frame 1: Door appears open
Frame 2: Door appears closed
Frame 3: Door appears open
```

must not generate:

```text
OPEN
CLOSE
OPEN
```

Instead use temporal confirmation.

Example:

```text
Potential event detected
       ↓
Observe N frames / T milliseconds
       ↓
Confirm state transition
       ↓
Generate event
```

The exact thresholds should be configurable and determined through testing.

---

# 13. TRACKING IS CENTRAL

Use persistent tracking IDs.

Example:

```text
Pane-02

Track-17
    ↓
identified as Ramesh
    ↓
approaches Door-01
    ↓
crosses Door-01
    ↓
enters Restricted-Zone
```

The event engine should operate on the **track state**, not independently on raw detections.

This allows the system to understand:

```text
WHERE
WHO
WHEN
DIRECTION
WHAT CHANGED
```

rather than simply:

```text
PERSON DETECTED
```

---

# 14. DIRECTION DETECTION

For doors and boundaries, determine movement direction.

Example:

```text
OUTSIDE → DOOR → INSIDE
```

means:

```text
ENTRY
```

while:

```text
INSIDE → DOOR → OUTSIDE
```

means:

```text
EXIT
```

Use trajectory/history of the tracked person's centroid or appropriate body reference point.

---

# 15. EVENT DEDUPLICATION

The same event must not be generated repeatedly.

Bad:

```text
Person entered
Person entered
Person entered
Person entered
```

Correct:

```text
10:32:11
Person entered Main Entrance.
```

Then maintain the person's state until the next meaningful transition.

Implement:

- event cooldown
- state transition detection
- track association
- temporal hysteresis
- duplicate suppression

---

# 16. EVENT SEVERITY

Events should have configurable severity.

Example:

```text
INFO
    Person entered workstation

NORMAL
    Door opened

WARNING
    Unknown person entered

HIGH
    Known but unauthorized person entered restricted area

CRITICAL
    Unauthorized access to critical zone
```

The UI should visually distinguish alert severity.

---

# 17. EVENT CONFIGURATION UI

Each finalized pane should provide an option:

```text
[ Configure Events ]
```

Example:

```text
Pane: Server Room

Events

☑ Person Entry
☑ Person Exit
☑ Door Open
☑ Door Close
☑ Unauthorized Entry
☑ Unknown Person
☐ Computer Activity
☐ Loitering

Zones

[ + Add Zone ]

Door-01
Restricted-Zone
```

This makes the system extensible without changing code for every new event.

---

# 18. EVENT LOGGING

Every meaningful event must be associated with:

```text
timestamp
pane_id
pane_label
zone_id
event_type
track_id
person_id
identity
authorization_status
confidence
description
severity
alert_status
```

Example:

```text
21:42:31
Pane: Server Room
Zone: Door-01

Event:
UNAUTHORIZED_ENTRY

Person:
Amit

Identity:
IDENTIFIED

Authorization:
DENIED

Description:
"Amit entered the server room through Door-01."

Severity:
HIGH

Alert:
GENERATED
```

---

# 19. REAL-TIME REQUIREMENT

Activity detection must NOT reduce monitoring FPS significantly.

Use a multi-rate architecture:

```text
                 ┌── Display: 15–30 FPS
                 │
Webcam ─→ Capture├── Tracking: high frequency
                 │
                 ├── Detection: configurable FPS
                 │
                 ├── Face Recognition: event/track based
                 │
                 ├── Rule Engine: continuous lightweight
                 │
                 └── VLM: event triggered only
```

The VLM must never sit directly inside the primary frame-processing loop.

---

# 20. IMPORTANT EVENT PRIORITY

The system should prioritize:

```text
1. Person tracking
2. Door crossing
3. Restricted-zone entry
4. Authorization verification
5. Unknown-person detection
6. Significant activity change
7. Event description
8. Secondary analytics
```

If the system becomes overloaded, reduce VLM/event-analysis frequency before sacrificing the primary monitoring pipeline.

---

# 21. FUTURE EXTENSIBILITY

Design the event system so additional events can be added through rules rather than rewriting the video pipeline.

For example:

```text
EventDetector
    ├── DoorDetector
    ├── ZoneEntryDetector
    ├── ZoneExitDetector
    ├── PersonActivityDetector
    ├── WorkstationDetector
    ├── LoiteringDetector
    ├── FallDetector
    └── CustomEventDetector
```

Each detector should produce a standardized event:

```json
{
  "pane_id": "pane-03",
  "zone_id": "door-01",
  "event_type": "UNAUTHORIZED_ENTRY",
  "track_id": "track-17",
  "person_id": "person-004",
  "confidence": 0.93,
  "timestamp": "...",
  "severity": "HIGH"
}
```

The alerting, logging, UI and VLM layers should consume this common event format.

---

# ACCEPTANCE TESTS

The implementation is not complete until the following scenarios work reliably:

### Scenario 1 — Authorized entry

```text
Ramesh
   ↓
Approaches Door-01
   ↓
Door opens
   ↓
Ramesh crosses entry boundary
   ↓
Identity recognized
   ↓
Authorization verified
   ↓
AUTHORIZED ENTRY
   ↓
Event logged
   ↓
No alert
```

### Scenario 2 — Unknown person

```text
Unknown person
   ↓
Door opens
   ↓
Person enters
   ↓
Identity unavailable
   ↓
UNKNOWN ENTRY
   ↓
Alert
   ↓
[Tag Person]
```

### Scenario 3 — Known but unauthorized

```text
Amit
   ↓
Identity recognized
   ↓
Attempts Server Room entry
   ↓
Authorization = DENIED
   ↓
UNAUTHORIZED ENTRY
   ↓
HIGH severity alert
```

### Scenario 4 — Computer activity

```text
Ramesh
   ↓
Approaches workstation
   ↓
Sits
   ↓
Interacts with computer
   ↓
COMPUTER_INTERACTION_STARTED
   ↓
Continuous state tracking
   ↓
Leaves workstation
   ↓
COMPUTER_INTERACTION_STOPPED
```

### Scenario 5 — No repeated events

A person remaining at a workstation for 30 minutes must NOT generate thousands of identical activity events.

### Scenario 6 — Multiple simultaneous panes

Six or more panes must continue monitoring simultaneously without the VLM or event processing causing unacceptable frame lag.

---

# FINAL ARCHITECTURAL PRINCIPLE

The system should evolve from:

```text
CCTV Pane
   ↓
Person Detection
```

into:

```text
CCTV Pane
   ↓
Person/Object Detection
   ↓
Tracking
   ↓
Spatial Zones
   ↓
Temporal State
   ↓
Event/Rule Engine
   ↓
Identity + Authorization
   ↓
Semantic Vision Analysis
   ↓
Event
   ↓
Alert + Log
```

The goal is to make the application behave like an **intelligent real-time CCTV monitoring system**, rather than a collection of independent image classifiers.

Do not sacrifice real-time video continuity for semantic analysis. Fast CV/tracking should continuously maintain the scene state, while expensive vision-model reasoning should be invoked selectively when the system detects a meaningful state transition.