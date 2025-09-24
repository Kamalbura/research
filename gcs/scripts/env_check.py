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
