# 4. Cryptographic Framework

This section presents the post-quantum cryptographic framework developed for securing unmanned aerial vehicle (UAV) communication systems against quantum computational threats. Our implementation provides a comprehensive solution that addresses the unique operational constraints of drone-to-ground control station communication while ensuring cryptographic agility and forward-looking security guarantees.

## 4.1 Quantum Threat Analysis and System Requirements

The emergence of quantum computing fundamentally challenges the mathematical foundations of contemporary public-key cryptography. Shor's polynomial-time quantum algorithm [1] for integer factorization and discrete logarithm computation renders RSA, DSA, and elliptic curve cryptographic systems vulnerable to attack by sufficiently large quantum computers. Current estimates suggest that a quantum computer with approximately 2,000-4,000 logical qubits could break RSA-2048, while 13 million physical qubits may be required when accounting for quantum error correction overhead [2].

The timeline for quantum computational threats exhibits significant uncertainty, with expert assessments ranging from 15-50 years for cryptographically relevant quantum computers [3]. However, the cryptographic shelf-life problem, wherein encrypted data must remain secure for decades beyond initial deployment, creates immediate vulnerability. UAV systems, with operational lifespans exceeding 20-30 years and mission-critical data requiring long-term confidentiality, face acute exposure to this threat model.

Our threat analysis indicates that drone communication systems require immediate quantum-resistant upgrade when the required security lifetime exceeds the estimated time until quantum computers arrive, minus the minimum migration time for complete system transition. For UAV systems, conservative estimates yield security lifetimes of 30 years or more, quantum computer arrival times between 15-50 years, and minimum migration times of 5 years for complete fleet transition, necessitating immediate post-quantum cryptographic deployment.

The NIST Post-Quantum Cryptography Standardization process [4] has identified quantum-resistant algorithms based on mathematical problems believed intractable for both classical and quantum computers. Our framework leverages these standardized algorithms while addressing the unique operational characteristics of UAV communication systems through novel protocol architecture and runtime cryptographic agility mechanisms.

## 4.2 Post-Quantum Algorithm Selection and Analysis

Our cryptographic framework incorporates four distinct post-quantum algorithm families, selected to provide cryptographic diversity and address different operational requirements within UAV communication systems. The selection process prioritized NIST-standardized algorithms with proven security foundations and implementation maturity suitable for safety-critical drone operations.

### 4.2.1 Module Learning With Errors Key Encapsulation

The Module Learning with Errors (MLWE) problem constitutes the security foundation for our key establishment mechanism. Formally, the MLWE problem asks to distinguish between uniformly random samples and samples derived from secret polynomials with small error terms over polynomial rings. The security of MLWE reduces to worst-case lattice problems including the Shortest Vector Problem (SVP) and the Closest Vector Problem (CVP) over module lattices [6].

Our implementation employs ML-KEM (Module Learning with Errors Key Encapsulation Mechanism) as specified in FIPS 203 [5]. ML-KEM operates over the polynomial ring $R_q = \mathbb{Z}_q[X]/(X^n + 1)$ where $q$ is prime and $n$ is a power of 2. The algorithm provides three security levels corresponding to different parameter sets. ML-KEM-512 provides NIST Level 1 security (128-bit post-quantum security) using module rank 2, ML-KEM-768 provides NIST Level 3 security (192-bit post-quantum security) using module rank 3, and ML-KEM-1024 provides NIST Level 5 security (256-bit post-quantum security) using module rank 4.

The key generation process samples secret and error polynomials from centered binomial distributions, computes public key components through polynomial arithmetic modulo $q$, and applies compression techniques to minimize key sizes. Encapsulation uses the public key to generate a shared secret and ciphertext through additional polynomial sampling and computation. Decapsulation recovers the shared secret using the private key through inverse polynomial operations. The best known classical attack against ML-KEM-768 requires approximately $2^{164}$ operations using lattice reduction techniques, while quantum attacks using Grover's algorithm provide at most quadratic speedup, requiring $2^{82}$ quantum operations.

### 4.2.2 Module Lattice-Based Digital Signatures

ML-DSA (Module Lattice-Based Digital Signature Algorithm) provides digital signature functionality based on the Fiat-Shamir transformation applied to a commitment scheme over module lattices, as specified in FIPS 204 [7]. The security foundation rests on two computational problems: the Module Short Integer Solution (MSIS) problem and the Module Learning with Errors (MLWE) problem. The MSIS problem asks to find a non-zero vector with bounded infinity norm that satisfies a system of linear equations over polynomial rings.

