"""Utility helpers for reading proxy and traffic counter artifacts.

These helpers keep the orchestration scripts decoupled from the exact JSON
structure emitted by the proxies and traffic generators.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ProxyCounters:
    """Parsed counters emitted by ``core.run_proxy``.

    Attributes
    ----------
    role:
        ``"gcs"`` or ``"drone"`` as recorded in the JSON payload.
    suite:
        Canonical suite identifier associated with the run.
    counters:
        Raw counter dictionary from the JSON payload.
    ts_stop_ns:
        Optional timestamp (nanoseconds) indicating when the proxy shut down.
    path:
        Filesystem location from which the payload was loaded.
    """

    role: str
    suite: str
    counters: Dict[str, Any]
    ts_stop_ns: Optional[int] = None
    path: Optional[Path] = None

    @property
    def rekeys_ok(self) -> int:
        """Return the number of successful rekeys recorded by the proxy."""

        return int(self.counters.get("rekeys_ok", 0))

    @property
    def rekeys_fail(self) -> int:
        """Return the number of failed rekeys recorded by the proxy."""

        return int(self.counters.get("rekeys_fail", 0))

    @property
    def last_rekey_suite(self) -> Optional[str]:
        """Return the last suite identifier applied during rekey, if any."""

        last_suite = self.counters.get("last_rekey_suite")
        if isinstance(last_suite, str) and last_suite:
            return last_suite
        return None

    def ensure_rekey(self, expected_suite: str) -> None:
        """Validate that at least one rekey succeeded to ``expected_suite``.

        Raises
        ------
        ValueError
            If no successful rekey occurred or the final suite does not match
            ``expected_suite``.
        """

        if self.rekeys_ok < 1:
            raise ValueError(
                f"Proxy {self.role} reported no successful rekeys (path={self.path})"
            )
        final_suite = self.last_rekey_suite
        if final_suite != expected_suite:
            raise ValueError(
                f"Proxy {self.role} last_rekey_suite={final_suite!r} does not match "
                f"expected {expected_suite!r}"
            )

    @property
    def handshake_metrics(self) -> Dict[str, Any]:
        """Return recorded handshake metrics if available."""

        payload = self.counters.get("handshake_metrics")
        return payload if isinstance(payload, dict) else {}

    @property
    def part_b_metrics(self) -> Dict[str, Any]:
        """Return flattened Part B primitive metrics if present."""

        payload = self.counters.get("part_b_metrics")
        if isinstance(payload, dict):
            return payload

        extracted: Dict[str, Any] = {}
        for key in (
            "kem_keygen_max_ms",
            "kem_keygen_avg_ms",
            "kem_keygen_ms",
            "kem_encaps_max_ms",
            "kem_encaps_avg_ms",
            "kem_encaps_ms",
            "kem_decaps_max_ms",
            "kem_decaps_avg_ms",
            "kem_decap_ms",
            "sig_sign_max_ms",
            "sig_sign_avg_ms",
            "sig_sign_ms",
            "sig_verify_max_ms",
            "sig_verify_avg_ms",
            "sig_verify_ms",
            "aead_encrypt_avg_ms",
            "aead_decrypt_avg_ms",
            "aead_encrypt_ms",
            "aead_decrypt_ms",
            "pub_key_size_bytes",
            "ciphertext_size_bytes",
            "sig_size_bytes",
            "shared_secret_size_bytes",
            "primitive_total_ms",
            "rekey_ms",
            "kem_keygen_mJ",
            "kem_encaps_mJ",
            "kem_decap_mJ",
            "sig_sign_mJ",
            "sig_verify_mJ",
        ):
            value = self.counters.get(key)
            if value is not None:
                extracted[key] = value

        return extracted

    @property
    def primitive_metrics(self) -> Dict[str, Dict[str, int]]:
        """Return primitive timing/size metrics recorded by the proxy."""

        payload = self.counters.get("primitive_metrics")
        if not isinstance(payload, dict):
            return {}

        sanitized: Dict[str, Dict[str, int]] = {}
        for name, stats in payload.items():
            if not isinstance(name, str) or not isinstance(stats, dict):
                continue
            count = int(stats.get("count", 0) or 0)
            total_ns = int(stats.get("total_ns", 0) or 0)
            min_ns_raw = stats.get("min_ns")
            try:
                min_ns = int(min_ns_raw) if min_ns_raw not in (None, "") else 0
            except (TypeError, ValueError):
                min_ns = 0
            max_ns = int(stats.get("max_ns", 0) or 0)
            total_in = int(stats.get("total_in_bytes", 0) or 0)
            total_out = int(stats.get("total_out_bytes", 0) or 0)
            sanitized[name] = {
                "count": count,
                "total_ns": total_ns,
                "min_ns": min_ns,
                "max_ns": max_ns,
                "total_in_bytes": total_in,
                "total_out_bytes": total_out,
            }
        return sanitized

    def primitive_average_ns(self, name: str) -> Optional[int]:
        """Return average duration in nanoseconds for primitive ``name`` if present."""

        stats = self.primitive_metrics.get(name)
        if not stats:
            return None
        count = stats.get("count", 0)
        if count <= 0:
            return None
        total_ns = stats.get("total_ns", 0)
        return int(total_ns) // int(count)

    def get_part_b_metric(self, key: str, default: Optional[float] = None) -> Optional[float]:
        """Convenience accessor for flattened Part B metrics as floats."""

        value = self.part_b_metrics.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


@dataclass(frozen=True)
class TrafficSummary:
    """Counters emitted by ``tools/traffic_*.py``."""

    role: str
    peer_role: Optional[str]
    sent_total: int
    recv_total: int
    tx_bytes_total: int
    rx_bytes_total: int
    first_send_ts: Optional[str]
    last_send_ts: Optional[str]
    first_recv_ts: Optional[str]
    last_recv_ts: Optional[str]
    out_of_order: int
    unique_senders: int
    path: Optional[Path] = None


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Counter file not found: {path}")
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive logging only
        raise ValueError(f"Failed to parse JSON from {path}: {exc}") from exc


def load_proxy_counters(path: Path | str) -> ProxyCounters:
    """Load proxy counters JSON from ``path``.

    Parameters
    ----------
    path:
        Filesystem path to the JSON payload created by ``--json-out``.

    Returns
    -------
    ProxyCounters
        Dataclass encapsulating the parsed counters.
    """

    target = Path(path)
    payload = _load_json(target)

    role = payload.get("role")
    suite = payload.get("suite")
    counters = payload.get("counters")

    if not isinstance(role, str) or not isinstance(suite, str) or not isinstance(counters, dict):
        raise ValueError(f"Invalid proxy counters JSON schema in {target}")

    ts_stop_ns = payload.get("ts_stop_ns")
    if ts_stop_ns is not None:
        try:
            ts_stop_ns = int(ts_stop_ns)
        except (TypeError, ValueError):
            ts_stop_ns = None

    return ProxyCounters(
        role=role,
        suite=suite,
        counters=counters,
        ts_stop_ns=ts_stop_ns,
        path=target,
    )


def load_traffic_summary(path: Path | str) -> TrafficSummary:
    """Load traffic generator summary JSON.

    Parameters
    ----------
    path:
        Path to the file created via ``--summary``.
    """

    target = Path(path)
    payload = _load_json(target)

    role = payload.get("role")
    peer_role = payload.get("peer_role")

    required_int_fields = {
        "sent_total": int,
        "recv_total": int,
        "tx_bytes_total": int,
        "rx_bytes_total": int,
        "out_of_order": int,
    }

    counters: Dict[str, int] = {}
    for field, field_type in required_int_fields.items():
        value = payload.get(field)
        if not isinstance(value, field_type):
            raise ValueError(f"Summary field {field} missing or wrong type in {target}")
        counters[field] = int(value)

    unique_senders_raw = payload.get("unique_senders")
    unique_senders = int(unique_senders_raw) if unique_senders_raw is not None else 0

    if not isinstance(role, str):
        raise ValueError(f"Summary missing role field in {target}")

    return TrafficSummary(
        role=role,
        peer_role=peer_role if isinstance(peer_role, str) else None,
        sent_total=counters["sent_total"],
        recv_total=counters["recv_total"],
        tx_bytes_total=counters["tx_bytes_total"],
        rx_bytes_total=counters["rx_bytes_total"],
        first_send_ts=_opt_str(payload.get("first_send_ts")),
        last_send_ts=_opt_str(payload.get("last_send_ts")),
        first_recv_ts=_opt_str(payload.get("first_recv_ts")),
        last_recv_ts=_opt_str(payload.get("last_recv_ts")),
        out_of_order=counters["out_of_order"],
        unique_senders=unique_senders,
        path=target,
    )


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


__all__ = [
    "ProxyCounters",
    "TrafficSummary",
    "load_proxy_counters",
    "load_traffic_summary",
]
