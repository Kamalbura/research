#!/usr/bin/env python3
"""Audit repository files for hard-coded network endpoints.

This script scans Python (and optional shell/Lua) files for IPv4 literals
and socket usage that should instead reference core.config.CONFIG.
It emits a JSON report of violations and exits non-zero if any are found.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parents[1]
ALLOW_IPS = {"127.0.0.1", "0.0.0.0", "::1"}
CODE_DIRS = ("core", "tools", "drone", "gcs")
IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
EXCLUDE_PARTS = {"docs", "logs", "__pycache__"}

Violation = dict[str, object]


def iter_files() -> Iterable[Path]:
    for directory in CODE_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in EXCLUDE_PARTS for part in path.parts):
                continue
            yield path


def flag(violations: List[Violation], path: Path, lineno: int, kind: str, detail: str, suggestion: str | None = None) -> None:
    rel = str(path.relative_to(ROOT))
    violations.append(
        {
            "file": rel,
            "line": lineno,
            "kind": kind,
            "detail": detail,
            "suggestion": suggestion or "",
        }
    )


def scan_file(path: Path, violations: List[Violation]) -> None:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return

    # Regex sweep for IPv4 literals
    for lineno, line in enumerate(source.splitlines(), start=1):
        for match in IPV4_RE.finditer(line):
            ip = match.group(0)
            if ip not in ALLOW_IPS:
                flag(
                    violations,
                    path,
                    lineno,
                    "ipv4-literal",
                    f"Found IPv4 literal '{ip}'",
                    "Use CONFIG['GCS_HOST'/'DRONE_HOST'] or accept a parameter",
                )

    # AST analysis for socket invocations with literal endpoints
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return

    def is_literal_str(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    def is_literal_int(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, int)

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            attr = getattr(node.func, "attr", None)
            if attr in {"bind", "connect"} and node.args:
                target = node.args[0]
                if isinstance(target, ast.Tuple) and len(target.elts) >= 2:
                    host, port = target.elts[0], target.elts[1]
                    if is_literal_str(host) and IPV4_RE.fullmatch(host.value or "") and host.value not in ALLOW_IPS:
                        flag(
                            violations,
                            path,
                            node.lineno,
                            f"{attr}-literal-host",
                            f"socket.{attr} uses literal host '{host.value}'",
                            "Replace with CONFIG['GCS_HOST'/'DRONE_HOST']",
                        )
                    if is_literal_int(port):
                        flag(
                            violations,
                            path,
                            node.lineno,
                            f"{attr}-literal-port",
                            f"socket.{attr} uses literal port {port.value}",
                            "Use CONFIG[...] for ports or pass via args",
                        )
            elif attr == "sendto" and len(node.args) >= 2:
                destination = node.args[1]
                if isinstance(destination, ast.Tuple) and len(destination.elts) >= 2:
                    host, port = destination.elts[0], destination.elts[1]
                    if is_literal_str(host) and IPV4_RE.fullmatch(host.value or "") and host.value not in ALLOW_IPS:
                        flag(
                            violations,
                            path,
                            node.lineno,
                            "sendto-literal-host",
                            f"socket.sendto uses literal host '{host.value}'",
                            "Replace with CONFIG['GCS_HOST'/'DRONE_HOST']",
                        )
                    if is_literal_int(port):
                        flag(
                            violations,
                            path,
                            node.lineno,
                            "sendto-literal-port",
                            f"socket.sendto uses literal port {port.value}",
                            "Use CONFIG[...] for ports",
                        )
            self.generic_visit(node)

    Visitor().visit(tree)


def main() -> int:
    violations: List[Violation] = []
    for path in iter_files():
        scan_file(path, violations)

    print(json.dumps({"violations": violations}, indent=2))
    if violations:
        print(f"\nFound {len(violations)} endpoint violations.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
