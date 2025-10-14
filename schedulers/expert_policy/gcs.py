#!/usr/bin/env python3
"""GCS-side expert policy scheduler entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional

from core.config import CONFIG

from ..common.control_client import ControlClient
from ..common.state import SchedulerContext
from ..common.telemetry import TelemetrySubscriber
from .policy import ExpertPolicyStrategy, default_expert_config


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GCS expert policy scheduler")
    parser.add_argument("session", help="Scheduler session identifier")
    parser.add_argument("initial_suite", help="Bootstrap cryptographic suite")
    parser.add_argument(
        "--control-host",
        default=CONFIG.get("GCS_CONTROL_HOST", CONFIG.get("GCS_HOST", "127.0.0.1")),
        help="GCS follower control host",
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=int(CONFIG.get("GCS_CONTROL_PORT", CONFIG.get("DRONE_CONTROL_PORT", 48080))),
        help="GCS follower control port",
    )
    parser.add_argument(
        "--telemetry-host",
        default=CONFIG.get("GCS_TELEMETRY_BIND", "127.0.0.1"),
        help="Telemetry listener host",
    )
    parser.add_argument(
        "--telemetry-port",
        type=int,
        default=int(CONFIG.get("GCS_TELEMETRY_PORT", 52080)),
        help="Telemetry listener port",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Decision interval seconds",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=15.0,
        help="Telemetry aggregation window seconds",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=45.0,
        help="Default suite dwell duration seconds",
    )
    parser.add_argument(
        "--pre-gap",
        type=float,
        default=1.0,
        help="Gap before starting traffic / capture seconds",
    )
    parser.add_argument(
        "--power-capture",
        action="store_true",
        help="Request synchronized power capture per decision",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    _setup_logging(args.verbose)

    logging.info("Starting GCS expert policy scheduler session=%s", args.session)

    context = SchedulerContext(
        session_id=args.session,
        role="gcs",
        initial_suite=args.initial_suite,
    )
    strategy = ExpertPolicyStrategy(config=default_expert_config())
    strategy.warmup(context)

    telemetry = TelemetrySubscriber(
        host=args.telemetry_host,
        port=args.telemetry_port,
        session_id=args.session,
        buffer_seconds=max(args.window * 2.0, 30.0),
    )
    telemetry.start()

    control = ControlClient(host=args.control_host, port=args.control_port)
    logging.info("Connected control_host=%s control_port=%d", args.control_host, args.control_port)

    try:
        while True:
            window = telemetry.snapshots(window_seconds=args.window)
            snapshots = list(window.snapshots)
            if not snapshots:
                time.sleep(min(args.interval, 1.0))
                continue

            decision = strategy.decide(context=context, telemetry=window)
            if decision is None:
                time.sleep(args.interval)
                continue

            logging.info(
                "Applying decision suite=%s ddos=%s",
                decision.target_suite,
                decision.ddos_mode.value,
            )
            control.schedule_suite(
                suite_id=decision.target_suite,
                duration_s=args.duration,
                pre_gap_s=args.pre_gap,
                algorithm=strategy.name,
            )

            if args.power_capture:
                try:
                    control.request_power_capture(decision.target_suite, duration_s=args.duration)
                except Exception as exc:
                    logging.warning("Power capture request failed: %s", exc)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        logging.info("Scheduler interrupted, shutting down")
    finally:
        telemetry.stop()
        strategy.teardown(context)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
