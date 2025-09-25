import pytest
from oqs.oqs import Signature
from core.handshake import build_server_hello, parse_and_verify_server_hello, HandshakeVerifyError, HandshakeFormatError
from core.suites import get_suite
from core.config import CONFIG


def test_version_mismatch_signed_transcript_blocks_downgrade():
    suite_id = "cs-kyber768-aesgcm-dilithium3"
    suite = get_suite(suite_id)
    sig = Signature(suite["sig_name"])
    pub = sig.generate_keypair()

    # Build a valid server hello
    wire, _ = build_server_hello(suite_id, sig)

    # Tamper with first byte (version) AFTER signing; should cause format error before signature verify
    tampered = bytearray(wire)
    tampered[0] ^= 0x01  # flip version bit

    # parse with expected version; should raise format error
    with pytest.raises(HandshakeFormatError):
        parse_and_verify_server_hello(bytes(tampered), CONFIG["WIRE_VERSION"], pub)

    # Now try calling parser with the tampered version as expected_version (simulate downgrade attempt)
    # Because transcript included original version, signature must fail.
    expected_tampered_version = tampered[0]
    if expected_tampered_version == CONFIG["WIRE_VERSION"]:
        pytest.skip("Tamper did not change version byte enough to test downgrade")
    with pytest.raises(HandshakeVerifyError):
        parse_and_verify_server_hello(bytes(tampered), expected_tampered_version, pub)
