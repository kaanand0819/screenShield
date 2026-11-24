"""Screen Shield: shared config, local viewer detection, and clip replay.

process(frame_bgr, timestamp_seconds) also supports future offline evaluation.
Timestamps must strictly increase and use the clip clock for recorded video.
No frames are written, displayed, or transmitted.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import sys
import time


DEFAULT_CONFIG = {
    "model_path": "~/.screen-shield/face_landmarker.task",
    "camera_index": 0, "camera_width": 640, "camera_height": 480,
    "camera_fps": 10, "process_fps": 10, "camera_retry_seconds": 5.0,
    "max_faces": 4, "min_face_detection_confidence": 0.65,
    "min_face_presence_confidence": 0.65, "min_tracking_confidence": 0.65,
    "owner_centrality_weight": 0.1, "owner_max_center_distance": 0.12,
    "owner_min_iou": 0.15, "owner_min_size_ratio": 0.5,
    "owner_memory_seconds": 2.0, "min_face_width_ratio": 0.12,
    "max_yaw_deg": 35, "max_pitch_deg": 30, "pnp_focal_length_ratio": 1.0,
    "pnp_max_reprojection_error_ratio": 0.03, "min_frame_brightness": 35.0,
    "min_face_brightness": 40.0, "trigger_seconds": 0.7, "clear_seconds": 2.0,
    "max_frame_gap_seconds": 0.5, "max_camera_failures": 20, "debug": False,
    "blur_radius": 40, "snooze_seconds": 10, "show_message": True,
    "hotkey": "ctrl+alt+s", "start_enabled": True, "fail_closed": False,
}


def validate_config(values: dict) -> dict:
    """Validate every setting without importing camera, UI, or hook libraries."""
    integers = {"camera_index", "camera_width", "camera_height", "max_faces", "max_camera_failures"}
    fractions = {"min_face_detection_confidence", "min_face_presence_confidence",
                 "min_tracking_confidence", "owner_centrality_weight", "min_face_width_ratio",
                 "owner_max_center_distance", "owner_min_iou", "owner_min_size_ratio",
                 "pnp_max_reprojection_error_ratio"}
    ranges = {key: (0, 1) for key in fractions}
    ranges.update({"camera_index": (0, 1024), "camera_width": (1, 16384),
                   "camera_height": (1, 16384), "camera_fps": (0.1, 240),
                   "process_fps": (0.1, 120), "max_faces": (2, 4),
                   "max_camera_failures": (1, 10000), "max_yaw_deg": (0, 90),
                   "max_pitch_deg": (0, 90), "min_frame_brightness": (0, 255),
                   "min_face_brightness": (0, 255), "blur_radius": (0.1, 1000),
                   "pnp_focal_length_ratio": (0.01, 100)})
    result = {}
    for key, default in DEFAULT_CONFIG.items():
        value = values.get(key, default)
        if key not in values:
            logging.warning("Config %s missing; using default %r", key, default)
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, str):
            valid = isinstance(value, str) and bool(value.strip()) and "\x00" not in value
        else:
            low, high = ranges.get(key, (0.001, 86400))
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and (key not in integers or isinstance(value, int)))
            if valid:
                try:
                    valid = math.isfinite(value) and low <= value <= high
                except (OverflowError, ValueError):
                    valid = False
        if not valid:
            logging.warning("Config %s has invalid type/value; using default %r", key, default)
            value = default
        result[key] = value
    for key in values.keys() - DEFAULT_CONFIG.keys():
        logging.warning("Ignoring unknown config key %s", key)
    return result


def load_config(path: str | Path) -> dict:
    path = Path(path).expanduser()
    recover = False
    try:
        with path.open(encoding="utf-8") as handle:
            values = json.load(handle)
        if not isinstance(values, dict):
            raise ValueError("configuration must be a JSON object")
    except (OSError, ValueError, UnicodeError) as exc:
        logging.warning("Cannot read config %s (%s); using and recreating defaults", path, exc)
        values = dict(DEFAULT_CONFIG)
        recover = True
    config = validate_config(values)
    if recover:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            logging.warning("Cannot write recovered config %s (%s); continuing with in-memory defaults", path, exc)
    model = Path(config["model_path"]).expanduser()
    config["model_path"] = str(model if model.is_absolute() else path.resolve().parent / model)
    return config


class TemporalGate:
    """Three-valued evidence: True=viewer, False=absence, None=unknown.

    Unknown holds state, breaks continuous presence, and pauses the clear
    timer. Only intervals bounded by two known-absent samples count toward
    clearing. A sampling gap also contributes no evidence.
    """

    def __init__(self, config: dict):
        self.config = config
        self.active = False
        self.last_time = None
        self.present_since = None
        self.absent_elapsed = 0.0
        self.previous_present = None

    def update(self, present: bool | None, timestamp: float) -> bool:
        if not math.isfinite(timestamp) or (self.last_time is not None and timestamp <= self.last_time):
            raise ValueError("Frame timestamps must be finite and strictly increasing")
        delta = 0.0 if self.last_time is None else timestamp - self.last_time
        gap = delta > self.config["max_frame_gap_seconds"]
        if gap:
            self.present_since = None
        self.last_time = timestamp
        if present is None:
            self.present_since = None
        elif present:
            self.absent_elapsed = 0.0
            if self.present_since is None:
                self.present_since = timestamp
            if timestamp - self.present_since >= self.config["trigger_seconds"]:
                self.active = True
        else:
            self.present_since = None
            if self.previous_present is False and not gap:
                self.absent_elapsed += delta
            if self.absent_elapsed >= self.config["clear_seconds"]:
                self.active = False
        self.previous_present = present
        return self.active


@dataclass(frozen=True)
class Face:
    index: int
    box: tuple[int, int, int, int]
    width_fraction: float
    owner_score: float
    yaw: float | None
    pitch: float | None
    brightness: float
    normalized_box: tuple[float, float, float, float]
    pose_source: str = "unavailable"


@dataclass(frozen=True)
class Detection:
    active: bool
    viewer_present: bool | None
    reason: str
    faces: tuple[Face, ...]
    owner_index: int | None
    viewer_indices: tuple[int, ...]

    @property
    def state(self) -> str:
        return "INTRUDER" if self.active else "SAFE"


class Detector:
    def __init__(self, config: dict, overrides: dict | None = None, *,
                 load_model: bool = True, emit_transitions: bool = True):
        # Each instance owns fresh tracking/timing state and a private config.
        config = dict(config)
        if overrides:
            unknown = set(overrides) - set(config)
            if unknown:
                raise ValueError(f"Unknown config overrides: {sorted(unknown)}")
            config.update(overrides)
            config = validate_config(config)
        self.config = config
        self.emit_transitions = emit_transitions
        self.gate = TemporalGate(config)
        self.last_model_ms = -1
        self.owner_box = None
        self.owner_last_seen = None
        self.last_detection = None
        self.landmarker = None
        # Evaluation can replay numeric face observations without reloading the
        # model. Such instances support process_observation(), not raw frames.
        if not load_model:
            return
        # Lazy imports keep configuration and timing logic testable without a camera.
        import cv2
        import mediapipe as mp
        import numpy as np

        self.cv2, self.mp, self.np = cv2, mp, np
        model = Path(config["model_path"])
        if not model.is_file():
            raise FileNotFoundError(f"Model missing: {model}. Follow README setup; no automatic download is performed.")
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model)),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=config["max_faces"],
            min_face_detection_confidence=config["min_face_detection_confidence"],
            min_face_presence_confidence=config["min_face_presence_confidence"],
            min_tracking_confidence=config["min_tracking_confidence"],
            output_facial_transformation_matrixes=True,
        )
        self.landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)

    def close(self):
        if self.landmarker is not None:
            self.landmarker.close()
            self.landmarker = None

    def _pose(self, matrix):
        np = self.np
        try:
            matrix = np.asarray(matrix, dtype=float)
        except (TypeError, ValueError):
            return None, None
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            return None, None
        # Remove scale/shear from the canonical-to-observed landmark transform.
        try:
            u, singular, vt = np.linalg.svd(matrix[:3, :3])
        except np.linalg.LinAlgError:
            return None, None
        if singular.min() <= np.finfo(float).eps:
            return None, None
        rotation = u @ vt
        if np.linalg.det(rotation) < 0:
            return None, None
        # Orientation of the canonical head's Z axis; zero is frontal.
        x, y, z = rotation[:, 2]
        return math.degrees(math.atan2(x, z)), math.degrees(math.atan2(-y, math.hypot(x, z)))

    def _pnp_pose(self, landmarks, width, height):
        """Fallback with nose, chin, outer eyes and mouth corners.

        Generic face coordinates use x right, y down, z into the head, so
        frontal pose has identity rotation in OpenCV camera coordinates.
        Intrinsics are approximate; reprojection error rejects poor fits.
        """
        np, cv2 = self.np, self.cv2
        indices = (1, 152, 33, 263, 61, 291)
        if len(landmarks) <= max(indices):
            return None, None
        image_points = np.array([(landmarks[i].x * width, landmarks[i].y * height)
                                 for i in indices], dtype=np.float64)
        if not np.isfinite(image_points).all():
            return None, None
        model_points = np.array(((0, 0, 0), (0, 330, 65), (-225, -170, 135),
                                 (225, -170, 135), (-150, 150, 125), (150, 150, 125)), dtype=np.float64)
        focal = width * self.config["pnp_focal_length_ratio"]
        camera = np.array(((focal, 0, width / 2), (0, focal, height / 2), (0, 0, 1)), dtype=np.float64)
        distortion = np.zeros((4, 1), dtype=np.float64)
        try:
            ok, rvec, tvec = cv2.solvePnP(model_points, image_points, camera, distortion,
                                         flags=cv2.SOLVEPNP_ITERATIVE)
            if not ok or not np.isfinite(rvec).all() or not np.isfinite(tvec).all():
                return None, None
            rotation, _ = cv2.Rodrigues(rvec)
            if ((rotation @ model_points.T + tvec)[2] <= 0).any():
                return None, None
            projected, _ = cv2.projectPoints(model_points, rvec, tvec, camera, distortion)
            error = np.linalg.norm(projected.reshape(-1, 2) - image_points, axis=1).mean() / width
            if not math.isfinite(error) or error > self.config["pnp_max_reprojection_error_ratio"]:
                return None, None
        except cv2.error:
            return None, None
        x, y, z = rotation[:, 2]
        return math.degrees(math.atan2(x, z)), math.degrees(math.atan2(-y, math.hypot(x, z)))

    def _select_owner(self, faces, timestamp):
        """Match geometry rather than MediaPipe list indices, which can reorder."""
        cfg = self.config
        matches = []
        if self.owner_box is not None and timestamp - self.owner_last_seen <= cfg["owner_memory_seconds"]:
            a, b, c, d = self.owner_box
            for face in faces:
                left, top, right, bottom = face.normalized_box
                distance = math.hypot((left + right - a - c) / 2,
                                      (top + bottom - b - d) / 2)
                size_ratio = min((right - left) / (c - a), (c - a) / (right - left),
                                 (bottom - top) / (d - b), (d - b) / (bottom - top))
                intersection = max(0, min(right, c) - max(left, a)) * max(0, min(bottom, d) - max(top, b))
                union = (right - left) * (bottom - top) + (c - a) * (d - b) - intersection
                iou = intersection / union
                if (distance <= cfg["owner_max_center_distance"] and iou >= cfg["owner_min_iou"]
                        and size_ratio >= cfg["owner_min_size_ratio"]):
                    matches.append((distance, -iou, face.index, face))
        owner = min(matches, key=lambda item: item[:3])[3] if matches else max(faces, key=lambda face: face.owner_score)
        self.owner_box = owner.normalized_box
        self.owner_last_seen = timestamp
        return owner

    def _candidate_reason(self, face):
        cfg = self.config
        if face.width_fraction < cfg["min_face_width_ratio"]:
            return "rejected: too small/distant"
        if face.brightness < cfg["min_face_brightness"]:
            return "rejected: poor face lighting"
        if face.yaw is None or face.pitch is None:
            return "rejected: pose unavailable"
        if abs(face.yaw) > cfg["max_yaw_deg"]:
            return "rejected: yaw outside limit"
        if abs(face.pitch) > cfg["max_pitch_deg"]:
            return "rejected: pitch outside limit"
        return "accepted"

    def process_frame(self, frame, timestamp: float | None = None) -> str:
        """Return SAFE/INTRUDER. Default clock is monotonic seconds.

        Recorded clips must pass increasing clip timestamps in seconds and
        use a fresh Detector per clip. Details are in last_detection. Frames
        remain in memory only. Do not mix live and clip clocks on one instance.
        """
        return self.process(frame, time.monotonic() if timestamp is None else timestamp).state

    def process(self, frame, timestamp: float) -> Detection:
        if not math.isfinite(timestamp) or (self.gate.last_time is not None and timestamp <= self.gate.last_time):
            raise ValueError("Frame timestamps must be finite and strictly increasing")
        if frame is None or frame.size == 0:
            return self._result(None, timestamp, "camera_unavailable")
        if self.landmarker is None:
            raise RuntimeError("Raw frames require a detector with a loaded model")
        cv2, np, cfg = self.cv2, self.np, self.config
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if float(gray.mean()) < cfg["min_frame_brightness"]:
            return self._result(None, timestamp, "poor_lighting")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        model_ms = max(self.last_model_ms + 1, int(timestamp * 1000))
        result = self.landmarker.detect_for_video(
            self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)), model_ms)
        self.last_model_ms = model_ms
        height, width = gray.shape
        faces = []
        for index, landmarks in enumerate(result.face_landmarks):
            points = np.asarray([(p.x, p.y) for p in landmarks], dtype=float)
            if not len(points) or not np.isfinite(points).all():
                continue
            left, top = np.clip(points.min(axis=0), 0, 1)
            right, bottom = np.clip(points.max(axis=0), 0, 1)
            x1, y1 = int(left * width), int(top * height)
            x2, y2 = min(width, math.ceil(right * width)), min(height, math.ceil(bottom * height))
            if x2 <= x1 or y2 <= y1:
                continue
            center_distance = math.hypot((left + right) / 2 - .5, (top + bottom) / 2 - .5) / math.sqrt(.5)
            score = (right - left) * (bottom - top) * (1 + cfg["owner_centrality_weight"] * (1 - center_distance))
            matrices = getattr(result, "facial_transformation_matrixes", None)
            yaw, pitch = (self._pose(matrices[index])
                          if matrices is not None and index < len(matrices) else (None, None))
            source = "matrix"
            if yaw is None or pitch is None:
                yaw, pitch = self._pnp_pose(landmarks, width, height)
                source = "solvePnP" if yaw is not None else "unavailable"
            faces.append(Face(index, (x1, y1, x2, y2), float(right - left), float(score),
                              yaw, pitch, float(gray[y1:y2, x1:x2].mean()),
                              (float(left), float(top), float(right), float(bottom)), source))
        return self.process_observation(tuple(faces), timestamp, "no_faces")

    def process_observation(self, faces: tuple[Face, ...], timestamp: float,
                            unknown_reason: str = "no_faces") -> Detection:
        """Replay cached numeric observations using the same decision logic.

        Reuse observations only when inference/lighting/owner-score config is
        unchanged. Distance and yaw thresholds can change without reinference.
        All temporal and owner state is recomputed for this detector instance.
        """
        if not math.isfinite(timestamp) or (self.gate.last_time is not None and timestamp <= self.gate.last_time):
            raise ValueError("Frame timestamps must be finite and strictly increasing")
        if not faces:
            return self._result(None, timestamp, unknown_reason)
        owner = self._select_owner(faces, timestamp)
        viewers = tuple(face.index for face in faces
                        if face.index != owner.index
                        and self._candidate_reason(face) == "accepted")
        return self._result(bool(viewers), timestamp, "viewer" if viewers else "no_qualifying_viewer",
                            tuple(faces), owner.index, viewers)

    def _result(self, present, timestamp, reason, faces=(), owner=None, viewers=()):
        previous_state = "INTRUDER" if self.gate.active else "SAFE"
        detection = Detection(self.gate.update(present, timestamp), present, reason, faces, owner, viewers)
        self.last_detection = detection
        if self.emit_transitions and detection.state != previous_state:
            print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                  f"[t={timestamp:.3f}s] {previous_state} -> {detection.state}", flush=True)
        if self.config["debug"]:
            print(f"[t={timestamp:.3f}s] faces={len(faces)} owner={owner} "
                  f"evidence={'unknown' if present is None else reason} reason={reason} "
                  f"state={detection.state}", flush=True)
            for face in faces:
                if face.index == owner:
                    continue
                angles = (f"yaw={face.yaw:+.1f} pitch={face.pitch:+.1f}"
                          if face.yaw is not None and face.pitch is not None else "yaw=unknown pitch=unknown")
                print(f"  candidate={face.index} width_ratio={face.width_fraction:.3f} "
                      f"{angles} pose={face.pose_source} {self._candidate_reason(face)}", flush=True)
        return detection


def _quit_pressed():
    """Poll terminal input without a preview window or a global key hook."""
    if sys.platform == "win32":
        import msvcrt
        return msvcrt.kbhit() and msvcrt.getwch().lower() == "q"
    import select
    return bool(select.select([sys.stdin], [], [], 0)[0]) and sys.stdin.read(1).lower() == "q"


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen Shield: console-only webcam detector (q or Ctrl+C to stop)")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    detector = camera = None
    terminal_settings = None
    try:
        config = load_config(args.config)
        detector = Detector(config)
        cv2 = detector.cv2
        camera = cv2.VideoCapture(config["camera_index"])
        if not camera.isOpened():
            raise RuntimeError("Cannot open webcam. Check camera permissions, index, and other camera apps.")
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, config["camera_width"])
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, config["camera_height"])
        camera.set(cv2.CAP_PROP_FPS, config["camera_fps"])
        if sys.platform != "win32" and sys.stdin.isatty():
            import termios
            import tty
            terminal_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        failures = 0
        while True:
            if sys.stdin.isatty() and _quit_pressed():
                break
            frame_start = time.monotonic()
            ok, frame = camera.read()
            now = time.monotonic()
            failures = 0 if ok and frame is not None and frame.size else failures + 1
            detector.process_frame(frame if not failures else None, now)
            if failures >= config["max_camera_failures"]:
                raise RuntimeError("Webcam stopped delivering frames; exiting and releasing camera.")
            time.sleep(max(0, 1 / config["process_fps"] - (time.monotonic() - frame_start)))
        return 0
    except KeyboardInterrupt:
        return 0
    except (ImportError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"Screen Shield: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if camera is not None:
                camera.release()
        finally:
            try:
                if detector is not None:
                    detector.close()
            finally:
                if terminal_settings is not None:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal_settings)


if __name__ == "__main__":
    raise SystemExit(main())
