"""
No-arg runner entry point for wrappers.

Provides a thin interface that loads configuration and suite definitions
but deliberately requires key material injection from external sources.
"""

from core.config import CONFIG
from core.suites import get_suite
from core.async_proxy import run_proxy


def start(role: str, suite_id: str):
    """
    Thin, no-args entrypoint. Loads suite & CONFIG. Does NOT guess keys/hosts.
    - For GCS: raises NotImplementedError unless signing secret is provided by the caller later (wrappers will inject).
    - For Drone: raises NotImplementedError unless pinned GCS public key is provided by the caller later (wrappers will inject).
    """
    suite = get_suite(suite_id)
    # Here we deliberately do NOT fabricate keys. Wrappers/systemd/CLI must supply them.
    raise NotImplementedError("start(role, suite_id) requires key material injection; use core/run_proxy.py for testing")