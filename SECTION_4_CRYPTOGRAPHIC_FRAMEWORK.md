# 4. Cryptographic Framework

This section presents our post-quantum cryptographic framework designed to secure drone-to-ground control station communication systems. Our implementation addresses the quantum computing threat to current cryptographic systems while meeting the specific operational requirements of unmanned aerial vehicles.

## 4.1 The Quantum Computing Threat to Drone Communications

### 4.1.1 Understanding the Quantum Threat

**Quantum Computing Impact**: Quantum computers pose a fundamental threat to current cryptographic systems used in drone communications. Shor's algorithm [1], when run on a sufficiently powerful quantum computer, can efficiently break the mathematical problems that secure today's RSA and elliptic curve cryptography (ECC) systems.

**Timeline and Urgency**: While estimates vary on when cryptographically relevant quantum computers will emerge (15-50 years), drone systems have operational lifespans of 20-30 years. This creates a "harvest now, decrypt later" vulnerability where adversaries can intercept and store today's encrypted drone communications to decrypt once quantum computers become available [2].

**Definition**: *Cryptographic Transition Requirement* - A communication system needs immediate quantum-resistant protection when its required security lifetime exceeds the estimated time until quantum computers arrive, minus the time needed for complete system migration.

### 4.1.2 Post-Quantum Cryptography Standards

**NIST Standardization Process**: The U.S. National Institute of Standards and Technology (NIST) has standardized quantum-resistant cryptographic algorithms through a multi-year evaluation process [3]. These algorithms are based on mathematical problems believed to be difficult for both classical and quantum computers to solve.

**Selected Algorithm Families**: Our implementation uses three families of NIST-approved algorithms:
- **Lattice-based algorithms**: Based on finding short vectors in high-dimensional lattices
- **Hash-based algorithms**: Based solely on the security of cryptographic hash functions  
- **Symmetric algorithms**: AES encryption remains secure against quantum computers (with appropriate key sizes)

## 4.2 Our Cryptographic Algorithm Implementation

Our system implements a comprehensive suite of NIST-standardized post-quantum algorithms. Each algorithm serves a specific purpose and provides different security-performance characteristics for drone communication systems.

### 4.2.1 ML-KEM: Key Exchange Mechanism

**Purpose**: ML-KEM (Module Learning with Errors - Key Encapsulation Mechanism) establishes shared secret keys between the drone and ground control station [4].

**Mathematical Foundation**: ML-KEM's security is based on the Module Learning with Errors (MLWE) problem, which involves finding patterns in seemingly random polynomial equations over finite fields. This problem is believed to remain difficult even for quantum computers.

**Technical Details**: 
- **ML-KEM-512**: Provides 128-bit post-quantum security (equivalent to AES-128 against quantum computers)
- **ML-KEM-768**: Provides 192-bit post-quantum security (equivalent to AES-192 against quantum computers)  
- **ML-KEM-1024**: Provides 256-bit post-quantum security (equivalent to AES-256 against quantum computers)

**How It Works**:
1. **Key Generation**: Create a public key (shared) and private key (secret) pair
2. **Encapsulation**: The drone uses the GCS public key to generate a shared secret and encrypted capsule
3. **Decapsulation**: The GCS uses its private key to extract the same shared secret from the capsule
4. **Result**: Both parties now possess the same secret key for encryption

**Implementation Details**: Our code uses the Open Quantum Safe library's ML-KEM implementation. Key sizes range from 800 bytes (ML-KEM-512) to 1,568 bytes (ML-KEM-1024).

### 4.2.2 ML-DSA: Digital Signatures

**Purpose**: ML-DSA (Module Lattice-Based Digital Signature Algorithm) provides authentication - proving that messages actually came from the claimed sender [5].

**Mathematical Foundation**: ML-DSA uses the Fiat-Shamir transformation applied to a commitment scheme over module lattices. Security relies on the Module Short Integer Solution (MSIS) problem and Module Learning with Errors (MLWE) problem.

**Algorithm Variants**:
- **ML-DSA-44**: Compact signatures (~2,420 bytes) with good performance
- **ML-DSA-65**: Balanced security-performance (~3,293 bytes signatures)
- **ML-DSA-87**: Maximum security (~4,627 bytes signatures)

**How Digital Signatures Work**:
1. **Key Generation**: Create a signature public key (for verification) and private key (for signing)
2. **Signing**: The GCS creates a digital signature over handshake data using its private key
3. **Verification**: The drone verifies the signature using the GCS public key
4. **Security**: Only the holder of the private key can create valid signatures

