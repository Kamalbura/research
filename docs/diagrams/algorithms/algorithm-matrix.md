# Algorithm Matrix and Specifications

This document contains detailed visualizations of the post-quantum algorithm matrix and specifications.

## Complete Algorithm Matrix

### 21 Supported Suite Combinations

```mermaid
graph TB
    subgraph "Key Exchange Algorithms (ML-KEM)"
        KEM1[ML-KEM-512<br/>NIST Level 1<br/>128-bit security<br/>Public key: 800 bytes<br/>Ciphertext: 768 bytes]
        
        KEM2[ML-KEM-768<br/>NIST Level 3<br/>192-bit security<br/>Public key: 1,184 bytes<br/>Ciphertext: 1,088 bytes]
        
        KEM3[ML-KEM-1024<br/>NIST Level 5<br/>256-bit security<br/>Public key: 1,568 bytes<br/>Ciphertext: 1,568 bytes]
    end
    
    subgraph "Digital Signature Algorithms"
        SIG1[ML-DSA-44<br/>NIST Level 1<br/>~2,420 bytes signatures<br/>Fast performance]
        
        SIG2[ML-DSA-65<br/>NIST Level 3<br/>~3,293 bytes signatures<br/>Balanced choice]
        
        SIG3[ML-DSA-87<br/>NIST Level 5<br/>~4,627 bytes signatures<br/>Maximum security]
        
        SIG4[Falcon-512<br/>NIST Level 1<br/>~666 bytes signatures<br/>Ultra-compact]
        
        SIG5[Falcon-1024<br/>NIST Level 5<br/>~1,280 bytes signatures<br/>Compact + secure]
        
        SIG6[SLH-DSA-128s<br/>NIST Level 1<br/>~7,856 bytes signatures<br/>Conservative security]
        
        SIG7[SLH-DSA-256s<br/>NIST Level 5<br/>~29,792 bytes signatures<br/>Ultimate security]
    end
    
    subgraph "21 Valid Combinations"
        MATRIX[3 KEM variants<br/>×<br/>7 Signature variants<br/>=<br/>21 Suite Combinations]
    end
    
    KEM1 --> MATRIX
    KEM2 --> MATRIX
    KEM3 --> MATRIX
    SIG1 --> MATRIX
    SIG2 --> MATRIX
    SIG3 --> MATRIX
    SIG4 --> MATRIX
    SIG5 --> MATRIX
    SIG6 --> MATRIX
    SIG7 --> MATRIX
    
    classDef kem fill:#e3f2fd,stroke:#0277bd,stroke-width:2px
    classDef sig fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px
    classDef matrix fill:#e8f5e8,stroke:#2e7d32,stroke-width:2px
    
    class KEM1,KEM2,KEM3 kem
    class SIG1,SIG2,SIG3,SIG4,SIG5,SIG6,SIG7 sig
    class MATRIX matrix
```

### Suite Configuration Mapping

```mermaid
graph LR
    subgraph "Suite ID Examples"
        A["cs-mlkem512-aesgcm-mldsa44<br/>• KEM: ML-KEM-512<br/>• AEAD: AES-256-GCM<br/>• Signature: ML-DSA-44<br/>• Security: Level 1"]
        
        B["cs-mlkem768-aesgcm-falcon512<br/>• KEM: ML-KEM-768<br/>• AEAD: AES-256-GCM<br/>• Signature: Falcon-512<br/>• Security: Level 3"]
        
        C["cs-mlkem1024-aesgcm-slhdsa256s<br/>• KEM: ML-KEM-1024<br/>• AEAD: AES-256-GCM<br/>• Signature: SLH-DSA-256s<br/>• Security: Level 5"]
    end
    
    subgraph "Wire Protocol IDs"
        D["Header Encoding:<br/>• KEM_ID: 0x00-0x02<br/>• KEM_PARAM: 0x00<br/>• SIG_ID: 0x00-0x21<br/>• SIG_PARAM: 0x00"]
    end
    
    subgraph "OQS Library Mapping"
        E["Algorithm Names<br/>- Kyber512 maps to ML-KEM-512<br/>- Dilithium2 maps to ML-DSA-44<br/>- Falcon-512 maps to Falcon-512<br/>- SPHINCS+ maps to SLH-DSA-128s"]
    end
    
    A --> D
    B --> D
    C --> D
    D --> E
    
    style A fill:#e3f2fd
    style B fill:#f3e5f5
    style C fill:#e8f5e8
    style D fill:#fff3e0
    style E fill:#fce4ec
```

## Algorithm Family Details

### ML-KEM (Key Exchange) Specifications