Our implementation supports three ML-DSA parameter sets. ML-DSA-44 provides compact signatures averaging 2,420 bytes with good performance characteristics suitable for bandwidth-constrained scenarios. ML-DSA-65 provides balanced security-performance with signature sizes averaging 3,293 bytes, representing our recommended configuration for most operational scenarios. ML-DSA-87 provides maximum security with signature sizes averaging 4,627 bytes for applications requiring highest security margins.

The signature generation process employs rejection sampling to ensure that signatures leak no information about the secret key beyond what is implied by the public key. The algorithm samples random polynomials, computes commitment values through polynomial multiplication, generates challenge polynomials using cryptographic hash functions, and computes response polynomials. If the response polynomials exceed predetermined bounds, the process restarts with fresh randomness. This rejection sampling technique provides provable security under the MSIS and MLWE assumptions while maintaining constant-time execution properties essential for side-channel resistance.

### 4.2.3 NTRU Lattice-Based Compact Signatures

Falcon constructs digital signatures over NTRU lattices using the Gaussian sampling paradigm, as specified in Draft FIPS 206 [8]. The scheme exploits the special structure of NTRU lattices to achieve exceptionally compact signatures through efficient trapdoor sampling. Given polynomials $f$ and $g$ over the polynomial ring $\mathbb{Z}[X]/(X^n + 1)$ where $f$ is invertible modulo $q$, the associated NTRU lattice is defined by the set of polynomial pairs $(u, v)$ such that $h \cdot u \equiv v \pmod{q}$ where $h = g \cdot f^{-1} \pmod{q}$ is the NTRU public key.

Our implementation supports two Falcon parameter sets. Falcon-512 operates with ring dimension 512 and produces signatures averaging 666 bytes with public keys of 897 bytes. Falcon-1024 operates with ring dimension 1024 and produces signatures averaging 1,280 bytes with public keys of 1,793 bytes. The key generation process generates NTRU polynomials with coefficients in the set $\{-1, 0, 1\}$, verifies invertibility conditions, computes the public key, and constructs an extended lattice basis for efficient Gaussian sampling.

Signature generation employs a tree-based Fast Fourier Transform (FFT) structure to sample from discrete Gaussian distributions over the NTRU lattice. For a given message hash, the algorithm computes a target point, uses the FFT tree structure to sample lattice vectors from the appropriate Gaussian distribution, verifies geometric constraints, and compresses the signature using variable-length encoding. The compact signature size results from exploiting the geometric properties of NTRU lattices and efficient Gaussian sampling techniques, making Falcon particularly suitable for bandwidth-limited communication channels.

### 4.2.4 Hash-Based Signatures with Provable Security

SLH-DSA (Stateless Hash-Based Digital Signature Algorithm) represents a fundamentally different approach to post-quantum signatures, deriving security exclusively from cryptographic hash function properties rather than number-theoretic or lattice-based assumptions, as specified in FIPS 205 [9]. This conservative design philosophy provides security guarantees that remain valid even under potential breakthroughs in lattice or code-based cryptanalysis.

The algorithm constructs a many-time signature scheme by combining three cryptographic primitives: WOTS+ (Winternitz One-Time Signatures), FORS (Forest of Random Subsets), and XMSS (eXtended Merkle Signature Scheme). WOTS+ creates one-time signatures based on Winternitz chaining of hash function evaluations. For a message digest represented in base $w$, WOTS+ computes signature chains where each signature component is derived by applying the hash function a number of times determined by the corresponding message digit. A checksum prevents forgery attempts by ensuring that signature components cannot be shortened.

FORS provides few-time signatures for signing multiple messages by partitioning message digests into indices and revealing authentication paths from randomly selected leaves in multiple Merkle trees. The hypertree construction stacks multiple XMSS trees in layers, with layer 0 containing FORS signatures authenticated by XMSS, and layer 1 containing XMSS signatures of layer 0 roots with top-level authentication. The public key consists of the root hash of the top-level XMSS tree.

Our implementation supports two SLH-DSA parameter sets. SLH-DSA-SHA2-128f provides 128-bit post-quantum security with signature sizes of approximately 7,856 bytes using SHA-256 with 16-byte output truncation. SLH-DSA-SHA2-256f provides 256-bit post-quantum security with signature sizes of approximately 29,792 bytes. The security reduction for SLH-DSA provides direct reduction to hash function one-wayness and collision resistance through explicit, tight bounds, offering exceptional confidence in long-term security based solely on well-understood hash function properties.

## 4.3 Hybrid Transport Protocol Architecture

