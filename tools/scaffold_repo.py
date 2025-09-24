# tools/scaffold_repo.py
# Create planned folders/files that aren't in the current tree.
# Safe by default: won't overwrite unless --force is given.

import argparse, os, sys, stat, textwrap
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def write(path: Path, content: str, force=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        print(f"skip  (exists) {path}")
        return False
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8", newline="\n")
    print(f"write {path}")
    return True

def make_executable(path: Path):
    try:
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    except Exception:
        pass  # windows ok

def main(force=False):
    wrote = 0

    # ---------- core additions ----------
    wrote += write(ROOT / "core" / "project_config.py", """
        # Thin shim so planned path 'project_config.py' exists without breaking tests.
        # Source of truth remains core/config.py
        from .config import CONFIG
        __all__ = ["CONFIG"]
    """, force)

    wrote += write(ROOT / "core" / "logging_utils.py", """
        import json, logging, sys, time
        from typing import Any, Dict

        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                    "level": record.levelname,
                    "name": record.name,
                    "msg": record.getMessage(),
                }
                if record.exc_info:
                    payload["exc_info"] = self.formatException(record.exc_info)
                # Allow extra fields via record.__dict__ (filtered)
                for k, v in record.__dict__.items():
                    if k not in ("msg", "args", "exc_info", "exc_text", "stack_info", "stack_level", "created",
                                 "msecs", "relativeCreated", "levelno", "levelname", "pathname", "filename",
                                 "module", "lineno", "funcName", "thread", "threadName", "processName", "process"):
                        try:
                            json.dumps({k: v})
                            payload[k] = v
                        except Exception:
                            payload[k] = str(v)
                return json.dumps(payload)

        def get_logger(name: str = "pqc") -> logging.Logger:
            logger = logging.getLogger(name)
            if logger.handlers:
                return logger
            logger.setLevel(logging.INFO)
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(JsonFormatter())
            logger.addHandler(h)
            logger.propagate = False
            return logger

        # Very small metrics hook (no deps)
        class Counter:
            def __init__(self): self.value = 0
            def inc(self, n: int = 1): self.value += n

        class Gauge:
            def __init__(self): self.value = 0
            def set(self, v: float): self.value = v

        class Metrics:
            def __init__(self):
                self.counters = {}
                self.gauges = {}
            def counter(self, name: str) -> Counter:
                self.counters.setdefault(name, Counter()); return self.counters[name]
            def gauge(self, name: str) -> Gauge:
                self.gauges.setdefault(name, Gauge()); return self.gauges[name]

        METRICS = Metrics()
    """, force)

    # ---------- wrappers (no-arg launchers) ----------
    WRAPPER_MAP = {
        # drone
        "drone/wrappers/drone_kyber_512.py":        "cs-kyber512-aesgcm-dilithium2",
        "drone/wrappers/drone_kyber_768.py":        "cs-kyber768-aesgcm-dilithium3",
        "drone/wrappers/drone_kyber_1024.py":       "cs-kyber1024-aesgcm-dilithium5",
        "drone/wrappers/drone_dilithium2.py":       "cs-kyber512-aesgcm-dilithium2",
        "drone/wrappers/drone_dilithium3.py":       "cs-kyber768-aesgcm-dilithium3",
        "drone/wrappers/drone_dilithium5.py":       "cs-kyber1024-aesgcm-dilithium5",
        "drone/wrappers/drone_falcon512.py":        "cs-kyber768-aesgcm-falcon512",
        "drone/wrappers/drone_falcon1024.py":       "cs-kyber1024-aesgcm-falcon1024",
        "drone/wrappers/drone_sphincs_sha2_128f.py":"cs-kyber512-aesgcm-sphincs128f_sha2",
        "drone/wrappers/drone_sphincs_sha2_256f.py":"cs-kyber1024-aesgcm-sphincs256f_sha2",
        # gcs
        "gcs/wrappers/gcs_kyber_512.py":            "cs-kyber512-aesgcm-dilithium2",
        "gcs/wrappers/gcs_kyber_768.py":            "cs-kyber768-aesgcm-dilithium3",
        "gcs/wrappers/gcs_kyber_1024.py":           "cs-kyber1024-aesgcm-dilithium5",
        "gcs/wrappers/gcs_dilithium2.py":           "cs-kyber512-aesgcm-dilithium2",
        "gcs/wrappers/gcs_dilithium3.py":           "cs-kyber768-aesgcm-dilithium3",
        "gcs/wrappers/gcs_dilithium5.py":           "cs-kyber1024-aesgcm-dilithium5",
        "gcs/wrappers/gcs_falcon512.py":            "cs-kyber768-aesgcm-falcon512",
        "gcs/wrappers/gcs_falcon1024.py":           "cs-kyber1024-aesgcm-falcon1024",
        "gcs/wrappers/gcs_sphincs_sha2_128f.py":    "cs-kyber512-aesgcm-sphincs128f_sha2",
        "gcs/wrappers/gcs_sphincs_sha2_256f.py":    "cs-kyber1024-aesgcm-sphincs256f_sha2",
    }
    WRAPPER_TMPL = """
        from core.runner import start
        ROLE="{role}"; SUITE_ID="{suite}"
        if __name__ == "__main__":
            start(ROLE, SUITE_ID)
    """
    for rel, suite in WRAPPER_MAP.items():
        role = "drone" if rel.startswith("drone/") else "gcs"
        wrote += write(ROOT / rel, WRAPPER_TMPL.format(role=role, suite=suite), force)

    # ---------- scripts (bash + ps1) ----------
    wrote += write(ROOT / "drone" / "scripts" / "start_suite.sh", """
        #!/usr/bin/env bash
        set -euo pipefail
        suite="${1:-cs-kyber768-aesgcm-dilithium3}"
        case "$suite" in
          cs-kyber512-aesgcm-dilithium2)  py="drone/wrappers/drone_kyber_512.py";;
          cs-kyber768-aesgcm-dilithium3)  py="drone/wrappers/drone_kyber_768.py";;
          cs-kyber1024-aesgcm-dilithium5) py="drone/wrappers/drone_kyber_1024.py";;
          cs-kyber768-aesgcm-falcon512)   py="drone/wrappers/drone_falcon512.py";;
          cs-kyber1024-aesgcm-falcon1024) py="drone/wrappers/drone_falcon1024.py";;
          cs-kyber512-aesgcm-sphincs128f_sha2) py="drone/wrappers/drone_sphincs_sha2_128f.py";;
          cs-kyber1024-aesgcm-sphincs256f_sha2) py="drone/wrappers/drone_sphincs_sha2_256f.py";;
          *) echo "Unknown suite: $suite"; exit 2;;
        esac
        exec python "$py"
    """, force)
    make_executable(ROOT / "drone" / "scripts" / "start_suite.sh")

    wrote += write(ROOT / "gcs" / "scripts" / "start_suite.sh", """
        #!/usr/bin/env bash
        set -euo pipefail
        suite="${1:-cs-kyber768-aesgcm-dilithium3}"
        case "$suite" in
          cs-kyber512-aesgcm-dilithium2)  py="gcs/wrappers/gcs_kyber_512.py";;
          cs-kyber768-aesgcm-dilithium3)  py="gcs/wrappers/gcs_kyber_768.py";;
          cs-kyber1024-aesgcm-dilithium5) py="gcs/wrappers/gcs_kyber_1024.py";;
          cs-kyber768-aesgcm-falcon512)   py="gcs/wrappers/gcs_falcon512.py";;
          cs-kyber1024-aesgcm-falcon1024) py="gcs/wrappers/gcs_falcon1024.py";;
          cs-kyber512-aesgcm-sphincs128f_sha2) py="gcs/wrappers/gcs_sphincs_sha2_128f.py";;
          cs-kyber1024-aesgcm-sphincs256f_sha2) py="gcs/wrappers/gcs_sphincs_sha2_256f.py";;
          *) echo "Unknown suite: $suite"; exit 2;;
        esac
        exec python "$py"
    """, force)
    make_executable(ROOT / "gcs" / "scripts" / "start_suite.sh")

    wrote += write(ROOT / "drone" / "scripts" / "start_suite.ps1", r"""
        param([string]$suite = "cs-kyber768-aesgcm-dilithium3")
        $map = @{
          "cs-kyber512-aesgcm-dilithium2"      = "drone/wrappers/drone_kyber_512.py"
          "cs-kyber768-aesgcm-dilithium3"      = "drone/wrappers/drone_kyber_768.py"
          "cs-kyber1024-aesgcm-dilithium5"     = "drone/wrappers/drone_kyber_1024.py"
          "cs-kyber768-aesgcm-falcon512"       = "drone/wrappers/drone_falcon512.py"
          "cs-kyber1024-aesgcm-falcon1024"     = "drone/wrappers/drone_falcon1024.py"
          "cs-kyber512-aesgcm-sphincs128f_sha2"= "drone/wrappers/drone_sphincs_sha2_128f.py"
          "cs-kyber1024-aesgcm-sphincs256f_sha2"= "drone/wrappers/drone_sphincs_sha2_256f.py"
        }
        if (-not $map.ContainsKey($suite)) { Write-Error "Unknown suite $suite"; exit 2 }
        python $map[$suite]
    """, force)

    wrote += write(ROOT / "gcs" / "scripts" / "start_suite.ps1", r"""
        param([string]$suite = "cs-kyber768-aesgcm-dilithium3")
        $map = @{
          "cs-kyber512-aesgcm-dilithium2"      = "gcs/wrappers/gcs_kyber_512.py"
          "cs-kyber768-aesgcm-dilithium3"      = "gcs/wrappers/gcs_kyber_768.py"
          "cs-kyber1024-aesgcm-dilithium5"     = "gcs/wrappers/gcs_kyber_1024.py"
          "cs-kyber768-aesgcm-falcon512"       = "gcs/wrappers/gcs_falcon512.py"
          "cs-kyber1024-aesgcm-falcon1024"     = "gcs/wrappers/gcs_falcon1024.py"
          "cs-kyber512-aesgcm-sphincs128f_sha2"= "gcs/wrappers/gcs_sphincs_sha2_128f.py"
          "cs-kyber1024-aesgcm-sphincs256f_sha2"= "gcs/wrappers/gcs_sphincs_sha2_256f.py"
        }
        if (-not $map.ContainsKey($suite)) { Write-Error "Unknown suite $suite"; exit 2 }
        python $map[$suite]
    """, force)

    wrote += write(ROOT / "drone" / "scripts" / "env_check.py", """
        import sys
        status = {}
        try:
            import cryptography
            status["cryptography"] = cryptography.__version__
        except Exception as e:
            status["cryptography"] = f"ERROR: {e}"
        try:
            import oqs.oqs as oqs
            status["oqs-python"] = oqs.oqs_version()
        except Exception as e:
            status["oqs-python"] = f"ERROR: {e}"
        print(status); sys.exit(0 if all("ERROR" not in v for v in status.values()) else 1)
    """, force)
    wrote += write(ROOT / "gcs" / "scripts" / "env_check.py", (ROOT / "drone" / "scripts" / "env_check.py").read_text() if (ROOT / "drone" / "scripts" / "env_check.py").exists() else """
        # same as drone/scripts/env_check.py
    """, force)

    # ---------- ddos stubs ----------
    wrote += write(ROOT / "ddos" / "features.py", """
        def extract_features(pkt_batch):
            raise NotImplementedError("DDoS pipeline is out of scope right now.")
    """, force)
    wrote += write(ROOT / "ddos" / "xgb_stage1.py", """
        def score(features):
            raise NotImplementedError("DDoS stage-1 XGBoost not implemented in this phase.")
    """, force)
    wrote += write(ROOT / "ddos" / "tst_stage2.py", """
        def confirm(features):
            raise NotImplementedError("DDoS stage-2 TST not implemented in this phase.")
    """, force)
    wrote += write(ROOT / "ddos" / "mitigations.py", """
        def apply(action):
            raise NotImplementedError("DDoS mitigations controlled by RL/ops; not implemented yet.")
    """, force)

    # ---------- rl stubs ----------
    wrote += write(ROOT / "rl" / "linucb.py", """
        class LinUCB:
            def __init__(self, *_, **__): raise NotImplementedError("RL is out of scope right now.")
    """, force)
    wrote += write(ROOT / "rl" / "agent_runtime.py", """
        def main(): raise NotImplementedError("RL runtime not implemented in this phase.")
        if __name__ == "__main__": main()
    """, force)
    wrote += write(ROOT / "rl" / "safety.py", """
        def guard(action, mission): raise NotImplementedError("RL safety shield not implemented in this phase.")
    """, force)

    # ---------- tools ----------
    wrote += write(ROOT / "tools" / "bench_cli.py", """
        import os, time
        from core.aead import Sender, Receiver
        from core.suites import header_ids_for_suite, AeadIds
        from core.config import CONFIG
        import os as _os
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
    """, force)
    wrote += write(ROOT / "tools" / "power_hooks.py", """
        # Placeholder for energy measurements; intentionally empty to avoid fake data.
        class PowerHook:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def sample(self): return {}
    """, force)
    wrote += write(ROOT / "tools" / "wireshark" / "pqc_tunnel.lua", """
        -- Minimal skeleton dissector (header-only) for dev convenience.
        local p = Proto("pqctun","PQC Tunnel")
        local f_version = ProtoField.uint8("pqctun.version","version", base.DEC)
        local f_kem_id  = ProtoField.uint8("pqctun.kem_id","kem_id", base.DEC)
        local f_kem_prm = ProtoField.uint8("pqctun.kem_param","kem_param", base.DEC)
        local f_sig_id  = ProtoField.uint8("pqctun.sig_id","sig_id", base.DEC)
        local f_sig_prm = ProtoField.uint8("pqctun.sig_param","sig_param", base.DEC)
        local f_sid     = ProtoField.bytes("pqctun.session_id","session_id")
        local f_seq     = ProtoField.uint64("pqctun.seq","seq", base.DEC)
        local f_epoch   = ProtoField.uint8("pqctun.epoch","epoch", base.DEC)
        p.fields = {f_version,f_kem_id,f_kem_prm,f_sig_id,f_sig_prm,f_sid,f_seq,f_epoch}
        function p.dissector(buf,pkt,tree)
          if buf:len() < 1+1+1+1+1+8+8+1 then return end
          local t = tree:add(p, buf(0))
          local o=0
          t:add(f_version, buf(o,1)); o=o+1
          t:add(f_kem_id,  buf(o,1)); o=o+1
          t:add(f_kem_prm, buf(o,1)); o=o+1
          t:add(f_sig_id,  buf(o,1)); o=o+1
          t:add(f_sig_prm, buf(o,1)); o=o+1
          t:add(f_sid,     buf(o,8)); o=o+8
          t:add(f_seq,     buf(o,8)); o=o+8
          t:add(f_epoch,   buf(o,1)); o=o+1
        end
        local udp_table = DissectorTable.get("udp.port")
        -- you can: udp_table:add(5810, p) etc.
    """, force)

    # ---------- benchmarks ----------
    wrote += write(ROOT / "benchmarks" / "matrix.yaml", """
        defaults:
          payloads: [64,256,512,1024]
          suites:
            - cs-kyber768-aesgcm-dilithium3
            - cs-kyber512-aesgcm-dilithium2
            - cs-kyber1024-aesgcm-dilithium5
    """, force)
    wrote += write(ROOT / "benchmarks" / "run_matrix.py", """
        def main():
            raise NotImplementedError("Bench harness will be added later; keeping repo honest.")
        if __name__=="__main__": main()
    """, force)

    # ---------- tests: add placeholder for loss/dup/oom (skipped) ----------
    wrote += write(ROOT / "tests" / "test_loss_dup_oom.py", """
        import pytest
        @pytest.mark.skip(reason="Placeholder; to be implemented when netem/backpressure harness is added.")
        def test_loss_dup_oom():
            pass
    """, force)

    # ---------- docs placeholder folder ----------
    wrote += write(ROOT / "docs" / "README.md", """
        This folder will host consolidated Markdown docs migrated from the top-level .txt design notes.
        Keep core/ as the single source of truth for crypto & transport; update docs when the wire changes.
    """, force)

    # ---------- environment.yml skeleton (optional) ----------
    wrote += write(ROOT / "environment.yml", """
        name: pqc-env
        channels: [conda-forge, defaults]
        dependencies:
          - python>=3.10
          - pip
          - pip:
              - cryptography>=41
              - oqs-python
              - pytest
    """, force)

    print(f"\nDone. Created/updated ~{wrote} files.")
    print("Launch examples:\n  python gcs/wrappers/gcs_kyber_768.py\n  python drone/wrappers/drone_kyber_768.py")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args()
    sys.exit(main(force=args.force) or 0)
