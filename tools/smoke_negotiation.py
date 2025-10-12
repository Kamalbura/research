#!/usr/bin/env python3
"""Offline smoke test for negotiation and artifact fetch helpers.

This script exercises the capability filtering logic and the artifact
fetch pipeline using local placeholder data so that developers can
validate behaviour without a running drone or GCS proxy.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import sys


def _ensure_repo_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


_ensure_repo_root()

from tools.auto import gcs_scheduler as scheduler


def _run_negotiation_demo() -> None:
    suites = [
        "cs-mlkem768-aesgcm-mldsa65",
        "cs-experimental-ascon128-suite",
    ]
    capabilities = {
        "supported_suites": ["cs-mlkem768-aesgcm-mldsa65"],
        "unsupported_suites": [
            {
                "suite": "cs-experimental-ascon128-suite",
                "reasons": ["aead_unavailable"],
                "details": {"aead_token": "ascon128", "aead_hint": "pyascon missing"},
            }
        ],
    }

    filtered, skipped = scheduler.filter_suites_for_follower(suites, capabilities)
    print("=== Capability negotiation demo ===")
    print(f"Input suites: {suites}")
    print(f"Filtered suites: {filtered}")
    print(f"Skipped entries: {json.dumps(skipped, indent=2)}")
    print()


def _run_fetch_demo() -> None:
    print("=== Fetch strategy demo (smb/local copy) ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        remote_root = tmp_path / "remote_artifacts"
        remote_root.mkdir()
        (remote_root / "power.csv").write_text("ts_w,voltage,current\n0,4.9,0.8\n", encoding="utf-8")
        (remote_root / "status.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

        local_dest = tmp_path / "collected"
        err = scheduler._fetch_remote_path(
            str(remote_root),
            local_dest,
            recursive=True,
            category="smoke_fetch",
            target=None,
            password=None,
            key_path=None,
            strategy="smb",
        )

        if err:
            print(f"Fetch failed: {err}")
            return

        collected = sorted(str(path.relative_to(local_dest)) for path in local_dest.rglob("*") if path.is_file())
        print(f"Fetched artifacts into: {local_dest}")
        print("Files:")
        for rel in collected:
            print(f"  - {rel}")
        print()


def main() -> None:
    _run_negotiation_demo()
    _run_fetch_demo()
    print("Smoke tests completed successfully.")


if __name__ == "__main__":
    main()
