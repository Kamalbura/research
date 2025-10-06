"""
AEAD framing for PQC drone-GCS secure proxy.

Provides authenticated encryption (AES-256-GCM) with wire header bound as AAD,
deterministic 96-bit counter IVs, sliding replay window, and epoch support for rekeys.
"""

import struct
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from .config import CONFIG
from .suites import header_ids_for_suite


# Exception types
class HeaderMismatch(Exception):
    """Header validation failed (version, IDs, or session_id mismatch)."""
    pass


class AeadAuthError(Exception):
    """AEAD authentication failed during decryption."""
    pass


class ReplayError(Exception):
    """Packet replay detected or outside acceptable window."""
    pass


class SequenceOverflow(Exception):
    """Sender sequence space exhausted for current epoch."""
    pass


# Constants
HEADER_STRUCT = "!BBBBB8sQB"
HEADER_LEN = 22
# IV is still logically 12 bytes (1 epoch + 11 seq bytes) but is NO LONGER transmitted on wire.
# Wire format: header(22) || ciphertext+tag
IV_LEN = 0  # length of IV bytes present on wire (0 after optimization)


@dataclass(frozen=True)
class AeadIds:
    kem_id: int
    kem_param: int
    sig_id: int
    sig_param: int

    def __post_init__(self):
        for field_name, value in [("kem_id", self.kem_id), ("kem_param", self.kem_param), 
                                  ("sig_id", self.sig_id), ("sig_param", self.sig_param)]:
            if not isinstance(value, int) or not (0 <= value <= 255):
                raise NotImplementedError(f"{field_name} must be int in range 0-255")


