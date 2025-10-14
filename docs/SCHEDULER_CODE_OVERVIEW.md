# Scheduler Code Reference (`src/scheduler`)

This note walks through the major modules that live under `src/scheduler/`, with
an emphasis on the battery modelling path and the way the unified scheduler
pulls every component together.  The goal is to make it easy to validate the
implementation and extend it for the paper-ready experiments.

---

## Components

### `components/battery_predictor.py`

**Purpose.**  Maintain a physics-informed, temperature-aware estimate of the
pack’s State of Charge (SOC) plus short-term power trends.

**Key data classes**

| Class | Role |
| ----- | ---- |
| `BatterySpec` | Static Li‑Po characteristics: nominal capacity and cell count, Peukert exponent, internal resistance, temperature coefficient, cutoff per cell.  Provides helpers for total pack voltage limits. |
| `BatteryState` | One sensor sample (voltage, current, temperature, power).  Power defaults to `V × I` if not supplied. |
| `BatteryPrediction` | Output bundle: SOC, remaining Ah/time, temperature-derated capacity, voltage under load, discharge rate (C), health score placeholder, and flags (critical warning, temperature derating factor). |

**`BatteryPredictor` workflow**

1. **Initialisation** (`__init__`):
   - Stores the pack spec, keeps a deque (`state_history`) for time-series
     analysis, tracks coulomb counting state and a simple ageing factor.
2. **`update(state)`**:
   - Appends the new `BatteryState`, prunes history older than
     `history_window_s`.
   - Integrates current draw via `_update_coulomb_counting` (trapezoidal,
     discharge-only, ignores gaps >1 h).
   - Computes a temperature derating factor (`_temperature_compensation_factor`)
     and effective capacity (derated × age).
   - Applies Peukert’s law (`_apply_peukert_equation`) to adjust usable capacity
     based on the instantaneous C-rate.
   - Converts voltage to SOC with a piecewise Li‑Po curve (`_voltage_to_soc`).
     Weighted blend with coulomb-counting SOC (0.3/0.7 while discharging,
     0.8/0.2 at rest).
   - Derives remaining Ah and time to cutoff, predicts voltage sag via internal
     resistance, and fills out `BatteryPrediction`.
   - Marks `critical_warning` if SOC ≤ critical threshold (`15 %` by default) or
     remaining time < 5 min.
3. **Trend helpers**:
   - `get_power_trend_analysis(window_s)` summarises power average, peak, and
     slope from recent history.
   - `predict_mission_viability(target_duration, expected_avg_power)` runs a
     what-if using Peukert + temperature derating and reports whether the pack
     can survive the mission.

### `components/thermal_guard.py`

**Purpose.**  Watch CPU/GPU/ambient/PMIC temperatures and advise throttling.

- Maintains a deque of `TemperatureSample` points and computes gradients over
  `trend_window_s` seconds.
- Determines `ThermalState` (NORMAL → EMERGENCY) with hysteresis and
  `rapid_rise` detection.
- Predicts time to critical temperature (`_predict_time_to_critical`) using a
  first-order thermal model.
- `ThermalAnalysis` bundles the state, rise rate, headroom, recommended action,
  throttling flag, shutdown flag, and a confidence score (derived from sample
  stability/regularity).
- `get_suite_thermal_mapping()` provides empirical expectations for each PQC
  suite (steady/peak temp rise, power).
- `recommend_optimal_suite()` picks the coolest viable suite from a candidate
  list given the current temperature and a desired safety margin.

### `components/security_advisor.py`

**Purpose.**  Connect network metrics to DDOS threat levels, detection tier
choices, and PQC suite recommendations.

- `NetworkMetrics` captures packet loss, RTT, throughput/goodput, jitter, etc.
- `DDOSPrediction` now **includes `timestamp_ns`** (updated during the review),
  threat level, confidence, tier, feature list, latency, per-feature anomaly
  scores, and raw model output.
- `SecurityAdvisor.analyze_threat(metrics, lightweight_score, heavyweight_score)`
  logic:
  1. Store metrics history, prune entries older than 10 min.
  2. Use heavyweight score if provided (Transformer/TST confirmation) else
     lightweight (XGBoost); fall back to heuristics.
  3. Update temporal threat state – suspicious activity escalates to confirmed if
     it persists longer than `escalation_window_s`.
  4. Build a `SecurityPosture`: PQC suite mapping, detection tier, throttling
     decision, alert cadence, emergency fallback flag, reasoning text.
- Heartbeat path: `generate_lightweight_alert` emits a pre-hashed alert packet
  every `alert_frequency_s`, piggybacking on MQTT-inspired behaviour so the GCS
  sees liveness even under congestion.

### `components/ipc_bridge.py`

**Purpose.**  Abstract fast switching between PQC/ DDOS algorithms via POSIX
shared memory or threading fallbacks.

- Supports modes: POSIX shared memory (`posix_ipc`), mmap-backed files, or
  plain threading if POSIX IPC is unavailable.
- `register_algorithm` stores an `AlgorithmConfig` (bytes payload, size, warmup
  metadata).  Optional warmup callbacks are scheduled for the background thread.
- `switch_algorithm` acquires the semaphore/lock, prefers warm pool hits (fast
  path), records stats (`IPCStats`), and toggles `active` flags.
- Warm pool manager periodically warms recently-used algorithms so a future
  switch is a cache hit.
- `create_pqc_suite_bridge` and `create_ddos_model_bridge` convenience helpers
  pre-register suites/models with sensible pool sizes.

---

## Unified Scheduler (`unified_scheduler.py`)

