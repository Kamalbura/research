
import sys
def check_module(modname):
    try:
        mod = __import__(modname)
        print(f"{modname} imported from:", mod.__file__)
        print(f"{modname} attributes:", dir(mod))
        # List available algorithms
        if hasattr(mod, "get_enabled_kems"):
            print("Available KEMs:", mod.get_enabled_kems())
        if hasattr(mod, "get_enabled_sigs"):
            print("Available Sigs:", mod.get_enabled_sigs())
        # Try to instantiate KEM and Signature if present
        kem_ok = hasattr(mod, "KeyEncapsulation")
        sig_ok = hasattr(mod, "Signature")
        print("KeyEncapsulation available:", kem_ok)
        print("Signature available:", sig_ok)
        if kem_ok:
            try:
                kem = mod.KeyEncapsulation("Kyber512")
                print("KEM Kyber512 instantiated successfully.")
            except Exception as e:
                print("KEM instantiation error:", e)
        if sig_ok:
            try:
                sig = mod.Signature("Dilithium2")
                print("Signature Dilithium2 instantiated successfully.")
            except Exception as e:
                print("Signature instantiation error:", e)
    except Exception as e:
        print(f"{modname} import error:", e)

def try_import_all():
    modules = ["oqs.oqs", "liboqs", "oqs"]
    for modname in modules:
        try:
            mod = __import__(modname, fromlist=["*"])
            print(f"Imported {modname} from {getattr(mod, '__file__', 'builtin')}")
            print(f"Attributes in {modname}: {dir(mod)}")
            # List available algorithms if present
            if hasattr(mod, "get_enabled_kems"):
                print("Available KEMs:", mod.get_enabled_kems())
            if hasattr(mod, "get_enabled_sigs"):
                print("Available Sigs:", mod.get_enabled_sigs())
            # Try to instantiate KEM and Signature if present
            kem_ok = hasattr(mod, "KeyEncapsulation")
            sig_ok = hasattr(mod, "Signature")
            print("KeyEncapsulation available:", kem_ok)
            print("Signature available:", sig_ok)
            if kem_ok:
                try:
                    kem = mod.KeyEncapsulation("Kyber512")
                    print("KEM Kyber512 instantiated successfully.")
                except Exception as e:
                    print("KEM instantiation error:", e)
            if sig_ok:
                try:
                    sig = mod.Signature("Dilithium2")
                    print("Signature Dilithium2 instantiated successfully.")
                except Exception as e:
                    print("Signature instantiation error:", e)
        except Exception as e:
            print(f"Could not import {modname}: {e}")

try_import_all()
