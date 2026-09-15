import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import cv2
import numpy as np

from .identity_registry import global_identity_registry
from .interfaces import DetectionBox, PersonDetector
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.person_detector")


def calculate_face_sharpness(image: np.ndarray) -> float:
    """Calculate Laplacian variance to assess image blurriness."""
    if image is None or image.size == 0:
        return 0.0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def calculate_iou(boxA: Tuple[int, int, int, int], boxB: Tuple[int, int, int, int]) -> float:
    """Calculate Intersection over Union (IoU) between two bounding boxes (x, y, w, h)."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[0] + boxA[2], boxB[0] + boxB[2])
    yB = min(boxA[1] + boxA[3], boxB[1] + boxB[3])

    interW = max(0, xB - xA)
    interH = max(0, yB - yA)
    interArea = interW * interH

    boxAArea = boxA[2] * boxA[3]
    boxBArea = boxB[2] * boxB[3]
    unionArea = float(boxAArea + boxBArea - interArea)

    if unionArea <= 0.0:
        return 0.0
    return interArea / unionArea


class FaceTrackBuffer:
    """
    Rolling Multi-Frame Track Buffer.
    Associates face detections across consecutive frames using Spatial-Centroid + IoU tracking.
    Maintains a rolling buffer of up to 50 high-quality face embeddings and snapshots per track.
    Retains track history for up to 120 seconds after the person leaves the camera view.
    """

    def __init__(
        self,
        max_samples_per_track: int = 50,
        track_ttl_seconds: float = 120.0,
        iou_threshold: float = 0.20,
    ):
        self.max_samples_per_track = max_samples_per_track
        self.track_ttl_seconds = track_ttl_seconds
        self.iou_threshold = iou_threshold
        self._tracks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._next_track_idx: int = 1

    def match_or_create_track(
        self,
        bbox: Tuple[int, int, int, int],
        now: Optional[float] = None,
    ) -> str:
        """Associate a detected face bounding box to an active track or generate a new track ID."""
        now = now or time.time()
        best_track_id: Optional[str] = None
        best_iou: float = 0.0

        with self._lock:
            # Clean up old tracks exceeding TTL
            expired_keys = [
                tid for tid, trk in self._tracks.items()
                if (now - trk["last_seen"]) > self.track_ttl_seconds
            ]
            for tid in expired_keys:
                del self._tracks[tid]

            # Find active track with highest IoU or spatial proximity
            for tid, trk in self._tracks.items():
                if (now - trk["last_seen"]) <= 2.5:  # Active within last 2.5 seconds
                    iou = calculate_iou(bbox, trk["bbox"])
                    if iou > best_iou:
                        best_iou = iou
                        best_track_id = tid

            if best_track_id is None or best_iou < self.iou_threshold:
                # Create a new track
                best_track_id = f"trk_{uuid.uuid4().hex[:6]}"
                self._tracks[best_track_id] = {
                    "track_id": best_track_id,
                    "bbox": bbox,
                    "first_seen": now,
                    "last_seen": now,
                    "embeddings": [],
                    "snapshots": [],
                    "best_snapshot": "",
                    "best_sharpness": 0.0,
                    "matched_person_id": None,
                    "matched_name": None,
                    "matched_tag": None,
                    "last_rec_time": 0.0,
                    "is_manual": False,
                    "hit_count": 1,
                }
            else:
                self._tracks[best_track_id]["bbox"] = bbox
                self._tracks[best_track_id]["last_seen"] = now
                self._tracks[best_track_id]["hit_count"] = self._tracks[best_track_id].get("hit_count", 0) + 1

        return best_track_id

    def bind_manual_identity(
        self,
        track_id: str,
        person_id: str,
        name: str,
        tag: str = "Authorized",
    ) -> None:
        """Immediately bind a manually assigned identity to an active spatial track."""
        now = time.time()
        with self._lock:
            if track_id not in self._tracks:
                self._tracks[track_id] = {
                    "track_id": track_id,
                    "bbox": (0, 0, 100, 100),
                    "first_seen": now,
                    "last_seen": now,
                    "embeddings": [],
                    "snapshots": [],
                    "best_snapshot": "",
                    "best_sharpness": 0.0,
                }

            trk = self._tracks[track_id]
            trk["matched_person_id"] = person_id
            trk["manual_person_id"] = person_id
            trk["manual_name"] = name
            trk["manual_tag"] = tag
            trk["matched_name"] = name
            trk["matched_tag"] = tag
            trk["is_manual"] = True
            trk["last_rec_time"] = now
            logger.info(f"🏷️ Track {track_id} bound to manual identity '{name}' ({person_id})")

    def get_track_identity_state(self, track_id: str) -> Dict[str, Any]:
        """Check if track has an active cached or manual identity."""
        with self._lock:
            if track_id in self._tracks:
                trk = self._tracks[track_id]
                return {
                    "is_manual": trk.get("is_manual", False),
                    "person_id": trk.get("manual_person_id") or trk.get("matched_person_id"),
                    "name": trk.get("manual_name") or trk.get("matched_name"),
                    "tag": trk.get("manual_tag") or trk.get("matched_tag", "Authorized"),
                    "last_rec_time": trk.get("last_rec_time", 0.0),
                    "best_snapshot": trk.get("best_snapshot", ""),
                    "hit_count": trk.get("hit_count", 0),
                }
        return {"is_manual": False, "person_id": None, "name": None, "tag": "Intruder", "last_rec_time": 0.0, "best_snapshot": "", "hit_count": 0}

    def clear(self) -> None:
        """Clear all active spatial face tracks."""
        with self._lock:
            self._tracks.clear()
            logger.info("Cleared all active face track buffers")

    def clear_person_binding(self, person_id: str) -> None:
        """Clear identity bindings for a deleted person across all active tracks."""
        with self._lock:
            for trk in self._tracks.values():
                if trk.get("matched_person_id") == person_id or trk.get("manual_person_id") == person_id:
                    trk["matched_person_id"] = None
                    trk["manual_person_id"] = None
                    trk["matched_name"] = None
                    trk["manual_name"] = None
                    trk["matched_tag"] = None
                    trk["manual_tag"] = None
                    trk["is_manual"] = False

    def add_sample(
        self,
        track_id: str,
        embedding: List[float],
        snapshot_b64: str,
        aligned_crop: np.ndarray,
        matched_person_id: Optional[str] = None,
        matched_name: Optional[str] = None,
        matched_tag: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        """Add a high-quality frame embedding and snapshot into the track's rolling buffer."""
        now = now or time.time()
        sharpness = calculate_face_sharpness(aligned_crop) if aligned_crop is not None else 0.0

        with self._lock:
            if track_id not in self._tracks:
                return

            trk = self._tracks[track_id]
            if matched_person_id:
                trk["matched_person_id"] = matched_person_id
                trk["matched_name"] = matched_name
                trk["matched_tag"] = matched_tag
                trk["last_rec_time"] = now

            if embedding and snapshot_b64:
                if len(trk["embeddings"]) < self.max_samples_per_track:
                    trk["embeddings"].append(embedding)
                    trk["snapshots"].append(snapshot_b64)
                else:
                    trk["embeddings"].pop(0)
                    trk["snapshots"].pop(0)
                    trk["embeddings"].append(embedding)
                    trk["snapshots"].append(snapshot_b64)

                if sharpness >= trk["best_sharpness"] or not trk["best_snapshot"]:
                    trk["best_sharpness"] = sharpness
                    trk["best_snapshot"] = snapshot_b64

    def get_track_history(self, track_id: str) -> Dict[str, Any]:
        """Retrieve all accumulated frame embeddings and best snapshot for a track."""
        with self._lock:
            if track_id in self._tracks:
                trk = self._tracks[track_id]
                return {
                    "track_id": track_id,
                    "count": len(trk["embeddings"]),
                    "embeddings": list(trk["embeddings"]),
                    "best_snapshot": trk["best_snapshot"] or (trk["snapshots"][-1] if trk["snapshots"] else ""),
                }
        return {"track_id": track_id, "count": 0, "embeddings": [], "best_snapshot": ""}


