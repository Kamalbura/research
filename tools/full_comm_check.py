from __future__ import annotations
import json, os, socket, threading, time, sys
from types import ModuleType

# --------- helpers ---------
def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

def _clone_config_with_ports(base_cfg: dict) -> dict:
    cfg = dict(base_cfg)
    # Make everything local loopback and unique per run
    cfg["DRONE_HOST"] = "127.0.0.1"
    cfg["GCS_HOST"] = "127.0.0.1"

    # Plaintext app ports (4 distinct)
    cfg["DRONE_PLAINTEXT_TX"] = _free_udp_port()
    cfg["DRONE_PLAINTEXT_RX"] = _free_udp_port()
    while cfg["DRONE_PLAINTEXT_RX"] == cfg["DRONE_PLAINTEXT_TX"]:
        cfg["DRONE_PLAINTEXT_RX"] = _free_udp_port()

    cfg["GCS_PLAINTEXT_TX"] = _free_udp_port()
    cfg["GCS_PLAINTEXT_RX"] = _free_udp_port()
    while cfg["GCS_PLAINTEXT_RX"] == cfg["GCS_PLAINTEXT_TX"]:
        cfg["GCS_PLAINTEXT_RX"] = _free_udp_port()

    # Encrypted RX ports (must be distinct)
    cfg["DRONE_ENCRYPTED_RX"] = _free_udp_port()
    cfg["GCS_ENCRYPTED_RX"] = _free_udp_port()
    while cfg["GCS_ENCRYPTED_RX"] == cfg["DRONE_ENCRYPTED_RX"]:
        cfg["GCS_ENCRYPTED_RX"] = _free_udp_port()

    # Handshake TCP port
    cfg["TCP_HANDSHAKE_PORT"] = max(5800, _free_udp_port())
    return cfg

# --------- step 1: pytest ---------
def run_pytests() -> dict:
    try:
        import pytest  # type: ignore
    except Exception as e:
        return {"status": "ERROR", "detail": f"pytest import failed: {e}"}
    # Run full test suite quietly
    code = pytest.main(["-q"])
    return {"status": "OK" if code == 0 else "FAIL", "exit_code": code}

# --------- step 2: loopback smoke ---------
def smoke_loopback() -> dict:
    try:
        from core.async_proxy import run_proxy
        from oqs.oqs import Signature
    except Exception as e:
        return {"status": "ERROR", "detail": f"cannot import required modules: {e}"}

    # Load baseline config
    try:
        from core.config import CONFIG, load_config, validate_config  # type: ignore
        base_cfg = CONFIG
        # If load_config/validate_config exist, run a quick check
        try:
            tmp = load_config(os.environ) if callable(load_config) else None  # type: ignore
            if callable(validate_config):  # type: ignore
                validate_config(base_cfg)  # type: ignore
        except Exception:
            pass
    except Exception:
        # Fallback: try project_config re-export
        try:
            from core.project_config import CONFIG  # type: ignore
            base_cfg = CONFIG
        except Exception as e2:
            return {"status": "ERROR", "detail": f"cannot load config: {e2}"}

    cfg = _clone_config_with_ports(base_cfg)
    
    # Generate REAL cryptographic keys for testing - SECURITY CRITICAL
    try:
        suite_dict = {"kem_name":"ML-KEM-768","kem_param":768,"sig_name":"ML-DSA-65","sig_param":65,"aead":"AES-256-GCM","kdf":"HKDF-SHA256","nist_level":3}
        sig = Signature(suite_dict["sig_name"])
        gcs_sig_public = sig.generate_keypair()
    except Exception as e:
        return {"status": "ERROR", "detail": f"failed to generate keys: {e}"}

    # Storage for proxy results and errors
    gcs_err = {"error": None}
    drn_err = {"error": None}

    def gcs_thread():
        try:
            run_proxy(
                role="gcs",
                suite=suite_dict,
                cfg=cfg,
                gcs_sig_secret=sig,  # Real signature object - SECURITY CRITICAL
                gcs_sig_public=None,
                stop_after_seconds=2.0,
            )
        except Exception as e:
            gcs_err["error"] = repr(e)

    def drone_thread():
        try:
            time.sleep(0.2)  # let GCS bind first
            run_proxy(
                role="drone",
                suite=suite_dict,
                cfg=cfg,
                gcs_sig_secret=None,
                gcs_sig_public=gcs_sig_public,  # Real public key - SECURITY CRITICAL
                stop_after_seconds=2.0,
            )
        except Exception as e:
            drn_err["error"] = repr(e)

    # Start receivers (apps side)
    received_at_gcs = {"data": None}
    received_at_drone = {"data": None}

    def recv_gcs():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as r:
                r.bind(("127.0.0.1", cfg["GCS_PLAINTEXT_RX"]))
                r.settimeout(2.0)
                data, _ = r.recvfrom(2048)
                received_at_gcs["data"] = data
        except Exception:
            pass

    def recv_drone():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as r:
                r.bind(("127.0.0.1", cfg["DRONE_PLAINTEXT_RX"]))
                r.settimeout(2.0)
                data, _ = r.recvfrom(2048)
                received_at_drone["data"] = data
        except Exception:
            pass

    tg = threading.Thread(target=gcs_thread, daemon=True)
    td = threading.Thread(target=drone_thread, daemon=True)
    rg = threading.Thread(target=recv_gcs, daemon=True)
    rd = threading.Thread(target=recv_drone, daemon=True)

    rg.start(); rd.start()
    tg.start(); td.start()

    time.sleep(0.7)  # allow handshake

    # Send both directions via plaintext TX
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"Hello from drone", ("127.0.0.1", cfg["DRONE_PLAINTEXT_TX"]))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"Hello from GCS", ("127.0.0.1", cfg["GCS_PLAINTEXT_TX"]))
    except Exception as e:
        return {"status": "ERROR", "detail": f"send failed: {e}"}

    rg.join(timeout=2.5); rd.join(timeout=2.5)
    tg.join(timeout=3.0);  td.join(timeout=3.0)

    if gcs_err["error"] or drn_err["error"]:
        return {"status": "FAIL", "detail": {"gcs": gcs_err["error"], "drone": drn_err["error"]}}

    ok = (received_at_gcs["data"] == b"Hello from drone" and
          received_at_drone["data"] == b"Hello from GCS")
    return {"status": "OK" if ok else "FAIL",
            "detail": {
                "gcs_rx": received_at_gcs["data"],
                "drone_rx": received_at_drone["data"],
                "ports": {
                    "DRONE_TX": cfg["DRONE_PLAINTEXT_TX"],
                    "DRONE_RX": cfg["DRONE_PLAINTEXT_RX"],
                    "GCS_TX": cfg["GCS_PLAINTEXT_TX"],
                    "GCS_RX": cfg["GCS_PLAINTEXT_RX"],
                    "ENC_DRONE": cfg["DRONE_ENCRYPTED_RX"],
                    "ENC_GCS": cfg["GCS_ENCRYPTED_RX"],
                    "HS_TCP": cfg["TCP_HANDSHAKE_PORT"],
                }
            }}

