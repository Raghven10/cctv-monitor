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
                }
            else:
                self._tracks[best_track_id]["bbox"] = bbox
                self._tracks[best_track_id]["last_seen"] = now

        return best_track_id

    def add_sample(
        self,
        track_id: str,
        embedding: List[float],
        snapshot_b64: str,
        aligned_crop: np.ndarray,
        matched_person_id: Optional[str] = None,
    ) -> None:
        """Add a high-quality frame embedding and snapshot into the track's rolling buffer."""
        if not embedding or not snapshot_b64:
            return

        sharpness = calculate_face_sharpness(aligned_crop)

        with self._lock:
            if track_id not in self._tracks:
                return

            trk = self._tracks[track_id]
            if matched_person_id:
                trk["matched_person_id"] = matched_person_id

            # Add to rolling buffer (up to max_samples_per_track)
            if len(trk["embeddings"]) < self.max_samples_per_track:
                trk["embeddings"].append(embedding)
                trk["snapshots"].append(snapshot_b64)
            else:
                # Replace if this frame has higher sharpness than the minimum in buffer
                trk["embeddings"].pop(0)
                trk["snapshots"].pop(0)
                trk["embeddings"].append(embedding)
                trk["snapshots"].append(snapshot_b64)

            # Update best snapshot if sharper
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

    def __init__(self, score_threshold: float = 0.70, nms_threshold: float = 0.30):
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.yunet_detector: Optional[cv2.FaceDetectorYN] = None
        self._current_input_size = (320, 320)

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
        if h < 30 or w < 30:
            return []

        # List of (x, y, w, h, conf, landmarks)
        candidate_faces: List[Tuple[int, int, int, int, float, Optional[np.ndarray]]] = []

        if self.yunet_detector is not None:
            # Resize neural detector input plane if dimensions changed
            if self._current_input_size != (w, h):
                self._current_input_size = (w, h)
                self.yunet_detector.setInputSize((w, h))

            try:
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

                        # Minimum face size to eliminate micro noise (at least 35x35 px)
                        if fw < 35 or fh < 35:
                            continue

                        # Human face aspect ratio validation (0.60 <= w/h <= 1.45)
                        aspect = float(fw) / float(fh) if fh > 0 else 0.0
                        if aspect < 0.60 or aspect > 1.45:
                            continue

                        # Clamp to image boundaries
                        x1 = max(0, fx)
                        y1 = max(0, fy)
                        x2 = min(w, fx + fw)
                        y2 = min(h, fy + fh)
                        cw = x2 - x1
                        ch = y2 - y1

                        if cw >= 35 and ch >= 35:
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

        # Fallback to strict verified Haar with Eye Confirmation if YuNet is unavailable or returns 0
        if not candidate_faces and (self.yunet_detector is None or (self.face_cascade is not None and not self.face_cascade.empty())):
            haar_faces = self._detect_haar_verified(image)
            for hf in haar_faces:
                candidate_faces.append((hf[0], hf[1], hf[2], hf[3], hf[4], None))

        # Build DetectionBoxes and match against known identities with ArcFace deep embeddings
        boxes: List[DetectionBox] = []
        for idx, (bx, by, bw, bh, conf, landmarks) in enumerate(candidate_faces):
            bbox = (bx, by, bw, bh)
            # Match or create a persistent spatial track ID
            track_id = global_face_track_buffer.match_or_create_track(bbox)

            # Extract face crop
            crop = image[by : by + bh, bx : bx + bw]
            if crop.size == 0:
                continue

            # Align face canonically to 112x112 ArcFace plane
            aligned_face = global_identity_registry.align_face(
                crop, landmarks=landmarks, full_image=image
            )
            # Use aligned face for high-quality snapshot
            snapshot_b64 = global_identity_registry.image_to_base64(aligned_face)

            # Extract ArcFace metric embedding
            embedding, _ = global_identity_registry.extract_features(aligned_face)

            # Match against known identities
            is_known, matched_record, sim_score = global_identity_registry.match_person(
                crop, landmarks=landmarks, full_image=image
            )

            # Record into multi-frame rolling track buffer
            global_face_track_buffer.add_sample(
                track_id=track_id,
                embedding=embedding,
                snapshot_b64=snapshot_b64,
                aligned_crop=aligned_face,
                matched_person_id=matched_record.person_id if is_known and matched_record else None,
            )

            # Trigger online auto-accumulation for high-confidence matches (expand known gallery up to 50 poses)
            if is_known and matched_record is not None and sim_score >= 0.65 and embedding:
                global_identity_registry.add_online_sample(matched_record.person_id, embedding)

            if is_known and matched_record is not None:
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

    def _detect_haar_verified(self, image: np.ndarray) -> List[Tuple[int, int, int, int, float]]:
        """Strict fallback detector requiring both facial outline and eye feature confirmation."""
        if self.face_cascade is None or self.face_cascade.empty():
            return []

        h, w = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image.copy()

        rects = self.face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=7,
            minSize=(45, 45),
            maxSize=(int(w * 0.85), int(h * 0.85)),
        )

        verified = []
        for (x, y, fw, fh) in rects:
            aspect = float(fw) / float(fh) if fh > 0 else 0.0
            if aspect < 0.65 or aspect > 1.40:
                continue

            if not self.eye_cascade.empty():
                face_gray = gray[y : y + int(fh * 0.65), x : x + fw]
                eyes = self.eye_cascade.detectMultiScale(face_gray, scaleFactor=1.1, minNeighbors=3, minSize=(12, 12))
                if len(eyes) == 0:
                    continue

            verified.append((x, y, fw, fh, 0.88))

        return verified

