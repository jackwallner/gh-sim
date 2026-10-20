#!/usr/bin/env python3
"""Capture native simulator/emulator video and score the motion in it.

Simulator recorders emit a frame only when the screen changes, so idle screens carry no
frames and a whole-file average says nothing about animation. Scoring happens inside motion
windows: runs of distinct frames that arrive close together. Each window reports its
cadence, every frame gap over 25 ms, and frame-to-frame changes that look like a flicker or
a layout jump. Contact sheets for each window and each flagged event are written beside the
video so a reviewer looks at the frames instead of a number.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

GAP = 0.025        # a frame later than this is a dropped frame at 60 fps
BREAK = 0.25       # a longer pause ends a motion window
MIN_FRAMES = 6     # shorter runs are a tap highlight, not an animation
CHANGE = 0.12      # scene score that reads as a visible discontinuity
QUIET = 0.04       # scene score of an ordinary animation step
SHEETS = 12        # contact sheets per kind, to bound the evidence folder


def ffmpeg(args: list[str]) -> str:
    return subprocess.run(["ffmpeg", "-v", "error", "-nostdin"] + args,
                          capture_output=True, text=True, check=True).stdout


def frames(path: Path) -> list[dict]:
    """One row per decoded frame: timestamp, content hash and change score against the previous frame."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries",
                            "frame=best_effort_timestamp_time", "-of", "json", str(path)],
                           capture_output=True, text=True, check=True)
    times = [float(x["best_effort_timestamp_time"]) for x in json.loads(probe.stdout).get("frames", [])
             if "best_effort_timestamp_time" in x]
    hashes = [line.split(",")[-1].strip() for line in
              ffmpeg(["-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "framemd5", "-"]).splitlines()
              if line and not line.startswith("#")]
    scores = [float(line.split("=")[1]) for line in
              ffmpeg(["-i", str(path), "-fps_mode", "passthrough", "-vf",
                      "scale=iw/4:ih/4,select='gte(scene,0)',metadata=print:file=-", "-f", "null", "-"]).splitlines()
              if line.startswith("lavfi.scene_score=")]
    rows = []
    for index in range(min(len(times), len(hashes), len(scores))):
        rows.append({"at": times[index], "hash": hashes[index], "score": scores[index],
                     "moving": index > 0 and hashes[index] != hashes[index-1]})
    return rows


def events(rows: list[dict], run: list[int]) -> list[dict]:
    """Flicker: two large changes bracketed by quiet frames. Jump: one change far above the window's usual step."""
    scores = [rows[i]["score"] for i in run]
    typical = max(statistics.median(scores), 0.005)
    big = lambda s: s >= CHANGE and s > 6*typical
    calm = lambda s: s < QUIET or s <= 3*typical
    found = []
    skip = -1
    for k, score in enumerate(scores):
        if k <= skip or not big(score):
            continue
        before = scores[k-1] if k else 0.0
        after = scores[k+1] if k+1 < len(scores) else 0.0
        settled = scores[k+2] if k+2 < len(scores) else 0.0
        at = round(rows[run[k]]["at"], 3)
        if big(after) and calm(before) and calm(settled):
            found.append({"at": at, "type": "flicker", "score": round(score, 3)})
            skip = k+1
        else:
            found.append({"at": at, "type": "jump", "score": round(score, 3)})
    return found


def summarize(rows: list[dict], run: list[int]) -> dict:
    times = [rows[i]["at"] for i in run]
    intervals = [b-a for a, b in zip(times, times[1:])]
    median = statistics.median(intervals)
    span = times[-1]-times[0]
    return {"start": round(times[0], 3), "end": round(times[-1], 3), "frames": len(run),
            "duration": round(span, 3), "fps": round((len(run)-1)/span, 1) if span else 0,
            "median_interval_ms": round(median*1000, 2), "at_60fps": median <= 1/54,
            "gaps": [{"at": round(times[k+1], 3), "ms": round(gap*1000, 1)} for k, gap in enumerate(intervals) if gap > GAP],
            "events": events(rows, run)}


def windows(rows: list[dict]) -> list[dict]:
    runs: list[list[int]] = []
    current: list[int] = []
    for index, row in enumerate(rows):
        if not row["moving"]:
            continue
        if current and row["at"]-rows[current[-1]]["at"] > BREAK:
            runs.append(current)
            current = []
        current.append(index)
    if current:
        runs.append(current)
    return [summarize(rows, run) for run in runs if len(run) >= MIN_FRAMES]


def sheet(path: Path, target: Path, start: float, end: float, step: int, layout: str, width: int) -> str | None:
    graph = ("trim=start=%.4f:end=%.4f,setpts=PTS-STARTPTS,select='not(mod(n,%d))',scale=%d:-2,tile=%s"
             % (max(0.0, start), end, step, width, layout))
    try:
        ffmpeg(["-i", str(path), "-vf", graph, "-frames:v", "1", "-y", str(target)])
    except subprocess.CalledProcessError:
        return None
    return str(target) if target.is_file() else None


def write_sheets(path: Path, motion: list[dict]) -> None:
    stem = path.with_suffix("")
    for number, window in enumerate(motion[:SHEETS], 1):
        step = max(1, window["frames"]//24)
        window["sheet"] = sheet(path, Path(str(stem)+".window-%02d.png" % number),
                                window["start"], window["end"]+0.02, step, "6x4", 240)
    number = 0
    for window in motion:
        for item in window["events"]:
            number += 1
            if number > SHEETS:
                return
            item["sheet"] = sheet(path, Path(str(stem)+".event-%02d.png" % number),
                                  item["at"]-2.5/60, item["at"]+3.5/60, 1, "6x1", 300)


def analyze(path: Path, window_start: float = 0, window_end: float | None = None) -> dict:
    rows = [r for r in frames(path) if r["at"] >= window_start and (window_end is None or r["at"] <= window_end)]
    times = [r["at"] for r in rows]
    gaps = [b-a for a, b in zip(times, times[1:]) if b > a]
    duration = times[-1]-times[0] if len(times) > 1 else 0
    motion = windows(rows)
    write_sheets(path, motion)
    moving_seconds = sum(w["duration"] for w in motion)
    moving_frames = sum(w["frames"] for w in motion)
    at_60 = any(w["at_60fps"] for w in motion)
    dropped = sum(len(w["gaps"]) for w in motion)
    flagged = sum(len(w["events"]) for w in motion)
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "window_start": window_start, "window_end": window_end, "requested_fps": 60,
            "measured_fps": (len(times)-1)/duration if duration else 0,
            "median_frame_interval": statistics.median(gaps) if gaps else 0,
            "frame_count": len(times), "duration": duration,
            "distinct_frames_analyzed_window": len({r["hash"] for r in rows}),
            "motion_windows": motion, "motion_seconds": round(moving_seconds, 3), "motion_frames": moving_frames,
            "motion_fps": round((moving_frames-len(motion))/moving_seconds, 1) if moving_seconds else 0,
            "windows_below_60fps": sum(not w["at_60fps"] for w in motion),
            "gaps_over_25ms": dropped, "events": flagged,
            "motion_60fps": at_60, "clean_motion": at_60 and not dropped and not flagged,
            "native_60fps_proven": at_60,
            "interpretation": "Scored inside motion windows only: idle screens emit no frames and are not counted. "
                              "gaps are frames that arrived over 25 ms after the previous one; events are flicker or "
                              "jump candidates for a reviewer to confirm on the contact sheets. No interpolation."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["capture", "verify"])
    parser.add_argument("--platform", choices=["ios", "android"])
    parser.add_argument("--device")
    parser.add_argument("--display")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--output", required=True)
    parser.add_argument("--window-start", type=float, default=0)
    parser.add_argument("--window-end", type=float)
    args = parser.parse_args()
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.mode == "capture":
        if not args.platform or not args.device or not 1 <= args.seconds <= 120:
            parser.error("capture requires platform, device and 1..120 seconds")
        if args.platform == "ios":
            command = ["xcrun", "simctl", "io", args.device, "recordVideo", "--codec=h264"]
            if args.display:
                command += ["--display=" + args.display]
            command += [str(path)]
        else:
            command = ["scrcpy", "--serial", args.device, "--no-playback", "--no-audio", "--max-fps=60",
                       "--record", str(path), "--time-limit=" + str(args.seconds)]
        print(json.dumps({"capture_started": str(path), "command": command}), flush=True)
        process = subprocess.Popen(command)
        try:
            process.wait(timeout=args.seconds + (10 if args.platform == "android" else 0))
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=20)
        provenance = {"source": args.platform, "command": command, "interpolated": False,
                      "finished_at": time.time(), "return_code": process.returncode}
        path.with_suffix(".capture.json").write_text(json.dumps(provenance, indent=2) + "\n")
    result = analyze(path, args.window_start, args.window_end)
    path.with_suffix(".frames.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["motion_60fps"] else 3


if __name__ == "__main__":
    sys.exit(main())