```mermaid
graph TB
    subgraph "ML-KEM-512 (Level 1)"
        A1[Security Level: 128-bit quantum<br/>Module rank: 2<br/>Polynomial degree: 256<br/>Modulus q: 3329]
        
        A2[Key Sizes:<br/>• Public key: 800 bytes<br/>• Secret key: 1,632 bytes<br/>• Ciphertext: 768 bytes<br/>• Shared secret: 32 bytes]
        
        A3[Performance:<br/>• KeyGen: ~0.1ms<br/>• Encaps: ~0.15ms<br/>• Decaps: ~0.2ms<br/>• Memory: ~3KB]
    end
    
    subgraph "ML-KEM-768 (Level 3)"
        B1[Security Level: 192-bit quantum<br/>Module rank: 3<br/>Polynomial degree: 256<br/>Modulus q: 3329]
        
        B2[Key Sizes:<br/>• Public key: 1,184 bytes<br/>• Secret key: 2,400 bytes<br/>• Ciphertext: 1,088 bytes<br/>• Shared secret: 32 bytes]
        
        B3[Performance:<br/>• KeyGen: ~0.15ms<br/>• Encaps: ~0.2ms<br/>• Decaps: ~0.25ms<br/>• Memory: ~4KB]
    end
    
    subgraph "ML-KEM-1024 (Level 5)"
        C1[Security Level: 256-bit quantum<br/>Module rank: 4<br/>Polynomial degree: 256<br/>Modulus q: 3329]
        
        C2[Key Sizes:<br/>• Public key: 1,568 bytes<br/>• Secret key: 3,168 bytes<br/>• Ciphertext: 1,568 bytes<br/>• Shared secret: 32 bytes]
        
        C3[Performance:<br/>• KeyGen: ~0.2ms<br/>• Encaps: ~0.25ms<br/>• Decaps: ~0.3ms<br/>• Memory: ~5KB]
    end
    
    A1 --> A2
    A2 --> A3
    B1 --> B2
    B2 --> B3
    C1 --> C2
    C2 --> C3
    
    style A1 fill:#e3f2fd
    style B1 fill:#f3e5f5
    style C1 fill:#e8f5e8
```

### Digital Signature Algorithm Comparison

```mermaid
graph TB
    subgraph "Lattice-Based Signatures"
        subgraph "ML-DSA (Dilithium)"
            A1[ML-DSA-44<br/>• Signature: ~2,420 bytes<br/>• Public key: 1,312 bytes<br/>• Private key: 2,528 bytes<br/>• Sign time: ~0.5ms<br/>• Verify time: ~0.3ms]
            
            A2[ML-DSA-65<br/>• Signature: ~3,293 bytes<br/>• Public key: 1,952 bytes<br/>• Private key: 4,000 bytes<br/>• Sign time: ~0.7ms<br/>• Verify time: ~0.4ms]
            
            A3[ML-DSA-87<br/>• Signature: ~4,627 bytes<br/>• Public key: 2,592 bytes<br/>• Private key: 4,864 bytes<br/>• Sign time: ~1.0ms<br/>• Verify time: ~0.6ms]
        end
        
        subgraph "Falcon (NTRU)"
            B1[Falcon-512<br/>• Signature: ~666 bytes<br/>• Public key: 897 bytes<br/>• Private key: 1,281 bytes<br/>• Sign time: ~8ms<br/>• Verify time: ~0.1ms]
            
            B2[Falcon-1024<br/>• Signature: ~1,280 bytes<br/>• Public key: 1,793 bytes<br/>• Private key: 2,305 bytes<br/>• Sign time: ~15ms<br/>• Verify time: ~0.2ms]
        end
    end
    
    subgraph "Hash-Based Signatures"
        subgraph "SLH-DSA (SPHINCS+)"
            C1[SLH-DSA-128s<br/>• Signature: ~7,856 bytes<br/>• Public key: 32 bytes<br/>• Private key: 64 bytes<br/>• Sign time: ~25ms<br/>• Verify time: ~2ms]
            
            C2[SLH-DSA-256s<br/>• Signature: ~29,792 bytes<br/>• Public key: 64 bytes<br/>• Private key: 128 bytes<br/>• Sign time: ~100ms<br/>• Verify time: ~8ms]
        end
    end
    
    style A1 fill:#e3f2fd
    style A2 fill:#e3f2fd
    style A3 fill:#e3f2fd
    style B1 fill:#f3e5f5
    style B2 fill:#f3e5f5
    style C1 fill:#e8f5e8
    style C2 fill:#e8f5e8
```

## Algorithm Selection Guidelines

### Performance vs Security Trade-offs

```mermaid
graph TB
    subgraph "Bandwidth Constrained Scenarios"
        A1[Recommended: Falcon<br/>• Ultra-compact signatures<br/>• Acceptable key sizes<br/>• Good performance for verification]
        
        A2[Alternative: ML-DSA-44<br/>• Larger signatures but<br/>• Faster signing<br/>• More predictable timing]
    end
    
    subgraph "Performance Critical Scenarios"
        B1[Recommended: ML-DSA-65<br/>• Balanced performance<br/>• Reasonable signature size<br/>• Fast verification]
        
        B2[Alternative: ML-KEM-512<br/>• Fastest key exchange<br/>• Minimal computational overhead<br/>• Level 1 security sufficient]
    end
    
    subgraph "Security Critical Scenarios"
        C1[Recommended: ML-KEM-1024 + SLH-DSA<br/>• Maximum security level<br/>• Conservative hash-based signatures<br/>• Long-term confidence]
        
        C2[Alternative: ML-KEM-1024 + ML-DSA-87<br/>• Maximum NIST levels<br/>• Better performance than SLH-DSA<br/>• Lattice-based consistency]
    end
    
    subgraph "Balanced Scenarios"
        D1[Recommended: ML-KEM-768 + ML-DSA-65<br/>• Level 3 security<br/>• Good performance<br/>• Reasonable bandwidth usage]
        
        D2[Alternative: ML-KEM-768 + Falcon-1024<br/>• Level 3+ security<br/>• Compact signatures<br/>• Slower signing acceptable]
    end
    
    style A1 fill:#e3f2fd
    style B1 fill:#f3e5f5
    style C1 fill:#e8f5e8
    style D1 fill:#fff3e0
```

