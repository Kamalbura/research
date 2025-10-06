"""Centralized configuration for the DDoS detection pipelines.

All user-facing scripts import values from this module. The defaults are
suitable for a Raspberry Pi 4B monitoring MAVLink traffic over UDP. Each
setting can be overridden via environment variables as documented below.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _get_env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()

def _get_env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logging.warning("Invalid float for %s=%s; using default %s", name, value, default)
        return default

def _get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logging.warning("Invalid int for %s=%s; using default %s", name, value, default)
        return default

def _get_env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    value_lower = value.strip().lower()
    if value_lower in {"1", "true", "yes", "on"}:
        return True
    if value_lower in {"0", "false", "no", "off"}:
        return False
    logging.warning("Invalid bool for %s=%s; using default %s", name, value, default)
    return default

# ---------------------------------------------------------------------------
# Network configuration
# ---------------------------------------------------------------------------

IFACE: str = _get_env_str("MAV_IFACE", "wlan0")
PORT: int = _get_env_int("MAV_UDP_PORT", 14550)

# ---------------------------------------------------------------------------
# Windowing / buffer sizes
# ---------------------------------------------------------------------------

WINDOW_SIZE: float = _get_env_float("DDOS_WINDOW_SIZE", 0.60)
XGB_SEQ_LENGTH: int = _get_env_int("DDOS_XGB_SEQ", 5)
TST_SEQ_LENGTH: int = _get_env_int("DDOS_TST_SEQ", 400)
BUFFER_SIZE: int = _get_env_int("DDOS_BUFFER_SIZE", 900)

# Gatekeeping
XGB_CONSECUTIVE_POSITIVES: int = _get_env_int("DDOS_XGB_CONSEC", 1)
TST_COOLDOWN_WINDOWS: int = _get_env_int("DDOS_TST_COOLDOWN", 5)

# Queue sizing
XGB_QUEUE_MAX: int = _get_env_int("DDOS_XGB_QUEUE_MAX", 64)
TST_QUEUE_MAX: int = _get_env_int("DDOS_TST_QUEUE_MAX", 8)

# ---------------------------------------------------------------------------
# Model paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

XGB_MODEL_FILE: Path = Path(_get_env_str("DDOS_XGB_MODEL", str(BASE_DIR / "xgboost_model.bin")))
TST_TORCHSCRIPT_FILE: Path = Path(
    _get_env_str("DDOS_TST_TORCHSCRIPT", str(BASE_DIR / "tst_model.torchscript"))
)
TST_MODEL_FILE: Path = Path(_get_env_str("DDOS_TST_MODEL", str(BASE_DIR / "tst_model.pth")))
SCALER_FILE: Path = Path(_get_env_str("DDOS_SCALER_FILE", str(BASE_DIR / "scaler.pkl")))

# Probability threshold for attack classification from TST softmax output.
TST_ATTACK_THRESHOLD: float = _get_env_float("DDOS_TST_THRESHOLD", 0.90)
TORCH_NUM_THREADS: int = _get_env_int("DDOS_TORCH_THREADS", 1)
TST_CONFIRM_POSITIVES: int = _get_env_int("DDOS_TST_CONFIRM", 2)
TST_CLEAR_THRESHOLD: float = _get_env_float("DDOS_TST_CLEAR", 0.80)

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

LOG_LEVEL_NAME: str = _get_env_str("DDOS_LOG_LEVEL", "INFO").upper()
LOG_FILE: Optional[str] = os.getenv("DDOS_LOG_FILE")


def configure_logging(program_name: str) -> None:
    """Configure the root logger.

    Parameters
    ----------
    program_name: str
        Used to differentiate loggers per script.
    """
    level = getattr(logging, LOG_LEVEL_NAME, logging.INFO)
    log_format = (
        f"{program_name} %(asctime)s %(levelname)s %(name)s "
        "%(threadName)s %(message)s"
    )

    handlers = []
    if LOG_FILE:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    else:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(level=level, format=log_format, handlers=handlers)
    logging.getLogger().name = program_name


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def ensure_file(path: Path, description: str) -> None:
    """Raise FileNotFoundError with a friendly message if path is missing."""
    if not path.exists():
        raise FileNotFoundError(f"Missing {description}: {path}")


def get_udp_bpf() -> str:
    """Return a BPF string filter for Scapy sniffing."""
    # Match both MAVLink v2 (0xFD) and v1 (0xFE) start bytes; collector threads
    # already fall back to a port-only filter if this one raises.
    return f"udp and port {PORT} and (udp[8] = 0xfd or udp[8] = 0xfe)"
