"""Aggregate LAN test artifacts into CSV/JSONL/Markdown summaries.

Usage:
    python -m tools.aggregate_lan_results --results-dir results-20250927-120000
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Iterable, List, Dict

SUMMARY_FIELDS = [
    "suite",
    "side",
    "ptx_out",
    "ptx_in",
    "enc_out",
    "enc_in",
    "drops",
    "drop_replay",
    "drop_auth",
    "drop_header",
    "drop_session_epoch",
    "drop_other",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate LAN matrix artifacts")
    parser.add_argument(
        "--results-dir",
        required=True,
        help="Path to the results directory (e.g., results-YYYYMMDD-HHMMSS)",
    )
    parser.add_argument(
        "--markdown-name",
        default="SUMMARY.md",
        help="Name of the generated Markdown summary file",
    )
    parser.add_argument(
        "--csv-name",
        default="summary.csv",
        help="Name of the generated CSV file",
    )
    parser.add_argument(
        "--jsonl-name",
        default="summary.jsonl",
        help="Name of the generated JSONL file",
    )
    return parser.parse_args()


def load_counters(path: Path) -> Dict[str, int]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse JSON from {path}: {exc}")
    return data.get("counters", {})


def discover_runs(results_dir: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for json_path in results_dir.glob("*_debug_*.json"):
        match = re.search(r"_(cs-[^_]+)\.json$", json_path.name)
        suite = match.group(1) if match else "unknown"
        side = "gcs" if "gcs_" in json_path.name else "drone"
        counters = load_counters(json_path)
        row = {"suite": suite, "side": side}
        for key in SUMMARY_FIELDS:
            if key in ("suite", "side"):
                continue
            row[key] = counters.get(key, 0)
        rows.append(row)
    return rows


def write_jsonl(rows: Iterable[Dict[str, object]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def write_csv(rows: Iterable[Dict[str, object]], path: Path) -> None:
    rows = list(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            record = {field: row.get(field, "") for field in SUMMARY_FIELDS}
            writer.writerow(record)


def suite_pass(rows: List[Dict[str, object]], suite: str) -> bool:
    gcs = next((row for row in rows if row["suite"] == suite and row["side"] == "gcs"), None)
    drone = next((row for row in rows if row["suite"] == suite and row["side"] == "drone"), None)
    if not gcs or not drone:
        return False
    checks = []
    for entry in (gcs, drone):
        checks.append(entry.get("drops", 0) == 0)
        checks.append(entry.get("enc_in", 0) > 0)
        checks.append(entry.get("enc_out", 0) > 0)
        checks.append(entry.get("ptx_in", 0) > 0)
        checks.append(entry.get("ptx_out", 0) > 0)
    return all(checks)


def write_markdown(rows: List[Dict[str, object]], path: Path) -> None:
    suites = sorted(set(row["suite"] for row in rows))
    lines = ["# PQC Drone↔GCS LAN Matrix — Summary", ""]
    for suite in suites:
        lines.append(f"- {suite}: {'PASS' if suite_pass(rows, suite) else 'FAIL'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    results_dir = Path(args.results_dir).expanduser().resolve()
    if not results_dir.exists():
        raise SystemExit(f"Results directory {results_dir} does not exist")

    rows = discover_runs(results_dir)
    if not rows:
        raise SystemExit(f"No *_debug_*.json files found in {results_dir}")

    jsonl_path = results_dir / args.jsonl_name
    csv_path = results_dir / args.csv_name
    md_path = results_dir / args.markdown_name

    write_jsonl(rows, jsonl_path)
    write_csv(rows, csv_path)
    write_markdown(rows, md_path)

    print(f"Wrote {jsonl_path}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