@dataclass
class Sender:
    version: int
    ids: AeadIds
    session_id: bytes
    epoch: int
    key_send: bytes
    _seq: int = 0

    def __post_init__(self):
        if not isinstance(self.version, int) or self.version != CONFIG["WIRE_VERSION"]:
            raise NotImplementedError(f"version must equal CONFIG WIRE_VERSION ({CONFIG['WIRE_VERSION']})")
        
        if not isinstance(self.ids, AeadIds):
            raise NotImplementedError("ids must be AeadIds instance")
        
        if not isinstance(self.session_id, bytes) or len(self.session_id) != 8:
            raise NotImplementedError("session_id must be exactly 8 bytes")
        
        if not isinstance(self.epoch, int) or not (0 <= self.epoch <= 255):
            raise NotImplementedError("epoch must be int in range 0-255")
        
        if not isinstance(self.key_send, bytes) or len(self.key_send) != 32:
            raise NotImplementedError("key_send must be exactly 32 bytes")
        
        if not isinstance(self._seq, int) or self._seq < 0:
            raise NotImplementedError("_seq must be non-negative int")
        
        self._aesgcm = AESGCM(self.key_send)

    @property
    def seq(self):
        """Current sequence number."""
        return self._seq

    def pack_header(self, seq: int) -> bytes:
        """Pack header with given sequence number."""
        if not isinstance(seq, int) or seq < 0:
            raise NotImplementedError("seq must be non-negative int")
        
        return struct.pack(
            HEADER_STRUCT,
            self.version,
            self.ids.kem_id,
            self.ids.kem_param, 
            self.ids.sig_id,
            self.ids.sig_param,
            self.session_id,
            seq,
            self.epoch
        )

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext returning: header || ciphertext + tag.

        Deterministic IV (epoch||seq) is derived locally and NOT sent on wire to
        reduce overhead (saves 12 bytes per packet). Receiver reconstructs it.
        """
        if not isinstance(plaintext, bytes):
            raise NotImplementedError("plaintext must be bytes")
        
        # Check for sequence overflow - header uses uint64, so check that limit
        # Bug #6 fix: Allow full uint64 range (0 to 2^64-1)
        if self._seq >= 2**64:
            raise SequenceOverflow("packet_seq overflow; rekey/epoch bump required")
        
        # Pack header with current sequence
        header = self.pack_header(self._seq)
        
        # Derive deterministic IV = epoch (1 byte) || seq (11 bytes)
        iv = bytes([self.epoch & 0xFF]) + self._seq.to_bytes(11, "big")

        try:
            ciphertext = self._aesgcm.encrypt(iv, plaintext, header)
        except Exception as e:
            raise NotImplementedError(f"AEAD encryption failed: {e}")
        
        # Increment sequence on success
        self._seq += 1
        
        # Return optimized wire format: header || ciphertext+tag (IV omitted)
        return header + ciphertext

    def bump_epoch(self) -> None:
        """Increase epoch and reset sequence.

        Safety policy: forbid wrapping 255->0 with the same key to avoid IV reuse.
        Callers should perform a new handshake to rotate keys before wrap.
        """
        if self.epoch == 255:
            raise NotImplementedError("epoch wrap forbidden without rekey; perform handshake to rotate keys")
        self.epoch += 1
        self._seq = 0


@dataclass
class Receiver:
    version: int
    ids: AeadIds
    session_id: bytes
    epoch: int
    key_recv: bytes
    window: int
    strict_mode: bool = False  # True = raise exceptions, False = return None
    _high: int = -1
    _mask: int = 0

    def __post_init__(self):
        if not isinstance(self.version, int) or self.version != CONFIG["WIRE_VERSION"]:
            raise NotImplementedError(f"version must equal CONFIG WIRE_VERSION ({CONFIG['WIRE_VERSION']})")
        
        if not isinstance(self.ids, AeadIds):
            raise NotImplementedError("ids must be AeadIds instance")
        
        if not isinstance(self.session_id, bytes) or len(self.session_id) != 8:
            raise NotImplementedError("session_id must be exactly 8 bytes")
        
        if not isinstance(self.epoch, int) or not (0 <= self.epoch <= 255):
            raise NotImplementedError("epoch must be int in range 0-255")
        
        if not isinstance(self.key_recv, bytes) or len(self.key_recv) != 32:
            raise NotImplementedError("key_recv must be exactly 32 bytes")
        
        if not isinstance(self.window, int) or self.window < 64:
            raise NotImplementedError(f"window must be int >= 64")
        
        if not isinstance(self._high, int):
            raise NotImplementedError("_high must be int")
        
        if not isinstance(self._mask, int) or self._mask < 0:
            raise NotImplementedError("_mask must be non-negative int")
        
        self._aesgcm = AESGCM(self.key_recv)
        self._last_error: Optional[str] = None

    def _check_replay(self, seq: int) -> None:
        """Check if sequence number should be accepted (anti-replay)."""
        if seq > self._high:
            # Future packet - shift window forward
            shift = seq - self._high
            if shift >= self.window:
                # Window completely shifts
                self._mask = 1  # Only mark the current position
            else:
                # Partial shift
                self._mask = (self._mask << shift) | 1
                # Mask to window size to prevent overflow
                self._mask &= (1 << self.window) - 1
            self._high = seq
        elif seq > self._high - self.window:
            # Within window - check if already seen
            offset = self._high - seq
            bit_pos = offset
            if self._mask & (1 << bit_pos):
                raise ReplayError(f"duplicate packet seq={seq}")
            # Mark as seen
            self._mask |= (1 << bit_pos)
        else:
            # Too old - outside window
            raise ReplayError(f"packet too old seq={seq}, high={self._high}, window={self.window}")

    def decrypt(self, wire: bytes) -> bytes:
        """Validate header, perform anti-replay, reconstruct IV, decrypt.

        Returns plaintext bytes or None (silent mode) on failure.
        """
        if not isinstance(wire, bytes):
            raise NotImplementedError("wire must be bytes")
        
        if len(wire) < HEADER_LEN:
            raise NotImplementedError("wire too short for header")
        
        # Extract header
        header = wire[:HEADER_LEN]
        
        # Unpack and validate header
        try:
            fields = struct.unpack(HEADER_STRUCT, header)
            version, kem_id, kem_param, sig_id, sig_param, session_id, seq, epoch = fields
        except struct.error as e:
            raise NotImplementedError(f"header unpack failed: {e}")
        
        # Validate header fields
        if version != self.version:
            self._last_error = "header"
            if self.strict_mode:
                raise HeaderMismatch(f"version mismatch: expected {self.version}, got {version}")
            return None
        
        if (kem_id, kem_param, sig_id, sig_param) != (self.ids.kem_id, self.ids.kem_param, self.ids.sig_id, self.ids.sig_param):
            self._last_error = "header"
            if self.strict_mode:
                raise HeaderMismatch(f"crypto ID mismatch")
            return None
        
        if session_id != self.session_id:
            self._last_error = "session"
            return None  # Wrong session - always fail silently for security
        
        if epoch != self.epoch:
            self._last_error = "session"
            return None  # Wrong epoch - always fail silently for rekeying
        
        # Check replay protection
        try:
            self._check_replay(seq)
        except ReplayError:
            self._last_error = "replay"
            if self.strict_mode:
                raise
            return None
        
        # Reconstruct deterministic IV instead of reading from wire
        iv = bytes([epoch & 0xFF]) + seq.to_bytes(11, "big")
        ciphertext = wire[HEADER_LEN:]
        
        # Decrypt with header as AAD
        try:
            plaintext = self._aesgcm.decrypt(iv, ciphertext, header)
        except InvalidTag:
            self._last_error = "auth"
            if self.strict_mode:
                raise AeadAuthError("AEAD authentication failed")
            return None
        except Exception as e:
            raise NotImplementedError(f"AEAD decryption failed: {e}")
        self._last_error = None
        return plaintext

    def reset_replay(self) -> None:
        """Clear replay protection state."""
        self._high = -1
        self._mask = 0

    def bump_epoch(self) -> None:
        """Increase epoch and reset replay state.
        
        Safety policy: forbid wrapping 255->0 with the same key to avoid IV reuse.
        Callers should perform a new handshake to rotate keys before wrap.
        """
        if self.epoch == 255:
            raise NotImplementedError("epoch wrap forbidden without rekey; perform handshake to rotate keys")
        self.epoch += 1
        self.reset_replay()

    def last_error_reason(self) -> Optional[str]:
        return getattr(self, "_last_error", None)