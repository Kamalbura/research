"""
AES-GCM AEAD framing with header authentication and replay protection.

Provides Sender and Receiver classes for encrypted packet framing with:
- Header as Associated Additional Data (AAD)
- Deterministic counter-based nonces
- Sliding window replay protection
- Epoch support for rekeying
"""

import struct
from typing import Optional, Set, Dict

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

# Header format: version(1) | kem_id(1) | kem_param(1) | sig_id(1) | sig_param(1) | session_id(8) | seq(8) | epoch(1)
HDR_FMT = "!BBBBB8sQB"   # 5xB, 8s, Q, B -> 23 bytes  
HDR_LEN = struct.calcsize(HDR_FMT)
IV_LEN = 12  # AES-GCM requires 96-bit (12-byte) nonce


class Sender:
    """Encrypts and frames packets with AES-GCM.
    
    Uses header as AAD and deterministic counter nonces for security.
    """
    
    def __init__(self, key: bytes, suite: Dict, session_id: bytes, epoch: int = 0):
        """Initialize sender with encryption key and session context.
        
        Args:
            key: 32-byte AES-256 key
            suite: Suite configuration dictionary with algorithm IDs
            session_id: 8-byte session identifier
            epoch: Current epoch number for rekeying
        """
        if len(key) != 32:
            raise ValueError("Key must be 32 bytes for AES-256")
        if len(session_id) != 8:
            raise ValueError("Session ID must be 8 bytes")
            
        self.aes = AESGCM(key)
        self.suite = suite
        self.session_id = session_id
        self.seq = 0
        self.epoch = epoch
    
    def pack(self, plaintext: bytes) -> bytes:
        """Encrypt and frame a packet.
        
        Args:
            plaintext: Data to encrypt
            
        Returns:
            Framed packet: header || iv || ciphertext_with_tag
        """
        # Construct header with current sequence and epoch
        hdr = struct.pack(
            HDR_FMT,
            1,  # version
            self.suite["kem_id"], 
            self.suite["kem_param"],
            self.suite["sig_id"], 
            self.suite["sig_param"],
            self.session_id,
            self.seq,
            self.epoch
        )
        
        # Deterministic counter IV - never reuse with same key
        iv = int(self.seq).to_bytes(IV_LEN, "big")
        
        # Encrypt with header as AAD  
        ct = self.aes.encrypt(iv, plaintext, hdr)
        
        # Increment sequence counter
        self.seq += 1
        
        return hdr + iv + ct


class Receiver:
    """Decrypts and validates framed packets with replay protection.
    
    Maintains sliding window to detect replayed or out-of-order packets.
    """
    
    def __init__(self, key: bytes, window: int = 1024):
        """Initialize receiver with decryption key and replay window.
        
        Args:
            key: 32-byte AES-256 key
            window: Size of replay protection sliding window
        """
        if len(key) != 32:
            raise ValueError("Key must be 32 bytes for AES-256")
            
        self.aes = AESGCM(key)
        self.high = -1  # Highest sequence number seen
        self.window = window
        self.received: Set[tuple] = set()  # Track (session_id, seq, epoch) tuples
    
    def unpack(self, wire: bytes) -> Optional[bytes]:
        """Decrypt and validate a framed packet.
        
        Args:
            wire: Framed packet bytes
            
        Returns:
            Decrypted plaintext or None if packet invalid/replayed
        """
        # Verify minimum packet size
        min_size = HDR_LEN + IV_LEN + 16  # header + iv + minimum GCM tag
        if len(wire) < min_size:
            return None
            
        # Parse packet components
        hdr = wire[:HDR_LEN]
        iv = wire[HDR_LEN:HDR_LEN+IV_LEN]
        ct = wire[HDR_LEN+IV_LEN:]
        
        # Unpack header fields
        try:
            fields = struct.unpack(HDR_FMT, hdr)
        except struct.error:
            return None
            
        version, kem_id, kem_param, sig_id, sig_param, session_id, seq, epoch = fields
        
        # Basic header validation
        if version != 1:
            return None
            
        # Replay window check
        if self.high == -1:
            self.high = seq
        
        # Drop packets too far behind the window
        if seq + self.window < self.high:
            return None
            
        # Update high water mark
        if seq > self.high:
            self.high = seq
            
        # Check for duplicate using (session_id, seq, epoch) tuple
        packet_key = (session_id, seq, epoch)
        if packet_key in self.received:
            return None  # Drop duplicate
            
        # Attempt decryption with header as AAD
        try:
            plaintext = self.aes.decrypt(iv, ct, hdr)
        except InvalidTag:
            return None  # Drop packets that fail authentication
        
        # Mark packet as received after successful decryption
        self.received.add(packet_key)
        
        # Clean up old entries to prevent memory growth
        if len(self.received) > self.window * 2:
            # Remove entries older than current window
            cutoff_seq = max(0, self.high - self.window)
            self.received = {
                (sid, s, e) for (sid, s, e) in self.received 
                if s >= cutoff_seq
            }
        
        return plaintext