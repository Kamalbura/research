import os, time, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.aead import Sender, Receiver, AeadIds
from core.suites import header_ids_for_suite
from core.config import CONFIG
def main():
    suite = {"kem_name":"ML-KEM-768","sig_name":"ML-DSA-65","aead":"AES-256-GCM","kdf":"HKDF-SHA256","kem_param":768,"sig_param":65}
    ids = AeadIds(*header_ids_for_suite(suite))
    key = os.urandom(32); sid = os.urandom(8)
    s = Sender(CONFIG["WIRE_VERSION"], ids, sid, 0, key)
    r = Receiver(CONFIG["WIRE_VERSION"], ids, sid, 0, key, CONFIG["REPLAY_WINDOW"])
    t0=time.perf_counter(); n=2000
    for _ in range(n):
        w = s.encrypt(b"x"*64)
        _ = r.decrypt(w)
    dt=time.perf_counter()-t0
    print({"pps": int(n/dt), "lat_us_per_pkt": int(dt/n*1e6)})
if __name__=="__main__": main()
