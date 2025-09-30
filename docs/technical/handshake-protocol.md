# Handshake Protocol Technical Documentation

This document provides comprehensive technical details for the post-quantum handshake protocol implementation.

## Protocol Overview

The handshake protocol establishes mutually authenticated session keys between the Ground Control Station (GCS) and Unmanned Aerial Vehicle (UAV) using post-quantum cryptographic algorithms. The protocol consists of a two-message exchange over TCP that prevents downgrade attacks and provides forward secrecy.

### Design Principles

1. **Post-Quantum Security**: Uses NIST-standardized algorithms resistant to quantum attacks
2. **Mutual Authentication**: Both parties verify each other's identity using digital signatures and pre-shared keys
3. **Forward Secrecy**: Ephemeral key generation ensures past session security despite future key compromise
4. **Downgrade Protection**: Mandatory signature verification prevents algorithm downgrade attacks
5. **Replay Resistance**: Session identifiers and timestamps prevent replay attacks

## Protocol Specification

### Message Flow

```
Drone (Client)                    GCS (Server)
      |                                |
      |     TCP Connection Setup       |
      |<------------------------------>|
      |                                |
      |                                | Generate ephemeral ML-KEM keypair
      |                                | Sign transcript with long-term key
      |     ServerHello                |
      |<-------------------------------|
      |                                |
Verify signature                       |
Perform ML-KEM encapsulation          |
Compute HMAC authentication           |
      |     ClientResponse             |
      |------------------------------->|
      |                                | Verify HMAC
      |                                | Perform ML-KEM decapsulation
      |                                | Derive session keys
Both parties derive session keys       |
      |                                |
   Switch to UDP Data Plane            |
```

### Message Formats

#### ServerHello Message

```
ServerHello = Length || Payload

Length (4 bytes):
    Big-endian uint32 length of Payload

Payload:
    Version (1 byte):           WIRE_VERSION = 1
    KEM_Name_Length (1 byte):   Length of KEM algorithm name
    KEM_Name (variable):        Algorithm name (e.g., "Kyber768")
    Sig_Name_Length (1 byte):   Length of signature algorithm name  
    Sig_Name (variable):        Algorithm name (e.g., "Dilithium3")
    Session_ID (8 bytes):       Random session identifier
    Challenge (8 bytes):        Random challenge value
    KEM_PubKey_Length (2 bytes): Big-endian uint16 length of public key
    KEM_PubKey (variable):      ML-KEM public key
    Signature_Length (2 bytes): Big-endian uint16 length of signature
    Signature (variable):       Digital signature over transcript
```

#### ClientResponse Message

```
ClientResponse = Length || Payload

Length (4 bytes):
    Big-endian uint32 length of Payload

Payload:
    KEM_Ciphertext (variable):  ML-KEM encapsulation result
    HMAC_Tag (32 bytes):        SHA-256 HMAC of ServerHello
```

### Cryptographic Operations

#### Transcript Construction

The transcript is constructed identically by both parties to ensure signature verification:

```python
def construct_transcript(version, session_id, kem_name, sig_name, kem_pubkey, challenge):
    transcript = struct.pack("!B", version)  # Version byte
    transcript += b"|pq-drone-gcs:v1|"       # Protocol identifier
    transcript += session_id                  # Session ID (8 bytes)
    transcript += b"|"
    transcript += kem_name.encode()          # KEM algorithm name
    transcript += b"|" 
    transcript += sig_name.encode()          # Signature algorithm name
    transcript += b"|"
    transcript += kem_pubkey                 # ML-KEM public key
    transcript += b"|"
    transcript += challenge                  # Challenge (8 bytes)
    return transcript
```

#### Key Derivation

Session keys are derived using HKDF-SHA256 with specific parameters:

```python
def derive_transport_keys(shared_secret, session_id, kem_name, sig_name):
    # Construct info parameter
    info = f"pq-drone-gcs:kdf:v1|{session_id.hex()}|{kem_name}|{sig_name}"
    
    # HKDF with fixed salt
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=64,  # 32 bytes for each direction
        salt=b"pq-drone-gcs|hkdf|v1",
        info=info.encode()
    )
    
    # Derive 64 bytes of key material
    okm = hkdf.derive(shared_secret)
    
    # Split into directional keys
    drone_to_gcs_key = okm[:32]
    gcs_to_drone_key = okm[32:64]
    
    return drone_to_gcs_key, gcs_to_drone_key
```

