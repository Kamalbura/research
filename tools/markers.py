"""Marker sink implementations for external power instrumentation.

Used by benchmark harnesses to emit precise START/END markers that align with
external power meters or logging systems.
"""

from __future__ import annotations

from typing import Protocol
import socket


class MarkerSink(Protocol):
    """Protocol for marker sinks used to signal run boundaries."""

    def start(self, run_id: str, t_wall_ns: int) -> None:
        """Emit a run start marker."""

    def end(self, run_id: str, t_wall_ns: int) -> None:
        """Emit a run end marker."""

    def close(self) -> None:  # pragma: no cover - optional hook
        """Optional resource cleanup."""


class NullMarker:
    """Marker sink that discards all events."""

    def start(self, run_id: str, t_wall_ns: int) -> None:  # pragma: no cover - trivial
        return

    def end(self, run_id: str, t_wall_ns: int) -> None:  # pragma: no cover - trivial
        return

    def close(self) -> None:  # pragma: no cover - trivial
        return


class FileMarker:
    """Append START/END markers to a text file."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _write(self, tag: str, run_id: str, t_wall_ns: int) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(f"{tag} {run_id} {t_wall_ns}\n")

    def start(self, run_id: str, t_wall_ns: int) -> None:
        self._write("START", run_id, t_wall_ns)

    def end(self, run_id: str, t_wall_ns: int) -> None:
        self._write("END", run_id, t_wall_ns)

    def close(self) -> None:  # pragma: no cover - nothing persistent
        return


class SerialMarker:
    """Write markers to a serial port.

    Requires ``pyserial`` to be installed in the environment.
    """

    def __init__(self, port: str, baud: int = 115_200) -> None:
        import serial  # type: ignore

        self._serial = serial.Serial(port=port, baudrate=baud, timeout=1)

    def _send(self, payload: str) -> None:
        self._serial.write(f"{payload}\n".encode("ascii"))
        self._serial.flush()

    def start(self, run_id: str, t_wall_ns: int) -> None:
        self._send(f"START {run_id} {t_wall_ns}")

    def end(self, run_id: str, t_wall_ns: int) -> None:
        self._send(f"END {run_id} {t_wall_ns}")

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:  # pragma: no cover - best effort cleanup
            pass


class UdpMarker:
    """Send markers over UDP to a remote host."""

    def __init__(self, host_port: str) -> None:
        host, port_str = host_port.split(":", 1)
        self.addr = (host, int(port_str))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _send(self, payload: str) -> None:
        self.sock.sendto(payload.encode("ascii"), self.addr)

    def start(self, run_id: str, t_wall_ns: int) -> None:
        self._send(f"START {run_id} {t_wall_ns}")

    def end(self, run_id: str, t_wall_ns: int) -> None:
        self._send(f"END {run_id} {t_wall_ns}")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:  # pragma: no cover - best effort cleanup
            pass