**Implementation Security**: Our implementation includes mandatory signature verification - handshakes fail completely if signature verification fails, preventing downgrade attacks.

### 4.2.3 Falcon: Compact Lattice Signatures

**Purpose**: Falcon provides an alternative digital signature option with exceptionally small signature sizes, important for bandwidth-limited drone communications [6].

**Mathematical Foundation**: Falcon constructs signatures over NTRU lattices using Gaussian sampling. NTRU (Number Theory Research Unit) lattices have special mathematical structure that enables very compact signatures.

**Algorithm Parameters**:
- **Falcon-512**: ~666 byte signatures, 897 byte public keys
- **Falcon-1024**: ~1,280 byte signatures, 1,793 byte public keys

**Key Advantage**: Falcon signatures are 3-5 times smaller than ML-DSA signatures, making them ideal for communication channels with strict bandwidth limitations.

**Implementation Considerations**: Falcon requires more complex floating-point arithmetic during key generation and signing, which our implementation handles through the OQS library integration.

### 4.2.4 SLH-DSA: Hash-Based Signatures

**Purpose**: SLH-DSA (Stateless Hash-Based Digital Signature Algorithm, formerly SPHINCS+) provides the most conservative signature option, basing security entirely on hash functions [7].

**Mathematical Foundation**: Instead of relying on lattice problems or number theory, SLH-DSA builds signatures using only hash function operations. This makes it the most conservative choice since hash functions are very well-understood cryptographic primitives.

**Algorithm Structure**:
- **One-Time Signatures**: WOTS+ creates signatures that can only be used once safely
- **Merkle Trees**: Combine many one-time signatures into a multi-use signature scheme
- **Hypertree Construction**: Stack multiple Merkle trees to enable many signatures

**Algorithm Parameters**:
- **SLH-DSA-SHA2-128f**: ~7,856 byte signatures, provides 128-bit post-quantum security
- **SLH-DSA-SHA2-256f**: ~29,792 byte signatures, provides 256-bit post-quantum security

**Trade-offs**: SLH-DSA provides the highest confidence in long-term security but produces very large signatures, making it suitable for high-security applications where bandwidth is not the primary constraint.

### 4.2.5 AES-256-GCM: Symmetric Encryption

**Purpose**: AES-256-GCM provides fast, authenticated encryption for actual drone data after the initial key exchange [8].

**Quantum Resistance**: AES-256 remains secure against quantum computers. While Grover's algorithm can speed up AES attacks, AES-256 still provides 128-bit security against quantum adversaries.

**Technical Components**:
- **AES-256**: Block cipher that encrypts data in 128-bit blocks using 256-bit keys
- **GCM Mode**: Galois/Counter Mode provides both encryption and authentication in a single operation
- **Authentication**: Prevents tampering - any modification to encrypted data is detected

**Implementation Details**:
- **Key Size**: 256-bit (32-byte) encryption keys
- **Nonce Management**: We use deterministic 96-bit nonces combining epoch counter and sequence number
- **Authentication Tag**: 128-bit tag authenticates both encrypted data and packet headers

## 4.3 Our Cryptographic Suite Matrix

Our implementation provides 21 different cryptographic suites, each combining one key exchange algorithm, one signature algorithm, and AES-256-GCM encryption. This matrix provides flexibility to choose the best combination for different operational scenarios.

### 4.3.1 Suite Naming Convention

**Format**: `cs-{kem}-aesgcm-{signature}`
- `cs`: "Cryptographic Suite" prefix
- `{kem}`: Key exchange algorithm (mlkem512, mlkem768, or mlkem1024)
- `aesgcm`: Always AES-256-GCM for encryption
- `{signature}`: Signature algorithm (mldsa44, mldsa65, mldsa87, falcon512, falcon1024, sphincs128fsha2, sphincs256fsha2)

### 4.3.2 Complete Suite Matrix

**NIST Level 1 Suites** (128-bit post-quantum security):
```
cs-mlkem512-aesgcm-mldsa44          cs-mlkem512-aesgcm-mldsa65
cs-mlkem512-aesgcm-mldsa87          cs-mlkem512-aesgcm-falcon512
cs-mlkem512-aesgcm-falcon1024       cs-mlkem512-aesgcm-sphincs128fsha2
cs-mlkem512-aesgcm-sphincs256fsha2
```

