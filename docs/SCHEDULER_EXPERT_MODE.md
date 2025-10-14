# Expert / Lookup Scheduler Guide

This note documents the UAV scheduler when it is run in expert-only (lookup)
mode.  The audience is the ground operator who chooses the policy before a
flight, as well as developers validating the control logic.

---

## Mode selection

* **Default (`--auto` or no flag):** the launcher inspects the requested mode.
  When unspecified, we initialise `UnifiedUAVScheduler` in
  `SchedulerMode.EXPERT_ONLY`, i.e. the rule-based policy documented below.
* **Explicit override:** the UI can expose a dropdown such as
  `["expert", "rl", "hybrid"]`.  Selecting “expert” forces
  `SchedulerMode.EXPERT_ONLY`.  Selecting another option is respected only when
  the user overrides the default.  If the front-end reverts to *auto* mid-run,
  the scheduler will remain in expert mode unless the operator explicitly
  switches to another mode.

Internally the scheduler keeps a `SchedulerContext` with the runtime role,
session ID, and the initial suite.  When expert mode is active we never invoke
the RL strategy and the hybrid fusion logic is bypassed.

---

## Inputs consumed by the expert policy

The expert policy operates on the consolidated `SystemTelemetry` structure that
arrives once per decision interval (default 1 s).  During
`UnifiedUAVScheduler.update_telemetry` we derive:

| Source | Example fields |
| ------ | -------------- |
| **Battery predictor** (`BatteryPrediction`) | SOC %, remaining Ah/time, discharge C-rate, voltage under load, temperature derating factor, `critical_warning` flag. |
| **Thermal guard** (`ThermalAnalysis`) | Thermal state (NORMAL → EMERGENCY), trend °C/s, predicted time to critical, throttling recommendation, headroom. |
| **Security advisor** (`SecurityPosture`) | Threat level, recommended PQC suite, DDOS tier (lightweight / heavyweight), throttling flag, emergency fallback flag. |
| **Network metrics** (`NetworkMetrics`) | Loss %, RTT average/p95, throughput/goodput, connection attempt rate (used to detect abuse). |
| **System telemetry** | CPU %, CPU frequency, memory %, optional heartbeat health (`heartbeat_ok`, `heartbeat_missed_count`). |

These artefacts are cached as `_last_*` attributes so the decision logic can
work against a consistent snapshot each cycle.

---

## Expert decision pipeline

The expert scheduler maps the latest state into a `SchedulerDecision` through a
series of guard rails.  The core is `_make_expert_decision` in
`src/scheduler/unified_scheduler.py`.

1. **Telemetry availability check.**  If the scheduler is still warming up and
   any of the three predictors have not produced output yet, fall back to a
   safe suite:
   * start from a middle-of-the-road suite (2nd in the sorted list);
   * escalate to the most secure suite if the current threat level is already
     CONFIRMED/CRITICAL;
   * downshift to the lowest-power suite if SOC < 20 %;
   * DDOS mode defaults to LIGHTWEIGHT unless threat is elevated.

2. **Battery binning and suite cap.**
   ```
   SOC ≤ 15 % or remaining time < 5 min  → cap index 0
   SOC ≤ 30 %                            → cap index 1
   SOC ≤ 55 %                            → cap index 2 (if ≥3 suites)
   ```
   The current cap is held in `max_index_allowed`.  Every time a tighter cap is
   applied we record the reason (`battery_critical`, `battery_low`, etc.) so the
   decision notes include full context.

3. **Thermal guard.**
   * EMERGENCY or CRITICAL → cap index 0.
   * ELEVATED → cap index 1; if the rise rate exceeds 0.5 °C/s or projected time
     to critical is < 180 s, tighten to cap 0.

4. **CPU and network guardrails.**
   * CPU ≥ 90 % → cap index 0.  CPU ≥ 80 % → cap index 1.
   * Severe congestion (loss > 12 %, RTT p95 > 400 ms, throughput < 1.5 Mbps)
     → cap index 0.  Moderate congestion → cap index 1.

