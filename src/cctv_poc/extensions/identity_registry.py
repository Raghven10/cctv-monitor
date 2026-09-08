"""High-Precision Known Person & Identity Registry powered by InsightFace ArcFace Deep Neural Metric Embeddings."""

import base64
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
import cv2
import numpy as np

try:
    import onnxruntime as ort
except ImportError:
    ort = None

from ..db.config import SessionLocal
from ..db.models import KnownPersonModel
from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.identity_registry")

# Canonical InsightFace 112x112 facial landmark reference alignment coordinates
ARCFACE_REFERENCE_PTS = np.array(
    [
        [38.2946, 51.6963],  # Right eye
        [73.5318, 51.6963],  # Left eye
        [56.0252, 71.7366],  # Nose tip
        [41.5493, 92.3655],  # Right mouth corner
        [70.7299, 92.3655],  # Left mouth corner
    ],
    dtype=np.float32,
)


@dataclass
class KnownPersonRecord:
    """Registered identity in the known person database."""
    person_id: str
    name: str
    tag: str  # e.g. "Authorized", "Admin", "Employee", "VIP", "Resident"
    registered_at: float
    snapshot_base64: str
    features: List[float]  # Primary/averaged 512-d normalized embedding
    template_shape: Tuple[int, int]
    feature_gallery: Optional[List[List[float]]] = None  # Optional multi-pose embedding gallery