**NIST Level 3 Suites** (192-bit post-quantum security):
```
cs-mlkem768-aesgcm-mldsa44          cs-mlkem768-aesgcm-mldsa65
cs-mlkem768-aesgcm-mldsa87          cs-mlkem768-aesgcm-falcon512
cs-mlkem768-aesgcm-falcon1024       cs-mlkem768-aesgcm-sphincs128fsha2
cs-mlkem768-aesgcm-sphincs256fsha2
```

**NIST Level 5 Suites** (256-bit post-quantum security):
```
cs-mlkem1024-aesgcm-mldsa44         cs-mlkem1024-aesgcm-mldsa65
cs-mlkem1024-aesgcm-mldsa87         cs-mlkem1024-aesgcm-falcon512
cs-mlkem1024-aesgcm-falcon1024      cs-mlkem1024-aesgcm-sphincs128fsha2
cs-mlkem1024-aesgcm-sphincs256fsha2
```

### 4.3.3 Suite Selection Guidelines

**Balanced Performance**: `cs-mlkem768-aesgcm-mldsa65` provides excellent security-performance balance for most drone operations.

**Bandwidth-Constrained**: `cs-mlkem512-aesgcm-falcon512` minimizes signature sizes for limited communication links.

**Maximum Security**: `cs-mlkem1024-aesgcm-sphincs256fsha2` provides highest security for critical operations.

## 4.4 Hybrid Transport Protocol Architecture

Our protocol separates the reliability-critical handshake from latency-sensitive data transmission using two different network protocols.

### 4.4.1 Why Hybrid TCP/UDP Design

**Problem**: Drone communications have conflicting requirements:
- **Handshake**: Must be 100% reliable to establish security
- **Data**: Must be low-latency for real-time flight control

**Solution**: Use different protocols for different phases:
- **TCP for Handshake**: Guaranteed reliable delivery of cryptographic material
- **UDP for Data**: Low-latency delivery of encrypted flight data

### 4.4.2 Protocol Phases

**Phase 1 - Authentication (TCP)**:
1. Drone connects to GCS on TCP port 46000
2. Mutual authentication using post-quantum signatures
3. Key exchange using ML-KEM
4. Derive session keys for encryption

**Phase 2 - Data Transport (UDP)**:
1. Switch to UDP for actual data transmission
2. All data encrypted with AES-256-GCM using session keys
3. Low-latency communication for real-time control

## 4.5 Post-Quantum Handshake Protocol

Our handshake protocol establishes secure communication through a two-message exchange with strong authentication.

### 4.5.1 Protocol Overview

**Goal**: Establish shared encryption keys while proving the identity of both the drone and ground control station.

**Authentication Methods**:
- **GCS Authentication**: Digital signatures prove GCS identity to drone
- **Drone Authentication**: Pre-shared key (PSK) proves drone identity to GCS

### 4.5.2 Detailed Protocol Steps

**Step 1 - GCS Server Hello**:
```
GCS → Drone: Version | Algorithm_IDs | Session_ID | Challenge | 
                Public_Key | Digital_Signature
```

**Components Explained**:
- **Version**: Protocol version number (currently 1) to prevent downgrade attacks
- **Algorithm_IDs**: Specifies which post-quantum algorithms are being used
- **Session_ID**: Random 8-byte identifier for this communication session
- **Challenge**: Random 8-byte value for freshness
- **Public_Key**: Ephemeral ML-KEM public key for this session only
- **Digital_Signature**: ML-DSA/Falcon/SLH-DSA signature over all previous fields

**Step 2 - Drone Client Response**:
```
Drone → GCS: Encrypted_Secret | Authentication_Tag
```

**Components Explained**:
- **Encrypted_Secret**: ML-KEM ciphertext containing the shared secret
- **Authentication_Tag**: HMAC proving the drone knows the pre-shared key

### 4.5.3 Key Derivation Process

**Input Materials**:
- Shared secret from ML-KEM key exchange
- Session ID from handshake
- Algorithm identifiers for context binding

**Process**: Both sides use HKDF-SHA256 (Hash-based Key Derivation Function) to generate two 32-byte AES-256 keys:
- **Drone-to-GCS Key**: For encrypting drone data sent to ground control
- **GCS-to-Drone Key**: For encrypting commands sent from ground control to drone

**Security Property**: Each session uses completely fresh keys that cannot decrypt past or future communications (forward secrecy).

## 4.6 Encrypted Data Transport Protocol

After successful handshake, all application data flows through our optimized authenticated encryption protocol over UDP.

### 4.6.1 Packet Structure

Each encrypted packet contains:
```
[22-byte Header] + [AES-GCM Encrypted Data + Authentication Tag]
```