5. **Heartbeat loss safeguard.**
   * If the passive heartbeat channel reports consecutive misses, treat that as
     a reachability issue.  Prefer the lightest suite unless the threat level is
     CONFIRMED/CRITICAL, in which case security wins.

6. **Threat-driven suite preference.**
   * If the security advisor provided a `pqc_suite` recommendation that lies
     within the capped index, use it.
   * Otherwise map threat level manually:
     ```
     NONE              → index min(1, max allowed)
     SUSPICIOUS        → index min(1, max allowed)
     CONFIRMED/CRITICAL → highest available index
     ```

7. **Dwell enforcement.**
   * Upgrades (higher suite index) require the last switch to be ≥ `_suite_dwell_ns`
     ago (8 s by default).  If not, stick with the current suite and flag
     `suite_dwell_blocked`.
   * DDOS mode downgrades obey `_ddos_dwell_ns` (6 s).  We only relax from
     HEAVYWEIGHT back to LIGHTWEIGHT or DISABLED after this interval.

8. **DDOS mode selection.**
   * Start from the `SecurityPosture` tier if available.
   * If the network metrics show abuse (loss > 15 % or connection attempts > 50/s)
     escalate to HEAVYWEIGHT regardless of posture (`ddos_reason=network_abuse`).
   * Otherwise use a threat-to-mode map:
     ```
     NONE/SUSPICIOUS → LIGHTWEIGHT
     CONFIRMED/CRITICAL → HEAVYWEIGHT
     ```
   * `ddos_dwell_blocked` is flagged if we intended to relax but the dwell timer
     hasn’t expired.

9. **Decision assembly.**
   The final `SchedulerDecision` contains:
   * `target_suite`: string ID of the chosen PQC suite.
   * `ddos_mode`: `DdosMode` enum (DISABLED / LIGHTWEIGHT / HEAVYWEIGHT).
   * `notes`: dictionary including
     - `strategy=expert`;
     - battery SOC and bin;
     - thermal state, CPU %, threat level;
     - the primary suite source (`security_posture`, `threat_high`, `heartbeat_missing_safe_mode`, etc.);
     - packet loss/RTT if available;
     - comma-separated constraint reasons (`battery_low,thermal_elevated`, …);
     - dwell flags when applicable.

Repeated decisions (same suite and DDOS mode) are suppressed unless the dwell
or constraint context changes, keeping the control channel quiet.

---

## Outputs observed by the operator

1. **Live logs** (`logs/auto/...`): every applied decision yields a JSON log line
   containing the suite, DDOS mode, decision latency, and notes.  These mirror
   the context above so one can audit why the scheduler held back an upgrade.
2. **`SchedulerState` telemetry**: exported via status APIs (control server) and
   saved at shutdown.  Includes `current_suite`, `battery_soc_percent`,
   `thermal_state`, `threat_level`, and `active_ddos_tier`.
3. **Dataset entry** (`summary.csv` / master dataset): the GCS scheduler records
   the chosen suite/ tier per window alongside power, latency, and blackout
   metrics, enabling post-flight analysis.

---

## Operator checklist

1. Before flight, select **Expert / Lookup** in the UI (or leave mode on auto).
2. Optionally review / tweak the constraint thresholds in
   `src/scheduler/unified_scheduler.py` if the airframe or pack has changed.
3. During the mission, monitor the expert scheduler indicators:
   * SOC and remaining time warnings,
   * thermal state transitions,
   * constraint reason histogram (steers battery vs thermal vs network limits).
4. After landing, pull the generated dataset to quantify how often each guardrail
   engaged and how the battery/thermal trajectories behaved under the chosen
   suites.

With this understanding, the expert scheduler can be confidently deployed as
the default policy, while still allowing RL or hybrid modes to be enabled on
demand by the operator before launch.
