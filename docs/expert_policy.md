Expert Scheduler Policy
=======================

This document explains the expert (rule-based) scheduler implemented in
`src/scheduler/unified_scheduler.py`. It describes the decision inputs, the
rule hierarchy, hysteresis/dwell behavior, DDOS tier mapping, and examples for
how the policy behaves under representative telemetry.

Overview
--------
The expert policy chooses two primary outputs each decision interval:
- `target_suite`: the PQC suite to run (from `available_suites` ordered by
  increasing computational cost/security), and
- `ddos_mode`: which DDOS detection tier to run (disabled/lightweight/heavyweight).

Decisions are based on several inputs (collected and summarized in the
scheduler):
- Battery prediction (`BatteryPrediction`) — SOC percent, remaining time,
  critical warning flag, and other battery metrics.
- Thermal analysis (`ThermalAnalysis`) — thermal state (NORMAL/ELEVATED/CRITICAL/EMERGENCY),
  temperature trend, time-to-critical, and throttling/emergency recommendations.
- Network metrics (`NetworkMetrics`) — packet loss, RTT P95, throughput, goodput,
  connection attempts, etc.
- Security posture (`SecurityPosture`) — per `SecurityAdvisor` mapping from
  DDOS predictions to recommended PQC suite, ddos detection tier and throttling.
- Telemetry fields like `cpu_percent` and mission context.

High-level rule ordering
------------------------
1. Emergency conditions (battery critical, thermal emergency, or CRITICAL threat)
   immediately activate emergency mode which forces the scheduler into a safe
   fallback (`SchedulerMode.EMERGENCY_SAFE`). In emergency mode the expert policy
   returns the lowest-power suite and disables heavy DDOS processing.

2. If not in emergency, the expert policy computes constraints and a desired
   suite index (position in `available_suites`) then caps that index downwards
   by the strictest constraint (battery/thermal/cpu/network). The final selected
   suite respects dwell (hysteresis) so upgrades to more expensive suites are
   delayed for a configurable dwell period while degradations are immediate.

Battery bins and behavior
-------------------------
The policy buckets battery into bins using the latest `BatteryPrediction`:
- "critical": SOC <= 15% or remaining_time < 5 minutes → cap to lowest-power
  suite (index 0) immediately.
- "low": SOC <= 30% → cap to low-power or second-lowest suite (index 1).
- "moderate": SOC <= 55% → allow mid-range suites (index 1..2 depending on
  available suites).
- "high": SOC > 55% → no battery cap applied by default.

Thermal guard integration
-------------------------
`ThermalGuard` provides a conservative analysis which the expert policy uses to
limit allowed suites:
- `EMERGENCY` or `CRITICAL` thermal states cap to the lowest-power suite.
- `ELEVATED` caps to a conservative suite (index 1). If the temperature
  trend is rising fast (trend > 0.5 °C/s) or time-to-critical is short (< 3
  minutes), the policy caps to the lowest-power suite.
- The policy also reads `throttling_recommended` and `emergency_shutdown` flags
  implicitly through the state mapping.

CPU and network guardrails
-------------------------
- CPU: If `cpu_percent` >= 90% → force lowest-power suite. If >= 80% → cap to
  moderate (index 1).
- Network: If packet loss > 12% or RTT P95 > 400ms or throughput < 1.5 Mbps →
  force lowest-power suite (because network is unusable). If packet loss > 6%
  or RTT P95 > 250ms → cap to a conservative suite (index 1).

Security / DDOS mapping
-----------------------
The `SecurityAdvisor` returns a `SecurityPosture` with a recommended `pqc_suite`
and `ddos_detection_tier`. The expert policy:
- Prefer the posture's recommended `pqc_suite` as the "desired" target (if
  available in `available_suites`).
- Map `SecurityPosture.ddos_detection_tier` to `DdosMode`:
  - `LIGHTWEIGHT` → `DdosMode.LIGHTWEIGHT`
  - `HEAVYWEIGHT` → `DdosMode.HEAVYWEIGHT`
  - Under low/no threat and no traffic throttling -> `DdosMode.DISABLED` is
    used to minimize CPU when safe.
- Independently, severe network abuse (packet loss > 15% or excessive
  connection attempts) upgrades the ddos mode to HEAVYWEIGHT for confirmation.