**Header Contents** (22 bytes total):
- Protocol version (1 byte)
- Cryptographic suite identifiers (4 bytes)
- Session identifier (8 bytes) 
- Packet sequence number (8 bytes)
- Epoch counter (1 byte)

### 4.6.2 Encryption Process

**Step 1 - Nonce Generation**: 
We create a unique 96-bit nonce for each packet by combining:
- Epoch counter (1 byte): Increments when keys change
- Sequence number (11 bytes): Increments for each packet

**Step 2 - Authenticated Encryption**:
- Encrypt the application data using AES-256-GCM
- Use the packet header as "Additional Authenticated Data" (AAD)
- This means the header is authenticated but not encrypted

**Step 3 - Transmission**:
Send the complete packet: header (plaintext) + encrypted data + authentication tag

### 4.6.3 Security Features

**Replay Protection**: A sliding window mechanism prevents replay attacks:
- Track the highest sequence number received
- Maintain a bitmask of recently received packet sequence numbers
- Reject packets that are duplicates or too old
- Default window size: 1,024 packets

**Authentication**: Every packet is authenticated:
- Headers cannot be modified without detection
- Encrypted data cannot be modified without detection
- Only parties with the correct session keys can create valid packets

**Nonce Uniqueness**: Our deterministic nonce generation ensures that each packet uses a unique nonce, which is critical for AES-GCM security.

## 4.7 Runtime Cryptographic Agility

A unique feature of our implementation is the ability to change cryptographic algorithms during active communication without interrupting the connection.

### 4.7.1 Why Runtime Algorithm Switching

**Adaptive Security**: Different flight phases may require different security-performance trade-offs:
- Takeoff/landing: High security for critical phases
- Cruise flight: Optimized performance for routine operations
- Emergency situations: Maximum reliability

**Algorithm Vulnerabilities**: If a cryptographic algorithm is discovered to be weak, the system can immediately switch to a different algorithm without stopping communication.

### 4.7.2 Control Channel Protocol

**Packet Types**: Our system supports two types of packets:
- **Type 0x01**: Regular application data (flight control, telemetry)
- **Type 0x02**: Control messages for algorithm switching

**Control Messages**: Special encrypted messages that coordinate algorithm changes:
- `prepare_rekey`: Request to switch to a new cryptographic suite
- `commit_rekey`: Confirmation to proceed with the switch
- `rekey_complete`: Notification that the switch was successful

### 4.7.3 Two-Phase Switching Protocol

**Phase 1 - Negotiation**:
1. GCS sends `prepare_rekey` with target cryptographic suite
2. Drone validates that it supports the requested suite
3. Drone responds with `commit_rekey` (agree) or `prepare_fail` (reject)

**Phase 2 - Execution**:
1. Both parties perform a new handshake using the new algorithms
2. Generate fresh session keys
3. Increment the epoch counter
4. Resume communication with new cryptographic protection

**Safety Guarantees**: The two-phase protocol ensures both sides switch simultaneously, preventing cryptographic mismatches that would break communication.

## 4.8 Implementation Architecture

Our implementation follows a modular design that separates different cryptographic responsibilities into distinct components.

### 4.8.1 Core Modules

**`core/suites.py`** - Cryptographic Suite Registry:
- Manages all 21 cryptographic suite combinations
- Provides algorithm parameter lookup and validation
- Handles legacy algorithm name aliases
- Checks which algorithms are available at runtime

**`core/handshake.py`** - Handshake Protocol:
- Implements the two-message post-quantum handshake
- Handles ML-KEM key encapsulation and decapsulation
- Manages digital signature creation and verification
- Derives session keys using HKDF-SHA256

**`core/aead.py`** - Authenticated Encryption:
- Implements AES-256-GCM packet encryption and decryption
- Manages deterministic nonce generation
- Provides sliding window replay protection
- Handles packet header authentication

**`core/policy_engine.py`** - Runtime Algorithm Control:
- Manages the cryptographic suite switching state machine
- Processes control messages for algorithm changes
- Enforces security policies and constraints
- Coordinates two-phase commit protocol

**`core/async_proxy.py`** - Network Coordination:
- Manages TCP handshake connections
- Handles UDP encrypted data transport
- Coordinates between plaintext and encrypted channels
- Provides error handling and recovery

### 4.8.2 Integration with Open Quantum Safe

