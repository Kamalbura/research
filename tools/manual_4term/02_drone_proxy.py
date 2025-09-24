import time, sys, os
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

def wait_for_pubkey(timeout=30.0) -> bytes:
    print("[DRONE] Waiting for GCS public key:", PUB)
    t0 = time.time()
    while not PUB.exists():
        if time.time() - t0 > timeout:
            raise TimeoutError("GCS public key not found. Start 01_gcs_proxy.py first.")
        time.sleep(0.2)
    return PUB.read_bytes()

if __name__ == "__main__":
    suite = get_suite(SUITE_ID)
    gcs_pub = wait_for_pubkey()
    print("[DRONE] Effective ports from CFG:", {k: v for k, v in CFG.items() if isinstance(v, int)})
    print(f"[DRONE] Connecting to GCS at {CFG['GCS_HOST']}:{CFG['TCP_HANDSHAKE_PORT']}...")
    try:
        run_proxy(role="drone", suite=suite, cfg=CFG, gcs_sig_secret=None, gcs_sig_public=gcs_pub)
    except Exception as e:
        print(f"[DRONE] ERROR: {e}")
        import traceback
        traceback.print_exc()
