import json

import pytest

from tools import verify_crypto


def _mock_suite(suite_id: str, aead_token: str = "aesgcm") -> dict:
    return {
        "suite_id": suite_id,
        "kem_name": "ML-KEM-768",
        "sig_name": "ML-DSA-65",
        "aead_token": aead_token,
    }


@pytest.fixture
def patch_suite_registry(monkeypatch):
    """Provide helpers to monkeypatch suite registry functions."""

    def _apply(aeads=("aesgcm",), missing_aeads=None):
        missing_map = missing_aeads or {}

        monkeypatch.setattr(verify_crypto.suites_mod, "enabled_kems", lambda: ("ML-KEM-768",))
        monkeypatch.setattr(verify_crypto.suites_mod, "enabled_sigs", lambda: ("ML-DSA-65",))
        monkeypatch.setattr(verify_crypto.suites_mod, "available_aead_tokens", lambda: tuple(aeads))
        monkeypatch.setattr(verify_crypto.suites_mod, "unavailable_aead_reasons", lambda: dict(missing_map))
        monkeypatch.setattr(
            verify_crypto.suites_mod,
            "list_suites",
            lambda: {"cs-mlkem768-aesgcm-mldsa65": _mock_suite("cs-mlkem768-aesgcm-mldsa65")},
        )
        monkeypatch.setattr(
            verify_crypto.suites_mod,
            "get_suite",
            lambda suite: _mock_suite(suite, aead_token="ascon128" if "ascon" in suite else "aesgcm"),
        )

    return _apply


def test_verify_crypto_reports_missing(monkeypatch, capsys, patch_suite_registry):
    """verify_crypto should surface missing primitives and exit non-zero when strict."""
    patch_suite_registry(
        aeads=("aesgcm",),
        missing_aeads={"ascon128": "pyascon module unavailable"},
    )

    exit_code = verify_crypto.main(
        [
            "--suite",
            "cs-test-ascon128-suite",
            "--json",
            "--strict",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    finding = payload["findings"][0]
    assert finding["status"] == "missing"
    assert finding["missing"] == ["aead"]
    assert finding["details"]["aead_hint"] == "pyascon module unavailable"


def test_verify_crypto_passes_with_available_primitives(monkeypatch, capsys, patch_suite_registry):
    """verify_crypto should return success when all primitives are present."""
    patch_suite_registry()
    exit_code = verify_crypto.main(["--suite", "cs-mlkem768-aesgcm-mldsa65", "--json", "--strict"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["findings"][0]["status"] == "ok"