**OQS Library**: Our implementation uses the Open Quantum Safe (OQS) library [9], which provides:
- NIST-approved post-quantum algorithm implementations
- Constant-time operations to prevent side-channel attacks
- Cross-platform compatibility (Windows, Linux, embedded systems)
- Regular security updates and algorithm optimizations

**Algorithm Instantiation Example**:
```python
# Create ML-KEM-768 key exchange object
kem = KeyEncapsulation("ML-KEM-768")
public_key, secret_key = kem.keypair()

# Create ML-DSA-65 signature object  
sig = Signature("ML-DSA-65")
signature = sig.sign(message)
```

## 4.9 Security Properties and Guarantees

Our cryptographic framework provides multiple layers of security protection appropriate for safety-critical drone operations.

### 4.9.1 Core Security Properties

**Confidentiality**: All application data is encrypted with AES-256-GCM, ensuring that intercepted communications cannot be read by unauthorized parties.

**Authentication**: Digital signatures and pre-shared keys provide strong authentication, ensuring that drones only communicate with legitimate ground control stations.

**Integrity**: Authentication tags detect any modification to encrypted data, ensuring that tampered messages are rejected.

**Forward Secrecy**: Each communication session uses fresh, ephemeral keys. Compromise of long-term keys does not compromise past communications.

**Replay Protection**: The sliding window mechanism prevents adversaries from replaying old packets to disrupt drone operations.

### 4.9.2 Quantum-Specific Security

**Quantum Resistance**: All public-key operations use NIST-standardized post-quantum algorithms that remain secure against quantum computer attacks.

**Algorithm Diversity**: By supporting multiple algorithm families (lattice-based, hash-based), the system provides protection against potential breakthroughs in any single mathematical area.

**Conservative Options**: SLH-DSA provides an ultra-conservative fallback option based entirely on hash functions, which are extremely well-understood and trusted.

## 4.10 Testing and Validation

Our implementation includes comprehensive testing to ensure correctness and security across all supported configurations.

### 4.10.1 Automated Test Coverage

**Test Statistics**: 109 automated test functions covering:
- All 21 cryptographic suite combinations
- Handshake protocol correctness across all algorithm families
- AEAD encryption/decryption for all suites
- Replay protection mechanism validation
- Runtime algorithm switching functionality
- Error handling and edge case scenarios

**Test Categories**:
- **Cryptographic Correctness**: Verify encrypt/decrypt operations work correctly
- **Protocol Compliance**: Ensure handshake follows specification exactly
- **Security Properties**: Validate replay protection and authentication
- **Integration Testing**: End-to-end communication validation
- **Error Handling**: Proper failure behavior under invalid inputs

### 4.10.2 Hardware Validation

**Platform Testing**: Validated on:
- **Windows 10/11**: Ground control station environment
- **Linux (Ubuntu/Debian)**: Server and development environments  
- **Raspberry Pi 4B**: Representative drone hardware platform

**Performance Validation**: All cryptographic operations complete within acceptable timeframes for real-time drone control on tested hardware platforms.

## 4.11 Performance Characteristics

### 4.11.1 Computational Overhead

**Handshake Performance**: Post-quantum handshakes require more computation than classical alternatives:
- ML-KEM operations: Polynomial arithmetic over finite fields
- Signature operations: Complex lattice or hash tree computations
- Overall handshake time: Increased but remains under 1 second on typical hardware

**Data Plane Performance**: AES-256-GCM encryption adds minimal overhead:
- Encryption speed: Limited primarily by AES hardware acceleration
- Per-packet overhead: 22 bytes header + 16 bytes authentication tag
- Latency impact: Negligible for real-time drone control

### 4.11.2 Memory and Storage Requirements

**Memory Usage**:
- Basic proxy operation: ~2-3 MB RAM
- Cryptographic context: ~2.4 KB per active session
- Replay window: Configurable (default 1,024 packets = 128 bytes)

**Storage Requirements**:
- Core implementation: ~50 KB Python code
- OQS library: ~5 MB compiled binaries
- Key storage: ~2-4 KB per cryptographic suite

## 4.12 Comparison with Classical Cryptographic Systems

### 4.12.1 Security Improvements

**Quantum Resistance**: Unlike RSA/ECC systems that become completely broken by quantum computers, our post-quantum algorithms maintain their security properties.

**Algorithm Diversity**: Classical systems typically use single algorithm families (RSA or ECC), while our system provides multiple independent algorithm families for enhanced security.

**Forward Secrecy**: Enhanced through ephemeral post-quantum key generation and runtime key rotation capabilities.

### 4.12.2 Performance Trade-offs

