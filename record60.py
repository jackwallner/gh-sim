#!/usr/bin/env python3
"""Capture native simulator/emulator video and measure its decoded frame cadence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time


def analyze(path: Path, window_start: float = 0, window_end: float | None = None) -> dict:
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
                            "-show_streams", "-show_entries",
                            "frame=best_effort_timestamp_time:stream=avg_frame_rate,r_frame_rate,width,height,duration",
                            "-of", "json", str(path)], capture_output=True, text=True, check=True)
    data = json.loads(probe.stdout)
    times = [float(x["best_effort_timestamp_time"]) for x in data.get("frames", [])
             if "best_effort_timestamp_time" in x]
    times = [t for t in times if t >= window_start and (window_end is None or t <= window_end)]
    gaps = [b-a for a, b in zip(times, times[1:]) if b > a]
    duration = times[-1] - times[0] if len(times) > 1 else 0
    fps = (len(times)-1) / duration if duration else 0
    median = statistics.median(gaps) if gaps else 0
    stream = data.get("streams", [{}])[0]
    # A timestamp test never proves distinct rendered frames. Hash decoded frames as well.
    hashes = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
                             "-f", "framemd5", "-"], capture_output=True, text=True, check=True)
    rows = [line for line in hashes.stdout.splitlines() if line and not line.startswith("#")]
    distinct = len({line.split(",")[-1].strip() for line in rows})
    return {"path": str(path.resolve()), "requested_fps": 60, "measured_fps": fps,
            "median_frame_interval": median, "frame_count": len(times), "duration": duration,
            "distinct_frames_whole_capture": distinct, "stream": stream,
            "cadence_60fps": bool(duration >= 1 and fps >= 54 and median and median <= 1/54),
            "native_60fps_proven": bool(duration >= 1 and fps >= 54 and median and median <= 1/54
                                        and distinct >= min(54, len(times))),
            "interpretation": "Decoded timestamps and distinct frame hashes; inspect the interaction window for dropped frames. No frame interpolation is performed."}


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
    return 0 if result["native_60fps_proven"] else 3


if __name__ == "__main__":
    sys.exit(main())