## Implementation Details

### Server-Side Implementation (`server_gcs_handshake`)

```python
def server_gcs_handshake(suite_id, gcs_signing_key):
    """
    Perform GCS-side handshake protocol.
    
    Args:
        suite_id: Cryptographic suite identifier
        gcs_signing_key: GCS long-term signing key
        
    Returns:
        tuple: (drone_to_gcs_key, gcs_to_drone_key, session_id)
    """
    # Get suite configuration
    suite = get_suite(suite_id)
    
    # Generate session parameters
    version = CONFIG["WIRE_VERSION"]
    session_id = os.urandom(8)
    challenge = os.urandom(8)
    
    # Generate ephemeral ML-KEM keypair
    kem_obj = KeyEncapsulation(suite["kem_name"])
    kem_pub, kem_priv = kem_obj.generate_keypair()
    
    # Build and sign transcript
    transcript = construct_transcript(
        version, session_id, suite["kem_name"], 
        suite["sig_name"], kem_pub, challenge
    )
    signature = gcs_signing_key.sign(transcript)
    
    # Construct ServerHello message
    hello_wire = build_server_hello_wire(
        version, suite["kem_name"], suite["sig_name"],
        session_id, challenge, kem_pub, signature
    )
    
    # Send ServerHello
    send_tcp_message(hello_wire)
    
    # Receive and process ClientResponse
    client_response = receive_tcp_message()
    kem_ciphertext, hmac_tag = parse_client_response(client_response)
    
    # Verify HMAC authentication
    expected_hmac = hmac.new(
        bytes.fromhex(CONFIG["DRONE_PSK"]),
        hello_wire,
        hashlib.sha256
    ).digest()
    
    if not hmac.compare_digest(hmac_tag, expected_hmac):
        raise HandshakeVerifyError("Drone authentication failed")
    
    # Perform ML-KEM decapsulation
    shared_secret = kem_obj.decap_secret(kem_ciphertext)
    
    # Derive session keys
    drone_to_gcs_key, gcs_to_drone_key = derive_transport_keys(
        shared_secret, session_id, suite["kem_name"], suite["sig_name"]
    )
    
    return drone_to_gcs_key, gcs_to_drone_key, session_id
```

### Client-Side Implementation (`client_drone_handshake`)

```python
def client_drone_handshake(suite_id, gcs_public_key):
    """
    Perform drone-side handshake protocol.
    
    Args:
        suite_id: Expected cryptographic suite identifier
        gcs_public_key: GCS public verification key
        
    Returns:
        tuple: (drone_to_gcs_key, gcs_to_drone_key, session_id)
    """
    # Receive ServerHello
    hello_wire = receive_tcp_message()
    hello_data = parse_server_hello(hello_wire)
    
    # Verify protocol version
    if hello_data["version"] != CONFIG["WIRE_VERSION"]:
        raise HandshakeFormatError("Unsupported protocol version")
    
    # Verify expected suite
    expected_suite = get_suite(suite_id)
    if (hello_data["kem_name"] != expected_suite["kem_name"] or
        hello_data["sig_name"] != expected_suite["sig_name"]):
        raise HandshakeFormatError("Unexpected cryptographic suite")
    
    # Reconstruct and verify transcript
    transcript = construct_transcript(
        hello_data["version"], hello_data["session_id"],
        hello_data["kem_name"], hello_data["sig_name"],
        hello_data["kem_pubkey"], hello_data["challenge"]
    )
    
    # Verify GCS signature
    sig_obj = Signature(hello_data["sig_name"])
    if not sig_obj.verify(transcript, hello_data["signature"], gcs_public_key):
        raise HandshakeVerifyError("GCS authentication failed")
    
    # Perform ML-KEM encapsulation
    kem_obj = KeyEncapsulation(hello_data["kem_name"])
    kem_ciphertext, shared_secret = kem_obj.encap_secret(hello_data["kem_pubkey"])
    
    # Compute HMAC for mutual authentication
    hmac_tag = hmac.new(
        bytes.fromhex(CONFIG["DRONE_PSK"]),
        hello_wire,
        hashlib.sha256
    ).digest()
    
    # Send ClientResponse
    response_wire = build_client_response_wire(kem_ciphertext, hmac_tag)
    send_tcp_message(response_wire)
    
    # Derive session keys
    drone_to_gcs_key, gcs_to_drone_key = derive_transport_keys(
        shared_secret, hello_data["session_id"],
        hello_data["kem_name"], hello_data["sig_name"]
    )
    
    return drone_to_gcs_key, gcs_to_drone_key, hello_data["session_id"]
```