Our protocol architecture addresses the fundamental tension between reliability requirements for cryptographic handshakes and latency requirements for operational data through a hybrid transport approach that separates authentication establishment from data transmission. This design optimizes for the dual requirements of drone communication: reliable cryptographic establishment and low-latency data transmission critical for real-time flight control systems.

The transport layer separation employs TCP for the handshake protocol and UDP for encrypted data transport. The TCP channel handles the post-quantum handshake protocol with reliable delivery guarantees essential for cryptographic material exchange, implementing timeout-based connection management and mutual authentication through digital signatures and pre-shared key mechanisms. The UDP channel carries encrypted application data using optimized Authenticated Encryption with Associated Data (AEAD) framing, minimizing latency for time-critical drone telemetry while maintaining cryptographic protection through AES-256-GCM authenticated encryption.

The system operates as a finite state automaton with states including initialization, handshake execution, transport operation, cryptographic rekeying, and error handling. State transitions are triggered by network events and cryptographic operations, with accepting states representing normal data flow conditions. This separation enables protocol optimization specific to each communication phase while maintaining cryptographic synchronization between endpoints.

### 4.3.1 Post-Quantum Handshake Protocol

Our handshake protocol establishes mutually authenticated session keys through a carefully designed two-message exchange that prevents downgrade attacks and provides forward secrecy. The protocol participants include the Ground Control Station (GCS) acting in server role and the Unmanned Aerial Vehicle (UAV) acting in client role. The cryptographic primitives employed include ML-KEM for key encapsulation, digital signature schemes for authentication, HKDF for key derivation following RFC 5869, and HMAC for message authentication.

The protocol execution begins with the GCS generating session parameters including a random 64-bit session identifier and 64-bit challenge value. The GCS generates an ephemeral ML-KEM keypair and constructs a cryptographic transcript consisting of protocol version identifier, session identifier, negotiated algorithm identifiers, ephemeral public key, and challenge value. The GCS generates a digital signature over this transcript using its long-term signature key and transmits the complete server hello message containing version, algorithm identifiers, session identifier, challenge, public key, and signature.

The UAV parses the server hello message, reconstructs the cryptographic transcript, and performs mandatory signature verification using the GCS's pre-configured public signature key. If signature verification fails, the handshake terminates immediately to prevent downgrade attacks. Upon successful verification, the UAV encapsulates a shared secret using the received ephemeral public key, generating both ciphertext and shared secret. The UAV computes an HMAC authentication tag over the received server hello using a pre-shared key to provide bidirectional authentication, then transmits the client response containing ciphertext and authentication tag.

Both parties derive identical session keys using HKDF-SHA256 with the shared secret as input keying material, a fixed salt "pq-drone-gcs|hkdf|v1", and an info parameter containing session identifier and algorithm names. The 64-byte output is split into directional 32-byte AES-256 keys for drone-to-GCS and GCS-to-drone communication. The handshake completes with both endpoints possessing session-specific AES-256 keys for bidirectional encrypted communication, with distinct keys ensuring proper cryptographic separation between communication directions.

### 4.3.2 Authenticated Encryption Data Plane

Following successful handshake completion, all application data transmits through an optimized authenticated encryption channel designed for the specific requirements of UAV communication. Each encrypted packet follows a structured format consisting of a 22-byte plaintext header followed by AES-256-GCM encrypted data and authentication tag. The header contains protocol version identifier, cryptographic suite identifiers, session identifier from handshake, monotonic sequence number, and cryptographic epoch counter.

The header format includes a protocol version byte to prevent downgrade attacks, four bytes of cryptographic suite identifiers specifying the KEM, KEM parameters, signature algorithm, and signature parameters, eight bytes of session identifier linking packets to the handshake session, eight bytes of sequence number providing ordering and replay protection, and one byte of epoch counter supporting cryptographic context transitions. This header structure serves as Additional Authenticated Data (AAD) for AES-GCM operations, ensuring that header modifications are detected during decryption.

To eliminate nonce transmission overhead while maintaining cryptographic security, we employ deterministic nonce generation by concatenating the epoch counter and sequence number to form 96-bit nonces. This construction ensures uniqueness within each cryptographic epoch while reducing per-packet overhead by 12 bytes compared to traditional random nonce approaches. Epoch transitions require new session key derivation to prevent nonce reuse, maintaining the security properties required for AES-GCM operation.

