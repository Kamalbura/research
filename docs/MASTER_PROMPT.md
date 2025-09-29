# MASTER PROMPT  Centralized-Config PQC Scheduler (GCS?Drone)

## 0) Non-negotiables (read first)

* **No hardcoded IPs/ports/flags anywhere.** Every script **must import** network and feature values from `core.config.CONFIG`.
  Examples: `DRONE_HOST`, `GCS_HOST`, `APP_SEND_PORT/APP_RECV_PORT` (or `APP_TX_PORT/APP_RX_PORT`), `CONTROL_PORT`, `ENABLE_PACKET_TYPE`, `STRICT_UDP_PEER_MATCH`.
* **No duplicated suite lists.** Discover suite IDs only from `core.suites` (e.g., `ALL_SUITE_IDS`) or, as a fallback, by scanning `secrets/matrix/<suite>/`.
* **Only expose test-plan knobs:**

  * `PACKETS_PER_SUITE` (int),
  * `INTER_PACKET_DELAY_S` (float seconds),
  * `SUITE_DURATION_S` (optional, float seconds per suite; if set, it overrides packets),
  * `PASSES` (int sweeps).
    Nothing else should be adjustable via CLI or file constants.
* **Master/Slave model:** GCS is **master scheduler**, Drone is **follower**. Synchronization happens via a **single temporary control API** (TCP) bound to `CONFIG["CONTROL_PORT"]`. No other control channels.
* **Refuse to start** if any required value is missing from `core.config` and cannot be inferred. Print an actionable error pointing to the exact key.

## 1) File layout (under `tools/auto/`)

* `gcs/` (master)

  * `scheduler.py`  starts GCS proxy once (with `--control-manual`), cycles suites, sends test packets, advances only when echo is verified.
* `drone/` (slave)

  * `follower.py`  starts drone proxy once, runs UDP echo (APP_RX?APP_TX), exposes control API (`ping|mark|stop`) on `CONFIG["CONTROL_PORT"]`.

> All other helpers (logging, markers) live alongside, but **import** their settings from `core.config` only.

## 2) Required imports from core

* `from core.config import CONFIG`
  Must read at least:
  `DRONE_HOST, GCS_HOST, ENABLE_PACKET_TYPE, STRICT_UDP_PEER_MATCH, CONTROL_PORT, CONTROL_BIND (optional), APP_SEND_PORT/APP_RECV_PORT (or APP_TX_PORT/APP_RX_PORT)`
* `from core import suites as suites_mod`
  Use `suites_mod.ALL_SUITE_IDS` (or equivalent). If absent, scan `secrets/matrix/`.

## 3) Drone follower (slave)  responsibilities

* **Start once:** launch `core.run_proxy drone` with the **current suites** `gcs_signing.pub` from `secrets/matrix/<suite>/gcs_signing.pub`.
  Read `DRONE_HOST/GCS_HOST/flags` from `CONFIG` (export to env for the subprocess).
* **Local UDP echo:** bind `APP_RECV_PORT`, forward payloads to `APP_SEND_PORT` (`127.0.0.1`). No tweaks, no flags.
* **Control API (TCP JSON line):** bind `CONFIG["CONTROL_PORT"]` (host = `CONFIG.get("CONTROL_BIND","0.0.0.0")`).

  * `{"cmd":"ping"}` ? `{"ok":true}`
  * `{"cmd":"mark","suite":"<id>"}` ? create timestamped marker file (for perf/telemetry alignment).
  * `{"cmd":"stop"}` ? clean shutdown of follower; the proxy may stay alive or be signaled to exit gracefully.
* **Logging outputs:** under `logs/auto/drone/`

  * `status.json`, `summary.json` from the proxy (via `--status-file/--json-out`)
  * `marks/<epoch>_<suite>.json` (from `mark` calls)
* **No suite list, no IPs, no ports inside follower.** Everything from `core.config` + suite selection driven by the master.

## 4) GCS scheduler (master)  responsibilities

