"""Simple consolidation helper for telemetry output folders.

Produces a manifest.json per session and can combine key CSVs into a single
directory for quick archival. This is intentionally small and dependency-free.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

OUT_BASE = Path("output/gcs")


def consolidate_session(session_id: str | int) -> Dict:
    session_dir = OUT_BASE / str(session_id)
    if not session_dir.exists():
        raise FileNotFoundError(session_dir)
    manifest = {
        "session_id": str(session_id),
        "files": [],
    }
    for f in sorted(session_dir.iterdir()):
        if f.is_file():
            manifest["files"].append(str(f.name))

    # write manifest
    manifest_path = session_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def consolidate_all() -> Dict[str, Dict]:
    results = {}
    for d in OUT_BASE.iterdir():
        if d.is_dir():
            try:
                results[str(d.name)] = consolidate_session(d.name)
            except Exception:
                continue
    return results


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--session", help="session id to consolidate (optional)")
    args = p.parse_args()
    if args.session:
        print(consolidate_session(args.session))
    else:
        print(consolidate_all())
