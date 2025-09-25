"""PQC cryptographic suite registry and algorithm ID mapping.

Provides a composable {KEM × AEAD × SIG} registry with synonym resolution and
helpers for querying oqs availability.
"""

from __future__ import annotations

from itertools import product
from types import MappingProxyType
from typing import Dict, Iterable, Tuple


def _normalize_alias(value: str) -> str:
    """Normalize alias strings for case- and punctuation-insensitive matching."""

    return "".join(ch for ch in value.lower() if ch.isalnum())


_KEM_REGISTRY = {
    "mlkem512": {
        "oqs_name": "ML-KEM-512",
        "token": "mlkem512",
        "nist_level": "L1",
        "kem_id": 1,
        "kem_param_id": 1,
        "aliases": (
            "ML-KEM-512",
            "ml-kem-512",
            "mlkem512",
            "kyber512",
            "kyber-512",
            "kyber_512",
        ),
    },
    "mlkem768": {
        "oqs_name": "ML-KEM-768",
        "token": "mlkem768",
        "nist_level": "L3",
        "kem_id": 1,
        "kem_param_id": 2,
        "aliases": (
            "ML-KEM-768",
            "ml-kem-768",
            "mlkem768",
            "kyber768",
            "kyber-768",
            "kyber_768",
        ),
    },
    "mlkem1024": {
        "oqs_name": "ML-KEM-1024",
        "token": "mlkem1024",
        "nist_level": "L5",
        "kem_id": 1,
        "kem_param_id": 3,
        "aliases": (
            "ML-KEM-1024",
            "ml-kem-1024",
            "mlkem1024",
            "kyber1024",
            "kyber-1024",
            "kyber_1024",
        ),
    },
}


_SIG_REGISTRY = {
    "mldsa44": {
        "oqs_name": "ML-DSA-44",
        "token": "mldsa44",
        "sig_id": 1,
        "sig_param_id": 1,
        "aliases": (
            "ML-DSA-44",
            "ml-dsa-44",
            "mldsa44",
            "dilithium2",
            "dilithium-2",
        ),
    },
    "mldsa65": {
        "oqs_name": "ML-DSA-65",
        "token": "mldsa65",
        "sig_id": 1,
        "sig_param_id": 2,
        "aliases": (
            "ML-DSA-65",
            "ml-dsa-65",
            "mldsa65",
            "dilithium3",
            "dilithium-3",
        ),
    },
    "mldsa87": {
        "oqs_name": "ML-DSA-87",
        "token": "mldsa87",
        "sig_id": 1,
        "sig_param_id": 3,
        "aliases": (
            "ML-DSA-87",
            "ml-dsa-87",
            "mldsa87",
            "dilithium5",
            "dilithium-5",
        ),
    },
    "falcon512": {
        "oqs_name": "Falcon-512",
        "token": "falcon512",
        "sig_id": 2,
        "sig_param_id": 1,
        "aliases": (
            "Falcon-512",
            "falcon512",
            "falcon-512",
        ),
    },
    "falcon1024": {
        "oqs_name": "Falcon-1024",
        "token": "falcon1024",
        "sig_id": 2,
        "sig_param_id": 2,
        "aliases": (
            "Falcon-1024",
            "falcon1024",
            "falcon-1024",
        ),
    },
    "sphincs128fsha2": {
        "oqs_name": "SLH-DSA-SHA2-128f",
        "token": "sphincs128fsha2",
        "sig_id": 3,
        "sig_param_id": 1,
        "aliases": (
            "SLH-DSA-SHA2-128f",
            "sphincs+-sha2-128f-simple",
            "sphincs128fsha2",
            "sphincs128f_sha2",
        ),
    },
    "sphincs256fsha2": {
        "oqs_name": "SLH-DSA-SHA2-256f",
        "token": "sphincs256fsha2",
        "sig_id": 3,
        "sig_param_id": 2,
        "aliases": (
            "SLH-DSA-SHA2-256f",
            "sphincs+-sha2-256f-simple",
            "sphincs256fsha2",
            "sphincs256f_sha2",
        ),
    },
}


_AEAD_REGISTRY = {
    "aesgcm": {
        "display_name": "AES-256-GCM",
        "token": "aesgcm",
        "kdf": "HKDF-SHA256",
        "aliases": (
            "AES-256-GCM",
            "aes-256-gcm",
            "aesgcm",
            "aes256gcm",
            "aes-gcm",
        ),
    },
}