* **Start once:** launch `core.run_proxy gcs` with `--control-manual`, initial suite = first from `core.suites` (or first from `secrets/matrix/`). Use **only** values from `CONFIG` for hosts/ports/flags.
* **Loop logic (quick-pass or duration mode):** for each suite and each pass:

  1. **Rekey:** write `<suite-id>\n` to the proxys stdin.
  2. **Mark:** send `{"cmd":"mark","suite": "<id>"}` to the drone control port.
  3. **Test:**

     * **Packets mode:** send `PACKETS_PER_SUITE` UDP payloads to `APP_SEND_PORT` (localhost), each time **wait for echo** on `APP_RECV_PORT` with timeout `VERIFY_TIMEOUT_S`.
     * **Duration mode:** if `SUITE_DURATION_S > 0`, send as many packets as possible with spacing `INTER_PACKET_DELAY_S` until time elapses, verifying echos.
  4. Record per-suite stats (sent/ok, min/median/max RTT ns). Advance to next suite only after at least one echo success (or after timeout is recorded).
* **Interactive mode:** if launched without `--auto`, expose prompt: `list | next | all | <suite-id> | quit` (no IP/port arguments).
* **Logging outputs:** under `logs/auto/`

  * `quickpass_summary.csv` (append-only; fields: `ts,suite,sent,ok,rtt_ns_min,rtt_ns_p50,rtt_ns_max`)
  * `gcs_*.log` (proxy stdout/stderr)
  * Per-suite `gcs_status.json`, `gcs_summary.json` under `logs/auto/<suite>/`
* **No IP/port flags.** Only test-plan knobs allowed at top of file or via minimal `--auto` switch.

## 5) Control-plane contract

* Single TCP line-JSON request/response. **No binary, no extra framing.**
* Timeouts: client side = 3s. Failures are **warnings** (dont crash the run) but must be logged.

## 6) Test-plan knobs (and only these)

* `PACKETS_PER_SUITE: int` (default 1)
* `INTER_PACKET_DELAY_S: float` (default 0.05)
* `SUITE_DURATION_S: float` (default 0; when >0, use duration mode)
* `PASSES: int` (default 1)
* `VERIFY_TIMEOUT_S: float` (default 5.0)

These may be constants at the top of each file or exposed as **minimal** CLI flags, but **must not** duplicate core config.

## 7) Acceptance criteria (verify before merging)

1. **Static checks**

   * Grep shows **no string literals** for LAN IPs, ports, or feature flags in the new scripts.
     Allowed: only reading from `CONFIG` keys and suite IDs from `core.suites` or secrets scan.
   * Changing `CONFIG["APP_SEND_PORT"]` changes both apps binding/sending without code edits.
2. **Runtime checks**

   * With the current `CONFIG`, the follower starts, answers `ping`, and writes `marks/…` on `mark`.
   * Scheduler runs `list`, `next`, `all`; echoes are received; `quickpass_summary.csv` is populated.
   * Rekey between at least three suites without restarting proxies; `status.json` shows the last suite applied.
3. **Failure behavior**

   * Missing key material for any suite ? clear error naming the missing file path; other suites still runnable.
   * Control API unreachable ? warn and continue; dont crash the scheduler.

## 8) Non-goals (avoid scope creep)

* No per-script environment overrides for hosts/ports.
* No separate suite catalogs or YAMLs.
* No parallel suite runs; keep it sequential and deterministic for clean logs.

## 9) Minimal run instructions

* **Drone (Pi):** `python tools/auto/drone/follower.py`
* **GCS (Windows):**

  * Manual: `python tools/auto/gcs/scheduler.py` (use prompt)
  * Auto:   `python tools/auto/gcs/scheduler.py --auto`

Thats it. If you need to change *only* the experiment plan, edit the four knobs at the top of `scheduler.py` (packets, delay, duration, passes). Everything else comes from core config and core suites. Dont re-introduce IP/port flags.