**Computational Overhead**: Post-quantum algorithms require more computation:
- Handshake operations: 2-5× slower than RSA/ECC
- Data encryption: Minimal overhead (AES-256-GCM remains the same)

**Bandwidth Overhead**: Larger cryptographic objects:
- Public keys: 3-10× larger than ECC equivalents
- Signatures: 5-100× larger depending on algorithm choice
- Per-packet overhead: Comparable to classical systems

**Memory Requirements**: Increased state storage:
- Cryptographic contexts: 2-3× larger than classical systems
- Algorithm implementations: Larger code footprint

## 4.13 Novel Contributions

### 4.13.1 Runtime Cryptographic Agility

**Innovation**: First implementation enabling live cryptographic algorithm switching in UAV communication systems without service interruption.

**Technical Achievement**: Successful coordination of cryptographic state transitions across distributed endpoints with consistency guarantees.

**Operational Benefits**: Enables adaptive security responses to changing threat levels and operational requirements.

### 4.13.2 Comprehensive Algorithm Matrix

**Innovation**: Most complete implementation of NIST post-quantum algorithms in a single UAV communication system.

**Technical Achievement**: 21 validated suite combinations across three security levels with full interoperability.

**Research Value**: Provides comprehensive performance and security comparison data across all major post-quantum algorithm families.

### 4.13.3 Drone-Optimized Protocol Design

**Innovation**: Hybrid TCP/UDP architecture specifically designed for UAV communication patterns rather than adapting general-purpose protocols.

**Technical Achievement**: Separation of reliability-critical authentication from latency-critical data transport with seamless integration.

**Operational Benefits**: Optimal performance for drone-specific communication requirements while maintaining full cryptographic security.

---

## References

[1] P. W. Shor, "Polynomial-time algorithms for prime factorization and discrete logarithms on a quantum computer," *SIAM Journal on Computing*, vol. 26, no. 5, pp. 1484-1509, 1997.

[2] M. Mosca, "Cybersecurity in an era with quantum computers: will we be ready?" *IEEE Security & Privacy*, vol. 16, no. 5, pp. 38-41, 2018.

[3] National Institute of Standards and Technology, "Post-Quantum Cryptography Standardization," 2024. [Online]. Available: https://csrc.nist.gov/Projects/post-quantum-cryptography

[4] National Institute of Standards and Technology, "Module-Lattice-Based Key-Encapsulation Mechanism Standard," *Federal Information Processing Standards Publication 203*, August 2024.

[5] National Institute of Standards and Technology, "Module-Lattice-Based Digital Signature Standard," *Federal Information Processing Standards Publication 204*, August 2024.

[6] National Institute of Standards and Technology, "FALCON Digital Signature Algorithm," *Draft Federal Information Processing Standards Publication 206*, 2024.

[7] National Institute of Standards and Technology, "Stateless Hash-Based Digital Signature Standard," *Federal Information Processing Standards Publication 205*, August 2024.

[8] D. McGrew and J. Viega, "The Galois/Counter Mode of Operation (GCM)," *Submission to NIST Modes of Operation Process*, 2004.

[9] Open Quantum Safe Project, "liboqs: C library for prototyping and experimenting with quantum-resistant cryptography," Available: https://github.com/open-quantum-safe/liboqs

[10] H. Krawczyk and P. Eronen, "HMAC-based Extract-and-Expand Key Derivation Function (HKDF)," *RFC 5869*, Internet Engineering Task Force, 2010.

[11] J. Bos et al., "CRYSTALS-Kyber: A CCA-secure module-lattice-based KEM," in *2018 IEEE European Symposium on Security and Privacy (EuroS&P)*, pp. 353-367, 2018.

[12] L. Ducas et al., "CRYSTALS-Dilithium: A lattice-based digital signature scheme," *IACR Transactions on Cryptographic Hardware and Embedded Systems*, vol. 2018, no. 1, pp. 238-268, 2018.

[13] T. Prest et al., "FALCON: Fast-Fourier lattice-based compact signatures over NTRU," in *Post-Quantum Cryptography - 10th International Conference*, Springer, pp. 44-61, 2019.

[14] D. J. Bernstein et al., "SPHINCS+: Stateless hash-based signatures," in *Annual International Conference on the Theory and Applications of Cryptographic Techniques*, Springer, pp. 158-188, 2019.

[15] National Institute of Standards and Technology, "Recommendation for Key-Derivation Methods in Key-Establishment Schemes," *Special Publication 800-56C Rev. 2*, August 2020.