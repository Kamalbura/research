"""Capability negotiator helpers for querying follower capabilities and filtering suites.

This module provides:
- request_capabilities(host, port) -> dict
- filter_suites_for_follower(suites, capabilities) -> (filtered_suites, skips)

It uses plain TCP JSON RPC compatible with the follower control server.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Dict, Iterable, List, Tuple


def _rpc(host: str, port: int, payload: dict, timeout: float = 1.5) -> dict:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = json.dumps(payload) + "\n"
            sock.sendall(data.encode())
            # Read single-line response
            resp = b""
            with sock.makefile("rb") as fh:
                line = fh.readline()
                if not line:
                    return {}
                return json.loads(line.decode())
    except Exception:
        return {}


def request_capabilities(host: str, port: int, timeout: float = 1.5) -> dict:
    resp = _rpc(host, port, {"cmd": "capabilities"}, timeout=timeout)
    if isinstance(resp, dict) and resp.get("ok"):
        return resp.get("capabilities") or {}
    return {}


def filter_suites_for_follower(suites: Iterable[str], capabilities: dict) -> Tuple[List[str], List[dict]]:
    """Return (filtered_suites, skips)

    skips is a list of dicts {suite: ..., reason: ...}
    """
    supported = set()
    raw_supported = capabilities.get("supported_suites")
    if isinstance(raw_supported, (list, tuple, set)):
        supported = {str(x) for x in raw_supported}

    skips = []
    out = []
    for suite in suites:
        if supported and suite not in supported:
            skips.append({"suite": suite, "reason": "not_supported_by_follower"})
            continue
        # Additional checks: KEMs, signatures, aead tokens can be enforced by inspecting suite registry
        out.append(suite)

    return out, skips
