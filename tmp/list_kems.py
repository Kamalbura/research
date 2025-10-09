from oqs import oqs
from oqs.oqs import KeyEncapsulation, Signature
import json

rows = []
for name in oqs.get_enabled_kem_mechanisms():
    with KeyEncapsulation(name) as kem:
        details = kem.details
    rows.append({
        "name": name,
        "nist_level": details.get("claimed_nist_level"),
        "classical_security": details.get("claimed_classical_security"),
        "quantum_security": details.get("claimed_quantum_security"),
    })

rows.sort(key=lambda item: item["name"].lower())
print(json.dumps(rows, indent=2))

sig_rows = []
for name in oqs.get_enabled_sig_mechanisms():
    with Signature(name) as sig:
        details = sig.details
    sig_rows.append({
        "name": name,
        "nist_level": details.get("claimed_nist_level"),
        "classical_security": details.get("claimed_classical_security"),
        "quantum_security": details.get("claimed_quantum_security"),
    })

sig_rows.sort(key=lambda item: item["name"].lower())
print(json.dumps(sig_rows, indent=2))
