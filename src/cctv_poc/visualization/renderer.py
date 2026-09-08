"""Live annotated visual renderer with HUD metrics and pane overlays."""

from typing import Optional, Tuple
import cv2
import numpy as np

from ..realtime.pipeline import ProcessedFrameResult


class VisualRenderer:
    """Renders clean HUD overlays, pane bounding boxes, and camera label diagnostics."""

    def __init__(
        self,
        primary_color: Tuple[int, int, int] = (0, 255, 128),     # Mint green for stable
        unstable_color: Tuple[int, int, int] = (0, 165, 255),    # Orange for stabilizing
        unknown_color: Tuple[int, int, int] = (0, 0, 255),       # Red for unknown/unstable
        hud_bg_color: Tuple[int, int, int] = (20, 20, 24),       # Dark slate
    ):
        self.primary_color = primary_color
        self.unstable_color = unstable_color
        self.unknown_color = unknown_color
        self.hud_bg_color = hud_bg_color

    def render(
        self,
        result: ProcessedFrameResult,
        view_mode: str = "rectified",  # "rectified" or "original"
    ) -> np.ndarray:
        """
        Produce an annotated visualization image.
        view_mode="rectified": Annotates the canonical display plane.
        view_mode="original": Projects annotations back to the webcam view.
        """
        if view_mode == "rectified" and result.rectified_image is not None:
            canvas = result.rectified_image.copy()
            self._draw_rectified_panes(canvas, result)
        else:
            canvas = result.raw_image.copy()
            self._draw_original_projected(canvas, result)

        self._draw_hud(canvas, result)
        self._draw_alerts(canvas, result)
        return canvas

    def _draw_alerts(self, canvas: np.ndarray, result: ProcessedFrameResult) -> None:
        """Render high-visibility alert banner if system alerts exist."""
        if not result.alerts:
            return

        h, w = canvas.shape[:2]
        for i, alert in enumerate(result.alerts):
            banner_y = 65 + (i * 42)
            banner_h = 36
            
            # Semi-transparent red/amber background
            sub = canvas[banner_y : banner_y + banner_h, 20 : w - 20]
            if sub.size > 0:
                bg = np.full_like(sub, (0, 30, 180)) # Dark red-amber
                cv2.addWeighted(sub, 0.2, bg, 0.8, 0, sub)
                cv2.rectangle(canvas, (20, banner_y), (w - 20, banner_y + banner_h), (0, 60, 255), 2)
                cv2.putText(
                    canvas,
                    f"⚠️ ALERT: {alert}",
                    (35, banner_y + 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )

    def _draw_rectified_panes(self, canvas: np.ndarray, result: ProcessedFrameResult) -> None:
        """Draw pane bounding boxes and diagnostic cards on rectified frame."""
        if not result.display_result.is_detected or not result.panes:
            return

        for pane in result.panes:
            pid = pane.pane_id
            ident = result.pane_identities.get(pid)
            state = result.stable_states.get(pid)

            x, y, w, h = pane.bbox

            # Pick color based on stability
            if state and state.is_stable and not state.is_unknown:
                color = self.primary_color
                status_text = state.stable_label
            elif state and not state.is_unknown:
                color = self.unstable_color
                status_text = f"STABILIZING: {state.stable_label}"
            else:
                color = self.unknown_color
                status_text = "UNKNOWN"

            # Draw pane border
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 3)

            # Draw label card in top-left of pane
            card_w = min(w - 10, max(220, int(w * 0.45)))
            card_h = min(h - 10, 85)
            card_x = x + 5
            card_y = y + 5

            # Semi-transparent card background
            sub_rect = canvas[card_y : card_y + card_h, card_x : card_x + card_w]
            if sub_rect.shape[0] == card_h and sub_rect.shape[1] == card_w:
                bg = np.full_like(sub_rect, (30, 30, 30))
                cv2.addWeighted(sub_rect, 0.25, bg, 0.75, 0, sub_rect)

            cv2.rectangle(canvas, (card_x, card_y), (card_x + card_w, card_y + card_h), color, 1)

            # Draw text lines
            label_conf = ident.confidence if ident else 0.0
            geom_conf = pane.geometry_confidence

            cv2.putText(canvas, f"{pid} | {status_text}", (card_x + 8, card_y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
            cv2.putText(canvas, f"Label conf: {label_conf:.2f}", (card_x + 8, card_y + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (200, 200, 200), 1)
            cv2.putText(canvas, f"Geometry: {geom_conf:.2f}", (card_x + 8, card_y + 70), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (200, 200, 200), 1)

    def _draw_original_projected(self, canvas: np.ndarray, result: ProcessedFrameResult) -> None:
        """Draw projected polygons on original webcam frame or person detection boxes."""
        if not result.display_result.is_detected:
            # Mode A: Direct Live Room Surveillance
            h, w = canvas.shape[:2]
            
            # Draw detected persons (Known: Green, Unknown: Red)
            if result.detected_persons:
                for p in result.detected_persons:
                    bx, by, bw, bh = p.bbox
                    if p.is_known:
                        # Green bounding box for identified/known person
                        box_color = (0, 255, 100) # Vibrant green
                        chip_text = f"AUTHORIZED: {p.person_name}"
                        sub_text = f"[{p.tag}] Conf: {p.confidence:.2f}"
                    else:
                        # Red bounding box for unknown intruder
                        box_color = (0, 0, 255) # Red alert
                        chip_text = f"UNKNOWN PERSON ({p.confidence:.2f})"
                        sub_text = "🚨 UNTAGGED INTRUDER"

                    # Draw bold bounding box
                    cv2.rectangle(canvas, (bx, by), (bx + bw, by + bh), box_color, 3)
                    
                    # Label chip above head
                    label_w = min(max(bw, 220), w - bx)
                    chip_h = 36
                    chip_y1 = max(0, by - chip_h)
                    chip_y2 = by
                    
                    # Filled header banner
                    cv2.rectangle(canvas, (bx, chip_y1), (bx + label_w, chip_y2), box_color, -1)
                    cv2.putText(
                        canvas,
                        chip_text,
                        (bx + 6, chip_y1 + 16),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.48,
                        (0, 0, 0) if p.is_known else (255, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.putText(
                        canvas,
                        sub_text,
                        (bx + 6, chip_y1 + 30),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.40,
                        (20, 20, 20) if p.is_known else (220, 220, 220),
                        1,
                        cv2.LINE_AA,
                    )
            return

        # Mode B: CCTV Wall Monitor
        if result.display_result and result.display_result.corners is not None:
            pts = np.int32(np.round(result.display_result.corners))
            cv2.polylines(canvas, [pts], True, (0, 255, 255), 3)

        # Draw projected pane polygons
        if result.corrector is not None:
            for pane in result.panes:
                pid = pane.pane_id
                state = result.stable_states.get(pid)
                color = self.primary_color if (state and state.is_stable) else self.unstable_color
                
                poly = result.corrector.map_rectified_bbox_to_original_polygon(pane.bbox)
                cv2.polylines(canvas, [poly], True, color, 2)
                
                # Draw label near top-left vertex
                tl_pt = poly[0]
                label = state.stable_label if state else pid
                cv2.putText(canvas, f"{pid}: {label}", (int(tl_pt[0]) + 5, int(tl_pt[1]) + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    def _draw_hud(self, canvas: np.ndarray, result: ProcessedFrameResult) -> None:
        """Draw global telemetry HUD banner."""
        h, w = canvas.shape[:2]
        hud_h = 55
        
        # Top banner background
        hud_crop = canvas[0:hud_h, 0:w]
        bg = np.full_like(hud_crop, self.hud_bg_color)
        cv2.addWeighted(hud_crop, 0.2, bg, 0.8, 0, hud_crop)
        cv2.line(canvas, (0, hud_h), (w, hud_h), (80, 80, 80), 1)

        metrics = result.metrics
        layout = result.layout

        if result.mode == "DIRECT_ROOM_SURVEILLANCE":
            person_count = len(result.detected_persons or [])
            mode_tag = f"LIVE ROOM SURVEILLANCE | Persons Detected: {person_count}"
            hud_text_1 = f"MODE: {mode_tag} | Frame #{result.frame_index}"
        else:
            hud_text_1 = (
                f"LAYOUT: {layout.pane_count} Panes ({layout.layout_id}) | "
                f"Layout Conf: {layout.confidence:.2f} | "
                f"Frame #{result.frame_index}"
            )

        hud_text_2 = (
            f"FPS: {metrics.processing_fps:.1f} (Cap: {metrics.capture_fps:.1f}) | "
            f"Age: {metrics.frame_age_ms:.0f}ms | "
            f"Proc: {metrics.processing_latency_ms:.0f}ms | "
            f"E2E: {metrics.end_to_end_latency_ms:.0f}ms"
        )

        cv2.putText(canvas, hud_text_1, (20, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(canvas, hud_text_2, (20, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 180), 1, cv2.LINE_AA)
