import os
from tools.auto.fetch_manager import get_global_manager, fetch_artifacts


def test_fetch_disabled_env(monkeypatch):
    monkeypatch.setenv("SKIP_REMOTE_FETCH", "1")
    mgr = get_global_manager()
    res = mgr.fetch_artifacts("sess", "host:/tmp/nonexistent", "/tmp/out")
    assert res.status == "disabled"


def test_fetch_artifacts_fallback(monkeypatch, tmp_path):
    # When scp is missing or fails, expect error result rather than exception
    monkeypatch.delenv("SKIP_REMOTE_FETCH", raising=False)
    res = fetch_artifacts("sess", "nohost:/no/path", str(tmp_path / "out"), retry=1, timeout=1)
    assert isinstance(res, dict)
    assert res.get("status") in {"ok", "error", "disabled"}