### Algorithm Security Foundations

```mermaid
graph LR
    subgraph "Mathematical Foundations"
        A[Lattice Problems<br/>- Shortest Vector Problem SVP<br/>- Closest Vector Problem CVP<br/>- Learning With Errors LWE<br/>- Module-LWE ML-KEM ML-DSA]
        
        B[NTRU Lattices<br/>- NTRU problem<br/>- Structured lattices<br/>- Efficient Gaussian sampling<br/>- Ring-based construction Falcon]
        
        C[Hash Functions<br/>- One-way functions<br/>- Collision resistance<br/>- Conservative security model<br/>- Merkle tree structures SLH-DSA]
    end
    
    subgraph "Security Reductions"
        D[Worst-case to Average-case<br/>- SVP hardness to ML-KEM security<br/>- MSIS problem to ML-DSA security<br/>- Tight security bounds]
        
        E[Conservative Assumptions<br/>- Hash function security only<br/>- No number theory assumptions<br/>- Explicit security reductions<br/>- SLH-DSA approach]
    end
    
    A --> D
    C --> E
    B --> D
    
    style A fill:#e3f2fd
    style B fill:#f3e5f5
    style C fill:#e8f5e8
    style D fill:#ccffcc
    style E fill:#ccffcc
```

## Wire Protocol Integration

### Algorithm ID Encoding

```mermaid
graph TB
    subgraph "Header Field Encoding"
        A[KEM_ID 1 byte<br/>0x00 ML-KEM-512<br/>0x01 ML-KEM-768<br/>0x02 ML-KEM-1024<br/>0x03-0xFF Reserved]
        
        B[KEM_PARAM 1 byte<br/>0x00 Standard parameters<br/>0x01-0xFF Future use]
        
        C[SIG_ID 1 byte<br/>0x00-0x02 ML-DSA variants<br/>0x10-0x11 Falcon variants<br/>0x20-0x21 SLH-DSA variants<br/>0x22-0xFF Reserved]
        
        D[SIG_PARAM 1 byte<br/>0x00 Standard parameters<br/>0x01-0xFF Future use]
    end
    
    subgraph "Suite to Header Mapping"
        E[get_suite suite_id<br/>returns kem_name sig_name etc]
        F[header_ids_for_suite suite_id<br/>returns kem_id kem_param sig_id sig_param]
        G[Packet header construction<br/>Uses 4-tuple for wire format]
    end
    
    A --> F
    B --> F
    C --> F
    D --> F
    E --> F
    F --> G
    
    style A fill:#e3f2fd
    style C fill:#f3e5f5
    style F fill:#e8f5e8
    style G fill:#ccffcc
```

### OQS Library Integration

```mermaid
sequenceDiagram
    participant Suite as Suite Registry
    participant OQS as OQS Library
    participant Crypto as Crypto Operations
    
    Note over Suite,Crypto: Algorithm Availability Check
    Suite->>OQS: Check algorithm support
    OQS->>Suite: Available algorithms list
    Suite->>Suite: Filter SUPPORTED_SUITES
    
    Note over Suite,Crypto: Key Generation
    Suite->>OQS: KeyEncapsulation(kem_name)
    OQS->>Suite: KEM object
    Suite->>OQS: kem.generate_keypair()
    OQS->>Suite: (public_key, secret_key)
    
    Note over Suite,Crypto: Signature Operations
    Suite->>OQS: Signature(sig_name)
    OQS->>Suite: Signature object
    Suite->>OQS: sig.sign(message)
    OQS->>Suite: signature_bytes
    
    Note over Suite,Crypto: Encapsulation/Decapsulation
    Suite->>OQS: kem.encap_secret(public_key)
    OQS->>Suite: (ciphertext, shared_secret)
    Suite->>OQS: kem.decap_secret(ciphertext)
    OQS->>Suite: shared_secret
    
    Note over Suite,Crypto: Verification
    Suite->>OQS: sig.verify(message, signature, public_key)
    OQS->>Suite: boolean_result
```

---

**Navigation**: 
- **Back to**: [Diagrams Index](../README.md)
- **Related**: [ML-KEM Details](ml-kem.md) | [Signature Algorithms](signatures.md) | [Security Levels](security-levels.md)
- **Technical Docs**: [Algorithm Matrix](../../technical/algorithm-matrix.md)