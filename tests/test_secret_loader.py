from pathlib import Path
from typing import Optional

import pytest

from core.run_proxy import _build_matrix_secret_loader


class DummySignature:
    """Minimal stand-in for oqs.Signature used in loader tests."""

    instances = []

    def __init__(self, name: str, secret_key: Optional[bytes] = None):
        self.name = name
        self.secret_key = secret_key
        self.imported_key = None
        DummySignature.instances.append(self)

    def import_secret_key(self, secret_bytes: bytes):
        self.imported_key = secret_bytes
        return b"dummy-public"


@pytest.fixture(autouse=True)
def reset_instances():
    DummySignature.instances.clear()
    yield
    DummySignature.instances.clear()


def test_loader_returns_cached_initial_secret(tmp_path: Path):
    initial = DummySignature("sig0")
    loader = _build_matrix_secret_loader(
        suite_id="suite-a",
        default_secret_path=None,
        initial_secret=initial,
        signature_cls=DummySignature,
        matrix_dir=tmp_path,
    )

    loaded = loader({"suite_id": "suite-a", "sig_name": "sig0"})
    assert loaded is initial

    # Ensure subsequent calls reuse the cached instance without touching disk
    loaded_again = loader({"suite_id": "suite-a", "sig_name": "sig0"})
    assert loaded_again is initial


def test_loader_reads_matrix_suite_key(tmp_path: Path):
    suite_dir = tmp_path / "suite-b"
    suite_dir.mkdir(parents=True)
    secret_bytes = b"matrix-secret"
    (suite_dir / "gcs_signing.key").write_bytes(secret_bytes)

    loader = _build_matrix_secret_loader(
        suite_id="suite-a",
        default_secret_path=None,
        initial_secret=None,
        signature_cls=DummySignature,
        matrix_dir=tmp_path,
    )

    loaded = loader({"suite_id": "suite-b", "sig_name": "sigB"})
    assert isinstance(loaded, DummySignature)
    assert loaded.imported_key == secret_bytes

    # Cache hit: repeated call should yield same instance
    loaded_again = loader({"suite_id": "suite-b", "sig_name": "sigB"})
    assert loaded_again is loaded


def test_loader_raises_when_secret_missing(tmp_path: Path):
    loader = _build_matrix_secret_loader(
        suite_id="suite-a",
        default_secret_path=None,
        initial_secret=None,
        signature_cls=DummySignature,
        matrix_dir=tmp_path,
    )

    with pytest.raises(FileNotFoundError):
        loader({"suite_id": "suite-missing", "sig_name": "sigX"})
