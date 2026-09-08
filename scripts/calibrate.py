"""Interactive calibration script to select physical monitor corners."""

import argparse
import sys
import cv2
import numpy as np

from cctv_poc.config import load_config
from cctv_poc.monitor.calibration import CalibrationStore
from cctv_poc.monitor.display_detector import order_corners
from cctv_poc.monitor.perspective import PerspectiveCorrector
from cctv_poc.utils.logging import setup_logger

logger = setup_logger("cctv_poc.calibrate")

points = []


def mouse_callback(event, x, y, flags, param):
    global points
    if event == cv2.EVENT_LBUTTONDOWN:
        if len(points) < 4:
            points.append((x, y))
            logger.info(f"Point {len(points)} selected: ({x}, {y})")


def main():
    global points
    parser = argparse.ArgumentParser(description="Calibrate physical monitor corners")
    parser.add_argument("--config", "-c", type=str, default="config/poc.yaml", help="Path to config file")
    parser.add_argument("--device", "-d", type=str, default=None, help="Camera device index")
    args = parser.parse_args()

    config = load_config(args.config)
    device = int(args.device) if (args.device and args.device.isdigit()) else (args.device or config.video.device)

    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        logger.error(f"Failed to open camera device: {device}")
        return 1

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.video.requested_width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.video.requested_height)

    ret, frame = cap.read()
    if not ret or frame is None:
        logger.error("Could not read frame from camera.")
        cap.release()
        return 1

    h, w = frame.shape[:2]
    window_name = "Monitor Calibration (Click 4 Corners: TL, TR, BR, BL) | 'r'=reset, 's'=save, 'q'=quit"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, 1280, 720)
    cv2.setMouseCallback(window_name, mouse_callback)

    store = CalibrationStore(config.display.calibration_file)

    logger.info("==================================================================")
    logger.info(" Physical Monitor Corner Calibration Tool                        ")
    logger.info(" Click 4 corners of the monitor in order:                        ")
    logger.info(" 1. Top-Left  2. Top-Right  3. Bottom-Right  4. Bottom-Left      ")
    logger.info(" Press 's' to Save, 'r' to Reset points, 'q' to Quit             ")
    logger.info("==================================================================")

    while True:
        ret, frame = cap.read()
        if not ret or frame is None:
            break

        disp = frame.copy()

        # Draw selected points
        for idx, (px, py) in enumerate(points):
            cv2.circle(disp, (px, py), 6, (0, 255, 0), -1)
            cv2.putText(disp, f"P{idx+1}", (px + 10, py - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        if len(points) >= 2:
            for i in range(len(points) - 1):
                cv2.line(disp, points[i], points[i + 1], (0, 255, 255), 2)
            if len(points) == 4:
                cv2.line(disp, points[3], points[0], (0, 255, 255), 2)

        # Instructions banner
        cv2.putText(
            disp,
            f"Points: {len(points)}/4. Press 's' to save, 'r' to reset, 'q' to quit.",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 255),
            2,
        )

        cv2.imshow(window_name, disp)
        key = cv2.waitKey(20) & 0xFF

        if key == ord("r"):
            points = []
            logger.info("Points reset.")
        elif key == ord("s"):
            if len(points) == 4:
                ordered = order_corners(np.array(points, dtype=np.float32))
                store.save_corners(
                    ordered,
                    source_width=w,
                    source_height=h,
                    target_width=config.display.target_width,
                    target_height=config.display.target_height,
                )
                logger.info("Calibration successfully saved! Previewing rectified display...")

                # Show rectified preview
                corrector = PerspectiveCorrector(ordered, target_width=1280, target_height=720)
                rectified = corrector.rectify(frame)
                cv2.imshow("Rectified Preview (Press any key to close)", rectified)
                cv2.waitKey(2000)
                break
            else:
                logger.warning(f"Please select exactly 4 points before saving (currently {len(points)}).")
        elif key == ord("q") or key == 27:
            logger.info("Calibration cancelled by user.")
            break

    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
