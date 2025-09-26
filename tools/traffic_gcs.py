"""CLI entry point for the GCS traffic generator."""
from __future__ import annotations

import sys

from tools.traffic_runner import run


if __name__ == "__main__":
    sys.exit(run("gcs"))
*** End of File***