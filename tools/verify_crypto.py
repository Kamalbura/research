#!/usr/bin/env python3
"""Verify PQC suite availability for the drone/GCS automation stack.

This helper inspects the current Python environment to determine which
KEMs, signatures, and AEAD tokens are available via liboqs and optional
dependencies (pyascon/ascon). It then checks that every suite requested
by the automation configuration can be serviced locally.

Example usage::

    python tools/verify_crypto.py              # check AUTO_GCS suites (default)
    python tools/verify_crypto.py --all        # check the entire registry
    python tools/verify_crypto.py --suite cs-mlkem768-aesgcm-mldsa65

Use ``--strict`` to exit with a non-zero status when a required primitive
is missing. ``--json`` emits a machine-readable summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional


def _ensure_repo_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


_ensure_repo_root()

from core import suites as suites_mod
from core.config import CONFIG


def _collect_enabled() -> Dict[str, object]:
    """Return enabled primitive sets along with probe errors."""

    result: Dict[str, object] = {}

    try:
        result["enabled_kems"] = sorted(set(suites_mod.enabled_kems()))
    except Exception as exc:  # pragma: no cover - depends on oqs install
        result["enabled_kems"] = []
        result["kem_error"] = str(exc)

    try:
        result["enabled_sigs"] = sorted(set(suites_mod.enabled_sigs()))
    except Exception as exc:  # pragma: no cover - depends on oqs install
        result["enabled_sigs"] = []
        result["sig_error"] = str(exc)

    available_aeads = set(suites_mod.available_aead_tokens())
    missing_aeads = suites_mod.unavailable_aead_reasons()
    result["available_aeads"] = sorted(available_aeads)
    result["missing_aead_reasons"] = missing_aeads

    return result


def _determine_target_suites(arg_suites: List[str], check_all: bool) -> List[str]:
    suite_map = suites_mod.list_suites()
    registry_ids = sorted(suite_map.keys())

    if check_all:
        return registry_ids

    if arg_suites:
        resolved: List[str] = []
        for name in arg_suites:
            try:
                resolved.append(suites_mod.get_suite(name)["suite_id"])
            except NotImplementedError:
                resolved.append(name)
        return resolved

    configured = CONFIG.get("AUTO_GCS", {}).get("suites")
    if configured:
        resolved = []
        for entry in configured:
            try:
                resolved.append(suites_mod.get_suite(entry)["suite_id"])
            except NotImplementedError:
                resolved.append(str(entry))
        return resolved

    # Default fallback: use the registry in stable order.
    return registry_ids


def _check_suites(
    suite_ids: List[str],
    primitives: Dict[str, object],
) -> Dict[str, object]:
    enabled_kems = set(primitives.get("enabled_kems") or [])
    enabled_sigs = set(primitives.get("enabled_sigs") or [])
    available_aeads = set(primitives.get("available_aeads") or [])
    missing_aead_reasons = primitives.get("missing_aead_reasons") or {}

    findings: List[Dict[str, object]] = []

    for suite_id in suite_ids:
        try:
            suite_info = suites_mod.get_suite(suite_id)
        except NotImplementedError as exc:
            findings.append(
                {
                    "suite": suite_id,
                    "status": "unknown_suite",
                    "details": {"error": str(exc)},
                }
            )
            continue

        kem_name = suite_info.get("kem_name")
        sig_name = suite_info.get("sig_name")
        aead_token = suite_info.get("aead_token")

        kem_ok = kem_name in enabled_kems if enabled_kems else False
        sig_ok = sig_name in enabled_sigs if enabled_sigs else False
        aead_ok = aead_token in available_aeads if available_aeads else False

        if kem_ok and sig_ok and aead_ok:
            findings.append({"suite": suite_info["suite_id"], "status": "ok"})
            continue

        details: Dict[str, object] = {}
        missing_parts: List[str] = []
        if not kem_ok:
            missing_parts.append("kem")
            details["kem_name"] = kem_name
        if not sig_ok:
            missing_parts.append("sig")
            details["sig_name"] = sig_name
        if not aead_ok:
            missing_parts.append("aead")
            details["aead_token"] = aead_token
            if aead_token in missing_aead_reasons:
                details["aead_hint"] = missing_aead_reasons[aead_token]

        findings.append(
            {
                "suite": suite_info["suite_id"],
                "status": "missing",
                "missing": missing_parts,
                "details": details,
            }
        )

    return {
        "findings": findings,
        "primitives": primitives,
    }


def _print_summary(summary: Dict[str, object]) -> None:
    primitives = summary["primitives"]
    findings: List[Dict[str, object]] = summary["findings"]  # type: ignore[assignment]

    def _fmt(name: str, values: Optional[List[str]]) -> str:
        if not values:
            return f"{name}: none"
        return f"{name}: {', '.join(values)}"

    print(_fmt("KEMs", primitives.get("enabled_kems")))
    if primitives.get("kem_error"):
        print(f"  [warn] KEM probe failed: {primitives['kem_error']}", file=sys.stderr)
    print(_fmt("Signatures", primitives.get("enabled_sigs")))
    if primitives.get("sig_error"):
        print(f"  [warn] signature probe failed: {primitives['sig_error']}", file=sys.stderr)
    print(_fmt("AEAD tokens", primitives.get("available_aeads")))

    missing_reasons: Dict[str, str] = primitives.get("missing_aead_reasons") or {}
    if missing_reasons:
        print("Missing AEAD reasons:")
        for token, reason in sorted(missing_reasons.items()):
            print(f"  - {token}: {reason}")

    missing = [item for item in findings if item["status"] != "ok"]
    if not missing:
        print("All requested suites are available.")
        return

    print("Suites with missing primitives:")
    for item in missing:
        parts = ", ".join(item.get("missing") or ["unknown"])
        print(f"  - {item['suite']}: {parts}")
        details = item.get("details") or {}
        for key, value in details.items():
            print(f"      {key}: {value}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify PQC suite availability.")
    parser.add_argument(
        "--suite",
        action="append",
        dest="suites",
        default=[],
        help="Suite ID to check (may be repeated). Defaults to AUTO_GCS suites.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check the entire suite registry instead of the configured subset.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 1 when any suite is missing primitives.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary (stdout) instead of the human-readable report.",
    )
    args = parser.parse_args(argv)

    primitives = _collect_enabled()
    suite_ids = _determine_target_suites(args.suites, args.all)
    summary = _check_suites(suite_ids, primitives)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)

    has_failures = any(item["status"] != "ok" for item in summary["findings"])  # type: ignore[index]
    if args.strict and has_failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