# Global face track buffer singleton
global_face_track_buffer = FaceTrackBuffer()


class FastLivePersonDetector(PersonDetector):
    """
    High-precision face-centric person detector powered by OpenCV's YuNet
    Deep Neural Network Face Detector (with Haar+Eye cascade fallback).
    Extracts 5 facial landmark points for ArcFace deep metric alignment.
    Maintains a 50-frame rolling track buffer for robust multi-frame identity enrollment.
    """

    def __init__(self, score_threshold: float = 0.65, nms_threshold: float = 0.30):
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.yunet_detector: Optional[cv2.FaceDetectorYN] = None
        self._current_input_size = (320, 320)
        self._detector_lock = threading.Lock()

        # 1. Initialize YuNet Neural Detector
        model_candidates = [
            "data/face_detection_yunet_2023mar.onnx",
            os.path.join(os.path.dirname(__file__), "../../../data/face_detection_yunet_2023mar.onnx"),
            os.path.abspath("data/face_detection_yunet_2023mar.onnx"),
        ]

        model_path = None
        for path in model_candidates:
            if os.path.exists(path):
                model_path = path
                break

        if model_path is not None and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.yunet_detector = cv2.FaceDetectorYN.create(
                    model_path,
                    "",
                    self._current_input_size,
                    score_threshold=self.score_threshold,
                    nms_threshold=self.nms_threshold,
                )
                logger.info(f"Initialized Deep Neural YuNet Face Detector from {model_path}")
            except Exception as e:
                logger.warning(f"Failed to load YuNet ONNX detector: {e}")
                self.yunet_detector = None

        # 2. Strict Fallback Cascades (Haar Frontal + Eye Verification)
        self.face_cascade = None
        self.eye_cascade = None
        try:
            cascades_dir = getattr(cv2, "data", None)
            cascades_path = cascades_dir.haarcascades if cascades_dir and hasattr(cascades_dir, "haarcascades") else ""
            cascade_cls = getattr(cv2, "CascadeClassifier", None)
            if cascade_cls is not None:
                self.face_cascade = cascade_cls(cascades_path + "haarcascade_frontalface_alt2.xml")
                self.eye_cascade = cascade_cls(cascades_path + "haarcascade_eye.xml")
        except Exception as e:
            logger.debug(f"Haar cascade initialization skipped: {e}")

    def detect_persons(self, image: np.ndarray, pane_id: str = "DIRECT_CAM") -> List[DetectionBox]:
        """
        Detect authentic human faces using Deep Neural YuNet or verified Haar+Eye features.
        Rejects inanimate objects (tables, chairs, monitors, wall textures).
        Extracts 5 facial landmark points for ArcFace similarity-transformation alignment.
        """
        if image is None or image.size == 0:
            return []

        h, w = image.shape[:2]
        if h < 28 or w < 28:
            return []

        # List of (x, y, w, h, conf, landmarks)
        candidate_faces: List[Tuple[int, int, int, int, float, Optional[np.ndarray]]] = []

        if self.yunet_detector is not None:
            try:
                with self._detector_lock:
                    # Resize neural detector input plane if dimensions changed
                    if self._current_input_size != (w, h):
                        self._current_input_size = (w, h)
                        self.yunet_detector.setInputSize((w, h))

                    _, detections = self.yunet_detector.detect(image)
                if detections is not None:
                    for det in detections:
                        fx = int(det[0])
                        fy = int(det[1])
                        fw = int(det[2])
                        fh = int(det[3])
                        conf = float(det[14])

                        # Neural validation filters
                        if conf < self.score_threshold:
                            continue

                        # Minimum face size to capture authentic faces (at least 28x28 px)
                        if fw < 28 or fh < 28:
                            continue

                        # Human face aspect ratio validation (0.60 <= w/h <= 1.40)
                        aspect = float(fw) / float(fh) if fh > 0 else 0.0
                        if aspect < 0.60 or aspect > 1.40:
                            continue

                        # Clamp to image boundaries
                        x1 = max(0, fx)
                        y1 = max(0, fy)
                        x2 = min(w, fx + fw)
                        y2 = min(h, fy + fh)
                        cw = x2 - x1
                        ch = y2 - y1

                        if cw >= 28 and ch >= 28:
                            # Sharpness verification: Reject blurry non-face textures
                            crop = image[y1 : y2, x1 : x2]
                            if calculate_face_sharpness(crop) < 3.0:
                                continue

                            # 5 landmark points: right eye, left eye, nose tip, right mouth, left mouth
                            landmarks = np.array(
                                [
                                    [det[4], det[5]],
                                    [det[6], det[7]],
                                    [det[8], det[9]],
                                    [det[10], det[11]],
                                    [det[12], det[13]],
                                ],
                                dtype=np.float32,
                            )
                            candidate_faces.append((x1, y1, cw, ch, conf, landmarks))
            except Exception as e:
                logger.debug(f"YuNet inference error: {e}")

        # Fallback to strict verified Haar with Eye Confirmation ONLY if YuNet neural detector is unavailable
        if not candidate_faces and (self.yunet_detector is None and self.face_cascade is not None and not self.face_cascade.empty()):
            haar_faces = self._detect_haar_verified(image)
            for hf in haar_faces:
                candidate_faces.append((hf[0], hf[1], hf[2], hf[3], hf[4], None))

        # Apply Non-Maximum Suppression (NMS) to eliminate duplicate/overlapping boxes
        if len(candidate_faces) > 1:
            nms_boxes = [[f[0], f[1], f[2], f[3]] for f in candidate_faces]
            nms_scores = [float(f[4]) for f in candidate_faces]
            indices = cv2.dnn.NMSBoxes(nms_boxes, nms_scores, score_threshold=self.score_threshold, nms_threshold=0.30)
            if len(indices) > 0:
                indices = [int(i[0]) if isinstance(i, (list, np.ndarray)) else int(i) for i in indices]
                candidate_faces = [candidate_faces[i] for i in indices]

        now = time.time()
        boxes: List[DetectionBox] = []
        for idx, (bx, by, bw, bh, conf, landmarks) in enumerate(candidate_faces):
            bbox = (bx, by, bw, bh)
            track_id = global_face_track_buffer.match_or_create_track(bbox, now=now)

            # Check if track has manual identity or recently evaluated identity
            track_state = global_face_track_buffer.get_track_identity_state(track_id)

            # 1. Manual identity takes top priority (instant 0-latency matching, skip ArcFace)
            if track_state["is_manual"] and track_state["person_id"]:
                crop = image[by : by + bh, bx : bx + bw]
                snapshot_b64 = track_state["best_snapshot"] or (global_identity_registry.image_to_base64(crop) if crop.size > 0 else "")
                boxes.append(
                    DetectionBox(
                        bbox=bbox,
                        confidence=1.00,
                        class_name="KNOWN_PERSON",
                        pane_id=pane_id,
                        person_id=track_state["person_id"],
                        person_name=track_state["name"] or "Authorized Person",
                        tag=track_state["tag"] or "Authorized",
                        is_known=True,
                        snapshot_base64=snapshot_b64,
                    )
                )
                continue

            # 2. Re-use cached identity if evaluated within last 2.0 seconds
            if track_state["person_id"] and (now - track_state["last_rec_time"]) < 2.0:
                crop = image[by : by + bh, bx : bx + bw]
                snapshot_b64 = track_state["best_snapshot"] or (global_identity_registry.image_to_base64(crop) if crop.size > 0 else "")
                boxes.append(
                    DetectionBox(
                        bbox=bbox,
                        confidence=round(conf, 2),
                        class_name="KNOWN_PERSON",
                        pane_id=pane_id,
                        person_id=track_state["person_id"],
                        person_name=track_state["name"] or "Authorized Person",
                        tag=track_state["tag"] or "Authorized",
                        is_known=True,
                        snapshot_base64=snapshot_b64,
                    )
                )
                continue

            # 3. Full ArcFace evaluation for new tracks or periodic re-evaluation (~every 2.0s)
            crop = image[by : by + bh, bx : bx + bw]
            if crop.size == 0:
                continue

            aligned_face = global_identity_registry.align_face(crop, landmarks=landmarks, full_image=image)
            snapshot_b64 = global_identity_registry.image_to_base64(aligned_face)

            # Extract embedding & match against identity registry
            embedding, _ = global_identity_registry.extract_features(aligned_face)
            is_known, matched_record, sim_score = global_identity_registry.match_person(aligned_face)

            global_face_track_buffer.add_sample(
                track_id=track_id,
                embedding=embedding,
                snapshot_b64=snapshot_b64,
                aligned_crop=aligned_face,
                matched_person_id=matched_record.person_id if is_known and matched_record else None,
                matched_name=matched_record.name if is_known and matched_record else None,
                matched_tag=matched_record.tag if is_known and matched_record else None,
                now=now,
            )

            if is_known and matched_record is not None:
                if sim_score >= 0.65 and embedding:
                    global_identity_registry.add_online_sample(matched_record.person_id, embedding)

                # Update POI last seen cache
                global_identity_registry.update_last_seen(
                    person_id=matched_record.person_id,
                    snapshot_b64=snapshot_b64,
                    timestamp=now,
                    pane_id=pane_id
                )

                boxes.append(
                    DetectionBox(
                        bbox=bbox,
                        confidence=round(max(conf, sim_score), 2),
                        class_name="KNOWN_PERSON",
                        pane_id=pane_id,
                        person_id=matched_record.person_id,
                        person_name=matched_record.name,
                        tag=matched_record.tag,
                        is_known=True,
                        snapshot_base64=snapshot_b64,
                    )
                )
            else:
                boxes.append(
                    DetectionBox(
                        bbox=bbox,
                        confidence=round(conf, 2),
                        class_name="UNKNOWN_PERSON",
                        pane_id=pane_id,
                        person_id=track_id,
                        person_name="Unknown Person",
                        tag="Intruder",
                        is_known=False,
                        snapshot_base64=snapshot_b64,
                    )
                )

        return boxes


