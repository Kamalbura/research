#!/usr/bin/env python3
"""
Consolidate JSON logs into a single text file.

Usage:
    python consolidate_json_logs.py <root_dir> <output_file>

Example:
    python tools/auto/consolidate_json_logs.py logs/auto/drone consolidated_drone_logs.txt

The script walks `root_dir` recursively, finds files ending in `.json` (case-insensitive), and writes a single
text file with entries like:

---
Folder: <relative/folder/path>
File: <filename.json>
Size: 1234 bytes
Modified: 2025-09-29 09:00:00

<pretty-printed JSON or raw content>
---

If a file is not valid JSON, the raw file contents are included (with non-UTF8 bytes replaced).
"""

from __future__ import annotations
import sys
import json
from pathlib import Path
from datetime import datetime


def consolidate(root: Path, out_file: Path, skip_dirs: set[str] | None = None):
    if skip_dirs is None:
        skip_dirs = set()

    json_files = []
    for p in sorted(root.rglob('*.json')):
        # skip files in hidden dirs or in skip_dirs
        if any(part.startswith('.') for part in p.parts):
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        json_files.append(p)

    if not json_files:
        print(f"No JSON files found under {root}")
        return

    with out_file.open('w', encoding='utf-8') as out:
        out.write(f"Consolidated JSON logs from: {root}\n")
        out.write(f"Generated: {datetime.now().isoformat()}\n")
        out.write('=' * 80 + '\n\n')

        for i, p in enumerate(json_files, 1):
            rel_folder = p.parent.relative_to(root)
            out.write('-' * 60 + '\n')
            out.write(f"Entry {i}/{len(json_files)}\n")
            out.write(f"Folder: {rel_folder}\n")
            out.write(f"File: {p.name}\n")
            try:
                st = p.stat()
                out.write(f"Size: {st.st_size} bytes\n")
                out.write(f"Modified: {datetime.fromtimestamp(st.st_mtime).isoformat()}\n")
            except Exception as e:
                out.write(f"[Error getting file stat: {e}]\n")

            out.write('\n')
            try:
                raw = p.read_bytes()
                try:
                    text = raw.decode('utf-8')
                except Exception:
                    text = raw.decode('utf-8', errors='replace')

                # Try to parse JSON and pretty-print
                try:
                    obj = json.loads(text)
                    pretty = json.dumps(obj, indent=2, ensure_ascii=False)
                    out.write(pretty + '\n')
                except Exception:
                    out.write(text + '\n')

            except Exception as e:
                out.write(f"[Error reading file: {e}]\n")

            out.write('\n')

    print(f"Wrote consolidated log to: {out_file}")


def main(argv: list[str]):
    if len(argv) < 3:
        print("Usage: consolidate_json_logs.py <root_dir> <output_file>")
        return
    root = Path(argv[1]).resolve()
    out = Path(argv[2]).resolve()
    if not root.exists() or not root.is_dir():
        print(f"Error: root dir {root} does not exist or is not a directory")
        return

    consolidate(root, out)


if __name__ == '__main__':
    main(sys.argv)
