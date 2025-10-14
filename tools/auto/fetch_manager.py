"""Persistent artifact fetch manager for sessions.

Provides fetch_artifacts(session_id, remote_path, local_target, retry=2, timeout=30)
that attempts SSH/SFTP via paramiko when available, falls back to scp via subprocess.

This module intentionally keeps networking optional so tests can mock behavior.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

try:
    import paramiko  # type: ignore
except Exception:
    paramiko = None


@dataclass
class FetchResult:
    status: str
    error: Optional[str] = None
    details: Dict[str, object] = None


class FetchManager:
    def __init__(self, *, allow_remote: bool = True):
        self.allow_remote = allow_remote
        self._clients: Dict[str, object] = {}

    def _ssh_client_for(self, host: str, username: Optional[str] = None, password: Optional[str] = None):
        if not paramiko:
            return None
        key = f"{host}:{username or ''}"
        client = self._clients.get(key)
        if client:
            return client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(hostname=host, username=username, password=password, allow_agent=True, timeout=10)
            self._clients[key] = client
            return client
        except Exception:
            return None

    def fetch_artifacts(self, session_id: str, remote_path: str, local_target: str, retry: int = 2, timeout: int = 30) -> FetchResult:
        if not self.allow_remote:
            return FetchResult(status="disabled", error="remote_fetch_disabled", details={})

        start = time.time()
        attempt = 0
        last_err: Optional[str] = None
        while attempt < retry and (time.time() - start) < timeout:
            attempt += 1
            # Try paramiko SFTP first
            try:
                if paramiko:
                    # Parse host:path or user@host:path
                    if ":" in remote_path and "@" in remote_path.split(":")[0]:
                        user_host, rpath = remote_path.split(":", 1)
                        username, host = user_host.split("@", 1)
                    elif ":" in remote_path:
                        host, rpath = remote_path.split(":", 1)
                        username = None
                    else:
                        host = remote_path
                        rpath = remote_path
                        username = None

                    client = self._ssh_client_for(host, username=username)
                    if client:
                        sftp = client.open_sftp()
                        local_target_path = Path(local_target)
                        local_target_path.parent.mkdir(parents=True, exist_ok=True)
                        # Use recursive copy via get for directories is not implemented; fallback to scp
                        try:
                            sftp.get(rpath, str(local_target_path))
                            return FetchResult(status="ok", details={"method": "sftp", "attempt": attempt})
                        except Exception as exc:
                            last_err = f"sftp_get_failed:{exc}"
            except Exception as exc:
                last_err = str(exc)

            # Fallback to scp
            try:
                scp_cmd = [
                    "scp",
                    "-r",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    remote_path,
                    local_target,
                ]
                subprocess.check_call(scp_cmd, timeout=min(20, timeout))
                return FetchResult(status="ok", details={"method": "scp", "attempt": attempt})
            except subprocess.CalledProcessError as exc:
                last_err = f"scp_failed:{exc.returncode}"
            except Exception as exc:
                last_err = str(exc)

            time.sleep(0.5)

        return FetchResult(status="error", error=last_err or "timeout", details={})


_GLOBAL_FETCH_MANAGER: Optional[FetchManager] = None


def get_global_manager() -> FetchManager:
    global _GLOBAL_FETCH_MANAGER
    if _GLOBAL_FETCH_MANAGER is None:
        allow = not bool(os.getenv("SKIP_REMOTE_FETCH"))
        _GLOBAL_FETCH_MANAGER = FetchManager(allow_remote=allow)
    return _GLOBAL_FETCH_MANAGER


def fetch_artifacts(session_id: str, remote_path: str, local_target: str, **kwargs) -> Dict[str, object]:
    mgr = get_global_manager()
    res = mgr.fetch_artifacts(session_id, remote_path, local_target, **kwargs)
    return {"status": res.status, "error": res.error, "details": res.details}
