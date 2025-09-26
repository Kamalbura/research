"""Static check to ensure IPs/ports are sourced from core.config."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_SUFFIXES = {".py", ".ps1", ".sh"}
ALLOW_DIRS = {".git", "__pycache__", "venv", "env"}
SKIP_PREFIXES = {
    REPO_ROOT / "core" / "config.py",
}
SKIP_DIRS = {REPO_ROOT / "tests" / "fixtures"}

IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PORT_PATTERN = re.compile(r"socket\.(?:bind|connect|sendto)\([^\n\#]*?(\d{4,5})")
ALLOWED_IPS = {"0.0.0.0", "127.0.0.1", "::1"}
ALLOWED_PORTS = {"0", "53"}


def iter_files() -> Iterable[Path]:
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in TARGET_SUFFIXES:
            continue
        if any(part in ALLOW_DIRS for part in path.parts):
            continue
        if any(path.is_relative_to(skip) for skip in SKIP_DIRS):
            continue
        yield path


def find_violations(path: Path) -> Tuple[List[str], List[str]]:
    if path in SKIP_PREFIXES:
        return [], []
    text = path.read_text(encoding="utf-8", errors="ignore")
    ips = []
    for match in IP_PATTERN.finditer(text):
        ip = match.group(0)
        if ip in ALLOWED_IPS:
            continue
        ips.append(f"{path}:{match.start()} -> {ip}")
    ports = []
    for match in PORT_PATTERN.finditer(text):
        port = match.group(1)
        if port in ALLOWED_PORTS:
            continue
        ports.append(f"{path}:{match.start()} -> {port}")
    return ips, ports


def main() -> int:
    ip_violations: List[str] = []
    port_violations: List[str] = []

    for path in iter_files():
        ips, ports = find_violations(path)
        ip_violations.extend(ips)
        port_violations.extend(ports)

    if ip_violations or port_violations:
        if ip_violations:
            print("IP literal violations detected:")
            for item in ip_violations:
                print(f"  {item}")
        if port_violations:
            print("Port literal violations detected:")
            for item in port_violations:
                print(f"  {item}")
        return 1

    print("No hard-coded IPs or forbidden port literals detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
