"""Small socket helpers to ensure sockets are closed on process exit or signals.

Provides open_udp_socket(...) which registers the socket for atexit and signal-driven
cleanup. Designed to be low-risk and dependency-free.
"""
from __future__ import annotations

import atexit
import signal
import socket
from typing import List, Optional

# Global registry of sockets to close on exit
_REG_SOCKS: List[socket.socket] = []


def _close_registered() -> None:
    # Close all sockets that are still open
    for s in list(_REG_SOCKS):
        try:
            s.close()
        except Exception:
            pass
    _REG_SOCKS.clear()


# Register atexit cleanup and signal handlers (best-effort)
atexit.register(_close_registered)


def _signal_handler(signum, frame):
    # Best-effort: close sockets and continue shutdown
    _close_registered()


# Install handlers for common signals where available
try:
    signal.signal(signal.SIGINT, _signal_handler)
except Exception:
    pass

try:
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, _signal_handler)
except Exception:
    pass


def open_udp_socket(host: str, port: int, timeout: Optional[float] = None, reuseaddr: bool = True) -> socket.socket:
    """Create, bind and return a UDP socket and register it for cleanup.

    The returned socket is ready to use. Close it with close_socket(sock) when
    you no longer need it to avoid relying on atexit cleanup.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if reuseaddr:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except Exception:
                pass
        s.bind((host, port))
        if timeout is not None:
            s.settimeout(timeout)
        _REG_SOCKS.append(s)
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        raise


def close_socket(s: socket.socket) -> None:
    """Close socket and unregister it from the cleanup list."""
    try:
        if s in _REG_SOCKS:
            _REG_SOCKS.remove(s)
    except Exception:
        pass
    try:
        s.close()
    except Exception:
        pass
