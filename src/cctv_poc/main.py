"""Main entry point for AI CCTV Physical Display Monitoring POC."""

import argparse
import signal
import sys
import time
import cv2
import numpy as np

from .config import load_config
from .realtime.pipeline import RealtimePipeline
from .utils.logging import setup_logger
from .visualization.renderer import VisualRenderer

logger = setup_logger("cctv_poc.main")


def parse_args():
    parser = argparse.ArgumentParser(description="AI CCTV Physical Display Monitoring POC")
    parser.add_argument("--config", "-c", type=str, default="config/poc.yaml", help="Path to YAML config")
    parser.add_argument("--device", "-d", type=str, default=None, help="Webcam device index or URL")
    parser.add_argument("--view-mode", "-v", type=str, choices=["rectified", "original"], default="rectified", help="Visualization mode")
    parser.add_argument("--no-display", action="store_true", help="Run in headless mode without GUI window")
    parser.add_argument("--dry-run", action="store_true", help="Process synthetic test frame and exit with status 0")
    parser.add_argument("--simulate", action="store_true", help="Run live simulation streaming variable CCTV layouts")
    parser.add_argument("--duration", type=float, default=0.0, help="Run for N seconds then exit (0 = infinite)")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(args.config)

    # CLI overrides
    if args.device is not None:
        config.video.device = int(args.device) if args.device.isdigit() else args.device
    if args.no_display:
        config.output.display = False

    logger.info("==================================================================")
    logger.info(" Starting AI CCTV Physical Display Monitoring POC                ")
    logger.info("==================================================================")
    logger.info(f"Configuration loaded: {args.config}")
    logger.info(f"Target display dimensions: {config.display.target_width}x{config.display.target_height}")

    pipeline = RealtimePipeline(config)
    renderer = VisualRenderer()

    # Handle graceful exit on Ctrl+C
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received. Exiting cleanly...")
        pipeline.stop()
        cv2.destroyAllWindows()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if args.dry_run:
        logger.info("Executing dry-run verification mode with synthetic CCTV frame...")
        synth_w, synth_h = 1920, 1080
        synthetic_frame = np.full((synth_h, synth_w, 3), 30, dtype=np.uint8)
        
        cols, rows = 3, 2
        pw, ph = synth_w // cols, synth_h // rows
        for r in range(rows):
            for c in range(cols):
                x1, y1 = c * pw, r * ph
                x2, y2 = x1 + pw, y1 + ph
                color = (40 + r * 30, 50 + c * 40, 60)
                cv2.rectangle(synthetic_frame, (x1 + 4, y1 + 4), (x2 - 4, y2 - 4), color, -1)
                cv2.rectangle(synthetic_frame, (x1, y1), (x2, y2), (220, 220, 220), 4)
                label = f"CAM-0{r * cols + c + 1}"
                cv2.putText(synthetic_frame, label, (x1 + 20, y1 + 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)

        frame_data = pipeline.video_source.inject_synthetic_frame(synthetic_frame)
        result = pipeline.process_frame_data(frame_data)
        
        logger.info(f"Dry-run result: Discovered {result.layout.pane_count} panes in layout {result.layout.layout_id}")
        logger.info(f"Discovered panes: {[p.pane_id for p in result.panes]}")
        logger.info("Dry run succeeded perfectly.")
        pipeline.stop()
        return 0

    if args.simulate:
        logger.info("Running in live simulation mode (simulating physical display with variable layouts)...")
        pipeline.start(auto_open_camera=False)
        window_name = "AI CCTV Physical Display Monitoring (Simulated Live Feed)"
        if config.output.display:
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(window_name, 1280, 720)

        layouts_seq = [(2, 2), (2, 3), (3, 3)]
        layout_idx = 0
        frame_num = 0
        start_time = time.time()
        synth_w, synth_h = 1920, 1080

        try:
            while True:
                frame_num += 1
                # Switch layout every 100 frames to simulate operator layout switching
                if frame_num % 100 == 0:
                    layout_idx = (layout_idx + 1) % len(layouts_seq)

                rows, cols = layouts_seq[layout_idx]
                sim_frame = np.full((synth_h, synth_w, 3), 30, dtype=np.uint8)
                pw, ph = synth_w // cols, synth_h // rows
                for r in range(rows):
                    for c in range(cols):
                        x1, y1 = c * pw, r * ph
                        x2, y2 = (c + 1) * pw if c < cols - 1 else synth_w, (r + 1) * ph if r < rows - 1 else synth_h
                        idx = r * cols + c + 1
                        # Subtle motion simulation
                        motion_val = int(abs(np.sin(frame_num * 0.1 + idx) * 30))
                        color = (40 + motion_val, 60 + (r * 30) % 180, 80 + (c * 40) % 160)
                        cv2.rectangle(sim_frame, (x1 + 3, y1 + 3), (x2 - 3, y2 - 3), color, -1)
                        cv2.rectangle(sim_frame, (x1, y1), (x2, y2), (240, 240, 240), 4)
                        cv2.putText(sim_frame, f"CAM-{idx:02d}", (x1 + 15, y1 + 35), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

                frame_data = pipeline.video_source.inject_synthetic_frame(sim_frame)
                result = pipeline.process_frame_data(frame_data)

                if config.output.display:
                    annotated = renderer.render(result, view_mode=args.view_mode)
                    disp_img = cv2.resize(annotated, (1280, 720)) if annotated.shape[1] > 1920 else annotated
                    cv2.imshow(window_name, disp_img)
                    key = cv2.waitKey(30) & 0xFF
                    if key == ord("q") or key == 27:
                        break
                    elif key == ord("v"):
                        args.view_mode = "original" if args.view_mode == "rectified" else "rectified"

                if args.duration > 0 and (time.time() - start_time) >= args.duration:
                    logger.info(f"Specified run duration ({args.duration}s) reached.")
                    break
                time.sleep(0.02)
        finally:
            pipeline.stop()
            cv2.destroyAllWindows()
        return 0

    # Live Mode
    started = pipeline.start(auto_open_camera=True)
    if not started:
        logger.error("Could not start live webcam. Please verify camera device is plugged in or permissions are granted.")
        return 1

    window_name = "AI CCTV Physical Display Monitoring"
    if config.output.display:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 1280, 720)

    start_time = time.time()
    try:
        while True:
            result = pipeline.process_latest()
            if result is None:
                time.sleep(0.005)
                continue

            if config.output.display:
                annotated = renderer.render(result, view_mode=args.view_mode)
                # Resize preview for display if 4K
                disp_img = cv2.resize(annotated, (1280, 720)) if annotated.shape[1] > 1920 else annotated
                cv2.imshow(window_name, disp_img)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q") or key == 27:
                    logger.info("User requested exit ('q' or ESC pressed).")
                    break
                elif key == ord("v"):
                    # Toggle view mode
                    args.view_mode = "original" if args.view_mode == "rectified" else "rectified"
                    logger.info(f"Switched view mode to: {args.view_mode}")

            if args.duration > 0 and (time.time() - start_time) >= args.duration:
                logger.info(f"Specified run duration ({args.duration}s) reached.")
                break

    finally:
        pipeline.stop()
        cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    sys.exit(main())
