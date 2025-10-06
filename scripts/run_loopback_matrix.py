#!/usr/bin/env python3
"""Local drone↔GCS automation for blast and saturation smoke tests."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from core import suites as suites_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
DRONE_SCRIPT = REPO_ROOT / "tools" / "auto" / "drone_follower.py"
GCS_SCRIPT = REPO_ROOT / "tools" / "auto" / "gcs_scheduler.py"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts" / "loopback_matrix"


@dataclass(frozen=True)
class Scenario:
    name: str
    traffic: str
    telemetry: bool
    monitors: bool
    passes: int
    duration_s: float
    rate_pps: int
    event_sample: int
    extra_gcs: Dict[str, object]


def available_scenarios() -> Dict[str, Scenario]:
    return {
        "blast": Scenario(
            name="blast",
            traffic="blast",
            telemetry=True,
            monitors=True,
            passes=1,
            duration_s=6.0,
            rate_pps=2000,
            event_sample=25,
            extra_gcs={"inter_gap_s": 1.0},
        ),
        "blast_no_telemetry": Scenario(
            name="blast_no_telemetry",
            traffic="blast",
            telemetry=False,
            monitors=True,
            passes=1,
            duration_s=6.0,
            rate_pps=1500,
            event_sample=50,
            extra_gcs={"inter_gap_s": 1.0},
        ),
        "blast_no_monitors": Scenario(
            name="blast_no_monitors",
            traffic="blast",
            telemetry=True,
            monitors=False,
            passes=1,
            duration_s=6.0,
            rate_pps=1500,
            event_sample=25,
            extra_gcs={"inter_gap_s": 1.0},
        ),
        "saturation_linear": Scenario(
            name="saturation_linear",
            traffic="saturation",
            telemetry=True,
            monitors=True,
            passes=1,
            duration_s=30.0,
            rate_pps=0,
            event_sample=10,
            extra_gcs={"sat_search": "linear", "max_rate_mbps": 75.0},
        ),
        "saturation_auto": Scenario(
            name="saturation_auto",
            traffic="saturation",
            telemetry=True,
            monitors=True,
            passes=1,
            duration_s=25.0,
            rate_pps=0,
            event_sample=20,
            extra_gcs={},
        ),
        "saturation_no_telemetry": Scenario(
            name="saturation_no_telemetry",
            traffic="saturation",
            telemetry=False,
            monitors=True,
            passes=1,
            duration_s=20.0,
            rate_pps=0,
            event_sample=20,
            extra_gcs={"sat_search": "coarse", "max_rate_mbps": 60.0},
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run drone follower and GCS scheduler locally across scenarios")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter for both agents")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for logs and run summaries")
    parser.add_argument("--startup-delay", type=float, default=4.0, help="Seconds to wait after follower launch")
    parser.add_argument("--timeout", type=float, default=480.0, help="Hard timeout for each GCS run")
    parser.add_argument("--grace", type=float, default=10.0, help="Follower shutdown grace period")
    parser.add_argument("--scenarios", nargs="*", help="Subset of scenario names to execute")
    parser.add_argument("--suites", nargs="*", help="Suite names (core.suites identifiers). Defaults to all registered")
    parser.add_argument("--dry-run", action="store_true", help="Print steps without executing")
    return parser.parse_args()


def resolve_suites(requested: Optional[Iterable[str]]) -> List[str]:
    available = list(suites_mod.list_suites())
    if not available:
        raise RuntimeError("No suites registered in core.suites; cannot proceed")
    if not requested:
        return available
    resolved: List[str] = []
    for name in requested:
        info = suites_mod.get_suite(name)
        suite_id = info["suite_id"]
        if suite_id not in available:
            raise RuntimeError(f"Suite {name} not recognised by registry")
        if suite_id not in resolved:
            resolved.append(suite_id)
    return resolved


def scenario_configs(scenario: Scenario, suites: List[str], run_id: str) -> Dict[str, Dict[str, object]]:
    auto_gcs = {
        "session_prefix": run_id,
        "traffic": scenario.traffic,
        "duration_s": scenario.duration_s,
        "pre_gap_s": 0.5,
        "inter_gap_s": scenario.extra_gcs.get("inter_gap_s", 2.0),
        "payload_bytes": 256,
        "event_sample": scenario.event_sample,
        "passes": scenario.passes,
        "rate_pps": scenario.rate_pps,
        "telemetry_enabled": scenario.telemetry,
        "monitors_enabled": scenario.monitors,
        "launch_proxy": True,
        "power_capture": False,
        "suites": suites,
    }
    for key, value in scenario.extra_gcs.items():
        if key != "inter_gap_s":
            auto_gcs[key] = value
    auto_drone = {
        "session_prefix": run_id,
        "telemetry_enabled": scenario.telemetry,
        "monitors_enabled": scenario.monitors,
        "cpu_optimize": False,
    }
    return {"gcs": auto_gcs, "drone": auto_drone}


def base_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DRONE_HOST", "127.0.0.1")
    env.setdefault("GCS_HOST", "127.0.0.1")
    env.setdefault("DRONE_CONTROL_PORT", "48080")
    env.setdefault("GCS_CONTROL_PORT", env["DRONE_CONTROL_PORT"])
    env.setdefault("GCS_PLAINTEXT_HOST", "127.0.0.1")
    env.setdefault("DRONE_PLAINTEXT_HOST", "127.0.0.1")
    env.setdefault("GCS_PLAINTEXT_TX", "47001")
    env.setdefault("GCS_PLAINTEXT_RX", "47002")
    env.setdefault("DRONE_PLAINTEXT_TX", "47003")
    env.setdefault("DRONE_PLAINTEXT_RX", "47004")
    env.setdefault("AUTO_GCS", "")
    env.setdefault("AUTO_DRONE", "")
    return env


def write_json(path: Path, data: Dict[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def launch_drone(
    python_bin: str,
    env: Dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[subprocess.Popen, Optional[object], Optional[object]]:
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [python_bin, str(DRONE_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
    )
    return proc, stdout_handle, stderr_handle


def run_gcs(python_bin: str, env: Dict[str, str], stdout_path: Path, stderr_path: Path, timeout: float) -> subprocess.CompletedProcess:
    with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
        return subprocess.run(
            [python_bin, str(GCS_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            stdout=out,
            stderr=err,
            text=True,
            timeout=timeout,
        )


def stop_drone(proc: subprocess.Popen, grace: float) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=grace)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_scenario(
    scenario: Scenario,
    python_bin: str,
    suites: List[str],
    output_dir: Path,
    startup_delay: float,
    timeout: float,
    grace: float,
    dry_run: bool,
) -> None:
    run_id = f"{int(time.time())}_{scenario.name}"
    scenario_dir = ensure_dir(output_dir / run_id)
    configs = scenario_configs(scenario, suites, run_id)
    env = base_env()
    env["AUTO_GCS"] = json.dumps(configs["gcs"])
    env["AUTO_DRONE"] = json.dumps(configs["drone"])
    write_json(scenario_dir / "auto_gcs.json", configs["gcs"])
    write_json(scenario_dir / "auto_drone.json", configs["drone"])

    if dry_run:
        print(f"[dry-run] scenario={scenario.name} env AUTO_GCS={env['AUTO_GCS']}")
        return

    drone_stdout = scenario_dir / "drone_stdout.log"
    drone_stderr = scenario_dir / "drone_stderr.log"
    gcs_stdout = scenario_dir / "gcs_stdout.log"
    gcs_stderr = scenario_dir / "gcs_stderr.log"

    drone_proc, drone_out_handle, drone_err_handle = launch_drone(
        python_bin,
        env,
        drone_stdout,
        drone_stderr,
    )
    time.sleep(startup_delay)
    gcs_result = None
    error: Optional[str] = None
    try:
        gcs_result = run_gcs(python_bin, env, gcs_stdout, gcs_stderr, timeout)
        if gcs_result.returncode != 0:
            error = f"GCS scheduler exited with {gcs_result.returncode}"
    except subprocess.TimeoutExpired:
        error = "GCS scheduler hit timeout"
    finally:
        stop_drone(drone_proc, grace)
        for handle in (drone_out_handle, drone_err_handle):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass
    (scenario_dir / "status.txt").write_text(
        (error or "ok") + "\n",
        encoding="utf-8",
    )
    if error:
        raise RuntimeError(f"Scenario {scenario.name} failed: {error}")


def main() -> None:
    args = parse_args()
    scenarios = available_scenarios()
    selection = args.scenarios or list(scenarios.keys())
    missing = [name for name in selection if name not in scenarios]
    if missing:
        raise SystemExit(f"Unknown scenarios: {', '.join(missing)}")
    suites = resolve_suites(args.suites)
    output_dir = ensure_dir(Path(args.output_dir))
    for name in selection:
        scenario = scenarios[name]
        print(f"[*] Running scenario {name} with suites {suites}")
        run_scenario(
            scenario,
            args.python,
            suites,
            output_dir,
            args.startup_delay,
            args.timeout,
            args.grace,
            args.dry_run,
        )
    print("All scenarios completed")


if __name__ == "__main__":
    main()