**Imports.**  Wires together the components above plus the expert/RL strategies
from `schedulers/nextgen_*`.

**State**

- `available_suites`: sorted PQC suite list.
- `current_state`: `SchedulerState` tracks mode (expert / RL / hybrid /
  emergency), active suite, active DDOS tier string, SOC, thermal state, threat
  level, timestamps, performance score, and the emergency flag.
- `metrics`: `SchedulerMetrics` accumulates decision cadence, latency, switch
  counts, and warning counters.
- `_rl_snapshots`: `deque` of `SuiteTelemetry` feeding the RL path (added during
  the review).  Max length = 6 ≈ 3 decision windows when running at 1 Hz.
- `_last_*` caches: last predictions/analyses to avoid recomputation.
- Dwell timers: `self._suite_dwell_ns` (default 8 s) prevents immediate
  upgrades; `self._ddos_dwell_ns` (6 s) prevents rapid relaxation of detection
  tiers.

**Lifecycle**

1. **Initialisation**: instantiate predictors/guards/advisors/bridges, warm up
   `NextGenExpertStrategy` and `NextGenRlStrategy` with a `SchedulerContext`.
   Logs initial configuration.
2. **`start()` / `stop()`**: spin up / tear down the main scheduler thread and
   clean IPC resources.
3. **`update_telemetry(SystemTelemetry)`**:
   - Feeds the battery predictor, thermal guard, and security advisor.
   - Computes a lightweight DDOS score from raw metrics (`_calculate_lightweight_ddos_score`).
   - Caches their outputs and increments warning counters.
   - Enters / exits emergency mode based on critical battery / thermal /
     threat triggers.
   - Appends a snapshot for the RL path via `_record_rl_snapshot`.
4. **Main loop** (`_scheduler_loop`):
   - Calls `_make_scheduling_decision`.
   - Applies the decision (`_apply_decision`).
   - Executes callbacks and updates performance metrics.
   - Sleeps for `decision_interval_s` (default 1 s).

**Decision logic**

| Method | Role |
| ------ | ---- |
| `_make_scheduling_decision` | Dispatch based on current scheduler mode. |
| `_make_hybrid_decision` | Compare expert vs. RL.  If both propose the same suite, take RL.  If disagreement, prefer expert when SOC is low, thermal state is CRITICAL/EMERGENCY, or threat ≥ CONFIRMED.  Otherwise take RL if confidence > 0.75; else expert. |
| `_make_expert_decision` | Implements the rule-based policy: derive suite caps from battery bins (≤15 % → cap index 0, ≤30 % → cap 1, ≤55 % → cap 2) and thermal/network constraints.  Heartbeat loss pushes the scheduler toward the lightest suite unless threat is elevated.  Chooses DDOS mode based on `SecurityPosture`, network abuse, and dwell timers. |
| `_record_rl_snapshot` | Packages the latest telemetry, battery and thermal stats, plus threat flag into a `SuiteTelemetry` snapshot for the RL strategy.  Includes counters such as `thermal_trend_c_per_s` and `battery_remaining_s`. |
| `_make_rl_decision` | **Implemented during the review.**  Builds a `TelemetryWindow` from `_rl_snapshots`, calls `NextGenRlStrategy.decide`, tags notes with `"strategy": "rl"`, and returns a `SchedulerDecision`.  If the RL strategy abstains or repeats the last action, returns `None`. |
| `_make_emergency_decision` | Forces the lightest suite and disables DDOS detection. |
| `_apply_decision` | Switch PQC suite via `pqc_bridge`, switch DDOS tier via `ddos_bridge`, update dwell timers, and refresh `context.initial_suite` (so the strategies track the new baseline). |
| `_activate_emergency_mode` / `_deactivate_emergency_mode` | Flip state mode and log transitions. |
| `_update_performance_metrics` | Update decisions-per-minute, average latency, and IPC cache stats for logging/monitoring. |

---

## Strategies (`src/scheduler/strategies`)

- `base.py`: lightweight protocol (`Strategy` + `StrategyContext`) that mirrors
  the original `schedulers` package to keep imports simple.
- `expert.py` / `rl.py`: adapters that lazily import `NextGenExpertStrategy` /
  `NextGenRlStrategy` from `schedulers/nextgen_*`, handling missing optional
  dependencies gracefully.  Return normalised dictionaries (`target_suite`,
  `ddos_mode`, `notes`).  Warmups construct `SchedulerContext` objects for the
  underlying implementations.
- `hybrid.py`: combination strategy that chooses RL decision if confidence ≥ 0.75
  and available; otherwise fall back to expert output.  Acts as a simple fusion
  wrapper used before the detailed hybrid logic at `unified_scheduler`.

---

## How to Extend / Validate the Battery Pipeline

1. **Calibration**: Inject real INA219 logs into `BatteryPredictor` to tune the
   voltage→SOC mapping and Peukert exponent per suite / temperature band.
2. **Mission projections**: Call `predict_mission_viability` with predicted
   power draw for upcoming suites and gate `_apply_decision` if the margin is
   negative.
3. **Dataset capture**: When exporting `SchedulerDecision`, include
   `battery_remaining_s`, `battery_bin`, and `constraint_reasons` so you can
   build histograms in the paper.
4. **Ageing**: Update `self.age_factor` using cycle counts from logs if you want
   to demonstrate end-of-life behaviour.

With this structure you can trace every battery-related decision from INA219
samples → `BatteryPredictor.update` → `UnifiedUAVScheduler.update_telemetry` →
expert/RL fusion → final suite selection, ensuring the final benchmark captures
both graceful degradation and real energy trade-offs.