def _build_alias_map(registry: Dict[str, Dict]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for key, entry in registry.items():
        for alias in entry["aliases"]:
            normalized = _normalize_alias(alias)
            alias_map[normalized] = key
        alias_map[_normalize_alias(entry["oqs_name"]) if "oqs_name" in entry else _normalize_alias(entry["display_name"])] = key
        alias_map[_normalize_alias(entry["token"])] = key
    return alias_map


_KEM_ALIASES = _build_alias_map(_KEM_REGISTRY)
_SIG_ALIASES = _build_alias_map(_SIG_REGISTRY)
_AEAD_ALIASES = _build_alias_map(_AEAD_REGISTRY)


def _resolve_kem_key(name: str) -> str:
    lookup = _KEM_ALIASES.get(_normalize_alias(name))
    if lookup is None:
        raise NotImplementedError(f"unknown KEM: {name}")
    return lookup


def _resolve_sig_key(name: str) -> str:
    lookup = _SIG_ALIASES.get(_normalize_alias(name))
    if lookup is None:
        raise NotImplementedError(f"unknown signature: {name}")
    return lookup


def _resolve_aead_key(name: str) -> str:
    lookup = _AEAD_ALIASES.get(_normalize_alias(name))
    if lookup is None:
        raise NotImplementedError(f"unknown AEAD: {name}")
    return lookup


def build_suite_id(kem: str, aead: str, sig: str) -> str:
    """Build canonical suite identifier from component aliases."""

    kem_key = _resolve_kem_key(kem)
    aead_key = _resolve_aead_key(aead)
    sig_key = _resolve_sig_key(sig)

    kem_entry = _KEM_REGISTRY[kem_key]
    aead_entry = _AEAD_REGISTRY[aead_key]
    sig_entry = _SIG_REGISTRY[sig_key]

    return f"cs-{kem_entry['token']}-{aead_entry['token']}-{sig_entry['token']}"


_LEGACY_SUITE_ALIASES: Tuple[Tuple[str, str, str], ...] = (
    ("ML-KEM-512", "AES-256-GCM", "ML-DSA-44"),
    ("ML-KEM-768", "AES-256-GCM", "ML-DSA-65"),
    ("ML-KEM-1024", "AES-256-GCM", "ML-DSA-87"),
    ("ML-KEM-768", "AES-256-GCM", "Falcon-512"),
    ("ML-KEM-1024", "AES-256-GCM", "Falcon-1024"),
    ("ML-KEM-512", "AES-256-GCM", "SLH-DSA-SHA2-128f"),
    ("ML-KEM-1024", "AES-256-GCM", "SLH-DSA-SHA2-256f"),
)


_SUITE_ALIASES = {
    legacy_id: build_suite_id(*components)
    for legacy_id, components in {
        "cs-kyber512-aesgcm-dilithium2": _LEGACY_SUITE_ALIASES[0],
        "cs-kyber768-aesgcm-dilithium3": _LEGACY_SUITE_ALIASES[1],
        "cs-kyber1024-aesgcm-dilithium5": _LEGACY_SUITE_ALIASES[2],
        "cs-kyber768-aesgcm-falcon512": _LEGACY_SUITE_ALIASES[3],
        "cs-kyber1024-aesgcm-falcon1024": _LEGACY_SUITE_ALIASES[4],
        "cs-kyber512-aesgcm-sphincs128f_sha2": _LEGACY_SUITE_ALIASES[5],
        "cs-kyber1024-aesgcm-sphincs256f_sha2": _LEGACY_SUITE_ALIASES[6],
    }.items()
}


def _compose_suite(kem_key: str, aead_key: str, sig_key: str) -> Dict[str, object]:
    kem_entry = _KEM_REGISTRY[kem_key]
    aead_entry = _AEAD_REGISTRY[aead_key]
    sig_entry = _SIG_REGISTRY[sig_key]

    suite_id = f"cs-{kem_entry['token']}-{aead_entry['token']}-{sig_entry['token']}"

    return {
        "suite_id": suite_id,
        "kem_name": kem_entry["oqs_name"],
        "kem_id": kem_entry["kem_id"],
        "kem_param_id": kem_entry["kem_param_id"],
        "sig_name": sig_entry["oqs_name"],
        "sig_id": sig_entry["sig_id"],
        "sig_param_id": sig_entry["sig_param_id"],
        "nist_level": kem_entry["nist_level"],
        "aead": aead_entry["display_name"],
        "kdf": aead_entry["kdf"],
    }


def _canonicalize_suite_id(suite_id: str) -> str:
    if not suite_id:
        raise NotImplementedError("suite_id cannot be empty")

    candidate = suite_id.strip()
    if candidate in _SUITE_ALIASES:
        return _SUITE_ALIASES[candidate]

    if not candidate.startswith("cs-"):
        raise NotImplementedError(f"unknown suite_id: {suite_id}")

    parts = candidate[3:].split("-")
    if len(parts) < 3:
        raise NotImplementedError(f"unknown suite_id: {suite_id}")

    kem_part = parts[0]
    aead_part = parts[1]
    sig_part = "-".join(parts[2:])

    try:
        return build_suite_id(kem_part, aead_part, sig_part)
    except NotImplementedError as exc:
        raise NotImplementedError(f"unknown suite_id: {suite_id}") from exc


def _generate_suite_registry() -> MappingProxyType:
    suites: Dict[str, MappingProxyType] = {}
    for kem_key, sig_key in product(_KEM_REGISTRY.keys(), _SIG_REGISTRY.keys()):
        suite_dict = _compose_suite(kem_key, "aesgcm", sig_key)
        suites[suite_dict["suite_id"]] = MappingProxyType(suite_dict)
    return MappingProxyType(suites)


SUITES = _generate_suite_registry()


def list_suites() -> Dict[str, Dict]:
    """Return all available suites as immutable mapping."""

    return {suite_id: dict(config) for suite_id, config in SUITES.items()}


def get_suite(suite_id: str) -> Dict:
    """Get suite configuration by ID, resolving legacy aliases and synonyms."""

    canonical_id = _canonicalize_suite_id(suite_id)

    if canonical_id not in SUITES:
        raise NotImplementedError(f"unknown suite_id: {suite_id}")

    suite = SUITES[canonical_id]

    required_fields = {"kem_name", "sig_name", "aead", "kdf", "nist_level"}
    missing_fields = required_fields - set(suite.keys())
    if missing_fields:
        raise NotImplementedError(f"malformed suite {suite_id}: missing fields {missing_fields}")

    return dict(suite)


def _safe_get_enabled_kem_mechanisms() -> Iterable[str]:
    from oqs.oqs import get_enabled_KEM_mechanisms

    return get_enabled_KEM_mechanisms()


def _safe_get_enabled_sig_mechanisms() -> Iterable[str]:
    from oqs.oqs import get_enabled_sig_mechanisms

    return get_enabled_sig_mechanisms()


def enabled_kems() -> Tuple[str, ...]:
    """Return tuple of oqs KEM mechanism names supported by the runtime."""

    mechanisms = {_normalize_alias(name) for name in _safe_get_enabled_kem_mechanisms()}
    result = [
        entry["oqs_name"]
        for entry in _KEM_REGISTRY.values()
        if _normalize_alias(entry["oqs_name"]) in mechanisms
    ]
    return tuple(result)


def enabled_sigs() -> Tuple[str, ...]:
    """Return tuple of oqs signature mechanism names supported by the runtime."""

    mechanisms = {_normalize_alias(name) for name in _safe_get_enabled_sig_mechanisms()}
    result = [
        entry["oqs_name"]
        for entry in _SIG_REGISTRY.values()
        if _normalize_alias(entry["oqs_name"]) in mechanisms
    ]
    return tuple(result)


def header_ids_for_suite(suite: Dict) -> Tuple[int, int, int, int]:
    """Return embedded header ID bytes for provided suite dict copy."""

    try:
        return (
            suite["kem_id"],
            suite["kem_param_id"],
            suite["sig_id"],
            suite["sig_param_id"],
        )
    except KeyError as e:
        raise NotImplementedError(f"suite missing embedded id field: {e}")


def suite_bytes_for_hkdf(suite: Dict) -> bytes:
    """Generate deterministic bytes from suite for HKDF info parameter."""

    if "suite_id" in suite:
        return suite["suite_id"].encode("utf-8")

    try:
        suite_id = build_suite_id(suite["kem_name"], suite["aead"], suite["sig_name"])
    except (KeyError, NotImplementedError) as exc:
        raise NotImplementedError("Suite configuration not found in registry") from exc

    return suite_id.encode("utf-8")