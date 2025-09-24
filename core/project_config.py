# Thin shim so planned path 'project_config.py' exists without breaking tests.
# Source of truth remains core/config.py
from .config import CONFIG
__all__ = ["CONFIG"]
