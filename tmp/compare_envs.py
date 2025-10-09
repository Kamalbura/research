from __future__ import annotations
from pathlib import Path
import json


def load_env(path: str) -> dict[str, tuple[str, str]]:
    pkgs: dict[str, tuple[str, str]] = {}
    for line in Path(path).read_text(encoding="utf-16").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("=")
        if len(parts) >= 3:
            name = parts[0].lower()
            version = parts[1]
            build = "=".join(parts[2:])
            pkgs[name] = (version, build)
    return pkgs


oqs = load_env("tmp/oqs-dev-conda-list-current.txt")
gcs = load_env("tmp/gcs-env-conda-list-current.txt")

oqs_only = sorted(set(oqs) - set(gcs))
gcs_only = sorted(set(gcs) - set(oqs))
version_mismatch = {
    name: {"oqs_dev": oqs[name], "gcs_env": gcs[name]}
    for name in sorted(oqs)
    if name in gcs and oqs[name] != gcs[name]
}

summary_lines = [
    f"oqs_dev_pkg_count: {len(oqs)}",
    f"gcs_env_pkg_count: {len(gcs)}",
    "oqs_dev_only ({}): {}".format(
        len(oqs_only), ", ".join(oqs_only[:40]) + (" ..." if len(oqs_only) > 40 else "")
    ),
    "gcs_env_only ({}): {}".format(
        len(gcs_only), ", ".join(gcs_only[:40]) + (" ..." if len(gcs_only) > 40 else "")
    ),
    f"version_mismatches: {len(version_mismatch)}",
]

Path("tmp/conda-env-diff-summary.txt").write_text("\n".join(summary_lines) + "\n")
Path("tmp/conda-env-version-mismatches.json").write_text(
    json.dumps(version_mismatch, indent=2)
)
