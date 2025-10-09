#!/usr/bin/env python3
"""Generate a short environment report for PQC tests.

Produces a markdown report containing:
- conda list (if available)
- Python executable and version
- oqs / liboqs import info and supported/enabled mechanisms (best-effort)
- Audit of secrets/matrix: count of suites, per-suite pub/key presence and pub sha256

Usage: python tools/generate_env_report.py --out docs/env_report.md
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import sysconfig
from typing import Dict, List


def run_cmd(cmd: List[str]) -> str:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        return p.stdout
    except Exception as e:
        return f'ERROR running {cmd}: {e}'


def sha256_hex(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def probe_oqs() -> dict:
    info = {}
    info['python_executable'] = sys.executable
    info['python_version'] = platform.python_version()
    # conda env name if present
    info['conda_prefix'] = os.environ.get('CONDA_PREFIX')

    # attempt to import oqs and liboqs
    try:
        oqs = importlib.import_module('oqs')
        info['oqs_file'] = getattr(oqs, '__file__', None)
        info['oqs_dir'] = getattr(oqs, '__path__', None)
        # Try common helper functions
        for fn in ('get_supported_kem_mechanisms', 'get_supported_sig_mechanisms', 'get_enabled_kem_mechanisms', 'get_enabled_sig_mechanisms'):
            f = getattr(oqs, fn, None)
            if callable(f):
                try:
                    items = f()
                    info[fn] = list(items)
                except Exception as e:
                    info[fn] = f'ERROR calling {fn}: {e}'
            else:
                info[fn] = None
    except Exception as e:
        info['oqs_import_error'] = repr(e)

    try:
        liboqs = importlib.import_module('liboqs')
        info['liboqs_file'] = getattr(liboqs, '__file__', None)
    except Exception as e:
        info['liboqs_import_error'] = repr(e)

    return info


def collect_compiler_flags() -> Dict[str, str]:
    flags: Dict[str, str] = {}
    env_keys = [
        'CFLAGS',
        'CXXFLAGS',
        'CPPFLAGS',
        'LDFLAGS',
        'OQS_CMAKE_FLAGS',
        'OQS_CMAKE_OPTIONS',
        'OQS_OPT_FLAGS',
    ]
    for key in env_keys:
        value = os.environ.get(key)
        if value:
            flags[key] = value

    try:
        python_opt = sysconfig.get_config_var('OPT')
        if python_opt:
            flags['PYTHON_OPT'] = python_opt
    except Exception:
        pass

    try:
        python_cflags = sysconfig.get_config_var('CFLAGS')
        if python_cflags:
            flags['PYTHON_CFLAGS'] = python_cflags
    except Exception:
        pass

    return flags


def audit_secrets_matrix(root: pathlib.Path) -> dict:
    out = {}
    if not root.exists():
        out['error'] = f'{root} does not exist'
        return out
    pubs = list(sorted(root.glob('*/gcs_signing.pub')))
    out['pub_count'] = len(pubs)
    suites = []
    for pub in pubs:
        suite = pub.parent.name
        key_path = pub.parent / 'gcs_signing.key'
        pub_sha = sha256_hex(pub)
        suites.append({'suite': suite, 'pub': str(pub), 'pub_size': pub.stat().st_size, 'pub_sha256': pub_sha, 'has_key': key_path.exists(), 'key_path': str(key_path) if key_path.exists() else None})
    out['suites'] = suites
    return out


def render_markdown(info: dict, secrets: dict, conda_list_text: str, compiler_flags: Dict[str, str]) -> str:
    lines = []
    lines.append('# Environment report')
    lines.append('')
    lines.append('## Python / Conda')
    lines.append('')
    lines.append(f"- Python executable: `{info.get('python_executable')}`")
    lines.append(f"- Python version: `{info.get('python_version')}`")
    lines.append(f"- CONDA_PREFIX: `{info.get('conda_prefix')}`")
    lines.append('')
    lines.append('### Conda packages (conda list)')
    lines.append('')
    lines.append('```')
    lines.append(conda_list_text.strip())
    lines.append('```')
    lines.append('')
    lines.append('## Compiler Flags')
    lines.append('')
    if compiler_flags:
        for key, value in compiler_flags.items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append('- None detected')
    lines.append('')
    lines.append('## oqs / liboqs info')
    lines.append('')
    if 'oqs_import_error' in info:
        lines.append(f"- oqs import error: {info['oqs_import_error']}")
    else:
        lines.append(f"- oqs module file: `{info.get('oqs_file')}`")
        for fn in ('get_supported_sig_mechanisms', 'get_enabled_sig_mechanisms', 'get_supported_kem_mechanisms', 'get_enabled_kem_mechanisms'):
            val = info.get(fn)
            if val is None:
                lines.append(f"- {fn}: MISSING")
            elif isinstance(val, str) and val.startswith('ERROR'):
                lines.append(f"- {fn}: {val}")
            else:
                lines.append(f"- {fn}: {len(val)} items (showing up to 10): {val[:10]}")
    if 'liboqs_import_error' in info:
        lines.append(f"- liboqs import error: {info['liboqs_import_error']}")
    else:
        lines.append(f"- liboqs module file: `{info.get('liboqs_file')}`")

    lines.append('')
    lines.append('## secrets/matrix audit')
    lines.append('')
    lines.append(f"- pub files found: {secrets.get('pub_count',0)}")
    lines.append('')
    lines.append('| suite | pub_size | pub_sha256 | has_key |')
    lines.append('|---|---:|---|---:|')
    for s in secrets.get('suites', []):
        lines.append(f"| {s['suite']} | {s['pub_size']} | `{s['pub_sha256']}` | {s['has_key']} |")

    lines.append('')
    lines.append('Generated by `tools/generate_env_report.py`')
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out', '--out-file', dest='out', default='docs/env_report.md')
    args = p.parse_args()

    # Get conda list if available
    conda_text = run_cmd(['conda', 'list']) if shutil.which('conda') else run_cmd([sys.executable, '-m', 'pip', 'freeze'])

    info = probe_oqs()
    compiler_flags = collect_compiler_flags()
    secrets = audit_secrets_matrix(pathlib.Path('secrets') / 'matrix')
    md = render_markdown(info, secrets, conda_text, compiler_flags)

    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding='utf-8')
    print(f'Wrote report to {out_path}')


if __name__ == '__main__':
    raise SystemExit(main())
