
import pytest
pytest.importorskip("oqs.oqs")
pytest.importorskip("cryptography.hazmat.primitives.kdf.hkdf")
from core.handshake import derive_transport_keys
import os

def test_key_directionality():
    for _ in range(5):
        session_id = os.urandom(8)
        kem_name = b"ML-KEM-768"
        sig_name = b"ML-DSA-65"
        shared_secret = os.urandom(32)
        cs, cr = derive_transport_keys("client", session_id, kem_name, sig_name, shared_secret)
        ss, sr = derive_transport_keys("server", session_id, kem_name, sig_name, shared_secret)
        assert cs == sr and cr == ss
        assert len(cs) == 32 and len(cr) == 32
        assert len(ss) == 32 and len(sr) == 32

def test_invalid_role():
    session_id = os.urandom(8)
    kem_name = b"ML-KEM-768"
    sig_name = b"ML-DSA-65"
    shared_secret = os.urandom(32)
    with pytest.raises(NotImplementedError):
        derive_transport_keys("invalid", session_id, kem_name, sig_name, shared_secret)

def test_invalid_session_id_length():
    kem_name = b"ML-KEM-768"
    sig_name = b"ML-DSA-65"
    shared_secret = os.urandom(32)
    with pytest.raises(NotImplementedError):
        derive_transport_keys("client", b"short", kem_name, sig_name, shared_secret)

def test_empty_kem_sig_name():
    session_id = os.urandom(8)
    shared_secret = os.urandom(32)
    with pytest.raises(NotImplementedError):
        derive_transport_keys("client", session_id, b"", b"ML-DSA-65", shared_secret)
    with pytest.raises(NotImplementedError):
        derive_transport_keys("client", session_id, b"ML-KEM-768", b"", shared_secret)
