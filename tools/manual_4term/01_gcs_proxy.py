import os, time, sys
from pathlib import Path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from core.async_proxy import run_proxy
from core.suites import get_suite
from core.config import CONFIG

SUITE_ID = "cs-kyber768-aesgcm-dilithium3"
CFG = {**CONFIG}
CFG.update({
    "TCP_HANDSHAKE_PORT": 45800,
    "UDP_DRONE_RX":       45801,
    "UDP_GCS_RX":         45802,
    "DRONE_PLAINTEXT_TX": 45803,
    "DRONE_PLAINTEXT_RX": 45804,
    "GCS_PLAINTEXT_TX":   45805,
    "GCS_PLAINTEXT_RX":   45806,
    "DRONE_HOST":         "127.0.0.1",
    "GCS_HOST":           "127.0.0.1",
})
KEYDIR = Path(__file__).resolve().parent / "keys"
PUB = KEYDIR / "gcs_pub.bin"
SEC = KEYDIR / "gcs_sec.bin"

def ensure_keys() -> tuple[bytes, bytes]:
    KEYDIR.mkdir(parents=True, exist_ok=True)
    if PUB.exists() and SEC.exists():
        print(f"[GCS] Loading keys from {KEYDIR}")
        return PUB.read_bytes(), SEC.read_bytes()
    try:
        import oqs.oqs as oqs
        suite = get_suite(SUITE_ID)
        with oqs.Signature(suite["sig_name"]) as sig:
            pub = sig.generate_keypair()
            sec = sig.export_secret_key()
    except Exception:
        pub = os.urandom(64)
        sec = os.urandom(64)
    PUB.write_bytes(pub); SEC.write_bytes(sec)
    print(f"[GCS] Wrote keys to {KEYDIR}")
    return pub, sec

if __name__ == "__main__":
    suite = get_suite(SUITE_ID)
    gcs_pub, gcs_sec = ensure_keys()
    print("[GCS] Effective ports from CFG:", {k: v for k, v in CFG.items() if isinstance(v, int)})
    print("[GCS] Starting proxy, waiting for drone connection...")
    try:
        run_proxy(role="gcs", suite=suite, cfg=CFG, gcs_sig_secret=gcs_sec, gcs_sig_public=None)
    except Exception as e:
        print(f"[GCS] ERROR: {e}")
        import traceback
        traceback.print_exc()
