# Handshake Protocol Diagrams

This document contains detailed visualizations of the post-quantum handshake protocol implementation.

## Complete Handshake Protocol Flow

### Detailed TCP Handshake Sequence

```mermaid
sequenceDiagram
    participant D as Drone (client_drone_handshake)
    participant N as Network (TCP Port 46000)
    participant G as GCS (server_gcs_handshake)
    participant OQS as Open Quantum Safe Library
    participant HKDF as Key Derivation
    
    Note over G: Phase 1 - Server Hello Generation (build_server_hello)
    G->>G: suite = get_suite(suite_id)
    G->>G: version = CONFIG["WIRE_VERSION"] (=1)
    G->>G: session_id = os.urandom(8)
    G->>G: challenge = os.urandom(8)
    
    G->>OQS: kem_obj = KeyEncapsulation(suite["kem_name"])
    OQS->>G: kem_pub, kem_priv = kem_obj.generate_keypair()
    
    Note over G: Transcript Construction
    G->>G: transcript = struct.pack("!B", version)<br/>+ "|pq-drone-gcs:v1|"<br/>+ session_id + "|"<br/>+ kem_name + "|"<br/>+ sig_name + "|"<br/>+ kem_pub + "|"<br/>+ challenge
    
    G->>OQS: signature = server_sig_obj.sign(transcript)
    OQS->>G: Digital signature (ML-DSA/Falcon/SLH-DSA)
    
    Note over G: Wire Format Construction
    G->>G: wire = version (1)<br/>+ len(kem_name) + kem_name<br/>+ len(sig_name) + sig_name<br/>+ session_id (8)<br/>+ challenge (8)<br/>+ len(kem_pub) + kem_pub<br/>+ len(signature) + signature
    
    G->>N: struct.pack("!I", len(hello_wire)) + hello_wire
    N->>D: TCP transmission
    
    Note over D: Phase 2 - Client Verification (parse_and_verify_server_hello)
    D->>D: Parse wire format:<br/>version, kem_name_len, kem_name,<br/>sig_name_len, sig_name,<br/>session_id, challenge,<br/>kem_pub_len, kem_pub,<br/>sig_len, signature
    
    D->>D: if version != CONFIG["WIRE_VERSION"]:<br/>    raise HandshakeFormatError
    
    Note over D: Transcript Reconstruction
    D->>D: Reconstruct identical transcript<br/>(same format as server)
    D->>OQS: sig = Signature(sig_name.decode())
    OQS->>D: signature_valid = sig.verify(<br/>    transcript, signature, gcs_public_key)
    
    alt Signature Verification SUCCESS
        Note over D: Phase 3 - Key Encapsulation (client_encapsulate)
        D->>OQS: kem = KeyEncapsulation(kem_name.decode())
        OQS->>D: (kem_ct, shared_secret) = kem.encap_secret(kem_pub)
        
        Note over D: Mutual Authentication
        D->>D: psk_bytes = bytes.fromhex(CONFIG["DRONE_PSK"])
        D->>D: tag = hmac.new(psk_bytes, hello_wire, hashlib.sha256).digest()
        
        D->>N: struct.pack("!I", len(kem_ct)) + kem_ct + tag
        N->>G: TCP transmission
        
        Note over G: Phase 4 - Server Decapsulation (server_decapsulate)
        G->>G: Verify HMAC tag using same PSK
        G->>G: if not hmac.compare_digest(tag, expected_tag):<br/>    raise HandshakeVerifyError
        
        G->>OQS: shared_secret = ephemeral.kem_obj.decap_secret(kem_ct)
        
        Note over D,G: Phase 5 - Key Derivation (derive_transport_keys)
        par HKDF-SHA256 Key Derivation
            D->>HKDF: info = "pq-drone-gcs:kdf:v1|"<br/>+ session_id + "|"<br/>+ kem_name + "|" + sig_name
            D->>HKDF: hkdf = HKDF(algorithm=SHA256,<br/>    length=64,<br/>    salt="pq-drone-gcs|hkdf|v1",<br/>    info=info)
            HKDF->>D: okm = hkdf.derive(shared_secret)
            D->>D: (key_d2g, key_g2d) = (okm[:32], okm[32:64])
        and
            G->>HKDF: Same HKDF computation with<br/>identical parameters
            HKDF->>G: Same okm output
            G->>G: (key_g2d, key_d2g) = (okm[:32], okm[32:64])
        end
        
        Note over D,G: Phase 6 - Session Establishment
        D->>D: sender = AeadSender(key_d2g, suite_id)<br/>receiver = AeadReceiver(key_g2d, suite_id)
        G->>G: sender = AeadSender(key_g2d, suite_id)<br/>receiver = AeadReceiver(key_d2g, suite_id)
        
        Note over D,G: SUCCESS - Switch to UDP Data Plane
        
    else Signature Verification FAILURE
        D->>D: logger.warning("Rejected handshake with bad signature")
        D->>D: raise HandshakeVerifyError("GCS authentication failed")
        Note over D: Connection terminated (Security violation)
    end
```

