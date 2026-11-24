"""Local clip evaluation; no webcam, UI, frame writes, or network access."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import sys

from detector import Detector, load_config


EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
WIDTH_GRID = (0.08, 0.10, 0.12, 0.15, 0.18)
YAW_GRID = (25, 30, 35, 45)


@dataclass
class ClipResult:
    filename: str
    label: str
    scenario: str
    frames: int = 0
    unknown_frames: int = 0
    no_face_frames: int = 0
    first_trigger: float | None = None
    error: str | None = None

    @property
    def result(self):
        if self.error:
            return "ERROR"
        if self.label == "alone" and self.first_trigger is not None:
            return "FALSE POSITIVE"
        if self.label == "intruder" and self.first_trigger is None:
            return "MISS"
        return "PASS"


def clip_identity(path):
    prefix, separator, suffix = path.stem.partition("_")
    label = prefix.lower()
    if not separator or label not in ("alone", "intruder"):
        return None
    # Strip only the final numeric take number; preserve compound scenarios.
    scenario, separator, take = suffix.rpartition("_")
    if not separator or not take.isdigit():
        scenario = suffix
    return label, scenario or "(unspecified)"


def find_clips(folder):
    clips = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        identity = clip_identity(path)
        if identity is None:
            logging.warning("Skipping %s: expected alone_ or intruder_ prefix", path.name)
        else:
            clips.append((path, *identity))
    return clips


def record_frame(result, detection, timestamp):
    result.frames += 1
    result.unknown_frames += detection.viewer_present is None
    result.no_face_frames += detection.reason == "no_faces"
    if detection.state == "INTRUDER" and result.first_trigger is None:
        result.first_trigger = timestamp


def evaluate_clip(path, label, scenario, config):
    """Decode once, calling process_frame with video-clock seconds.

    Cache only numeric Face tuples, time, and reason for this clip. No images
    or model landmark meshes are retained. Caller discards cache after replay.
    """
    import cv2

    result = ClipResult(path.name, label, scenario)
    capture = detector = None
    observations = []
    try:
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise ValueError("Cannot open/decode video")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("Missing/invalid video FPS; cannot compute video timestamps")
        reported_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        detector = Detector(config, {"debug": False}, emit_transitions=False)
        frame_index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame is None or not frame.size:
                raise ValueError(f"Empty decoded frame at index {frame_index}")
            timestamp = frame_index / fps
            detector.process_frame(frame, timestamp=timestamp)
            detection = detector.last_detection
            record_frame(result, detection, timestamp)
            observations.append((timestamp, detection.faces, detection.reason))
            frame_index += 1
            del frame
        if result.frames == 0:
            raise ValueError("No decodable frames")
        if math.isfinite(reported_count) and reported_count > 0 and frame_index < round(reported_count):
            raise ValueError(f"Decode ended early: {frame_index} frames; backend reported {round(reported_count)}")
    except Exception as exc:
        result.error = str(exc)
        observations.clear()
    finally:
        try:
            if capture is not None:
                capture.release()
        finally:
            if detector is not None:
                detector.close()
    return result, observations


def replay_clip(original, observations, config, overrides=None, debug=False):
    result = ClipResult(original.filename, original.label, original.scenario, error=original.error)
    if result.error:
        return result
    # Fresh owner/gate state for every clip AND every grid combination.
    changes = dict(overrides or {})
    changes["debug"] = debug
    detector = Detector(config, changes, load_model=False, emit_transitions=False)
    try:
        for timestamp, faces, reason in observations:
            detection = detector.process_observation(faces, timestamp, reason)
            record_frame(result, detection, timestamp)
    finally:
        detector.close()
    return result


def stats(results):
    valid = [result for result in results if not result.error]
    alone = sum(result.label == "alone" for result in valid)
    intruder = len(valid) - alone
    fp = sum(result.result == "FALSE POSITIVE" for result in valid)
    misses = sum(result.result == "MISS" for result in valid)
    latencies = [result.first_trigger for result in valid
                 if result.label == "intruder" and result.first_trigger is not None]
    return dict(total=len(valid), errors=len(results) - len(valid), alone=alone, intruder=intruder,
                fp=fp, misses=misses, accuracy=(len(valid) - fp - misses) / len(valid) if valid else None,
                fpr=fp / alone if alone else None,
                detection_rate=(intruder - misses) / intruder if intruder else None,
                latency=sum(latencies) / len(latencies) if latencies else None,
                frames=sum(result.frames for result in valid),
                unknown=sum(result.unknown_frames for result in valid),
                no_faces=sum(result.no_face_frames for result in valid))


def percent(value):
    return "n/a" if value is None else f"{value * 100:.2f}%"


def seconds(value):
    return "--" if value is None else f"{value:.3f}s"


def table(headers, rows):
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [max([len(header)] + [len(row[index]) for row in rows])
              for index, header in enumerate(headers)]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def print_results(results):
    print("\nCurrent config: clip results (latency / first false trigger is video time)")
    table(("Filename", "Label", "Result", "Latency/FP time", "Unknown", "No-face"),
          [(r.filename, r.label, r.result, seconds(r.first_trigger),
            percent(r.unknown_frames / r.frames if r.frames and not r.error else None),
            percent(r.no_face_frames / r.frames if r.frames and not r.error else None)) for r in results])
    for result in results:
        if result.error:
            print(f"ERROR {result.filename}: {result.error}")
    summary = stats(results)
    print(f"\nTotal clips: {len(results)}; evaluated: {summary['total']}; errors excluded: {summary['errors']}")
    print(f"False positives: {summary['fp']}; misses: {summary['misses']}; overall accuracy: {percent(summary['accuracy'])}")
    print(f"False positive rate (alone): {percent(summary['fpr'])}; detection rate (intruder): {percent(summary['detection_rate'])}")
    print(f"Average detection latency (detected intruder clips only): {seconds(summary['latency'])}")
    frames = summary["frames"]
    print(f"Frames: {frames}; unknown: {percent(summary['unknown'] / frames if frames else None)}; "
          f"no-face: {percent(summary['no_faces'] / frames if frames else None)} (subset of unknown)")
    print("\nScenario breakdown")
    rows = []
    for scenario in sorted({r.scenario for r in results}):
        s = stats([r for r in results if r.scenario == scenario])
        rows.append((scenario, s["total"], s["errors"], s["alone"], s["intruder"], s["fp"],
                     s["misses"], percent(s["accuracy"]), percent(s["fpr"]),
                     percent(s["detection_rate"]), seconds(s["latency"])))
    table(("Scenario", "Clips", "Errors", "Alone", "Intruder", "FP", "Misses",
           "Accuracy", "FP rate", "Detection", "Avg latency"), rows)


def print_sweep(sweeps):
    print("\nSweep: only width/yaw vary; other config values remain fixed")
    summaries = [(width, yaw, stats(results)) for (width, yaw), results in sweeps.items()]
    table(("Width ratio", "Yaw limit", "FP", "Misses", "Accuracy", "Scored", "Errors"),
          [(f"{w:.2f}", y, s["fp"], s["misses"], percent(s["accuracy"]), s["total"], s["errors"])
           for w, y, s in summaries])
    candidates = [(w, y, s) for w, y, s in summaries if s["total"]]
    if not candidates:
        print("No recommendation: no clips could be evaluated.")
        return
    # Stable grid order resolves exact ties; do not make latency a hidden goal.
    width, yaw, best = min(candidates, key=lambda entry: (entry[2]["fp"], entry[2]["misses"]))
    print(f"Recommended on these clips: min_face_width_ratio={width:.2f}, max_yaw_deg={yaw} "
          f"(FP={best['fp']}, misses={best['misses']}, accuracy={percent(best['accuracy'])}).")
    print("Ranked by fewest false positives, then fewest misses; exact ties use grid order.")
    print("Recommendation only: config.json was not modified. Validate on separate held-out clips.")


def debug_failure(result, observations, config, overrides=None, title="current config"):
    if result.result not in ("FALSE POSITIVE", "MISS"):
        return
    print(f"\nDEBUG {result.filename}: {result.result} ({title})")
    replay_clip(result, observations, config, overrides, debug=True)


def main():
    parser = argparse.ArgumentParser(description="Evaluate local Screen Shield video clips without saving frames")
    parser.add_argument("clips_folder", type=Path)
    parser.add_argument("--sweep", action="store_true", help="test 20 width/yaw combinations using cached numeric observations")
    parser.add_argument("--debug", action="store_true", help="per-frame diagnostics for failing clip/config pairs only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if not args.clips_folder.is_dir():
        parser.error("clips_folder must be an existing directory")
    try:
        config = load_config(Path(__file__).with_name("config.json"))
        clips = find_clips(args.clips_folder)
        if not clips:
            logging.error("No labeled video clips found")
            return 1
        results = []
        sweeps = {(w, y): [] for w in WIDTH_GRID for y in YAW_GRID} if args.sweep else {}
        for index, (path, label, scenario) in enumerate(clips, 1):
            print(f"Evaluating {index}/{len(clips)}: {path.name}", file=sys.stderr, flush=True)
            result, observations = evaluate_clip(path, label, scenario, config)
            results.append(result)
            if args.debug:
                debug_failure(result, observations, config)
            for (width, yaw), sweep_results in sweeps.items():
                overrides = {"min_face_width_ratio": width, "max_yaw_deg": yaw}
                replayed = replay_clip(result, observations, config, overrides)
                sweep_results.append(replayed)
                if args.debug:
                    debug_failure(replayed, observations, config, overrides,
                                  f"width={width:.2f}, yaw={yaw}")
            # Only one clip's numeric observations are held at a time.
            del observations
        print_results(results)
        if args.sweep:
            print_sweep(sweeps)
        # Classification failures are measurements, not execution errors.
        return 1 if any(result.error for result in results) else 0
    except KeyboardInterrupt:
        print("\nEvaluation cancelled; video/model resources closed.", file=sys.stderr)
        return 130
    except (ImportError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        logging.error("Evaluation failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