## Security Analysis

### Security Properties

1. **Authentication**: Digital signatures provide non-repudiable authentication of handshake parameters
2. **Key Agreement**: ML-KEM provides IND-CCA2 secure key encapsulation
3. **Forward Secrecy**: Ephemeral key generation ensures past session security
4. **Mutual Authentication**: HMAC verification ensures both parties have correct PSK
5. **Replay Protection**: Session identifiers prevent replay of handshake messages

### Threat Model

The protocol is secure against:

- **Passive Adversaries**: Cannot recover session keys from observed handshake
- **Active Adversaries**: Cannot inject or modify handshake messages
- **Quantum Adversaries**: Post-quantum algorithms resist quantum attacks
- **Replay Attacks**: Session identifiers prevent message replay
- **Downgrade Attacks**: Mandatory signature verification prevents algorithm substitution

### Security Assumptions

1. **Long-term Keys**: GCS signing key and drone PSK are securely stored
2. **Algorithm Security**: ML-KEM, digital signatures, and AES-256-GCM are secure
3. **Implementation Security**: Constant-time operations prevent side-channel attacks
4. **Random Number Generation**: Cryptographically secure random number generators

## Error Handling

### Exception Types

```python
class HandshakeError(Exception):
    """Base class for handshake errors"""
    pass

class HandshakeFormatError(HandshakeError):
    """Invalid message format or unexpected data"""
    pass

class HandshakeVerifyError(HandshakeError):
    """Authentication or signature verification failure"""
    pass

class HandshakeTimeoutError(HandshakeError):
    """Network timeout during handshake"""
    pass

class UnsupportedSuiteError(HandshakeError):
    """Requested cryptographic suite not available"""
    pass
```

### Recovery Procedures

1. **Network Errors**: Exponential backoff retry with maximum attempts
2. **Authentication Failures**: Immediate termination with security logging
3. **Format Errors**: Parsing failures result in connection reset
4. **Timeout Errors**: Configurable timeout with fallback to reconnection

## Performance Considerations

### Optimization Techniques

1. **Algorithm Selection**: Balance security level with performance requirements
2. **Hardware Acceleration**: Utilize AES-NI and other cryptographic acceleration
3. **Connection Reuse**: Minimize handshake frequency through long-lived sessions
4. **Async Operations**: Non-blocking I/O for improved responsiveness

### Timing Analysis

| Operation | ML-KEM-512 | ML-KEM-768 | ML-KEM-1024 |
|-----------|------------|------------|-------------|
| KeyGen    | ~0.1ms     | ~0.15ms    | ~0.2ms      |
| Encaps    | ~0.15ms    | ~0.2ms     | ~0.25ms     |
| Decaps    | ~0.2ms     | ~0.25ms    | ~0.3ms      |

| Signature | Sign Time | Verify Time | Size |
|-----------|-----------|-------------|------|
| ML-DSA-44 | ~0.5ms    | ~0.3ms      | 2.4KB |
| Falcon-512| ~8ms      | ~0.1ms      | 666B  |
| SLH-DSA   | ~25ms     | ~2ms        | 7.8KB |

---

**Navigation**: 
- **Back to**: [Technical Documentation](README.md)
- **Related**: [Data Transport](data-transport.md) | [Algorithm Matrix](algorithm-matrix.md)
- **Diagrams**: [Handshake Protocol](../diagrams/protocols/handshake.md)