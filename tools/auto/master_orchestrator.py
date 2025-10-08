#!/usr/bin/env python3
"""Master orchestration script for PQC evaluation runs.

Phase 1 deliverable: launch follower / scheduler, execute a suite plan,
monitor rekey progress, and gather raw artifacts into a dedicated run directory.

The script is intentionally conservative: it checks for existing processes,
starts them only when needed, records detailed step status/power telemetry, and
captures artifacts for follow-on analysis phases.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from core.config import CONFIG

DEFAULT_CONDITION = "noddos"
DEFAULT_LEVEL = "L1"
DEFAULT_DURATION = 45.0
DEFAULT_PRE_GAP = 1.0
DEFAULT_REPEAT = 1


@dataclass
class CommandStep:
    """Single switch_suite step to feed into the GCS scheduler."""

    algorithm: str
    suite: str
    duration_s: float
    pre_gap_s: float

    def as_payload(self) -> dict:
        return {
            "cmd": "switch_suite",
            "algorithm": self.algorithm,
            "suite": self.suite,
            "duration_s": self.duration_s,
            "pre_gap_s": self.pre_gap_s,
        }


LEVEL_PLAN: Dict[str, Sequence[CommandStep]] = {
    "L1": (
        CommandStep("L1-Falcon", "cs-mlkem512-aesgcm-falcon512", DEFAULT_DURATION, DEFAULT_PRE_GAP),
        CommandStep("L1-MLDSA", "cs-mlkem512-aesgcm-mldsa65", DEFAULT_DURATION, DEFAULT_PRE_GAP),
        CommandStep("L1-SPHINCS", "cs-mlkem512-aesgcm-sphincs128fsha2", DEFAULT_DURATION, DEFAULT_PRE_GAP),
    ),
    "L3": (
        CommandStep("L3-Falcon", "cs-mlkem768-aesgcm-falcon512", DEFAULT_DURATION, DEFAULT_PRE_GAP),
        CommandStep("L3-MLDSA", "cs-mlkem768-aesgcm-mldsa65", DEFAULT_DURATION, DEFAULT_PRE_GAP),
        CommandStep("L3-SPHINCS", "cs-mlkem768-aesgcm-sphincs128fsha2", DEFAULT_DURATION, DEFAULT_PRE_GAP),
    ),
    "L5": (
        CommandStep("L5-Falcon", "cs-mlkem1024-aesgcm-falcon1024", DEFAULT_DURATION, DEFAULT_PRE_GAP),
        CommandStep("L5-MLDSA", "cs-mlkem1024-aesgcm-mldsa87", DEFAULT_DURATION, DEFAULT_PRE_GAP),
        CommandStep("L5-SPHINCS", "cs-mlkem1024-aesgcm-sphincs256fsha2", DEFAULT_DURATION, DEFAULT_PRE_GAP),
    ),
}


@dataclass
class StepRecord:
    index: int
    step: CommandStep
    started_ns: int
    completed_ns: int
    success: bool
    error: Optional[str] = None
    status_series: List[dict] = field(default_factory=list)
    final_status: Optional[dict] = None
    power_status: Optional[dict] = None

    def to_jsonable(self) -> dict:
        payload = asdict(self)
        payload["step"] = asdict(self.step)
        return payload


class FollowerClient:
    """Helper for interacting with the drone follower control socket."""

    def __init__(self, host: str, port: int, timeout: float = 1.2) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def _request(self, payload: dict, timeout: Optional[float] = None, retries: int = 2) -> dict:
        timeout = timeout or self.timeout
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
                    sock.sendall((json.dumps(payload) + "\n").encode("ascii"))
                    sock.shutdown(socket.SHUT_WR)
                    reply = sock.makefile().readline()
                    return json.loads(reply.strip()) if reply else {}
            except Exception as exc:  # pragma: no cover - network errors
                last_exc = exc
                if attempt < retries:
                    time.sleep(0.3 * attempt)
                    continue
                raise
        if last_exc:
            raise last_exc
        return {}

    def status(self) -> dict:
        return self._request({"cmd": "status"}, timeout=self.timeout)

    def session_info(self) -> Optional[str]:
        try:
            resp = self._request({"cmd": "session_info"}, timeout=self.timeout)
        except Exception:
            return None
        return str(resp.get("session_id")) if resp.get("ok") else None

    def power_status(self) -> dict:
        return self._request({"cmd": "power_status"}, timeout=self.timeout)

    def poll_power_status(self, wait_hint_s: float, max_wait_s: float = 12.0) -> dict:
        deadline = time.time() + max(wait_hint_s, 1.0)
        limit = time.time() + max_wait_s
        last: dict = {}
        while time.time() < limit:
            try:
                last = self.power_status()
            except Exception as exc:  # pragma: no cover - network errors
                last = {"ok": False, "error": str(exc)}
                time.sleep(0.6)
                continue
            if not last.get("busy"):
                break
            if time.time() >= deadline:
                # still busy, keep polling until limit but slow down
                time.sleep(0.6)
            else:
                time.sleep(0.3)
        return last


class SchedulerClient:
    """Minimal TCP client for the GCS scheduler control inlet."""

    def __init__(self, host: str, port: int, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def send_switch(self, step: CommandStep) -> None:
        payload = step.as_payload()
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall((json.dumps(payload) + "\n").encode("ascii"))
        except Exception as exc:  # pragma: no cover - network errors
            raise RuntimeError(f"scheduler_send_failed:{exc}") from exc

    def probe(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                return True
        except Exception:
            return False


@dataclass
class ProcessHandle:
    name: str
    popen: subprocess.Popen[str]
    log_path: Path


class ProcessSupervisor:
    """Launches follower and scheduler processes when needed."""

    def __init__(
        self,
        run_logs_dir: Path,
        follower_client: FollowerClient,
        scheduler_client: SchedulerClient,
        follower_cmd: Sequence[str],
        scheduler_cmd: Sequence[str],
        wait_timeout_s: float = 45.0,
        stop_on_exit: bool = False,
    ) -> None:
        self.run_logs_dir = run_logs_dir
        self.follower_client = follower_client
        self.scheduler_client = scheduler_client
        self.follower_cmd = list(follower_cmd)
        self.scheduler_cmd = list(scheduler_cmd)
        self.wait_timeout_s = wait_timeout_s
        self.stop_on_exit = stop_on_exit
        self.started: Dict[str, ProcessHandle] = {}

    def ensure_follower(self) -> Optional[str]:
        try:
            status = self.follower_client.status()
            if status.get("ok", True):
                return self.follower_client.session_info()
        except Exception:
            pass
        logging.info("Follower not reachable; launching new process")
        handle = self._launch_process("follower", self.follower_cmd)
        self.started["follower"] = handle
        return self._wait_for_follower_ready()

    def ensure_scheduler(self) -> None:
        if self.scheduler_client.probe():
            logging.info("GCS scheduler already listening on control port")
            return
        logging.info("GCS scheduler not reachable; launching new process")
        handle = self._launch_process("gcs_scheduler", self.scheduler_cmd)
        self.started["gcs_scheduler"] = handle
        self._wait_for_scheduler_ready()

    def _launch_process(self, name: str, cmd: Sequence[str]) -> ProcessHandle:
        self.run_logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.run_logs_dir / f"{name}.log"
        logging.info("Starting %s: %s", name, " ".join(map(str, cmd)))
        stdout = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(Path.cwd()),
            stdout=stdout,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        return ProcessHandle(name=name, popen=proc, log_path=log_path)

    def _wait_for_follower_ready(self) -> Optional[str]:
        deadline = time.time() + self.wait_timeout_s
        while time.time() < deadline:
            try:
                status = self.follower_client.status()
                if status.get("ok", True):
                    session_id = self.follower_client.session_info()
                    logging.info("Follower ready; session_id=%s", session_id)
                    return session_id
            except Exception:
                pass
            time.sleep(0.6)
        raise RuntimeError("follower_start_timeout")

    def _wait_for_scheduler_ready(self) -> None:
        deadline = time.time() + self.wait_timeout_s
        while time.time() < deadline:
            if self.scheduler_client.probe():
                logging.info("GCS scheduler listening on control port")
                return
            time.sleep(0.6)
        raise RuntimeError("scheduler_start_timeout")

    def maybe_stop_processes(self) -> None:
        if not self.stop_on_exit:
            return
        for handle in self.started.values():
            proc = handle.popen
            if proc.poll() is None:
                logging.info("Stopping %s", handle.name)
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
        self.started.clear()


class RunOrchestrator:
    """Coordinates the execution of a single condition/level plan."""

    def __init__(
        self,
        run_dir: Path,
        condition: str,
        level: str,
        follower_client: FollowerClient,
        scheduler_client: SchedulerClient,
        post_wait_s: float = 3.0,
        status_poll_interval_s: float = 0.35,
    ) -> None:
        self.run_dir = run_dir
        self.condition = condition
        self.level = level
        self.follower = follower_client
        self.scheduler = scheduler_client
        self.post_wait_s = post_wait_s
        self.status_poll_interval_s = status_poll_interval_s
        self.status_log_path = run_dir / "step_status_series.jsonl"
        self.summary_path = run_dir / "step_results.json"

    def execute_plan(self, steps: Sequence[CommandStep]) -> List[StepRecord]:
        records: List[StepRecord] = []
        series_handle = self.status_log_path.open("w", encoding="utf-8")
        try:
            for index, step in enumerate(steps, start=1):
                record = self._execute_step(index, step)
                records.append(record)
                series_handle.write(json.dumps({
                    "index": index,
                    "status_series": record.status_series,
                }) + "\n")
                series_handle.flush()
        finally:
            series_handle.close()
        with self.summary_path.open("w", encoding="utf-8") as handle:
            json.dump([record.to_jsonable() for record in records], handle, indent=2)
        return records

    def _execute_step(self, index: int, step: CommandStep) -> StepRecord:
        logging.info(
            "Executing step %d: algorithm=%s suite=%s duration=%.1fs pre_gap=%.1fs",
            index,
            step.algorithm,
            step.suite,
            step.duration_s,
            step.pre_gap_s,
        )
        started_ns = time.time_ns()
        try:
            self.scheduler.send_switch(step)
        except Exception as exc:
            logging.error("Failed to send switch_suite command: %s", exc)
            return StepRecord(
                index=index,
                step=step,
                started_ns=started_ns,
                completed_ns=time.time_ns(),
                success=False,
                error=str(exc),
            )

        deadline = time.monotonic() + step.pre_gap_s + step.duration_s + self.post_wait_s
        status_series: List[dict] = []
        current_suite = None
        pending_suite = None
        success = False
        error: Optional[str] = None

        while time.monotonic() < deadline:
            time.sleep(self.status_poll_interval_s)
            try:
                status = self.follower.status()
            except Exception as exc:
                logging.warning("Status poll failed: %s", exc)
                continue
            status_record = {
                "ts_ns": time.time_ns(),
                "suite": status.get("suite"),
                "pending_suite": status.get("pending_suite"),
                "last_requested_suite": status.get("last_requested_suite"),
                "running": status.get("running"),
            }
            status_series.append(status_record)
            current_suite = status_record["suite"]
            pending_suite = status_record["pending_suite"]
            if current_suite == step.suite and not pending_suite:
                success = True
                if time.monotonic() >= deadline - self.post_wait_s:
                    break

        completed_ns = time.time_ns()
        if not success:
            error = "suite_not_active" if current_suite != step.suite else "pending_not_cleared"
            logging.warning(
                "Step %d did not confirm activation (suite=%s pending=%s)",
                index,
                current_suite,
                pending_suite,
            )

        power_status = self.follower.poll_power_status(step.duration_s)
        final_status = None
        try:
            final_status = self.follower.status()
        except Exception as exc:
            logging.warning("Final status fetch failed: %s", exc)

        logging.info(
            "Step %d summary: success=%s suite=%s power_busy=%s",
            index,
            success,
            (final_status or {}).get("suite"),
            power_status.get("busy"),
        )

        return StepRecord(
            index=index,
            step=step,
            started_ns=started_ns,
            completed_ns=completed_ns,
            success=success,
            error=error,
            status_series=status_series,
            final_status=final_status,
            power_status=power_status,
        )


def build_plan(level: str, repeat: int, duration_s: float, pre_gap_s: float) -> List[CommandStep]:
    if level not in LEVEL_PLAN:
        raise ValueError(f"unknown level '{level}'")
    template = LEVEL_PLAN[level]
    plan: List[CommandStep] = []
    for _ in range(repeat):
        for step in template:
            plan.append(
                CommandStep(
                    algorithm=step.algorithm,
                    suite=step.suite,
                    duration_s=duration_s,
                    pre_gap_s=pre_gap_s,
                )
            )
    return plan


def prepare_run_directory(output_root: Path, condition: str, level: str) -> Path:
    ts_str = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    run_dir = output_root / f"{ts_str}_{condition}_{level}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "raw").mkdir(exist_ok=True)
    return run_dir


def configure_logging(run_dir: Path, verbose: bool) -> None:
    log_path = run_dir / "run.log"
    handlers = [
        logging.FileHandler(log_path, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    logging.info("Logging initialised; file=%s", log_path)


def compute_session_dir(session_id: Optional[str]) -> Optional[Path]:
    if not session_id:
        return None
    auto_cfg = CONFIG.get("AUTO_DRONE", {})
    base = auto_cfg.get("monitor_output_base") or os.getenv(
        "DRONE_MONITOR_OUTPUT_BASE",
        "/home/dev/research/output/drone",
    )
    session_dir = Path(base).expanduser().resolve() / session_id
    return session_dir if session_dir.exists() else None


def snapshot_artifacts(
    run_dir: Path,
    follower_session_dir: Optional[Path],
    gcs_outdir: Path,
) -> Dict[str, str]:
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    artifacts: Dict[str, str] = {}
    if follower_session_dir and follower_session_dir.exists():
        dest = raw_dir / f"drone_{follower_session_dir.name}"
        logging.info("Copying follower session directory -> %s", dest)
        shutil.copytree(follower_session_dir, dest, dirs_exist_ok=True)
        artifacts["follower_session"] = str(dest)
    else:
        logging.warning("Follower session directory not found; skipping copy")
    if gcs_outdir.exists():
        dest = raw_dir / "gcs_out"
        logging.info("Copying GCS outdir -> %s", dest)
        shutil.copytree(gcs_outdir, dest, dirs_exist_ok=True)
        artifacts["gcs_outdir"] = str(dest)
    else:
        logging.warning("GCS outdir %s missing", gcs_outdir)
    summary_src = Path("logs/auto/gcs/summary.csv")
    if summary_src.exists():
        summary_dest = run_dir / "gcs_summary_snapshot.csv"
        shutil.copy2(summary_src, summary_dest)
        artifacts["gcs_summary_snapshot"] = str(summary_dest)
    status_src = gcs_outdir / "gcs_status.json"
    if status_src.exists():
        status_dest = run_dir / "gcs_status_snapshot.json"
        shutil.copy2(status_src, status_dest)
        artifacts["gcs_status_snapshot"] = str(status_dest)
    return artifacts


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Master orchestrator for PQC evaluation")
    parser.add_argument("--condition", default=DEFAULT_CONDITION, help="traffic condition tag (noddos/xgboost/tst)")
    parser.add_argument("--level", default=DEFAULT_LEVEL, choices=sorted(LEVEL_PLAN.keys()))
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION, help="step duration seconds")
    parser.add_argument("--pre-gap", type=float, default=DEFAULT_PRE_GAP, dest="pre_gap", help="schedule pre-gap seconds")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="repeat the level plan this many times")
    parser.add_argument("--output-root", type=Path, default=Path("output/campaign_runs"))
    parser.add_argument("--scheduler-host", default=CONFIG.get("GCS_HOST", "127.0.0.1"))
    parser.add_argument("--scheduler-port", type=int, default=int(CONFIG.get("DRONE_TO_GCS_CTL_PORT", 48181)))
    parser.add_argument("--control-host", default=CONFIG.get("DRONE_HOST", "127.0.0.1"))
    parser.add_argument("--control-port", type=int, default=int(CONFIG.get("DRONE_CONTROL_PORT", 48080)))
    parser.add_argument("--follower-script", type=Path, default=Path("tools/auto/drone_follower.py"))
    parser.add_argument("--follower-extra", default="--pi5", help="additional args for follower script")
    parser.add_argument("--scheduler-script", type=Path, default=Path("gcs/mav_gcs_scheduler.py"))
    parser.add_argument(
        "--scheduler-extra",
        default="--listen-host 0.0.0.0 --listen-port 48181 --outdir logs/mavproxy/gcs",
        help="additional args for scheduler script",
    )
    parser.add_argument("--python", default=sys.executable, help="Python interpreter to use for launched processes")
    parser.add_argument("--stop-processes", action="store_true", help="terminate follower/scheduler when run completes")
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--dry-run", action="store_true", help="plan only; do not send switch commands")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    run_dir = prepare_run_directory(args.output_root, args.condition, args.level)
    configure_logging(run_dir, verbose=args.verbose)
    logging.info("Run directory initialised: %s", run_dir)

    plan = build_plan(args.level, args.repeat, args.duration, args.pre_gap)
    logging.info("Plan contains %d steps", len(plan))

    follower_cmd = [args.python, str(args.follower_script)]
    if args.follower_extra:
        follower_cmd.extend(shlex.split(args.follower_extra))
    scheduler_cmd = [args.python, str(args.scheduler_script)]
    if args.scheduler_extra:
        scheduler_cmd.extend(shlex.split(args.scheduler_extra))

    follower_client = FollowerClient(args.control_host, args.control_port)
    scheduler_client = SchedulerClient(args.scheduler_host, args.scheduler_port)
    supervisor = ProcessSupervisor(
        run_logs_dir=run_dir / "logs",
        follower_client=follower_client,
        scheduler_client=scheduler_client,
        follower_cmd=follower_cmd,
        scheduler_cmd=scheduler_cmd,
        stop_on_exit=args.stop_processes,
    )

    try:
        session_id = supervisor.ensure_follower()
        supervisor.ensure_scheduler()
    except Exception as exc:
        logging.error("Process initialisation failed: %s", exc)
        supervisor.maybe_stop_processes()
        return 1

    run_meta = {
        "condition": args.condition,
        "level": args.level,
        "duration_s": args.duration,
        "pre_gap_s": args.pre_gap,
        "repeat": args.repeat,
        "run_dir": str(run_dir),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "follower_cmd": follower_cmd,
        "scheduler_cmd": scheduler_cmd,
    }

    outcomes: List[StepRecord] = []
    if args.dry_run:
        logging.info("Dry-run mode: skipping execution of switch commands")
    else:
        orchestrator = RunOrchestrator(
            run_dir=run_dir,
            condition=args.condition,
            level=args.level,
            follower_client=follower_client,
            scheduler_client=scheduler_client,
        )
        outcomes = orchestrator.execute_plan(plan)

    follower_session_dir = compute_session_dir(session_id)
    gcs_outdir_tokens = shlex.split(args.scheduler_extra)
    gcs_outdir = Path("logs/mavproxy/gcs")
    if "--outdir" in gcs_outdir_tokens:
        try:
            idx = gcs_outdir_tokens.index("--outdir")
            gcs_outdir = Path(gcs_outdir_tokens[idx + 1])
        except (ValueError, IndexError):
            logging.warning("Failed to parse --outdir from scheduler-extra; using default %s", gcs_outdir)
    artifact_index = snapshot_artifacts(run_dir, follower_session_dir, gcs_outdir)

    step_summary = [record.to_jsonable() for record in outcomes]
    run_meta["step_results_path"] = str((run_dir / "step_results.json").resolve())
    run_meta["artifacts"] = artifact_index
    run_meta["steps"] = step_summary

    with (run_dir / "run_manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(run_meta, handle, indent=2)

    supervisor.maybe_stop_processes()

    failed = [record for record in outcomes if not record.success]
    if failed:
        logging.error("Run completed with %d failed steps", len(failed))
        return 2
    logging.info("Run completed successfully")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