The system implements a sliding window anti-replay mechanism based on RFC 3711 with configurable window sizes defaulting to 1,024 packets. The receiver maintains a high watermark representing the highest sequence number received and a bitmask tracking recently received packet sequence numbers within the window. Future packets beyond the high watermark cause window advancement with appropriate bitmask updates. Packets within the current window are checked against the bitmask to detect duplicates, while packets outside the window are rejected as too old. This algorithm provides constant-time replay detection while accommodating legitimate packet reordering in UDP transport.

## 4.4 Runtime Cryptographic Agility

A novel contribution of our framework is the runtime cryptographic suite transition capability, enabling dynamic algorithm adaptation during active communication sessions without service interruption. This mechanism addresses evolving cryptographic requirements and potential algorithm vulnerabilities by providing seamless transitions between different post-quantum algorithm combinations while maintaining communication continuity.

The system implements a typed packet mechanism that multiplexes control plane and data plane traffic within the same encrypted channel. Packet type classification distinguishes between application data packets marked with type 0x01 and control plane messages marked with type 0x02. Control packets are authenticated and encrypted within the existing session context, ensuring that cryptographic negotiations cannot be tampered with by network adversaries.

Control messages follow a structured format including message type field specifying operations such as prepare_rekey and commit_rekey, target suite field identifying the proposed cryptographic suite, nonce field providing replay protection for control messages, and type-specific payload data. The control channel architecture ensures that algorithm switching negotiations are protected by the same cryptographic mechanisms as application data, preventing man-in-the-middle attacks on the switching protocol itself.

Cryptographic suite transitions employ a distributed consensus mechanism through a two-phase commit protocol ensuring atomic updates across both communication endpoints. The preparation phase begins with the initiator sending prepare_rekey messages specifying the target cryptographic suite. The responder validates suite availability and compatibility with current operational requirements, then replies with commit_rekey confirmation or prepare_fail rejection. The execution phase proceeds upon commit_rekey confirmation, with both parties initiating a new handshake using the target algorithms, deriving fresh session keys with incremented epoch counter, atomically switching to the new cryptographic context, and resuming data transmission with updated protection parameters.

The two-phase protocol ensures that both endpoints transition simultaneously, preventing cryptographic mismatch conditions where endpoints operate with incompatible algorithm suites. Consistency guarantees maintain communication continuity with high probability, limited only by network partition events during the commit phase. This approach provides robust algorithm switching capabilities while preserving the security properties established during initial handshake execution.

## 4.5 Security Analysis and Formal Properties

Our security analysis follows the computational indistinguishability paradigm, modeling adversaries as probabilistic polynomial-time algorithms with access to quantum computers. The adversary model considers quantum-capable adversaries with quantum polynomial-time computation capabilities, network message interception and injection abilities, adaptive corruption of long-term keys with forward secrecy limitations, and access to protocol transcripts and timing information.

The security objectives include message confidentiality through computational indistinguishability under chosen-ciphertext attacks, message origin authentication with strong unforgeability, integrity detection of any message modification attempts, perfect forward secrecy maintaining past session security despite future key compromise, and replay protection preventing message replay attacks within configurable time windows.

Under the computational hardness assumptions of ML-KEM, ML-DSA, and the security of AES-256-GCM, our protocol achieves IND-CCA2 security for message confidentiality, sEUF-CMA security for message authentication, perfect forward secrecy for past sessions, and replay protection with configurable time windows. The post-quantum security levels provided by our implementation correspond to NIST-defined categories: Level 1 equivalent to breaking AES-128 requiring $2^{64}$ quantum operations via Grover's algorithm, Level 3 equivalent to breaking AES-192 requiring $2^{96}$ quantum operations, and Level 5 equivalent to breaking AES-256 requiring $2^{128}$ quantum operations.

The availability of multiple algorithm families provides protection against algorithmic breakthroughs through cryptographic diversity. Lattice-based algorithms including ML-KEM and ML-DSA share mathematical foundations but address different computational problems. NTRU-based Falcon provides alternative lattice structure reducing dependence on module lattice assumptions. Hash-based SLH-DSA offers conservative security foundation independent of number-theoretic or lattice assumptions. This diversity ensures that breakthroughs against any single mathematical foundation do not compromise the entire system.

## 4.6 Implementation Architecture and Integration

The implementation follows a modular design separating cryptographic concerns from network protocol logic through five core components. The suite registry in `core/suites.py` manages all 21 cryptographic suite combinations, provides algorithm parameter lookup and validation, handles legacy algorithm name aliases, and checks algorithm availability at runtime through Open Quantum Safe library integration.

