#!/usr/bin/env python3
"""Robust debug info about oqs/python and native liboqs availability.

This probes both the top-level `oqs` package and the `oqs.oqs` binding
submodule, tries several API-name variants (different capitalizations),
and prints any discovered classes and mechanism lists. Run inside your
`gcs-env` to see what the runtime exposes.
"""
import importlib
import sys
import traceback


def try_import(name):
    try:
        m = importlib.import_module(name)
        return True, m
    except Exception as e:
        return False, e


def probe_functions(mod, logical_name, variants):
    """Try variants on mod and return the first callable found and its name."""
    for var in variants:
        fn = getattr(mod, var, None)
        if callable(fn):
            return var, fn
    return None, None


def print_mech_list(fn):
    try:
        res = fn()
        try:
            size = len(res)
        except Exception:
            size = 'unknown'
        print(f"  -> {size} items")
        try:
            # show a short sample
            sample = list(res)[:10]
            print('   ', sample)
        except Exception:
            print('   (unable to list sample)')
    except Exception as e:
        print('  ERROR calling function:', e)
        traceback.print_exc()


def main():
    print('Python executable:', sys.executable)

    # Try several import points: oqs.oqs (binding) preferred, then oqs
    ok_binding, oqs_binding = try_import('oqs.oqs')
    ok_pkg, oqs_pkg = try_import('oqs')

    if not ok_binding and not ok_pkg:
        print('Could not import oqs or oqs.oqs:', oqs_pkg)
        return 2

    # Choose module to probe: binding wins if present
    oqs_mod = oqs_binding if ok_binding else oqs_pkg
    print('Probing module:', getattr(oqs_mod, '__name__', repr(oqs_mod)))
    print('Module file:', getattr(oqs_mod, '__file__', '<built-in or namespace>'))

    # Look for common classes used by the codebase
    for cls_name in ('Signature', 'KeyEncapsulation'):
        obj = getattr(oqs_mod, cls_name, None)
        print(f"\nClass {cls_name}:", 'FOUND' if obj is not None else 'MISSING')

    # Logical API names and their common variants (case differences)
    api_variants = {
        'get_enabled_kem_mechanisms': ['get_enabled_kem_mechanisms', 'get_enabled_KEM_mechanisms', 'get_enabled_KEM_mechanism', 'get_enabled_kem_mechanisms'],
        'get_enabled_sig_mechanisms': ['get_enabled_sig_mechanisms', 'get_enabled_sig_mechanism', 'get_enabled_SIG_mechanisms', 'get_enabled_sig_mechanisms'],
        'get_supported_kem_mechanisms': ['get_supported_kem_mechanisms', 'get_supported_KEM_mechanisms', 'get_supported_kem_mechanism'],
        'get_supported_sig_mechanisms': ['get_supported_sig_mechanisms', 'get_supported_SIG_mechanisms', 'get_supported_sig_mechanism'],
    }

    for logical, variants in api_variants.items():
        print('\nChecking logical API:', logical)
        name, fn = probe_functions(oqs_mod, logical, variants)
        if name is None:
            print('  NO matching function found on module; trying package-level fallback (if different)')
            # If we probed oqs.oqs, also try top-level package if available
            if oqs_mod is not oqs_pkg and ok_pkg:
                name, fn = probe_functions(oqs_pkg, logical, variants)
        if name is None:
            print('  MISSING API (no variant found)')
        else:
            print(f'  Found function name: {name}')
            print_mech_list(fn)

    # Try to import native module 'liboqs' if present
    ok_native, lib = try_import('liboqs')
    print('\nNative liboqs import:', 'OK' if ok_native else f'FAIL: {lib}')
    if ok_native:
        print('liboqs module file:', getattr(lib, '__file__', '<unknown>'))

    # Also dump a short dir() to help diagnose odd packaging
    try:
        print('\nShort dir() of probed module:')
        names = [n for n in dir(oqs_mod) if not n.startswith('_')][:80]
        print(' ', names)
    except Exception:
        pass

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
