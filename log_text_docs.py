#!/usr/bin/env python3
"""Aggregate all Markdown and text files into a single report."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable


def find_text_docs(root: Path) -> Iterable[Path]:
    """Yield only .txt files under root (recursive)."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".txt":
            yield path


def load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Fall back to replacing undecodable bytes so dump never aborts.
        return path.read_text(encoding="utf-8", errors="replace")


def write_report(files: Iterable[Path], root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for doc in sorted(files):
            if doc.resolve() == output.resolve():
                continue
            rel = doc.relative_to(root)
            handle.write(f"===== BEGIN {rel.as_posix()} =====\n")
            body = load_text(doc)
            handle.write(body)
            if not body.endswith("\n"):
                handle.write("\n")
            handle.write(f"===== END {rel.as_posix()} =====\n\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dump all Markdown/text files into one log")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Root directory to scan (default: current working directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("codebase-read.txt"),
        help="Destination file for the aggregated contents",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    files = list(find_text_docs(root))
    write_report(files, root, args.output.resolve())


if __name__ == "__main__":
    main()
