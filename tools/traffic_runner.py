"""Shared runner for automated plaintext traffic generators."""
from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from tools.traffic_common import (
    TokenBucket,
    configured_selector,
    load_ports_and_hosts,
    ndjson_logger,
    open_udp_socket,
)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"traffic_{role}",
        description="Generate UDP plaintext traffic for the PQC proxy.",
    )
    parser.add_argument("--count", type=int, default=200, help="Total messages to send (default: 200)")
    parser.add_argument("--rate", type=float, default=50.0, help="Maximum send rate in packets/sec (default: 50)")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Optional duration cap in seconds. When omitted, exits after sending all messages and an idle grace period.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Path for NDJSON event log")
    parser.add_argument("--summary", type=Path, default=None, help="Path for JSON summary output")
    parser.add_argument("--peer-hint", type=str, default=None, help="Annotate payloads with expected peer role")
    parser.add_argument(
        "--payload-bytes",
        type=int,
        default=0,
        help="Optional number of '.' bytes appended to each payload for throughput testing.",
    )
    return parser


def _default_paths(role: str) -> Dict[str, Path]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    logs_dir = Path("logs")
    return {
        "out": logs_dir / f"{role}_traffic_{ts}.jsonl",
        "summary": logs_dir / f"{role}_traffic_summary_{ts}.json",
    }


def run(role: str, argv: Optional[list[str]] = None) -> int:
    parser = _build_parser(role)
    args = parser.parse_args(argv)

    defaults = _default_paths(role)
    out_path: Path = args.out or defaults["out"]
    summary_path: Path = args.summary or defaults["summary"]

    settings = load_ports_and_hosts(role)  # type: ignore[arg-type]
    rx_host, rx_port = settings["rx_bind"]  # type: ignore[index]
    rx_sock = open_udp_socket((rx_host, rx_port))
    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    selector = configured_selector(rx_sock)
    bucket = TokenBucket(args.rate)

    log_event, close_log = ndjson_logger(out_path)

    payload_pad = b"." * max(args.payload_bytes, 0)

    start = time.monotonic()
    deadline = start + args.duration if args.duration else None
    last_activity = start
    idle_grace = 1.0

    counters: Dict[str, Optional[object]] = {
        "role": role,
        "peer_role": settings["peer_role"],
        "sent_total": 0,
        "recv_total": 0,
        "first_send_ts": None,
        "last_send_ts": None,
        "first_recv_ts": None,
        "last_recv_ts": None,
        "out_of_order": 0,
        "unique_senders": 0,
        "rx_bytes_total": 0,
        "tx_bytes_total": 0,
    }

    expected_seq: Dict[str, int] = {}
    unique_senders = set()

    seq = 0
    send_done = False

    tx_addr = settings["tx_addr"]  # type: ignore[assignment]

    try:
        while True:
            now = time.monotonic()
            if deadline and now >= deadline:
                break

            if not send_done:
                if seq >= args.count:
                    send_done = True
                else:
                    if bucket.consume(now):
                        seq += 1
                        payload = {
                            "role": role,
                            "seq": seq,
                            "t_send_ns": time.monotonic_ns(),
                        }
                        if args.peer_hint:
                            payload["peer_hint"] = args.peer_hint
                        packet = json.dumps(payload, separators=(",", ":")).encode("utf-8") + payload_pad
                        sent_bytes = tx_sock.sendto(packet, tx_addr)
                        counters["sent_total"] = int(counters["sent_total"]) + 1  # type: ignore[arg-type]
                        counters["tx_bytes_total"] = int(counters["tx_bytes_total"]) + sent_bytes  # type: ignore[arg-type]
                        iso_ts = iso_now()
                        counters["last_send_ts"] = iso_ts
                        if counters["first_send_ts"] is None:
                            counters["first_send_ts"] = iso_ts
                        log_event({"event": "send", "seq": seq, "bytes": sent_bytes})
                        last_activity = now

            timeout = 0.05
            if deadline:
                timeout = max(0.0, min(timeout, deadline - now))

            events = selector.select(timeout)
            if events:
                for _key, _mask in events:
                    try:
                        data, addr = rx_sock.recvfrom(4096)
                    except BlockingIOError:
                        continue
                    now = time.monotonic()
                    last_activity = now
                    counters["recv_total"] = int(counters["recv_total"]) + 1  # type: ignore[arg-type]
                    counters["rx_bytes_total"] = int(counters["rx_bytes_total"]) + len(data)  # type: ignore[arg-type]
                    iso_ts = iso_now()
                    counters["last_recv_ts"] = iso_ts
                    if counters["first_recv_ts"] is None:
                        counters["first_recv_ts"] = iso_ts

                    sender_label = f"{addr[0]}:{addr[1]}"
                    try:
                        message = json.loads(data.decode("utf-8"))
                        sender_label = message.get("role", sender_label)
                        seq_val = message.get("seq")
                        if isinstance(seq_val, int):
                            expected = expected_seq.get(sender_label)
                            if expected is None:
                                expected_seq[sender_label] = seq_val + 1
                            else:
                                if seq_val != expected:
                                    counters["out_of_order"] = int(counters["out_of_order"]) + abs(seq_val - expected)  # type: ignore[arg-type]
                                expected_seq[sender_label] = seq_val + 1
                    except (ValueError, UnicodeDecodeError):
                        message = None

                    unique_senders.add(sender_label)
                    log_payload: Dict[str, object] = {
                        "event": "recv",
                        "bytes": len(data),
                        "from": f"{addr[0]}:{addr[1]}",
                        "sender": sender_label,
                    }
                    if isinstance(message, dict) and "seq" in message:
                        log_payload["seq"] = message["seq"]
                    log_event(log_payload)

            if send_done and not events and not deadline:
                if now - last_activity >= idle_grace:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        selector.close()
        rx_sock.close()
        tx_sock.close()
        close_log()

    counters["unique_senders"] = len(unique_senders)

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(counters, indent=2), encoding="utf-8")
    return 0
*** End of File***