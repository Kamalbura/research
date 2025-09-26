# PQC Drone ↔ GCS Secure Proxy

A safety-critical, post-quantum secure tunnel that bridges plaintext telemetry/command traffic between a drone and a Ground Control Station (GCS). The system delivers authenticated PQC handshakes, AES-GCM packet protection, replay resistance, and operational tooling validated on a Raspberry Pi 4 (drone) and Windows host (GCS).

> **Status:** Fully operational with 82/82 automated tests passing (one scenario intentionally skipped). Recent LAN validation steps are documented in [`docs/lan-test.txt`](docs/lan-test.txt).

---

## Highlights

- **Post-quantum handshake** – ML-KEM + signature (ML-DSA / Falcon / SPHINCS+) with HKDF-derived transport keys.
- **Hardened AEAD framing** – AES-256-GCM, deterministic nonces, and a 1024-packet replay window.
- **Hybrid transport** – Authenticated TCP handshake with UDP data plane and policy hooks for rate limiting/rekey.
- **Single-source configuration** – `core/config.py` exposes validated defaults with environment overrides.
- **Field-ready tooling** – TTY injectors, encrypted taps, and diagnostics scripts for LAN deployments.

The implementation follows the security guidance captured in [`.github/copilot-instructions.md`](.github/copilot-instructions.md) and is organized so that **`core/` remains the only cryptographic source of truth**.

---

## Repository layout

```
core/               PQC handshake, AEAD framing, async proxy, suites, config
  ├─ config.py      Validated defaults + env override support
  ├─ handshake.py   ML-KEM + signature transcript processing
  ├─ aead.py        AES-GCM sender/receiver with replay window
  ├─ async_proxy.py TCP↔UDP hybrid transport + policy hooks
  ├─ run_proxy.py   CLI entry point used by both drone & GCS hosts
  └─ suites.py      Immutable registry of 7 PQC suites

tools/              Operational helpers
  ├─ manual_4term/  `gcs_tty.py`, `drone_tty.py` plaintext consoles
  ├─ udp_forward_log.py  Inline tap/forwarder with header logging
  └─ ...            Capture harnesses, diagnostics, benchmarks

tests/             55 unit/integration tests (100% pass rate)
docs/              Field notes and validation reports (`lan-test.txt`)
```

---

## Prerequisites