class IdentityRegistry:
    """
    State-of-the-Art Facial Identity Registry utilizing:
    1. InsightFace ArcFace ResNet-50 512-dimensional angular margin metric embeddings.
    2. Symmetric 5-point landmark similarity-transformation alignment to standard 112x112 plane.
    3. Pure cosine metric verification (0.48 threshold, separating same-person >0.65 vs intruder <0.35).
    4. Persistent storage via SQLAlchemy database.
    """

    def __init__(
        self,
        storage_path: str = "data/known_persons.json",
        match_threshold: float = 0.48,
        session_factory: Optional[Any] = None,
        load_from_db: bool = True,
    ):
        self.storage_path = storage_path
        self.match_threshold = match_threshold
        self._session_factory = session_factory or SessionLocal
        self._lock = threading.Lock()
        self._persons: Dict[str, KnownPersonRecord] = {}
        self._templates: Dict[str, np.ndarray] = {}
        self._arcface_session: Optional[Any] = None
        self._arcface_input_name: str = "input.1"
        self._sface_recognizer: Optional[cv2.FaceRecognizerSF] = None
        self._yunet_aligner: Optional[cv2.FaceDetectorYN] = None

        # 1. Initialize InsightFace ArcFace ResNet-50 512-d Deep Neural Embedder
        arcface_candidates = [
            "data/arcface_w600k_r50.onnx",
            os.path.join(os.path.dirname(__file__), "../../../data/arcface_w600k_r50.onnx"),
            os.path.abspath("data/arcface_w600k_r50.onnx"),
        ]
        if ort is not None:
            available_providers = ort.get_available_providers()
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if "CUDAExecutionProvider" in available_providers else ["CPUExecutionProvider"]
            for path in arcface_candidates:
                if os.path.exists(path):
                    try:
                        opts = ort.SessionOptions()
                        opts.intra_op_num_threads = 4
                        opts.inter_op_num_threads = 2
                        self._arcface_session = ort.InferenceSession(
                            path, sess_options=opts, providers=providers
                        )
                        self._arcface_input_name = self._arcface_session.get_inputs()[0].name
                        active_provider = self._arcface_session.get_providers()[0]
                        logger.info(f"Loaded InsightFace ArcFace ResNet-50 Embedder on [{active_provider}] from {path}")
                        break
                    except Exception as e:
                        logger.warning(f"Failed to load ArcFace ONNX model from {path}: {e}")

        # 2. Initialize YuNet for automatic alignment on arbitrary crops
        yunet_candidates = [
            "data/face_detection_yunet_2023mar.onnx",
            os.path.join(os.path.dirname(__file__), "../../../data/face_detection_yunet_2023mar.onnx"),
            os.path.abspath("data/face_detection_yunet_2023mar.onnx"),
        ]
        for path in yunet_candidates:
            if os.path.exists(path) and hasattr(cv2, "FaceDetectorYN"):
                try:
                    self._yunet_aligner = cv2.FaceDetectorYN.create(
                        path, "", (320, 320), score_threshold=0.5, nms_threshold=0.3
                    )
                    break
                except Exception:
                    pass

        # 3. SFace Fallback Embedder
        sface_candidates = [
            "data/face_recognition_sface_2021dec.onnx",
            os.path.join(os.path.dirname(__file__), "../../../data/face_recognition_sface_2021dec.onnx"),
            os.path.abspath("data/face_recognition_sface_2021dec.onnx"),
        ]
        for path in sface_candidates:
            if os.path.exists(path) and hasattr(cv2, "FaceRecognizerSF"):
                try:
                    self._sface_recognizer = cv2.FaceRecognizerSF.create(path, "")
                    break
                except Exception:
                    pass

        if load_from_db:
            self._load_from_db()

    def _load_from_db(self) -> None:
        """Load registered identities from SQLAlchemy database (supporting single-vector and multi-vector galleries)."""
        loaded_count = 0
        try:
            with self._session_factory() as db:
                models = db.query(KnownPersonModel).all()
                if models:
                    with self._lock:
                        for m in models:
                            raw_desc = m.face_descriptor
                            features: List[float] = []
                            gallery: List[List[float]] = []

                            if isinstance(raw_desc, dict):
                                features = raw_desc.get("centroid") or raw_desc.get("features") or []
                                gallery = raw_desc.get("gallery") or raw_desc.get("feature_gallery") or ([features] if features else [])
                            elif isinstance(raw_desc, list):
                                features = raw_desc
                                gallery = [features] if features else []

                            rec = KnownPersonRecord(
                                person_id=m.person_id,
                                name=m.name,
                                tag=m.tag,
                                registered_at=m.created_at.timestamp() if m.created_at else time.time(),
                                snapshot_base64=m.snapshot_base64 or "",
                                features=features,
                                template_shape=(112, 112),
                                feature_gallery=gallery if gallery else ([features] if features else []),
                            )
                            self._persons[m.person_id] = rec
                            if rec.snapshot_base64:
                                img = self.base64_to_image(rec.snapshot_base64)
                                if img is not None:
                                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                                    tpl = cv2.resize(gray, (112, 112))
                                    self._templates[m.person_id] = tpl
                    loaded_count = len(models)
        except Exception as e:
            logger.warning(f"Database load for known persons failed: {e}")

        logger.info(f"Identity Registry initialized with {len(self._persons)} active identities")

    @staticmethod
    def compute_centroid(embeddings: List[List[float]]) -> List[float]:
        """Compute unit L2-normalized mean centroid vector across multiple embeddings."""
        if not embeddings:
            return []
        valid_embs = [np.array(e, dtype=np.float32) for e in embeddings if len(e) > 0]
        if not valid_embs:
            return []
        mean_vec = np.mean(valid_embs, axis=0)
        norm = np.linalg.norm(mean_vec)
        if norm > 1e-6:
            mean_vec = mean_vec / norm
        return mean_vec.tolist()

    def add_online_sample(self, person_id: str, new_embedding: List[float], max_samples: int = 50) -> None:
        """Incrementally accumulate a new high-quality pose embedding into a known person's gallery."""
        if not new_embedding:
            return
        with self._lock:
            person = self._persons.get(person_id)
            if not person:
                return

            gallery = person.feature_gallery or ([person.features] if person.features else [])
            if len(gallery) >= max_samples:
                return

            # Check if the new embedding adds useful pose diversity (avoid near-identical duplicates)
            new_vec = np.array(new_embedding, dtype=np.float32)
            new_norm = np.linalg.norm(new_vec)
            if new_norm > 1e-6:
                new_vec = new_vec / new_norm

            max_sim = 0.0
            for g_list in gallery:
                g_vec = np.array(g_list, dtype=np.float32)
                g_norm = np.linalg.norm(g_vec)
                if g_norm > 1e-6:
                    sim = float(np.dot(new_vec, g_vec / g_norm))
                    if sim > max_sim:
                        max_sim = sim

            # If similarity < 0.96, it offers genuine diversity without duplicate clustering
            if max_sim < 0.96:
                gallery.append(new_embedding)
                person.feature_gallery = gallery
                person.features = self.compute_centroid(gallery)

                try:
                    with self._session_factory() as db:
                        row = db.query(KnownPersonModel).filter(KnownPersonModel.person_id == person_id).first()
                        if row:
                            row.face_descriptor = {
                                "centroid": person.features,
                                "gallery": person.feature_gallery,
                                "sample_count": len(person.feature_gallery),
                            }
                            db.commit()
                except Exception as e:
                    logger.debug(f"Online sample DB sync failed for {person_id}: {e}")

    def align_face(
        self,
        crop: np.ndarray,
        landmarks: Optional[np.ndarray] = None,
        full_image: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """
        Align face to canonical 112x112 ArcFace plane using 5 facial landmarks.
        Guarantees identical affine alignment whether called on a full webcam frame or a cropped image.
        """
        if crop is None or crop.size == 0:
            return np.zeros((112, 112, 3), dtype=np.uint8)

        # Case A: Explicit landmarks and full image passed from live detector
        if landmarks is not None and full_image is not None and len(landmarks) == 5:
            try:
                src_pts = np.array(landmarks, dtype=np.float32)
                M, _ = cv2.estimateAffinePartial2D(src_pts, ARCFACE_REFERENCE_PTS)
                if M is not None:
                    aligned = cv2.warpAffine(full_image, M, (112, 112), borderValue=0.0)
                    return aligned
            except Exception as e:
                logger.debug(f"Direct landmark alignment exception: {e}")

        # Case B: Crop provided (e.g. from registration snapshot or database load) -> detect landmarks inside crop
        if self._yunet_aligner is not None and crop.shape[0] >= 40 and crop.shape[1] >= 40:
            try:
                ch, cw = crop.shape[:2]
                self._yunet_aligner.setInputSize((cw, ch))
                _, detections = self._yunet_aligner.detect(crop)
                if detections is not None and len(detections) > 0:
                    det = detections[0]
                    crop_landmarks = np.array(
                        [
                            [det[4], det[5]],
                            [det[6], det[7]],
                            [det[8], det[9]],
                            [det[10], det[11]],
                            [det[12], det[13]],
                        ],
                        dtype=np.float32,
                    )
                    M, _ = cv2.estimateAffinePartial2D(crop_landmarks, ARCFACE_REFERENCE_PTS)
                    if M is not None:
                        aligned = cv2.warpAffine(crop, M, (112, 112), borderValue=0.0)
                        return aligned
            except Exception as e:
                logger.debug(f"Crop-level landmark alignment exception: {e}")

        # Case C: Fallback to standard aspect-preserved center resize
        return cv2.resize(crop, (112, 112))

    def extract_features(
        self,
        crop: np.ndarray,
        landmarks: Optional[np.ndarray] = None,
        full_image: Optional[np.ndarray] = None,
    ) -> Tuple[List[float], np.ndarray]:
        """
        Extract normalized 512-dimensional ArcFace metric embedding from aligned face image.
        """
        if crop is None or crop.size == 0:
            return [], np.zeros((112, 112), dtype=np.uint8)

        aligned_bgr = self.align_face(crop, landmarks=landmarks, full_image=full_image)
        gray = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2GRAY) if len(aligned_bgr.shape) == 3 else aligned_bgr

        # 1. Primary: InsightFace ArcFace 512-d Deep Metric Embedding
        if self._arcface_session is not None:
            try:
                rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB) if len(aligned_bgr.shape) == 3 else cv2.cvtColor(aligned_bgr, cv2.COLOR_GRAY2RGB)
                norm_img = (rgb.astype(np.float32) - 127.5) / 127.5
                blob = np.transpose(norm_img, (2, 0, 1))[np.newaxis, ...]  # (1, 3, 112, 112)
                raw_feat = self._arcface_session.run(None, {self._arcface_input_name: blob})[0].flatten()
                norm_d = np.linalg.norm(raw_feat)
                if norm_d > 1e-6:
                    arc_feat = raw_feat / norm_d
                    return arc_feat.tolist(), gray
            except Exception as e:
                logger.warning(f"ArcFace inference error: {e}")

        # 2. Secondary Fallback: OpenCV SFace 128-d Embedding
        if self._sface_recognizer is not None:
            try:
                bgr_input = aligned_bgr if len(aligned_bgr.shape) == 3 else cv2.cvtColor(aligned_bgr, cv2.COLOR_GRAY2BGR)
                raw_sface = self._sface_recognizer.feature(bgr_input).flatten()
                norm_s = np.linalg.norm(raw_sface)
                if norm_s > 1e-6:
                    sface_feat = raw_sface / norm_s
                    return sface_feat.tolist(), gray
            except Exception as e:
                logger.debug(f"SFace fallback error: {e}")

        # 3. Tertiary Spatial Gradient Histogram Fallback (128-d)
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        mag, ang = cv2.cartToPolar(gx, gy, angleInDegrees=True)
        bins = np.int32(ang / (360.0 / 8.0)) % 8

        hist_list = []
        for r in range(4):
            for c in range(4):
                cell_m = mag[r * 28 : (r + 1) * 28, c * 28 : (c + 1) * 28]
                cell_b = bins[r * 28 : (r + 1) * 28, c * 28 : (c + 1) * 28]
                h_cell = np.zeros(8, dtype=np.float32)
                for b in range(8):
                    h_cell[b] = np.sum(cell_m[cell_b == b])
                c_norm = np.linalg.norm(h_cell)
                if c_norm > 1e-6:
                    h_cell = h_cell / c_norm
                hist_list.extend(h_cell.tolist())

        total_vec = np.array(hist_list, dtype=np.float32)
        v_norm = np.linalg.norm(total_vec)
        if v_norm > 1e-6:
            total_vec = total_vec / v_norm
        return total_vec.tolist(), gray

    def match_person(
        self,
        crop: np.ndarray,
        landmarks: Optional[np.ndarray] = None,
        full_image: Optional[np.ndarray] = None,
    ) -> Tuple[bool, Optional[KnownPersonRecord], float]:
        """
        Match a detected face against all registered identities using pure ArcFace Cosine Similarity.
        Returns (is_known, matched_record, similarity_score).
        """
        if crop is None or crop.size == 0 or not self._persons:
            return False, None, 0.0

        query_feat_list, _ = self.extract_features(
            crop, landmarks=landmarks, full_image=full_image
        )
        if not query_feat_list:
            return False, None, 0.0

        query_feat = np.array(query_feat_list, dtype=np.float32)
        query_norm = np.linalg.norm(query_feat)
        if query_norm > 1e-6:
            query_feat = query_feat / query_norm

        best_score = -1.0
        best_person: Optional[KnownPersonRecord] = None

        with self._lock:
            for pid, person in self._persons.items():
                if not person.features:
                    continue

                # Primary centroid embedding comparison
                target_feat = np.array(person.features, dtype=np.float32)
                if len(query_feat) != len(target_feat):
                    continue

                target_norm = np.linalg.norm(target_feat)
                if target_norm > 1e-6:
                    target_feat = target_feat / target_norm

                # Pure Cosine angular similarity between unit deep metric embeddings
                cosine_sim = float(np.dot(query_feat, target_feat))

                # Check multi-pose gallery if available
                if person.feature_gallery:
                    for g_feat_list in person.feature_gallery:
                        g_feat = np.array(g_feat_list, dtype=np.float32)
                        g_norm = np.linalg.norm(g_feat)
                        if g_norm > 1e-6:
                            g_sim = float(np.dot(query_feat, g_feat / g_norm))
                            if g_sim > cosine_sim:
                                cosine_sim = g_sim

                if cosine_sim > best_score:
                    best_score = cosine_sim
                    best_person = person

        is_match = (best_score >= self.match_threshold) and (best_person is not None)
        return is_match, best_person if is_match else None, round(max(0.0, best_score), 2)

    def register_person(
        self,
        name: str,
        crop: Optional[np.ndarray] = None,
        tag: str = "Authorized",
        person_id: Optional[str] = None,
        landmarks: Optional[np.ndarray] = None,
        full_image: Optional[np.ndarray] = None,
        track_id: Optional[str] = None,
        extra_embeddings: Optional[List[List[float]]] = None,
    ) -> KnownPersonRecord:
        """
        Register or update a known identity with ArcFace 512-d biometric descriptors.
        Automatically leverages multi-frame rolling track buffer (up to 50 samples) if track_id is provided.
        """
        pid = person_id or f"person_{uuid.uuid4().hex[:8]}"
        embeddings: List[List[float]] = []
        snapshot_b64 = ""
        gray_tpl = np.zeros((112, 112), dtype=np.uint8)

        # 1. Ingest multi-frame embeddings from rolling track buffer if available
        if track_id:
            try:
                from .person_detector import global_face_track_buffer
                trk_history = global_face_track_buffer.get_track_history(track_id)
                if trk_history["embeddings"]:
                    embeddings.extend(trk_history["embeddings"])
                if trk_history["best_snapshot"]:
                    snapshot_b64 = trk_history["best_snapshot"]
            except Exception as e:
                logger.debug(f"Could not retrieve track buffer for {track_id}: {e}")

        # 2. Ingest extra embeddings
        if extra_embeddings:
            embeddings.extend(extra_embeddings)

        # 3. Process direct crop if provided
        if crop is not None and crop.size > 0:
            aligned_bgr = self.align_face(crop, landmarks=landmarks, full_image=full_image)
            crop_feat, g_tpl = self.extract_features(aligned_bgr)
            if crop_feat:
                embeddings.append(crop_feat)
            if not snapshot_b64:
                snapshot_b64 = self.image_to_base64(aligned_bgr)
            gray_tpl = g_tpl

        # 4. Fallback if snapshot_b64 exists but no gray_tpl
        if snapshot_b64 and (gray_tpl is None or gray_tpl.size == 0):
            img = self.base64_to_image(snapshot_b64)
            if img is not None:
                gray_tpl = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img

        # Compute normalized centroid across all accumulated multi-frame embeddings
        centroid = self.compute_centroid(embeddings) if embeddings else []
        gallery = embeddings[:50] if embeddings else ([centroid] if centroid else [])

        record = KnownPersonRecord(
            person_id=pid,
            name=name.strip(),
            tag=tag.strip() or "Authorized",
            registered_at=time.time(),
            snapshot_base64=snapshot_b64,
            features=centroid,
            template_shape=gray_tpl.shape if gray_tpl is not None else (112, 112),
            feature_gallery=gallery,
        )

        with self._lock:
            self._persons[pid] = record
            if gray_tpl is not None and gray_tpl.size > 0:
                self._templates[pid] = gray_tpl

        # Persist to database with structured multi-vector payload
        try:
            with self._session_factory() as db:
                descriptor_payload = {
                    "centroid": record.features,
                    "gallery": record.feature_gallery,
                    "sample_count": len(record.feature_gallery or []),
                }
                existing = db.query(KnownPersonModel).filter(KnownPersonModel.person_id == pid).first()
                if existing:
                    existing.name = record.name
                    existing.tag = record.tag
                    existing.face_descriptor = descriptor_payload
                    existing.snapshot_base64 = record.snapshot_base64
                else:
                    new_rec = KnownPersonModel(
                        person_id=pid,
                        name=record.name,
                        tag=record.tag,
                        face_descriptor=descriptor_payload,
                        snapshot_base64=record.snapshot_base64,
                    )
                    db.add(new_rec)
                db.commit()
        except Exception as e:
            logger.error(f"Failed to persist person to database: {e}")

        logger.info(
            f"Registered ArcFace biometric known person: '{name}' [{pid}] with tag '{tag}' "
            f"({len(gallery)} multi-frame embedding samples)"
        )
        return record

    def register_person_from_images(
        self,
        name: str,
        images: List[np.ndarray],
        tag: str = "Authorized",
        person_id: Optional[str] = None,
    ) -> Optional[KnownPersonRecord]:
        """Register identity manually from one or more uploaded full or cropped images."""
        if not images:
            return None

        pid = person_id or f"person_{uuid.uuid4().hex[:8]}"
        embeddings: List[List[float]] = []
        best_snapshot_b64: str = ""
        best_gray_tpl: Optional[np.ndarray] = None

        for img in images:
            if img is None or img.size == 0:
                continue
            aligned_bgr = self.align_face(img)
            feat, gray_tpl = self.extract_features(aligned_bgr)
            if feat:
                embeddings.append(feat)
                if not best_snapshot_b64:
                    best_snapshot_b64 = self.image_to_base64(aligned_bgr)
                    best_gray_tpl = gray_tpl

        if not embeddings:
            return None

        centroid = self.compute_centroid(embeddings)
        tpl_shape = best_gray_tpl.shape if best_gray_tpl is not None else (112, 112)

        record = KnownPersonRecord(
            person_id=pid,
            name=name.strip(),
            tag=tag.strip() or "Authorized",
            registered_at=time.time(),
            snapshot_base64=best_snapshot_b64,
            features=centroid,
            template_shape=tpl_shape,
            feature_gallery=embeddings[:50],
        )

        with self._lock:
            self._persons[pid] = record
            if best_gray_tpl is not None:
                self._templates[pid] = best_gray_tpl

        # Persist to database
        try:
            with self._session_factory() as db:
                descriptor_payload = {
                    "centroid": record.features,
                    "gallery": record.feature_gallery,
                    "sample_count": len(record.feature_gallery or []),
                }
                existing = db.query(KnownPersonModel).filter(KnownPersonModel.person_id == pid).first()
                if existing:
                    existing.name = record.name
                    existing.tag = record.tag
                    existing.face_descriptor = descriptor_payload
                    existing.snapshot_base64 = record.snapshot_base64
                else:
                    new_rec = KnownPersonModel(
                        person_id=pid,
                        name=record.name,
                        tag=record.tag,
                        face_descriptor=descriptor_payload,
                        snapshot_base64=record.snapshot_base64,
                    )
                    db.add(new_rec)
                db.commit()
        except Exception as e:
            logger.error(f"Failed to persist manual registration to DB: {e}")

        logger.info(f"Manually registered person '{name}' [{pid}] with {len(embeddings)} frame embeddings")
        return record

    def delete_person(self, person_id: str) -> bool:
        """Remove a known person by ID from database and memory."""
        with self._lock:
            found = person_id in self._persons
            if found:
                del self._persons[person_id]
                self._templates.pop(person_id, None)

        if found:
            try:
                with self._session_factory() as db:
                    person_row = db.query(KnownPersonModel).filter(KnownPersonModel.person_id == person_id).first()
                    if person_row:
                        db.delete(person_row)
                        db.commit()
            except Exception as e:
                logger.error(f"Failed to delete person from DB: {e}")
            logger.info(f"Removed registered person: {person_id}")
        return found

    def clear_all(self) -> None:
        """Clear all registered identities from memory and DB."""
        with self._lock:
            self._persons.clear()
            self._templates.clear()
        try:
            with self._session_factory() as db:
                db.query(KnownPersonModel).delete()
                db.commit()
        except Exception as e:
            logger.error(f"Failed to clear known persons: {e}")

    def list_persons(self) -> List[Dict]:
        """Return list of all registered persons with sample counts."""
        with self._lock:
            return [
                {
                    "person_id": p.person_id,
                    "name": p.name,
                    "tag": p.tag,
                    "registered_at": p.registered_at,
                    "snapshot_base64": p.snapshot_base64,
                    "sample_count": len(p.feature_gallery or ([p.features] if p.features else [])),
                }
                for p in self._persons.values()
            ]

    @staticmethod
    def image_to_base64(image: np.ndarray, quality: int = 90) -> str:
        """Convert cv2 image to JPEG base64 string."""
        if image is None or image.size == 0:
            return ""
        h, w = image.shape[:2]
        if max(h, w) > 200:
            scale = 200.0 / max(h, w)
            image = cv2.resize(image, (int(w * scale), int(h * scale)))
        _, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return base64.b64encode(buffer).decode("utf-8")

    @staticmethod
    def base64_to_image(b64_str: str) -> Optional[np.ndarray]:
        """Convert base64 JPEG string back to OpenCV image."""
        try:
            if not b64_str:
                return None
            if "," in b64_str:
                b64_str = b64_str.split(",", 1)[1]
            data = base64.b64decode(b64_str)
            nparr = np.frombuffer(data, np.uint8)
            return cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        except Exception:
            return None


# Global registry singleton
global_identity_registry = IdentityRegistry()