## Wire Format Details

### Server Hello Message Structure

```mermaid
graph TB
    subgraph "TCP Message Frame"
        A[Length Field<br/>4 bytes<br/>Big-endian uint32]
        B[Server Hello Payload<br/>Variable length]
    end
    
    subgraph "Server Hello Payload"
        C[Version<br/>1 byte<br/>WIRE_VERSION=1]
        D[KEM Name Length<br/>1 byte]
        E[KEM Name<br/>Variable<br/>e.g., "Kyber512"]
        F[Sig Name Length<br/>1 byte]
        G[Sig Name<br/>Variable<br/>e.g., "Dilithium2"]
        H[Session ID<br/>8 bytes<br/>Random]
        I[Challenge<br/>8 bytes<br/>Random]
        J[KEM Public Key Length<br/>2 bytes<br/>Big-endian uint16]
        K[KEM Public Key<br/>Variable<br/>ML-KEM public key]
        L[Signature Length<br/>2 bytes<br/>Big-endian uint16]
        M[Signature<br/>Variable<br/>Digital signature]
    end
    
    A --> B
    B --> C
    C --> D
    D --> E
    E --> F
    F --> G
    G --> H
    H --> I
    I --> J
    J --> K
    K --> L
    L --> M
    
    style A fill:#e3f2fd
    style C fill:#f3e5f5
    style H fill:#e8f5e8
    style I fill:#e8f5e8
    style K fill:#fff3e0
    style M fill:#fce4ec
```

### Client Response Message Structure

```mermaid
graph TB
    subgraph "TCP Message Frame"
        A[Length Field<br/>4 bytes<br/>Big-endian uint32]
        B[Client Response Payload<br/>Variable length]
    end
    
    subgraph "Client Response Payload"
        C[KEM Ciphertext<br/>Variable length<br/>ML-KEM encapsulation result]
        D[HMAC Tag<br/>32 bytes<br/>SHA-256 HMAC of Server Hello]
    end
    
    A --> B
    B --> C
    C --> D
    
    style A fill:#e3f2fd
    style C fill:#fff3e0
    style D fill:#e8f5e8
```

## Algorithm-Specific Flows

### ML-KEM Key Exchange Detail

```mermaid
flowchart TB
    subgraph "GCS (Server) Side"
        A1[Generate ML-KEM Keypair]
        A2[Sample secret vector s]
        A3[Sample error vector e]
        A4[Compute A·s + e = t]
        A5[Public key: (A, t)]
        A6[Private key: s]
    end
    
    subgraph "Drone (Client) Side"
        B1[Receive public key (A, t)]
        B2[Sample randomness r, e1, e2]
        B3[Compute u = A^T·r + e1]
        B4[Compute v = t^T·r + e2 + m]
        B5[Ciphertext: (u, v)]
        B6[Shared secret: derived from m]
    end
    
    subgraph "GCS Decapsulation"
        C1[Receive ciphertext (u, v)]
        C2[Compute m' = v - s^T·u]
        C3[Shared secret: derived from m']
    end
    
    A1 --> A2
    A2 --> A3
    A3 --> A4
    A4 --> A5
    A5 --> B1
    B1 --> B2
    B2 --> B3
    B3 --> B4
    B4 --> B5
    B5 --> C1
    C1 --> C2
    C2 --> C3
    
    style A5 fill:#e3f2fd
    style B5 fill:#f3e5f5
    style C3 fill:#e8f5e8
```

### Digital Signature Verification Flow