The handshake protocol implementation in `core/handshake.py` provides the two-message post-quantum handshake with ML-KEM key encapsulation and decapsulation, digital signature creation and verification, HKDF-SHA256 key derivation, and mandatory signature verification with downgrade attack prevention. The authenticated encryption module in `core/aead.py` implements AES-256-GCM packet encryption and decryption, deterministic nonce generation, sliding window replay protection, and packet header authentication.

The policy engine in `core/policy_engine.py` manages the cryptographic suite switching state machine, processes control messages for algorithm changes, enforces security policies and constraints, and coordinates the two-phase commit protocol. The network coordination module in `core/async_proxy.py` manages TCP handshake connections, handles UDP encrypted data transport, coordinates between plaintext and encrypted channels, and provides error handling and recovery mechanisms.

Algorithm integration leverages the Open Quantum Safe (OQS) library [10] providing NIST-approved post-quantum algorithm implementations, constant-time operations preventing side-channel attacks, cross-platform compatibility across Windows, Linux, and embedded systems, and regular security updates with algorithm optimizations. The modular architecture enables straightforward algorithm updates and additions while maintaining interface stability for application integration.

## 4.7 Validation and Performance Analysis

The implementation includes comprehensive validation through 109 automated test functions covering cryptographic correctness verification of all 21 suite combinations, protocol compliance ensuring handshake follows specifications exactly, security properties validation including replay protection and authentication, integration testing for end-to-end communication validation, and error handling verification for proper failure behavior under invalid inputs.

Critical protocol components undergo formal verification including nonce uniqueness proofs for deterministic generation, replay window correctness under packet reordering scenarios, key derivation security in multi-party settings, and protocol state machine safety properties. Hardware validation demonstrates feasibility for resource-constrained UAV platforms with deployment validation on Windows 10/11 ground control station environments, Linux server and development environments, and Raspberry Pi 4B representative drone hardware platforms.

Performance analysis indicates that post-quantum handshakes require increased computation compared to classical alternatives, with ML-KEM operations involving polynomial arithmetic over finite fields and signature operations requiring complex lattice or hash tree computations. Overall handshake completion times remain under one second on typical hardware platforms. Data plane performance shows that AES-256-GCM encryption adds minimal overhead, with encryption speed limited primarily by AES hardware acceleration availability, per-packet overhead of 38 bytes for header and authentication tag, and negligible latency impact for real-time drone control applications.

Memory and storage requirements include basic proxy operation consuming 2-3 MB RAM, cryptographic context requiring 2.4 KB per active session, configurable replay window consuming 128 bytes for default 1,024 packet window, core implementation occupying 50 KB Python code, OQS library requiring 5 MB compiled binaries, and key storage needing 2-4 KB per cryptographic suite. These requirements demonstrate feasibility for deployment on resource-constrained drone platforms while maintaining full cryptographic functionality.

## 4.8 Comparative Analysis and Novel Contributions

Compared to classical RSA/ECC-based drone communication systems, our post-quantum implementation provides quantum-resistant security with measured performance trade-offs. Security enhancements include quantum-resistant algorithms maintaining security properties against quantum computers, algorithm diversity through multiple independent algorithm families, and enhanced forward secrecy through ephemeral post-quantum key generation and runtime key rotation capabilities.

Performance trade-offs include computational overhead with handshake operations 2-5 times slower than RSA/ECC equivalents while data encryption maintains comparable performance through continued use of AES-256-GCM. Bandwidth overhead includes larger cryptographic objects with public keys 3-10 times larger than ECC equivalents and signatures 5-100 times larger depending on algorithm choice, while per-packet overhead remains comparable to classical systems. Memory requirements increase by 2-3 times for cryptographic contexts and larger code footprint for algorithm implementations.

Our novel contributions include runtime cryptographic agility as the first implementation enabling live cryptographic algorithm switching in UAV communication systems without service interruption, representing a significant technical achievement in maintaining cryptographic state consistency across distributed endpoints. The comprehensive algorithm matrix provides the most complete implementation of NIST post-quantum algorithms in a single UAV communication system, with 21 validated suite combinations across three security levels demonstrating full interoperability.

The drone-optimized protocol design features hybrid TCP/UDP architecture specifically designed for UAV communication patterns rather than adapting general-purpose protocols. This innovation separates reliability-critical authentication from latency-critical data transport with seamless integration, providing optimal performance for drone-specific communication requirements while maintaining full cryptographic security. These contributions advance the state of the art in post-quantum cryptography for safety-critical UAV systems while providing a foundation for future research in adaptive security mechanisms.

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