# --------- step 3: config checks ---------
def config_checks() -> dict:
    out = {}
    try:
        from core.config import CONFIG, load_config, validate_config  # type: ignore
    except Exception as e:
        return {"status": "UNKNOWN", "detail": f"no load/validate available: {e}"}

    # Base validate
    try:
        validate_config(CONFIG)  # type: ignore
        out["base_validate"] = "OK"
    except Exception as e:
        out["base_validate"] = f"FAIL: {e}"

    # Env override smoke
    try:
        env = os.environ.copy()
        env["DRONE_HOST"] = "127.0.0.1"
        env["GCS_HOST"] = "127.0.0.1"
        env["DRONE_PLAINTEXT_TX"] = "14650"
        env["DRONE_PLAINTEXT_RX"] = "14651"
        env["GCS_PLAINTEXT_TX"] = "15652"
        env["GCS_PLAINTEXT_RX"] = "15653"
        env["DRONE_ENCRYPTED_RX"] = "6810"
        env["GCS_ENCRYPTED_RX"] = "6811"
        cfg2 = load_config(env)  # type: ignore
        validate_config(cfg2)  # type: ignore
        out["env_override"] = "OK"
    except Exception as e:
        out["env_override"] = f"FAIL: {e}"

    # Port dedupe failure
    try:
        bad = dict(CONFIG)
        bad["DRONE_PLAINTEXT_RX"] = bad["DRONE_PLAINTEXT_TX"]
        validate_config(bad)  # type: ignore
        out["dedupe_check"] = "FAIL: expected ValueError"
    except Exception:
        out["dedupe_check"] = "OK"

    status = ("OK" if all(v == "OK" for v in out.values()) else "FAIL")
    out["status"] = status
    return out

# --------- step 4: wrapper import check ---------
def wrapper_imports() -> dict:
    import importlib, pathlib
    results = {"drone": {}, "gcs": {}}
    base = pathlib.Path(__file__).resolve().parents[1]

    for side in ("drone", "gcs"):
        wdir = base / side / "wrappers"
        if not wdir.exists():
            results[side]["status"] = "UNKNOWN: wrappers dir missing"
            continue
        for f in sorted(wdir.glob("*.py")):
            modname = f"{side}.wrappers.{f.stem}"
            try:
                m: ModuleType = importlib.import_module(modname)  # noqa
                results[side][f.name] = "IMPORTED"
            except Exception as e:
                results[side][f.name] = f"IMPORT_FAIL: {e}"
        results[side]["status"] = "OK" if all(v=="IMPORTED" for k,v in results[side].items() if k.endswith(".py")) else "FAIL"
    return results

# --------- main ---------
def main():
    report = {}
    report["pytest"] = run_pytests()
    report["smoke"] = smoke_loopback()
    report["config"] = config_checks()
    report["wrappers"] = wrapper_imports()
    print(json.dumps(report, indent=2, default=str))

if __name__ == "__main__":
    main()