class YoloV11PersonDetector(PersonDetector):
    """
    State-of-the-Art Full-Body Person & Activity Detector powered by Ultralytics YOLOv11.
    
    Features:
    - Real-time multi-person full-body detection across all orientations (front, back, walking, crouching).
    - Seamlessly crops upper head/face regions and integrates with ArcFace for high-accuracy identity recognition.
    - Preserves tracked identity across frame sequences via temporal track buffer.
    - Gracefully falls back to YuNet neural face detection if YOLO weights are initializing.
    """

    def __init__(
        self,
        model_name: str = "yolo11n.pt",
        score_threshold: float = 0.35,
        nms_threshold: float = 0.40,
        device: str = "cpu",
    ):
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.device = device
        self.model = None
        self._fallback_yunet = FastLivePersonDetector(score_threshold=0.60, nms_threshold=0.30)
        self._lock = threading.Lock()

        # Initialize YOLOv11
        try:
            from ultralytics import YOLO

            model_candidates = [
                f"data/{model_name}",
                os.path.join(os.path.dirname(__file__), f"../../../data/{model_name}"),
                model_name,
            ]
            chosen_path = model_name
            for path in model_candidates:
                if os.path.exists(path):
                    chosen_path = path
                    break

            self.model = YOLO(chosen_path)
            logger.info(f"✅ Loaded YOLOv11 Model from [{chosen_path}] on device [{device}]")
        except Exception as e:
            logger.warning(f"YOLOv11 initialization notice ({e}). Using YuNet neural detector fallback.")
            self.model = None

    def detect_persons(self, image: np.ndarray, pane_id: str = "DIRECT_CAM") -> List[DetectionBox]:
        if image is None or image.size == 0:
            return []

        if self.model is None:
            return self._fallback_yunet.detect_persons(image, pane_id=pane_id) or []

        h, w = image.shape[:2]
        if h < 28 or w < 28:
            return []

        now = time.time()
        detected_boxes: List[DetectionBox] = []

        try:
            with self._lock:
                results = self.model.predict(
                    source=image,
                    classes=[0],  # COCO Class 0 = Person
                    conf=self.score_threshold,
                    iou=self.nms_threshold,
                    device=self.device,
                    verbose=False,
                )

            if results and len(results) > 0:
                yolo_boxes = results[0].boxes
                if yolo_boxes is not None and len(yolo_boxes) > 0:
                    for box in yolo_boxes:
                        coords = box.xyxy[0].cpu().numpy()
                        x1, y1, x2, y2 = map(int, coords)
                        conf = float(box.conf[0].cpu().numpy())

                        x1 = max(0, min(w - 1, x1))
                        y1 = max(0, min(h - 1, y1))
                        x2 = max(x1 + 4, min(w, x2))
                        y2 = max(y1 + 4, min(h, y2))
                        bw = x2 - x1
                        bh = y2 - y1

                        if bw < 16 or bh < 24:
                            continue

                        bbox = (x1, y1, bw, bh)
                        track_id = global_face_track_buffer.match_or_create_track(bbox, now=now)
                        track_state = global_face_track_buffer.get_track_identity_state(track_id)

                        full_body_crop = image[y1:y2, x1:x2]
                        head_h = max(24, int(bh * 0.40))
                        head_crop = image[y1 : min(h, y1 + head_h), x1:x2]

                        snapshot_b64 = (
                            track_state["best_snapshot"]
                            or (global_identity_registry.image_to_base64(full_body_crop) if full_body_crop.size > 0 else "")
                        )

                        if track_state["is_manual"] and track_state["person_id"]:
                            detected_boxes.append(
                                DetectionBox(
                                    bbox=bbox,
                                    confidence=1.00,
                                    class_name="KNOWN_PERSON",
                                    pane_id=pane_id,
                                    person_id=track_state["person_id"],
                                    person_name=track_state["name"] or "Authorized Person",
                                    tag=track_state["tag"] or "Authorized",
                                    is_known=True,
                                    snapshot_base64=snapshot_b64,
                                )
                            )
                            continue

                        if track_state["person_id"] and (now - track_state["last_rec_time"]) < 3.0:
                            detected_boxes.append(
                                DetectionBox(
                                    bbox=bbox,
                                    confidence=round(conf, 2),
                                    class_name="KNOWN_PERSON",
                                    pane_id=pane_id,
                                    person_id=track_state["person_id"],
                                    person_name=track_state["name"] or "Authorized Person",
                                    tag=track_state["tag"] or "Authorized",
                                    is_known=True,
                                    snapshot_base64=snapshot_b64,
                                )
                            )
                            continue

                        # Biometric matching on upper body
                        matched_person = None
                        sim_score = 0.0
                        embedding = None

                        if head_crop.size > 0:
                            aligned_face = global_identity_registry.align_face(head_crop, full_image=image)
                            if aligned_face is not None and aligned_face.size > 0:
                                embedding, _ = global_identity_registry.extract_features(aligned_face)
                                is_known, matched_record, sim = global_identity_registry.match_person(aligned_face)
                                if is_known and matched_record:
                                    matched_person = matched_record
                                    sim_score = sim

                        if matched_person is not None:
                            global_face_track_buffer.add_sample(
                                track_id=track_id,
                                embedding=embedding,
                                snapshot_b64=snapshot_b64,
                                aligned_crop=full_body_crop,
                                matched_person_id=matched_person.person_id,
                                matched_name=matched_person.name,
                                matched_tag=matched_person.tag,
                                now=now,
                            )
                            global_identity_registry.update_last_seen(
                                person_id=matched_person.person_id,
                                snapshot_b64=snapshot_b64,
                                timestamp=now,
                                pane_id=pane_id,
                            )
                            detected_boxes.append(
                                DetectionBox(
                                    bbox=bbox,
                                    confidence=round(max(conf, sim_score), 2),
                                    class_name="KNOWN_PERSON",
                                    pane_id=pane_id,
                                    person_id=matched_person.person_id,
                                    person_name=matched_person.name,
                                    tag=matched_person.tag,
                                    is_known=True,
                                    snapshot_base64=snapshot_b64,
                                )
                            )
                        else:
                            detected_boxes.append(
                                DetectionBox(
                                    bbox=bbox,
                                    confidence=round(conf, 2),
                                    class_name="UNKNOWN_PERSON",
                                    pane_id=pane_id,
                                    person_id=track_id,
                                    person_name="Unknown Person",
                                    tag="Intruder",
                                    is_known=False,
                                    snapshot_base64=snapshot_b64,
                                )
                            )

            if not detected_boxes:
                return self._fallback_yunet.detect_persons(image, pane_id=pane_id) or []

            return detected_boxes

        except Exception as e:
            logger.debug(f"YOLOv11 inference error ({e}). Falling back to YuNet.")
            return self._fallback_yunet.detect_persons(image, pane_id=pane_id) or []