- Python **3.10+** (checked via `pyproject.toml`).
- [`oqs-python`](https://github.com/open-quantum-safe/liboqs-python) ≥ 0.10.0 with liboqs installed.
- [`cryptography`](https://cryptography.io/) ≥ 45.0.
- Optional: `pytest`, `pytest-anyio` for the test suite.
- Hardware tested on: Raspberry Pi 4B (drone role) + Windows 11 (GCS role).

### Environment setup (example)

```bash
# Clone the repo
git clone https://github.com/Kamalbura/research.git
cd research

# Create and activate a Python environment
python -m venv .venv
source .venv/bin/activate            # Linux / macOS
#.venv\Scripts\activate              # Windows PowerShell

# Install runtime dependencies
pip install oqs cryptography

# Install test extras if desired
pip install -e .[test]
```

> On Windows with Conda, use `conda env create -f environment.yml` to mirror the maintained `gcs-env` setup.

---

## Running the proxies

You can operate the system either on a single machine (loopback testing) or across two hosts on a LAN. All examples below use the `cs-kyber768-aesgcm-dilithium3` suite, which balances performance and security.

### Default ports

Values are defined in `core/config.py` and validated at startup:

| Purpose                | Default port |
|------------------------|--------------|
| TCP handshake          | 46000        |
| GCS encrypted UDP RX   | 46011        |
| Drone encrypted UDP RX | 46012        |
| GCS plaintext TX/RX    | 47001 / 47002|
| Drone plaintext TX/RX  | 47003 / 47004|

Default host bindings (`DRONE_HOST`, `GCS_HOST`) target the LAN IPs defined in `core/config.py` (for example `192.168.0.102` and `192.168.0.103`). Override any value by setting an environment variable before launching a proxy. Examples:

```bash
export UDP_DRONE_RX=56012
export DRONE_HOST=127.0.0.1
```

### Quick loopback smoke test (single host)

1. **Start the GCS proxy** (TCP listener + UDP forwarder):

  ```bash
  python -m core.run_proxy gcs --suite cs-kyber768-aesgcm-dilithium3 --stop-seconds 180 --json-out gcs_debug.json --ephemeral
  ```

2. **Start the drone proxy** in another terminal. When using the ephemeral option above, copy the printed public key into `--gcs-pub-hex`:

  ```bash
  python -m core.run_proxy drone --suite cs-kyber768-aesgcm-dilithium3 --stop-seconds 180 --json-out drone_debug.json --gcs-pub-hex <hex-from-gcs>
  ```

3. **Attach plaintext injectors** to exercise both directions:

  ```bash
  python tools/manual_4term/gcs_tty.py
  python tools/manual_4term/drone_tty.py
  ```

Type into the GCS TTY; the drone TTY should display each line, confirming end-to-end encryption/decryption.

### 2. LAN deployment (two hosts)

The sequence validated in September 2025 is recorded in [`docs/lan-test.txt`](docs/lan-test.txt). A condensed version:

**Drone host (Raspberry Pi)**
```bash
export UDP_DRONE_RX=56012                      # tap backend
source ~/cenv/bin/activate
python -m core.run_proxy drone --suite cs-kyber768-aesgcm-mldsa65 --stop-seconds 360 --json-out drone_debug.json
python tools/manual_4term/drone_tty.py         # keep open for plaintext output
```

**GCS host (Windows PowerShell)**
```powershell
conda activate gcs-env
$Env:UDP_GCS_RX = "56011"                       # tap backend
python -m core.run_proxy gcs --suite cs-kyber768-aesgcm-dilithium3 --stop-seconds 360 --json-out gcs_debug.json
python tools\manual_4term\gcs_tty.py           # keep open for plaintext input
```

Keep both TTYs and proxies running simultaneously. Type in the GCS console and verify the drone console receives each line.

> **Tip:** Stop proxies with `Ctrl+C` after traffic has flowed to ensure `gcs_debug.json` / `drone_debug.json` capture non-zero counters.

---

## Operational tooling

| Tool | Purpose |
|------|---------|
| `tools/manual_4term/gcs_tty.py` | Sends plaintext commands to the GCS proxy and prints decrypted telemetry. Defaults to `127.0.0.1` loopback ports. |
| `tools/manual_4term/drone_tty.py` | Symmetric console for the drone side. |
| `tools/udp_forward_log.py` | Inline UDP forwarder that logs PQC header metadata (`session_id`, `seq`, `epoch`) while forwarding packets—ideal for LAN taps. |
| `tools/netcapture/gcs_capture.py` / `drone_capture.py` | Windows `pktmon`/Linux `tcpdump` wrappers for handshake and encrypted traffic capture. |
| `tools/udp_dual_probe.py` | Diagnostics probe that sends numbered messages in both directions to confirm port wiring before proxies are launched. |

Operational notes:

- Logs are written to `logs/<role>-YYYYMMDD-HHMMSS.log`.
- JSON summaries (`--json-out`) include plaintext/encrypted counters, drop causes, and rekey metadata.
- Replay drops are classified: `drop_header`, `drop_auth`, `drop_session_epoch`, `drop_replay`, `drop_other`.

---

## Testing

The repository ships with **82 automated tests** (plus one intentionally skipped long-running scenario) covering configuration, handshake, AEAD framing, replay prevention, control policy logic, and network transport.

```bash
python -m pytest tests/ -vv
```

To target a subset:

```bash
python -m pytest tests/test_handshake.py -vv
python -m pytest tests/test_end_to_end_proxy.py -vv
```

> Test dependencies are defined under the `test` extra in `pyproject.toml`. End-to-end tests reserve loopback ports dynamically, so they are safe to run even if proxies are already bound to default ports during manual experiments.

---

## Troubleshooting

| Symptom | Resolution |
|---------|------------|
| `ModuleNotFoundError: No module named 'oqs'` | Install `oqs-python` in the active environment (ensure liboqs shared library is available). |
| `WinError 10048` when starting a proxy | The default encrypted port is already bound (often by `udp_forward_log.py`). Override `UDP_GCS_RX`/`UDP_DRONE_RX` to the tap’s backend. |
| JSON counters remain zero | The proxies were stopped before plaintext flowed. Keep TTYs active while proxies run, then terminate proxies with `Ctrl+C`. |
| Handshake stalls | Confirm the GCS host is reachable on the TCP handshake port (default 46000) and that firewall rules allow inbound connections. |

---

## Documentation & support

- **LAN validation log** – [`docs/lan-test.txt`](docs/lan-test.txt)
- **Project roadmap** – [`PROJECT_STATUS.md`](PROJECT_STATUS.md)
- **Change history** – [`CHANGELOG.md`](CHANGELOG.md)
- **AI coding guidelines** – [`.github/copilot-instructions.md`](.github/copilot-instructions.md)

For issues or enhancements, open a GitHub issue in this repository. Security-sensitive disclosures should be coordinated privately prior to publication.

---

## Security notes

This project implements high-assurance cryptographic primitives, but no formal certification has been completed. Before production deployment:

1. Commission an independent security review.
2. Re-run the full automated test suite on the target hardware.
3. Keep liboqs/`oqs-python` patched to the latest stable release.

---

**Built for quantum-safe, real-time drone operations – tested across LAN and ready for advanced policy integration.**