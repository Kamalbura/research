"""Benchmark driver for orchestrated multi-run measurements.

Runs paired GCS/Drone proxies for a fixed duration, emits external power
markers, optionally captures Windows Performance Recorder traces, and writes a
manifest describing each run artifact.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

from core.suites import get_suite
from tools.markers import FileMarker, MarkerSink, NullMarker, SerialMarker, UdpMarker


DEFAULT_OUTDIR = Path("benchmarks/out")
GCS_JSON_NAME = "gcs.json"
DRONE_JSON_NAME = "drone.json"
GCS_LOG_NAME = "gcs.log"
DRONE_LOG_NAME = "drone.log"
WPR_FILE_NAME = "system_trace.etl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PQC proxy benchmarks with external power markers")
    parser.add_argument("--suite", required=True, help="Suite identifier to run (e.g., cs-mlkem768-aesgcm-mldsa65)")
    parser.add_argument("--duration", required=True, type=float, help="Measurement duration in seconds")
    parser.add_argument("--repeat", type=int, default=1, help="Number of repetitions for the suite")
    parser.add_argument("--start-delay", type=float, default=0.0, help="Optional delay before emitting START marker")
    parser.add_argument("--marker", choices=["null", "file", "serial", "udp"], default="null", help="Marker sink backend")
    parser.add_argument("--marker-file", help="Path for file marker output")
    parser.add_argument("--marker-serial-port", help="Serial port (e.g., COM3) for marker emission")
    parser.add_argument("--marker-udp", help="host:port for UDP marker emission")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="Base output directory for artifacts")
    parser.add_argument("--wpr", choices=["on", "off"], default="off", help="Enable Windows Performance Recorder capture")
    parser.add_argument("--gcs-args", help="Additional arguments appended to the GCS command")
    parser.add_argument("--drone-args", help="Additional arguments appended to the drone command")
    return parser.parse_args()


def sanitize_run_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def resolve_marker(args: argparse.Namespace) -> MarkerSink:
    marker_type = args.marker
    if marker_type == "null":
        return NullMarker()
    if marker_type == "file":
        if not args.marker_file:
            raise SystemExit("--marker-file is required when --marker=file")
        Path(args.marker_file).parent.mkdir(parents=True, exist_ok=True)
        return FileMarker(args.marker_file)
    if marker_type == "serial":
        if not args.marker_serial_port:
            raise SystemExit("--marker-serial-port is required when --marker=serial")
        return SerialMarker(args.marker_serial_port)
    if marker_type == "udp":
        if not args.marker_udp:
            raise SystemExit("--marker-udp is required when --marker=udp")
        return UdpMarker(args.marker_udp)
    raise SystemExit(f"Unknown marker type: {marker_type}")


def maybe_split_args(arg_string: Optional[str]) -> List[str]:
    if not arg_string:
        return []
    return shlex.split(arg_string)


def build_command(role: str, suite_id: str, stop_seconds: float, json_path: Path, extra_args: List[str]) -> List[str]:
    base_cmd = [
        sys.executable,
        "-m",
        "core.run_proxy",
        role,
        "--suite",
        suite_id,
        "--stop-seconds",
        f"{stop_seconds:.3f}",
        "--json-out",
        str(json_path),
    ]
    return base_cmd + extra_args


def start_wpr(run_dir: Path) -> Tuple[bool, Optional[Path]]:
    if shutil.which("wpr") is None:
        print("Warning: wpr.exe not found in PATH; skipping WPR capture.")
        return False, None

    print("Starting Windows Performance Recorder (GeneralProfile.Light)...")
    subprocess.run(["wpr", "-start", "GeneralProfile.Light", "-filemode"], check=False)
    return True, run_dir / WPR_FILE_NAME


def stop_wpr(etl_path: Optional[Path]) -> None:
    if not etl_path:
        return
    args = ["wpr", "-stop", str(etl_path)]
    subprocess.run(args, check=False)


def init_psutil_process(pid: int) -> Optional[psutil.Process]:
    try:
        proc = psutil.Process(pid)
        proc.cpu_percent(None)  # prime
        return proc
    except psutil.Error:
        return None


def sample_stats(process: Optional[psutil.Process]) -> Tuple[Optional[float], Optional[int]]:
    if process is None:
        return None, None
    try:
        cpu = process.cpu_percent(None)
        rss = process.memory_info().rss
        return cpu, rss
    except psutil.Error:
        return None, None


def summarise(samples: List[float]) -> Dict[str, Optional[float]]:
    if not samples:
        return {"avg": None, "max": None, "p95": None}
    sorted_samples = sorted(samples)
    avg = sum(sorted_samples) / len(sorted_samples)
    max_val = sorted_samples[-1]
    p95_index = max(0, min(len(sorted_samples) - 1, math.floor(0.95 * (len(sorted_samples) - 1))))
    return {"avg": avg, "max": max_val, "p95": sorted_samples[p95_index]}


def ensure_run_dir(base_outdir: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_root = base_outdir / timestamp
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root


def write_manifest(run_dir: Path, manifest: Dict[str, object]) -> None:
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote manifest to {manifest_path}")


def orchestrate_run(
    args: argparse.Namespace,
    suite_info: Dict[str, object],
    run_root: Path,
    repeat_idx: int,
    marker: MarkerSink,
) -> None:
    suite_id = suite_info["suite_id"]
    run_id = sanitize_run_id(f"{suite_id}_rep{repeat_idx}")
    run_dir = run_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    gcs_json_path = run_dir / GCS_JSON_NAME
    drone_json_path = run_dir / DRONE_JSON_NAME
    gcs_log_path = run_dir / GCS_LOG_NAME
    drone_log_path = run_dir / DRONE_LOG_NAME

    stop_seconds = args.duration + 2.0
    gcs_cmd = build_command("gcs", suite_id, stop_seconds, gcs_json_path, maybe_split_args(args.gcs_args))
    drone_cmd = build_command("drone", suite_id, stop_seconds, drone_json_path, maybe_split_args(args.drone_args))

    wpr_enabled = args.wpr == "on"
    wpr_started = False
    wpr_path: Optional[Path] = None

    print(f"\n=== Run {repeat_idx}/{args.repeat} :: {suite_id} ===")
    print(f"Output directory: {run_dir}")
    print(f"GCS command: {' '.join(gcs_cmd)}")
    print(f"Drone command: {' '.join(drone_cmd)}")

    if wpr_enabled:
        wpr_started, wpr_path = start_wpr(run_dir)

    if args.start_delay > 0:
        print(f"Waiting {args.start_delay:.2f}s before start marker...")
        time.sleep(args.start_delay)

    wall_start_ns = time.time_ns()
    perf_start_ns = time.perf_counter_ns()
    marker.start(run_id, wall_start_ns)

    with open(gcs_log_path, "w", encoding="utf-8", buffering=1) as gcs_log, open(
        drone_log_path, "w", encoding="utf-8", buffering=1
    ) as drone_log:
        gcs_proc = subprocess.Popen(gcs_cmd, stdout=gcs_log, stderr=subprocess.STDOUT)
        drone_proc = subprocess.Popen(drone_cmd, stdout=drone_log, stderr=subprocess.STDOUT)

        gcs_ps = init_psutil_process(gcs_proc.pid)
        drone_ps = init_psutil_process(drone_proc.pid)

        deadline = time.perf_counter() + args.duration
        cpu_samples = {"gcs": [], "drone": []}
        rss_samples = {"gcs": [], "drone": []}

        try:
            while True:
                now = time.perf_counter()
                if now >= deadline:
                    break
                to_sleep = min(1.0, deadline - now)
                if to_sleep > 0:
                    time.sleep(to_sleep)
                gcs_cpu, gcs_rss = sample_stats(gcs_ps)
                drone_cpu, drone_rss = sample_stats(drone_ps)
                if gcs_cpu is not None:
                    cpu_samples["gcs"].append(gcs_cpu)
                if drone_cpu is not None:
                    cpu_samples["drone"].append(drone_cpu)
                if gcs_rss is not None:
                    rss_samples["gcs"].append(gcs_rss)
                if drone_rss is not None:
                    rss_samples["drone"].append(drone_rss)
        finally:
            wall_end_ns = time.time_ns()
            perf_end_ns = time.perf_counter_ns()
            marker.end(run_id, wall_end_ns)

            for proc_name, proc in {"gcs": gcs_proc, "drone": drone_proc}.items():
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    print(f"{proc_name.upper()} still running; terminating...")
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        print(f"{proc_name.upper()} unresponsive; killing...")
                        proc.kill()

    if wpr_started:
        stop_wpr(wpr_path)

    gcs_exit = gcs_proc.returncode
    drone_exit = drone_proc.returncode

    manifest: Dict[str, object] = {
        "run_id": run_id,
        "kem": suite_info["kem_name"],
        "sig": suite_info["sig_name"],
        "aead": suite_info["aead"],
        "suite": suite_id,
        "duration_s": args.duration,
        "repeat_idx": repeat_idx,
        "host": platform.system(),
        "start_wall_ns": wall_start_ns,
        "end_wall_ns": wall_end_ns,
        "start_perf_ns": perf_start_ns,
        "end_perf_ns": perf_end_ns,
        "gcs_json": GCS_JSON_NAME,
        "drone_json": DRONE_JSON_NAME,
        "gcs_log": GCS_LOG_NAME,
        "drone_log": DRONE_LOG_NAME,
        "wpr_etl": WPR_FILE_NAME if wpr_started else None,
        "gcs_exit_code": gcs_exit,
        "drone_exit_code": drone_exit,
        "gcs_cmd": gcs_cmd,
        "drone_cmd": drone_cmd,
        "notes": "external-power-mode",
        "cpu_stats": {
            "gcs": summarise(cpu_samples["gcs"]),
            "drone": summarise(cpu_samples["drone"]),
        },
        "rss_stats": {
            "gcs_max": max(rss_samples["gcs"]) if rss_samples["gcs"] else None,
            "drone_max": max(rss_samples["drone"]) if rss_samples["drone"] else None,
        },
    }

    write_manifest(run_dir, manifest)


def main() -> None:
    args = parse_args()
    suite_info = get_suite(args.suite)
    run_root = ensure_run_dir(Path(args.outdir))
    marker = resolve_marker(args)

    try:
        for repeat_idx in range(1, args.repeat + 1):
            orchestrate_run(args, suite_info, run_root, repeat_idx, marker)
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user.")
    finally:
        marker.close()


if __name__ == "__main__":
    main()
