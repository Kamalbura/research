# Manual Four-Terminal PQC Proxy Test Harness

This directory contains helper scripts for exercising the full drone ↔ GCS
proxy stack outside of the pytest harness.  The launcher brings up:

1. **GCS proxy** (`core.run_proxy gcs`)
2. **Drone proxy** (`core.run_proxy drone`)
3. **Ground-station simulator** (`gcs_ground_station_sim.py`)
4. **Drone autopilot simulator** (`drone_autopilot_sim.py`)

Optionally a fifth process – `encrypted_bridge_logger.py` – can sit between
the proxies to log encrypted traffic while forwarding packets to their real
destinations.

## Quick start

```powershell
# From the repository root
python tools/manual_4term/launch_manual_test.py --with-intercept
```

The launcher automatically ensures a signing identity exists (`secrets/`
by default), applies environment overrides so the processes use a dedicated
port range (46000–47004), and starts each component.  Press `Ctrl+C` in the
launcher window to terminate all managed child processes.

> **oqs compatibility:** If your `oqs-python` build lacks the optional
> key import/export APIs, the launcher now falls back to constructing the
> signer with the raw secret key bytes. Keep the matching `.pub` file in
> the same directory so the drone side can still verify signatures.

### Command-line options

| Option | Description |
| --- | --- |
| `--suite` | Cryptographic suite (default `cs-kyber768-aesgcm-dilithium3`) |
| `--secrets-dir` | Alternate location for the GCS signing keypair |
| `--no-auto-init` | Skip automatic key generation if files are missing |
| `--with-intercept` | Launch the encrypted bridge logger between proxies |
| `--new-windows` | On Windows, request a new console window per process |

If `--new-windows` is requested on a non-Windows platform, the launcher falls
back to streaming process output inline.

## Interactive plaintext consoles

When you want to bypass the simulators and drive each proxy manually, use the
lightweight TTY tools:

| Script | Default bind/target | Purpose |
| --- | --- | --- |
| `gcs_tty.py` | sends to `GCS_PLAINTEXT_TX`, listens on `GCS_PLAINTEXT_RX` | Inject commands toward the drone and watch telemetry coming back through the GCS proxy. |
| `drone_tty.py` | sends to `DRONE_PLAINTEXT_TX`, listens on `DRONE_PLAINTEXT_RX` | Feed telemetry into the drone proxy and observe decrypted command traffic. |

Both tools accept `--host`, `--tx-port`, `--rx-port`, `--expect`, and
`--verbose` options. `--expect N` exits after receiving `N` lines (useful for
deterministic test scripts), while `--verbose` prints debug messages to
stderr. Each line you type is truncated to 4096 bytes, newline-terminated, and
sent as a single UDP datagram.

Example session with both proxies already running in quiet mode:

```powershell
# Drone side: read decrypted commands, push telemetry lines
python tools/manual_4term/drone_tty.py --expect 0

# GCS side: send commands and display telemetry
python tools/manual_4term/gcs_tty.py --expect 0
```

Run each command in its own terminal so you can interact with stdin/stdout
independently. Pair these consoles with `encrypted_bridge_logger.py` when you
need ciphertext visibility.

## Manual in-band rekey workflow

To exercise the new interactive control path, launch the proxies with the
manual console enabled on the GCS side and quiet mode so the payload terminals
stay clean:

```powershell
python -m core.run_proxy gcs   --suite cs-kyber768-aesgcm-dilithium3 --control-manual --quiet
python -m core.run_proxy drone --suite cs-kyber768-aesgcm-dilithium3 --quiet
```

Each proxy now writes JSON logs under `logs/{role}-{YYYYmmdd-HHMMSS}.log` for
later timing/power correlation. The manual console prints the current state and
prompts for suite changes. Type `list` to see available suites, `status` for the
latest control-plane snapshot, or a valid suite ID (for example
`cs-kyber512-aesgcm-dilithium2`) to begin a two-phase rekey.

The GCS console enqueues a `prepare_rekey` request (packet type `0x02`) inside
the encrypted tunnel. The drone console either replies with `prepare_ok` or
`prepare_fail` (simulated safety gating), after which the proxies perform a
parallel handshake and atomically swap keys. Telemetry and command traffic keep
flowing; the status feed emits `RUNNING→NEGOTIATING→SWAPPING→RUNNING` with the
new suite ID.

> **Note:** Packet typing must remain enabled (`CONFIG["ENABLE_PACKET_TYPE"] =
> True`) so the proxies can distinguish control messages from payload bytes.

## What the simulators do

* `gcs_ground_station_sim.py` pushes a rotating set of high-level commands to
  the GCS plaintext ingress port and prints any telemetry frames that the
  proxy delivers back.
* `drone_autopilot_sim.py` emits synthetic telemetry frames and logs any
  decrypted command packets forwarded from the GCS side.

Both scripts accept `--send-port`, `--recv-port`, `--host`, `--interval`, and
`--loop` options when run standalone.

## Encrypted bridge logger

`encrypted_bridge_logger.py` listens on two UDP ports:

* Drone → GCS traffic (`--d2g-listen`) and forwards to the real GCS
  encrypted port (`--d2g-forward host:port`).
* GCS → Drone traffic (`--g2d-listen`) and forwards to the real drone
  encrypted port (`--g2d-forward host:port`).

For each packet the logger prints a timestamp, packet number, source address,
and a short hex preview of the ciphertext.  This is useful when validating
that encrypted links are alive without exposing plaintext data.

Example standalone invocation (mirrors the launcher defaults):

```powershell
python tools/manual_4term/encrypted_bridge_logger.py `
  --d2g-listen 46001 --d2g-forward 127.0.0.1:46011 `
  --g2d-listen 46002 --g2d-forward 127.0.0.1:46012
```

## Port map used by the launcher

| Purpose | Port |
| --- | --- |
| TCP handshake | 46000 |
| Drone encrypted bind | 46012 |
| GCS encrypted bind | 46011 |
| Intercept Drone→GCS listen | 46001 |
| Intercept GCS→Drone listen | 46002 |
| GCS plaintext ingress (commands) | 47001 |
| GCS plaintext egress (telemetry) | 47002 |
| Drone plaintext ingress (telemetry) | 47003 |
| Drone plaintext egress (commands) | 47004 |

Feel free to adjust the values by editing `launch_manual_test.py` or by setting
appropriate environment variables before invoking the launcher.

## Manual teardown

If any process is left running (for example when using `--new-windows` on
Windows), close the respective console window or send an interrupt signal to
terminate it.  The launcher makes a best-effort attempt to stop all children
on exit, but cannot forcibly close windows opened by the OS.  In the rare case
of a hung socket, run `taskkill /f /im python.exe` (Windows) or `pkill -f
core.run_proxy` (POSIX) as a last resort.
