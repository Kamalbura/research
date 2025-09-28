#!/usr/bin/env python3
"""Copy gcs_signing.pub files under secrets/matrix to a remote Pi and verify sha256.

Usage:
  python tools/copy_pubs_to_pi.py --pi dev@100.101.93.23

The script will:
 - Find all secrets/matrix/*/gcs_signing.pub
 - For each one, ensure the remote directory exists (ssh user@host mkdir -p ...)
 - Copy the file with scp
 - Run sha256sum on remote and compare to local
 - Print a concise per-suite result
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shlex
import subprocess
import sys


def sha256_hex(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--pi', required=True, help='Remote user@host for the Pi (e.g. dev@100.101.93.23)')
    p.add_argument('--remote-root', default='/home/dev/research', help='Remote repo root on the Pi')
    p.add_argument('--use-sudo', action='store_true', help='Use sudo on the remote side to create dirs and move files (scp to /tmp then sudo mv)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    root = pathlib.Path('secrets') / 'matrix'
    if not root.exists():
        print('No secrets/matrix directory found in cwd', os.getcwd(), file=sys.stderr)
        return 2

    pubs = list(sorted(root.glob('*/gcs_signing.pub')))
    if not pubs:
        print('No gcs_signing.pub files found under secrets/matrix')
        return 0

    summary = []

    for pub in pubs:
        suite = pub.parent.name
        local_hex = sha256_hex(pub)
        remote_dir = f"{args.remote_root.rstrip('/')}/secrets/matrix/{suite}"
        remote_path = f"{remote_dir}/gcs_signing.pub"

        print(f'[{suite}] local: {pub} ({pub.stat().st_size} bytes) sha256={local_hex}')

        if args.dry_run:
            summary.append((suite, 'dry-run'))
            continue

        # Ensure remote dir exists. If using sudo, create with sudo (may require password).
        if args.use_sudo:
            mkdir_cmd = ['ssh', args.pi, 'sudo', 'mkdir', '-p', shlex.quote(remote_dir)]
        else:
            mkdir_cmd = ['ssh', args.pi, 'mkdir', '-p', shlex.quote(remote_dir)]
        rc = run(mkdir_cmd)
        if rc.returncode != 0:
            print(f'[{suite}] ERROR making remote dir: {rc.stderr.strip()}')
            summary.append((suite, 'mkdir-fail'))
            continue

        # Copy via scp. If use_sudo is set, copy to /tmp then sudo-move into place.
        if args.use_sudo:
            remote_tmp = f"/tmp/{suite}_gcs_signing.pub"
            scp_cmd = ['scp', str(pub), f"{args.pi}:{remote_tmp}"]
            rc = run(scp_cmd)
            if rc.returncode != 0:
                print(f'[{suite}] ERROR scp to /tmp: {rc.stderr.strip()}')
                summary.append((suite, 'scp-fail'))
                continue

            # Move into place with sudo and set ownership to remote user
            # extract username from user@host
            if '@' in args.pi:
                remote_user = args.pi.split('@', 1)[0]
            else:
                remote_user = None

            if remote_user:
                mv_cmd = ['ssh', args.pi, 'sudo', 'mv', shlex.quote(remote_tmp), shlex.quote(remote_path), '&&', 'sudo', 'chown', f"{remote_user}:{remote_user}", shlex.quote(remote_path)]
            else:
                mv_cmd = ['ssh', args.pi, 'sudo', 'mv', shlex.quote(remote_tmp), shlex.quote(remote_path)]

            rc = run(mv_cmd)
            if rc.returncode != 0:
                print(f'[{suite}] ERROR sudo-move: {rc.stderr.strip() or rc.stdout.strip()}')
                summary.append((suite, 'sudo-move-fail'))
                continue
        else:
            scp_cmd = ['scp', str(pub), f"{args.pi}:{remote_path}"]
            rc = run(scp_cmd)
            if rc.returncode != 0:
                print(f'[{suite}] ERROR scp: {rc.stderr.strip()}')
                summary.append((suite, 'scp-fail'))
                continue

        # Compute remote sha256
        sha_cmd = ['ssh', args.pi, 'sha256sum', shlex.quote(remote_path)]
        rc = run(sha_cmd)
        if rc.returncode != 0:
            print(f'[{suite}] ERROR remote sha256: {rc.stderr.strip()}')
            summary.append((suite, 'remote-sha-fail'))
            continue

        remote_out = rc.stdout.strip().split()[0]
        if remote_out == local_hex:
            print(f'[{suite}] OK (sha256 matched)')
            summary.append((suite, 'ok'))
        else:
            print(f'[{suite}] MISMATCH local={local_hex} remote={remote_out}')
            summary.append((suite, 'mismatch'))

    # Print summary
    print('\nSummary:')
    counts = {}
    for _, s in summary:
        counts[s] = counts.get(s, 0) + 1
    for k, v in counts.items():
        print(f'  {k}: {v}')

    # exit 0 if all ok or dry-run, else non-zero
    bad = [s for _, s in summary if s not in ('ok', 'dry-run')]
    return 0 if not bad else 3


if __name__ == '__main__':
    raise SystemExit(main())
