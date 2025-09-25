"""Launch a four-terminal manual end-to-end test for the PQC proxy.

Processes spawned:
  1. GCS proxy (core.run_proxy gcs)
  2. Drone proxy (core.run_proxy drone)
  3. GCS ground-station simulator (commands -> proxy, telemetry <- proxy)
  4. Drone autopilot simulator (telemetry -> proxy, commands <- proxy)

Optional fifth process:
  - Encrypted bridge logger that sits between the proxies and prints
    ciphertext metadata while forwarding packets in both directions.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
MANUAL_DIR = Path(__file__).resolve().parent
DEFAULT_SECRETS = REPO_ROOT / "secrets"

PORTS = {
    "TCP_HANDSHAKE": 46000,
    "GCS_ENCRYPTED_BIND": 46011,
    "DRONE_ENCRYPTED_BIND": 46012,
    "INTERCEPT_D2G_LISTEN": 46001,
    "INTERCEPT_G2D_LISTEN": 46002,
    "GCS_PLAINTEXT_TX": 47001,
    "GCS_PLAINTEXT_RX": 47002,
    "DRONE_PLAINTEXT_TX": 47003,
    "DRONE_PLAINTEXT_RX": 47004,
}

_BUFFERED_TEXT = bool(os.name != "nt")  # On Windows CREATE_NEW_CONSOLE forbids capturing


@dataclass
class ProcessSpec:
    label: str
    command: List[str]
    env: Dict[str, str]
    new_window: bool


@dataclass
class ProcessHandle:
    spec: ProcessSpec
    process: subprocess.Popen
    pump_thread: Optional[threading.Thread]


def _ensure_identity(suite: str, secrets_dir: Path) -> None:
    secret_path = secrets_dir / "gcs_signing.key"
    public_path = secrets_dir / "gcs_signing.pub"
    if secret_path.exists() and public_path.exists():
        return

    print(f"[setup] Generating GCS signing identity in {secrets_dir}")
    secrets_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "core.run_proxy", "init-identity", "--suite", suite, "--output-dir", str(secrets_dir)]
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise RuntimeError("Failed to initialise GCS signing identity")


def _stream_output(label: str, proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"[{label}] {line.rstrip()}" )
    proc.stdout.close()


def _launch_process(spec: ProcessSpec, cwd: Path) -> ProcessHandle:
    creationflags = 0
    stdout = None
    stderr = None

    if spec.new_window and os.name == "nt":
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    elif spec.new_window:
        print(f"[warn] '--new-windows' requested but not supported on this platform. Running inline instead for {spec.label}.")

    if not spec.new_window:
        stdout = subprocess.PIPE
        stderr = subprocess.STDOUT

    proc = subprocess.Popen(
        spec.command,
        cwd=cwd,
        env=spec.env,
        stdout=stdout,
        stderr=stderr,
        text=True,
        bufsize=1,
    )

    pump_thread: Optional[threading.Thread] = None
    if stdout is not None and proc.stdout is not None:
        pump_thread = threading.Thread(target=_stream_output, args=(spec.label, proc), daemon=True)
        pump_thread.start()

    return ProcessHandle(spec, proc, pump_thread)


def _build_env(overrides: Dict[str, int]) -> Dict[str, str]:
    env = os.environ.copy()
    for key, value in overrides.items():
        env[key] = str(value)
    return env


def _build_specs(args: argparse.Namespace, secrets_dir: Path) -> List[ProcessSpec]:
    secret_path = secrets_dir / "gcs_signing.key"
    public_path = secrets_dir / "gcs_signing.pub"

    # Base overrides shared by both proxies
    base_overrides = {
        "TCP_HANDSHAKE_PORT": PORTS["TCP_HANDSHAKE"],
        "DRONE_HOST": "127.0.0.1",
        "GCS_HOST": "127.0.0.1",
    }

    if args.with_intercept:
        drone_peer_port = PORTS["INTERCEPT_D2G_LISTEN"]
        gcs_peer_port = PORTS["INTERCEPT_G2D_LISTEN"]
    else:
        drone_peer_port = PORTS["GCS_ENCRYPTED_BIND"]
        gcs_peer_port = PORTS["DRONE_ENCRYPTED_BIND"]

    gcs_env = _build_env({
        **base_overrides,
        "UDP_GCS_RX": PORTS["GCS_ENCRYPTED_BIND"],
        "UDP_DRONE_RX": gcs_peer_port,
        "GCS_PLAINTEXT_TX": PORTS["GCS_PLAINTEXT_TX"],
        "GCS_PLAINTEXT_RX": PORTS["GCS_PLAINTEXT_RX"],
    })

    drone_env = _build_env({
        **base_overrides,
        "UDP_DRONE_RX": PORTS["DRONE_ENCRYPTED_BIND"],
        "UDP_GCS_RX": drone_peer_port,
        "DRONE_PLAINTEXT_TX": PORTS["DRONE_PLAINTEXT_TX"],
        "DRONE_PLAINTEXT_RX": PORTS["DRONE_PLAINTEXT_RX"],
    })

    specs: List[ProcessSpec] = []

    gcs_cmd = [sys.executable, "-m", "core.run_proxy", "gcs", "--suite", args.suite]
    if secret_path != DEFAULT_SECRETS / "gcs_signing.key":
        gcs_cmd += ["--gcs-secret-file", str(secret_path)]
    specs.append(ProcessSpec("GCS", gcs_cmd, gcs_env, args.new_windows))

    drone_cmd = [
        sys.executable,
        "-m",
        "core.run_proxy",
        "drone",
        "--suite",
        args.suite,
        "--peer-pubkey-file",
        str(public_path),
    ]
    specs.append(ProcessSpec("DRONE", drone_cmd, drone_env, args.new_windows))

    gcs_sim_cmd = [
        sys.executable,
        str(MANUAL_DIR / "gcs_ground_station_sim.py"),
        "--send-port",
        str(PORTS["GCS_PLAINTEXT_TX"]),
        "--recv-port",
        str(PORTS["GCS_PLAINTEXT_RX"]),
        "--loop",
    ]
    specs.append(ProcessSpec("GCS-SIM", gcs_sim_cmd, os.environ.copy(), args.new_windows))

    drone_sim_cmd = [
        sys.executable,
        str(MANUAL_DIR / "drone_autopilot_sim.py"),
        "--send-port",
        str(PORTS["DRONE_PLAINTEXT_TX"]),
        "--recv-port",
        str(PORTS["DRONE_PLAINTEXT_RX"]),
        "--loop",
    ]
    specs.append(ProcessSpec("DRONE-SIM", drone_sim_cmd, os.environ.copy(), args.new_windows))

    if args.with_intercept:
        bridge_cmd = [
            sys.executable,
            str(MANUAL_DIR / "encrypted_bridge_logger.py"),
            "--d2g-listen",
            str(PORTS["INTERCEPT_D2G_LISTEN"]),
            "--d2g-forward",
            f"127.0.0.1:{PORTS['GCS_ENCRYPTED_BIND']}",
            "--g2d-listen",
            str(PORTS["INTERCEPT_G2D_LISTEN"]),
            "--g2d-forward",
            f"127.0.0.1:{PORTS['DRONE_ENCRYPTED_BIND']}",
        ]
        specs.append(ProcessSpec("BRIDGE", bridge_cmd, os.environ.copy(), args.new_windows))

    return specs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch a manual four-terminal PQC proxy test")
    parser.add_argument("--suite", default="cs-kyber768-aesgcm-dilithium3", help="Cryptographic suite for both proxies (default: %(default)s)")
    parser.add_argument("--secrets-dir", default=str(DEFAULT_SECRETS), help="Directory containing GCS keypair (default: %(default)s)")
    parser.add_argument("--no-auto-init", action="store_true", help="Do not auto-generate GCS keys if missing")
    parser.add_argument("--with-intercept", action="store_true", help="Launch the encrypted bridge logger between proxies")
    parser.add_argument("--new-windows", action="store_true", help="Attempt to open each process in a new console window (Windows only)")
    return parser.parse_args()


def print_banner(args: argparse.Namespace) -> None:
    print("Manual PQC proxy test launcher")
    print("Repository root:", REPO_ROOT)
    print("Suite:", args.suite)
    print("Secrets directory:", args.secrets_dir)
    print("Intercept enabled:" if args.with_intercept else "Intercept disabled", args.with_intercept)
    print("Ports in use:")
    for key, value in PORTS.items():
        print(f"  {key:<24} {value}")
    print()
    print("Press Ctrl+C in this window to stop all managed processes.")


def main() -> None:
    args = parse_args()
    secrets_dir = Path(args.secrets_dir).resolve()

    if not args.no_auto_init:
        _ensure_identity(args.suite, secrets_dir)

    print_banner(args)

    specs = _build_specs(args, secrets_dir)
    handles: List[ProcessHandle] = []

    try:
        for spec in specs:
            handle = _launch_process(spec, REPO_ROOT)
            handles.append(handle)
            print(f"[launch] Started {spec.label} (PID {handle.process.pid})")

        while True:
            for handle in list(handles):
                code = handle.process.poll()
                if code is not None:
                    print(f"[exit] {handle.spec.label} exited with code {code}")
                    handles.remove(handle)
            if not handles:
                print("[launcher] All processes exited. Stopping launcher.")
                break
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n[launcher] Interrupt received, terminating child processes...")
    finally:
        for handle in handles:
            if handle.process.poll() is None:
                try:
                    if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                        handle.process.send_signal(signal.CTRL_BREAK_EVENT)
                        time.sleep(0.3)
                    handle.process.terminate()
                except Exception:
                    pass
        time.sleep(0.5)
        for handle in handles:
            if handle.process.poll() is None:
                try:
                    handle.process.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