Hysteresis (dwell) policy
-------------------------
Two dwell timers prevent oscillation:
- `_suite_dwell_ns` (default 8s) delays upgrades to a *more* expensive suite
  for the dwell period after the last suite change. Downgrades (to safer
  suites) happen immediately.
- `_ddos_dwell_ns` (default 6s) prevents relaxing an aggressive DDOS tier too
  quickly. Switching to heavier ddos modes can be immediate; switching down is
  subject to the dwell timer.

Decision notes and observability
-------------------------------
Every `SchedulerDecision` returned by the expert policy includes a `notes`
map with fields that explain the decision: battery_soc, battery_bin,
thermal_state, cpu_pct, threat_level, suite_source, ddos_reason, constraints,
and flags indicating if dwell prevented an upgrade.

Implementation details (code references)
----------------------------------------
- Telemetry persistence: `update_telemetry()` stores the last artifacts used by
  the expert policy: `_last_battery_prediction`, `_last_thermal_analysis`,
  `_last_network_metrics`, `_last_security_posture`, `_last_telemetry`.
- Battery/thermal caps are applied via `apply_cap(new_cap, reason)` which
  reduces `max_index_allowed` and records `constraint_reasons`.
- Desired suite index is computed from `SecurityPosture.pqc_suite` (if
  available) or from `ThreatLevel` fallback mapping.
- DDoS mapping uses `SecurityPosture.ddos_detection_tier` and enforces heavy
  mode on strong network abuse indicators.
- Dwell enforcement: if the target index is greater than the current index
  (upgrade) and the elapsed time since `_last_suite_change_ns` is less than
  `_suite_dwell_ns`, the upgrade is blocked and `suite_dwell_blocked` is set in
  the notes. The same pattern applies to DDOS tier relaxations and
  `_last_ddos_change_ns` / `_ddos_dwell_ns`.
- When a suite or ddos switch is applied successfully, `_apply_decision()` now
  updates `_last_suite_change_ns` and `_last_ddos_change_ns` so dwell timers
  can be enforced.

Examples
--------
1) Battery-critical + normal thermal/network:
   - battery SOC = 12% → battery_bin `critical` → cap to index 0
   - Result: `target_suite = available_suites[0]`, `ddos_mode = lightweight`
     (unless threat is high). Notes include `constraints=battery_critical`.

2) Elevated temperature with rapid rise:
   - thermal.state = ELEVATED, trend = 1.2 °C/s, time_to_critical = 120s
   - Policy applies `thermal_trend` cap to index 0 (immediate conservatism)
   - Result: switch to lowest-power suite and `throttling_recommended` noted.

3) Suspicious DDOS but sufficient battery/thermal:
   - Security advisor suggests high-security suite (index N) and
     `ddos_detection_tier=HEAVYWEIGHT`.
   - Battery and thermal caps keep `max_index_allowed` at a mid index → final
     target is capped but `ddos_mode=HEAVYWEIGHT` to confirm attack.

4) Network congested but CPU idle:
   - packet_loss = 18% and throughput = 0.8 Mbps → `network_congested`
   - Cap to index 0 and upgrade ddos to HEAVYWEIGHT.

How to test / validate
-----------------------
- Unit/smoke tests should instantiate `UnifiedUAVScheduler`, call
  `update_telemetry()` with synthetic `SystemTelemetry` samples representing
  each case above, then call `_make_expert_decision()` to assert expected
  `target_suite` and `ddos_mode`.
- Replay recorded telemetry (e.g., `logs/`) through the scheduler and inspect
  `docs/logging/scheduler/*.log` or `SchedulerDecision` callbacks for `notes`.

Next steps (recommended)
------------------------
- Surface tunable parameters as configuration options (battery bins, dwell
  durations, network thresholds) so they can be swept in experiments.
- Add unit tests and a small simulator harness (`tests/test_expert_policy.py`) to
  cover the main branches (battery critical, thermal emergency, CPU saturation,
  DDOS suspicious/confirmed).
- Collect empirical thermal and power data per-suite and use it to replace the
  current heuristic thermal mapping with measured values.


Changelog
---------
- Document created to explain the rule-based expert policy implemented in
  `src/scheduler/unified_scheduler.py` (battery/thermal/cpu/network/ddos
  integration, dwell/hysteresis behavior, and notes fields).
