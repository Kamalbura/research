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