```mermaid
sequenceDiagram
    participant GCS as GCS (Signer)
    participant Drone as Drone (Verifier)
    participant OQS as OQS Library
    
    Note over GCS: Signature Generation
    GCS->>GCS: Construct transcript from handshake data
    GCS->>OQS: Load long-term signing key
    OQS->>GCS: Signing key object
    GCS->>OQS: signature = sign(transcript)
    
    alt ML-DSA (Dilithium)
        OQS->>OQS: Rejection sampling<br/>Fiat-Shamir transform
        OQS->>GCS: ML-DSA signature (2.4-4.6 KB)
    else Falcon
        OQS->>OQS: NTRU lattice sampling<br/>Fast Fourier Transform
        OQS->>GCS: Falcon signature (666-1280 bytes)
    else SLH-DSA (SPHINCS+)
        OQS->>OQS: Hash tree construction<br/>WOTS+ signatures
        OQS->>GCS: SLH-DSA signature (7.8-29.8 KB)
    end
    
    GCS->>Drone: Send signature in Server Hello
    
    Note over Drone: Signature Verification
    Drone->>Drone: Reconstruct identical transcript
    Drone->>OQS: Load GCS public verification key
    OQS->>Drone: Verification key object
    Drone->>OQS: verify(transcript, signature, public_key)
    
    alt Valid Signature
        OQS->>Drone: Verification success
        Drone->>Drone: Continue handshake
    else Invalid Signature
        OQS->>Drone: Verification failure
        Drone->>Drone: Terminate handshake<br/>Log security violation
    end
```

## Error Handling in Handshake

### Handshake Failure Scenarios

```mermaid
flowchart TB
    A[Begin Handshake] --> B{TCP Connection?}
    B -->|Fail| C[ConnectionError]
    B -->|Success| D[Receive Server Hello]
    
    D --> E{Valid Format?}
    E -->|No| F[HandshakeFormatError]
    E -->|Yes| G{Version Match?}
    
    G -->|No| H[HandshakeVersionError]
    G -->|Yes| I{Signature Valid?}
    
    I -->|No| J[HandshakeVerifyError<br/>Security Violation]
    I -->|Yes| K{Suite Available?}
    
    K -->|No| L[UnsupportedSuiteError]
    K -->|Yes| M[ML-KEM Encapsulation]
    
    M --> N{Encap Success?}
    N -->|No| O[CryptographicError]
    N -->|Yes| P[Send Client Response]
    
    P --> Q{Network Send?}
    Q -->|Fail| R[NetworkError]
    Q -->|Success| S[Key Derivation]
    
    S --> T{HKDF Success?}
    T -->|No| U[KeyDerivationError]
    T -->|Success| V[Handshake Complete]
    
    style C fill:#ffcccc
    style F fill:#ffcccc
    style H fill:#ffcccc
    style J fill:#ff9999
    style L fill:#ffcccc
    style O fill:#ffcccc
    style R fill:#ffcccc
    style U fill:#ffcccc
    style V fill:#ccffcc
```

### Recovery and Retry Logic

```mermaid
sequenceDiagram
    participant App as Application
    participant Proxy as Proxy
    participant Remote as Remote
    
    Note over Proxy: Initial Handshake Attempt
    Proxy->>Remote: TCP handshake
    Remote--xProxy: Timeout/Failure
    
    Note over Proxy: Exponential Backoff Retry
    Proxy->>Proxy: Wait 1 second
    Proxy->>Remote: Retry handshake
    Remote--xProxy: Failure again
    
    Proxy->>Proxy: Wait 2 seconds
    Proxy->>Remote: Retry handshake
    Remote--xProxy: Persistent failure
    
    Proxy->>Proxy: Wait 4 seconds
    Proxy->>Remote: Final retry attempt
    
    alt Success
        Remote->>Proxy: Handshake complete
        Proxy->>App: Connection established
    else Max Retries Exceeded
        Proxy->>Proxy: Mark connection failed
        Proxy->>App: Connection unavailable
        Note over Proxy: Enter reconnection mode
    end
```

---

**Navigation**: 
- **Back to**: [Diagrams Index](../README.md)
- **Related**: [Data Transport](data-transport.md) | [Runtime Switching](runtime-switching.md)
- **Technical Docs**: [Handshake Protocol](../../technical/handshake-protocol.md)