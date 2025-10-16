A Post-Quantum Secure Command and Control Stack for UAVs: Architecture, Implementation, and Performance Analysis
The transition to post-quantum cryptography (PQC) is a critical imperative for securing long-lived, high-value assets against the emergent threat of quantum computing. For Unmanned Aerial Vehicles (UAVs), the command and control (C2) link represents a primary attack surface where the compromise of classical cryptographic protocols could lead to catastrophic failure. This paper presents the architecture, implementation, and rigorous performance analysis of a complete, end-to-end PQC-secured C2 stack designed for resource-constrained aerial platforms. The proposed architecture combines a robust PQC handshake protocol, which uses a Key Encapsulation Mechanism (KEM) and digital signatures for authenticated key establishment, with an efficient Authenticated Encryption with Associated Data (AEAD) framing scheme optimized to reduce per-packet overhead. To ensure operational resilience, an expert-policy scheduler dynamically adapts the data rate to maintain link stability under adverse network conditions. The core contribution of this work is a comprehensive empirical evaluation of 30 distinct PQC suites, quantifying the trade-offs in network throughput, latency, cryptographic operation overhead, and on-device power consumption under a simulated lightweight denial-of-service attack, providing a practical blueprint for deploying quantum-resistant security in real-world UAV systems.
--------------------------------------------------------------------------------
1.0 Introduction
The strategic importance of Unmanned Aerial Vehicles (UAVs) in both civilian and military domains relies fundamentally on the integrity and confidentiality of their command and control (C2) links. These links, responsible for transmitting mission-critical telemetry and operator commands, have traditionally been secured using classical public-key cryptography, such as RSA and Elliptic Curve Cryptography (ECC). However, the development of large-scale quantum computers poses a direct and existential threat to these algorithms, rendering them insecure and exposing sensitive C2 channels to interception and manipulation by sophisticated adversaries. This necessitates a proactive migration to Post-Quantum Cryptography (PQC)—a new generation of cryptographic primitives believed to be resistant to attacks from both classical and quantum computers. This paper demonstrates that while many PQC families are theoretically secure, only specific classes of algorithms—primarily those based on structured lattices—meet the stringent real-world performance requirements of UAV C2 links. For UAV platforms, which are often characterized by constrained computational power, memory, and energy budgets, the theoretical security of PQC must be validated by practical, performance-evaluated implementations. The primary goal of this paper is to present the design, implementation, and rigorous evaluation of a complete PQC-secured C2 stack, analyzing the real-world performance trade-offs to guide the selection of viable algorithms for this demanding environment.
1.1 Contributions
The primary contributions of this work are as follows:
System Architecture and Protocol Design: We present the design of a complete C2 stack featuring a robust PQC handshake protocol for authenticated key exchange using a KEM and digital signatures. This is complemented by an efficient AEAD framing scheme that utilizes a non-transmitted Initialization Vector (IV) to reduce per-packet network overhead by 12 bytes and an in-band control plane for seamless cryptographic agility, allowing for on-the-fly rekeying to different PQC suites without link disruption.
Adaptive Scheduling Policy: We detail an expert-policy scheduler that dynamically probes the communication channel's capacity to determine the maximum sustainable data rate. The scheduler employs heuristics based on one-way delay latency spikes and packet loss thresholds to gracefully degrade performance under adverse network conditions, thereby preserving the stability of the C2 link.
Comprehensive Empirical Evaluation: We provide an in-depth performance and power analysis of 30 distinct PQC suites, comprising various combinations of KEMs and digital signatures, under a simulated network condition labeled ddos =xgboost (lightweight ). This evaluation quantifies the practical trade-offs in network throughput, latency, cryptographic operation overhead, CPU and memory utilization, and total energy consumption, offering a vital, data-driven guide for system architects selecting PQC algorithms for resource-constrained platforms.
This paper first details the system architecture, then describes the implementation and testbed instrumentation, followed by the experimental methodology. Finally, we present a detailed analysis of the results and offer concluding remarks.
2.0 System Architecture
The system is composed of two primary components: a Ground Control Station (GCS) and a Drone. On each endpoint, a secure proxy application operates transparently, intercepting plaintext UDP traffic from a local application (e.g., a flight controller or mission planner), encrypting it, and transmitting it over a secure UDP channel. The proxy on the receiving end decrypts the traffic and forwards the original plaintext UDP packet to the local application. This design creates a transparently encrypted tunnel, requiring no modification to the underlying C2 application software. All communication between the local application and the proxy uses plaintext UDP, while the over-the-air link between the GCS and Drone proxies is secured with the post-quantum transport protocol.
2.1 Post-Quantum Secure Transport Protocol
The core of the secure transport protocol consists of two distinct phases: a one-time, TCP-based handshake to authenticate the parties and establish shared symmetric keys, and a subsequent, highly efficient AEAD framing scheme for all UDP data packets.
2.1.1 PQC Handshake Protocol
The handshake protocol is executed over TCP to ensure reliable delivery of cryptographic materials. The GCS acts as the server and the Drone as the client. The message flow is as follows:
Server Hello: The GCS initiates the connection by sending a ServerHello message. This message contains the KEM and Signature algorithm names as UTF-8 encoded byte strings, each prefixed with a 2-byte length. It also includes a fresh 8-byte session ID, its ephemeral KEM public key, and an 8-byte random challenge. The entire ServerHello payload is digitally signed using the GCS's long-term PQC signature private key.
Client Verification and Encapsulation: Upon receiving the ServerHello, the Drone verifies the GCS's signature using its pre-configured GCS public key. If the signature is valid, the Drone proceeds to encapsulate a shared secret against the KEM public key provided by the GCS. This operation generates a ciphertext that can only be decrypted by the GCS.
Client Response and Authentication: The Drone sends the KEM ciphertext back to the GCS. Concurrently, it authenticates itself by computing an HMAC-SHA256 tag over the entire received ServerHello message using a pre-shared key (DRONE_PSK). This tag is transmitted to the GCS alongside the KEM ciphertext.
Key Derivation: The GCS receives the response, authenticates the Drone by verifying the HMAC tag, and uses its KEM private key to decapsulate the shared secret from the ciphertext. With the shared secret now known to both parties, the GCS and the Drone independently use the secret and the session ID as input to an HKDF based on SHA-256 (HKDF-SHA256) to derive symmetric keys for the AEAD transport layer.
2.1.2 AEAD Framing and Replay Protection
Once the handshake is complete, all subsequent UDP traffic is encrypted using an AEAD cipher. The framing is designed for minimal overhead and robust security.
Wire Format: Each encrypted packet is prepended with a 22-byte header. This header, defined by the HEADER_STRUCT format !BBBBB8sQB, contains the protocol version (1 byte), KEM and signature algorithm identifiers (5 bytes), the session ID (8 bytes), a sequence number (8 bytes), and an epoch (1 byte). This header is used as associated data in the AEAD operation, ensuring its integrity.
Supported Ciphers: The stack is implemented to support multiple high-performance AEAD ciphers, including AES-256-GCM and ChaCha20-Poly1305, allowing for cryptographic agility at the symmetric layer as well.
Optimized IV Handling: A critical optimization is the elimination of an explicit Initialization Vector (IV) from the wire format. A deterministic 96-bit nonce is constructed locally on both the sender and receiver side using the formula bytes([epoch & 0xFF]) + seq.to_bytes(11, "big"). Because the epoch and sequence number are already present in the header, the nonce does not need to be transmitted, saving 12 bytes of overhead on every single packet.
Replay Protection: To prevent replay attacks, the receiver implements a sliding window mechanism. Based on the configuration in config.py, this window maintains a state for the last 1024 received sequence numbers. Any packet arriving with a sequence number that is stale (older than the window) or has already been seen is immediately rejected, ensuring each packet is processed only once.
2.2 Adaptive Scheduler with Graceful Degradation
The system incorporates an expert-policy scheduler, implemented in the SaturationTester class, to maintain link stability in contested or degraded network environments. The scheduler actively probes the channel by sending traffic at various rates to determine the maximum sustainable data rate, or saturation point. It monitors the link quality and gracefully degrades the transmission rate upon detecting signs of congestion. Link degradation is determined using a combination of the following signals:
owd_p95_spike: A significant increase in the 95th percentile one-way delay (OWD) of packets compared to a stable baseline, indicating growing network queues.
delivery_degraded: A drop in the ratio of goodput (application data received) to the send rate, falling below a configured threshold of 0.85 (sat_delivery_threshold). This signals that the network is unable to deliver the offered load.
loss_excess: A packet loss percentage that exceeds a configured threshold of 5.0% (sat_loss_threshold_pct), indicating severe congestion.
2.3 In-Band Control Plane for Rekeying
To support cryptographic agility, the protocol includes an in-band control plane for changing PQC suites on-the-fly without tearing down the secure channel. This mechanism, detailed in core/policy_engine.py, uses a two-phase commit protocol. A rekey is initiated by sending a prepare_rekey message, followed by a commit_rekey message upon acknowledgment. These control messages are encapsulated within special packets identified by a type field of 0x02 and are sent over the established secure AEAD channel, ensuring their confidentiality and integrity.
This architecture provides a robust foundation for secure post-quantum communications. The following sections detail its implementation and the methods used to measure its real-world performance.
3.0 Implementation and Instrumentation
The secure proxy is implemented in Python, leveraging the oqs-python library for the underlying PQC primitives and the standard selectors module for efficient, non-blocking asynchronous I/O, as detailed in core/async_proxy.py. This approach provides a portable and performant solution suitable for both the GCS and embedded drone platforms.
3.1 Testbed Instrumentation
To capture detailed performance data, the system is heavily instrumented at multiple levels, with primary orchestration logic contained within the gcs_scheduler module.
Traffic Generation and Network Measurement: A custom Blaster class serves as a high-rate UDP traffic generator. It creates a constant packet rate to saturate the C2 link for testing. Each packet sent by the Blaster embeds a high-precision timestamp, which is used by the receiver to calculate critical network performance metrics, including round-trip time (RTT) and one-way delay (OWD) percentiles.
Drone Telemetry Collection: A TelemetryCollector class runs on the GCS, listening for a JSON-based telemetry stream transmitted by the drone. This allows for the collection of on-device performance metrics in real-time. The collected telemetry includes CPU utilization (cpu_percent), Resident Set Size memory usage (rss_bytes), and flight kinematics data such as predicted flight constraint (predicted_flight_constraint_w), horizontal velocity (velocity_horizontal_mps), and vertical velocity (velocity_vertical_mps).
Power Measurement: Power consumption is measured directly on the drone hardware using an INA219 high-side current sensor. The testbed is instrumented to remotely trigger power captures for each experimental run via control messages (request_power_capture) and poll for the results (poll_power_status), ensuring that energy consumption is precisely correlated with the cryptographic workload of each PQC suite.
This instrumentation provides a comprehensive view of the system's performance, from network behavior down to hardware resource consumption, which is essential for the experimental evaluation.
4.0 Experimental Methodology
A series of experiments were conducted to systematically quantify the performance of the PQC-secured C2 stack across a wide and diverse range of post-quantum algorithms. The goal was to establish a clear understanding of the operational trade-offs associated with each cryptographic family in a realistic UAV context.
4.1 Testbed and Configuration
The experiments were conducted on a testbed consisting of a GCS host at IP address 192.168.1.207 and a Drone host at 192.168.1.139. For each of the 30 PQC suites under evaluation, a dedicated 45-second test run was performed. During each run, a constant traffic profile was generated to stress the C2 link, as configured in the AUTO_GCS settings.
4.2 Evaluated PQC Suites
The evaluation covers 30 distinct PQC suites, combining KEMs and digital signature schemes from different cryptographic families to provide broad coverage of the available options. The algorithms tested are categorized in the table below.
Cryptographic Primitive
Algorithms Tested
NIST Security Level
KEMs (Lattice-Based)
ML-KEM (512, 768, 1024), FrodoKEM (640, 976)
L1, L3, L5
KEMs (Code-Based)
Classic-McEliece (348864, 460896, 8192128), HQC (128, 192, 256)
L1, L3, L5
Signatures (Lattice-Based)
ML-DSA (44, 65, 87), Falcon (512, 1024)
L1, L5
Signatures (Hash-Based)
SPHINCS+ (128f, 256f)
L1, L5
4.3 Test Scenario and Metrics
All performance results presented in this paper were collected under a simulated adverse network condition labeled "ddos =xgboost (lightweight )". This scenario was chosen to evaluate the robustness and performance of each PQC suite under non-ideal link conditions. The following key metrics were captured for each run:
Network Performance: Throughput (Mb/s), packet loss (%), Round-Trip Time (RTT), and One-Way Delay (OWD).
Cryptographic Overhead: Handshake completion time (ms) and a breakdown of individual primitive costs (KEM key generation, KEM decapsulation, Signature signing).
Resource Consumption: Maximum CPU utilization (%) and Resident Set Size (RSS) memory (MiB).
Power and Energy: Average power draw (W) and total energy consumed (J) over the 45-second test window.
The following section presents and analyzes the results gathered through this methodology, offering insights into the practical costs and benefits of each PQC family.
5.0 Evaluation and Results
This section analyzes the empirical data collected from the experiments, focusing on the performance trade-offs between different PQC algorithm families. The goal is to provide practical guidance for system architects on selecting appropriate PQC suites for latency-sensitive and resource-constrained UAV C2 links.
5.1 Network Performance Analysis
Network performance was largely stable across most PQC suites, however, the choice of algorithm had a measurable impact on link stability under load. The higher packet loss observed for the NIST Level 5 suites cs-hqc256-aesgcm-mldsa87 (3.226% loss) and cs-mlkem1024-aesgcm-mldsa87 (2.021% loss) is a direct consequence of their higher computational intensity. Under the ddos =xgboost (lightweight ) network load, the drone's CPU struggled to keep pace with both the demanding cryptographic workload and the high rate of network packet processing. This resource contention created processing bottlenecks that led to buffer overruns and dropped packets. This result demonstrates a clear trade-off between achieving the highest theoretical security level and maintaining link stability under adverse conditions. In contrast, suites combining lattice-based KEMs and signatures at lower security levels, such as cs-mlkem768-aesgcm-mldsa65, maintained near-zero packet loss (0.025%), highlighting their superior performance for high-reliability links.
Suite
Throughput (Mb/s)
Loss (%)
RTT p50 (ms)
RTT p95 (ms)
cs-mlkem768-aesgcm-mldsa65
7.946
0.025
10.050
36.206
cs-classicmceliece460896-aesgcm-mldsa65
7.915
0.279
12.384
54.085
cs-hqc256-aesgcm-mldsa87
7.663
3.226
22.687
286.684
cs-mlkem1024-aesgcm-mldsa87
7.775
2.021
15.935
67.624
5.2 Cryptographic Operation Overhead
The analysis of cryptographic primitive timings reveals significant performance disparities between the algorithm families. Lattice-based schemes, particularly ML-KEM and Falcon, demonstrated exceptionally fast performance, with handshake times often under 20 milliseconds. In contrast, code-based and hash-based schemes incurred substantial computational costs for specific operations. The key generation for Classic-McEliece proved to be extremely expensive, taking 442.547 ms for the L3 variant (cs-classicmceliece460896...), making it impractical for scenarios requiring frequent rekeying. Similarly, the hash-based SPHINCS+ signature scheme introduced significant signing latency; cs-mlkem512-aesgcm-sphincs128fsha2 recorded a sign time of 43.710 ms. This 43.7 ms signing latency represents a direct, per-command tax on the C2 link's responsiveness. In a teleoperation scenario requiring rapid operator inputs, such a delay is operationally unacceptable and introduces a significant control loop bottleneck.
Suite
Total Handshake (ms)
KEM Keygen (ms)
KEM Decap (ms)
Sig Sign (ms)
cs-mlkem1024-aesgcm-falcon1024
10.961
0.116
0.098
1.861
cs-classicmceliece460896-aesgcm-mldsa65
641.085
442.547
69.117
2.268
cs-mlkem512-aesgcm-sphincs128fsha2
62.766
0.208
0.184
43.710
5.3 Power and Resource Consumption Analysis
The analysis of power and resource consumption yields a critical insight for system architects: for this class of device and workload, the baseline power consumption of the radio and System-on-Chip (SoC) overwhelms the comparatively small variations caused by different PQC algorithms. While CPU utilization varied significantly across suites—from 76.9% for the McEliece-based suite to 85.4% for a high-performance ML-KEM/Falcon combination—these fluctuations were not reflected proportionally in power draw. Across nearly all 30 suites, the average power consumption on the drone remained remarkably consistent, hovering around 4.3 W. For example, the CPU-intensive cs-mlkem1024-aesgcm-falcon1024 (85.4% CPU) drew 4.314 W, nearly identical to the less-intensive cs-classicmceliece460896-aesgcm-mldsa65 (76.9% CPU) at 4.303 W. This indicates that for this platform, the direct energy cost of PQC is a secondary consideration compared to its impact on CPU availability, network latency, and link stability.
Suite
Avg Power (W)
Total Energy (J)
Max CPU (%)
cs-mlkem1024-aesgcm-falcon1024
4.314
194.112
85.4
cs-frodokem640aes-aesgcm-mldsa44
4.345
195.525
79.5
cs-classicmceliece460896-aesgcm-mldsa65
4.303
193.629
76.9
cs-mlkem512-aesgcm-sphincs128fsha2
4.340
195.297
80.6
These results provide a clear picture of the performance landscape for deploying PQC on UAVs, leading to the final conclusions of this study.
6.0 Conclusion
This study successfully demonstrated the design, implementation, and rigorous evaluation of a post-quantum secure command and control stack for UAVs. The empirical results confirm that deploying PQC on resource-constrained aerial platforms is not only feasible but also practical with the right choice of algorithms. The performance analysis of 30 distinct PQC suites revealed critical trade-offs: lattice-based cryptography, particularly combinations of ML-KEM with ML-DSA or Falcon, consistently provides an excellent balance of high performance, low-latency cryptographic operations, and strong, standardized security. These schemes are well-suited for the demands of a real-time C2 link. In contrast, other families impose performance penalties that render them operationally unsuitable for latency-critical C2 applications where command authentication delays are unacceptable. Specifically, the high computational cost of key generation for code-based KEMs like Classic-McEliece and the substantial latency of signing operations for hash-based signatures like SPHINCS+ make them poor choices for this use case. Ultimately, this work affirms the viability of migrating UAV C2 links to post-quantum security today and provides a data-driven foundation for architects to make informed decisions. Future work could explore the benefits of hardware acceleration for PQC primitives on UAV-grade processors or expand the evaluation to a wider range of challenging network conditions.
==============research paper draft==============================
draft-2
============================================================
A Resilient Post-Quantum Command and Control Stack for Unmanned Aerial Vehicles
Abstract
The operational integrity of Unmanned Aerial Vehicles (UAVs) depends on quantum-resistant, high-performance command and control (C2) links. However, the integration of Post-Quantum Cryptography (PQC) into resource-constrained UAV platforms presents a critical challenge due to its significant computational overhead, a problem magnified when the system is under duress from ancillary tasks like on-board threat detection. This paper introduces a complete PQC-secured C2 stack featuring an adaptive scheduler designed for the graceful degradation of cryptographic policies in response to real-time system telemetry. We present a comprehensive performance evaluation of numerous PQC suites under three distinct computational loads, simulating a baseline scenario and the concurrent operation of both lightweight (XGBoost) and heavyweight (Transformer-based) on-board DDOS detection models. Our results establish that static cryptographic policies are operationally untenable under realistic computational loads, positioning adaptive security as a fundamental prerequisite for any mission-critical UAV system in the post-quantum era.
--------------------------------------------------------------------------------
1.0 Introduction
Secure and reliable command and control (C2) links are the lifeline of modern Unmanned Aerial Vehicle (UAV) operations, ensuring mission success and operational safety. The imminent threat posed by fault-tolerant quantum computers, which can break the classical public-key cryptography underpinning current secure communication standards, necessitates a transition to Post-Quantum Cryptography (PQC). The central challenge lies in implementing these robust, next-generation cryptographic algorithms on embedded UAV platforms without compromising mission-critical performance metrics. The computational intensity of many PQC schemes can lead to prohibitive increases in network latency, reductions in throughput, and accelerated battery depletion, particularly when the platform's limited processing resources are contended by other essential tasks such as on-board analytics or threat detection.
This paper addresses this challenge by designing, implementing, and evaluating a resilient, PQC-secured C2 stack capable of adapting its security posture to maintain operational viability under varying computational loads. We demonstrate that a one-size-fits-all approach to PQC selection is untenable for UAVs and that an adaptive framework is essential for balancing security with performance.
The main contributions of this work are:
The design and implementation of an end-to-end, PQC-secured UAV C2 transport protocol, detailing its PQC-authenticated handshake for session establishment and its efficient Authenticated Encryption with Associated Data (AEAD) framing mechanism for data transport.
The architecture of an adaptive scheduling system that leverages real-time system telemetry, including high-frequency power data and physics-based battery models, to enable graceful cryptographic degradation via an expert policy engine.
A comprehensive empirical evaluation of a wide range of PQC suites, analyzing the performance and energy trade-offs under three distinct computational load scenarios that simulate the impact of co-located, on-board DDOS detection models of varying complexity.
The remainder of this paper is structured as follows: Section 2.0 details the modular system architecture. Section 3.0 describes the experimental methodology, testbed, and performance metrics. Section 4.0 presents a detailed evaluation and analysis of the results across all scenarios. Finally, Section 5.0 concludes the paper by summarizing our findings and discussing their implications for future secure UAV system design.
2.0 System Architecture
A modular system architecture is paramount to achieving the dual goals of robust security and operational resilience. The architecture presented here deliberately separates the cryptographic transport core from the adaptive policy engine. This separation allows the system to flexibly manage its security posture based on real-time conditions without requiring modifications to the underlying cryptographic primitives. This design ensures that the system can gracefully degrade its security level to preserve mission-critical functions when resources are constrained, providing a resilient foundation for secure UAV operations. The following sections detail the core components and their interactions, which are empirically evaluated in this study.
2.1 System Overview
The system operates as a transparent proxy, intercepting plaintext application data from a Ground Control Station (GCS) or UAV and securing it for transmission over a wireless link. The data flow begins with an application sending a standard plaintext UDP packet. A local proxy, orchestrated by the async_proxy.py module, captures this packet. It then uses the secure transport core to encrypt and frame the data, which is subsequently transmitted over the network. On the receiving end, the peer proxy receives the encrypted packet, validates its authenticity and freshness, decrypts it, and forwards the original plaintext data to the destination application. This entire process is designed to be transparent to the end applications, requiring no modification to existing UAV control software.
2.2 Secure Transport Core
The cryptographic foundation of the C2 link is implemented within the core/ directory, providing mechanisms for authenticated key establishment and secure data framing.
2.2.1 PQC-Authenticated Handshake
Initial session establishment and subsequent rekeying events are handled by a TCP-based handshake protocol defined in handshake.py. This protocol establishes a secure session by leveraging a hybrid cryptographic approach. A Post-Quantum Key Encapsulation Mechanism (KEM) is used to generate and securely exchange a shared secret between the UAV and the GCS. Concurrently, a PQC digital signature scheme is used to authenticate the GCS, preventing man-in-the-middle attacks. As defined in suites.py, the system supports a wide variety of KEM and signature algorithm combinations. Once the shared secret is established, transport keys for encrypting data in each direction are derived using the standard HKDF (HMAC-based Key Derivation Function) primitive.
2.2.2 AEAD Packet Framing
Once a session is established, all data packets are encrypted using an Authenticated Encryption with Associated Data (AEAD) scheme, as implemented in aead.py. This ensures both the confidentiality and integrity of the C2 data. The structure of each encrypted packet includes a 22-byte wire header, which serves as the "associated data" in the AEAD operation, binding the ciphertext to the packet's metadata.
The table below deconstructs the 22-byte wire header, which serves as the integrity-protected Associated Data (AD) in every AEAD operation.
Field
Size (bytes)
Description
Version
1
Protocol version identifier.
KEM ID
1
Identifier for the Key Encapsulation Mechanism.
KEM Param
1
Parameter identifier for the KEM.
Sig ID
1
Identifier for the Signature scheme.
Sig Param
1
Parameter identifier for the Signature scheme.
Session ID
8
Unique identifier for the current session.
Sequence
8
Per-packet sequence number (64-bit).
Epoch
1
Key epoch, incremented on each rekey event.
Total
22

A key design choice for efficiency is the use of a deterministic Initialization Vector (IV). The IV is constructed locally on both the sender and receiver side from the 1-byte epoch and an 8-byte sequence number, padded to the required length (e.g., 12 bytes). By not transmitting the IV over the air, this design saves 12 bytes of overhead on every single packet. To protect against message replay attacks, the receiver implements a sliding window mechanism that validates the sequence number of incoming packets. The implementation supports multiple symmetric AEAD algorithms, including AES-GCM and ChaCha20Poly1305.
2.3 Adaptive Scheduling and Instrumentation
The system's resilience stems from its ability to adapt its security posture in response to changing operational conditions. This is managed by the adaptive scheduling system, which relies on a continuous stream of telemetry from the instrumentation pipeline.
2.3.1 Real-Time System Instrumentation
The scheduler's decisions are informed by a rich telemetry pipeline. The core/power_monitor.py module details the capability to perform high-frequency power sampling at up to 1000 Hz using INA219 sensors. This provides granular, real-time data on the UAV's power consumption. Furthermore, the architecture includes a physics-based battery model described in src/scheduler/components/battery_predictor.py. This model uses Peukert's equation, which accounts for non-linear capacity effects under varying loads, and incorporates temperature compensation to provide accurate, real-time predictions of the battery's State of Charge (SOC) and the UAV's remaining flight time.
2.3.2 Expert Policy for Graceful Degradation
The adaptive scheduling logic is encapsulated in modules such as src/scheduler/unified_scheduler.py and src/scheduler/strategies/expert.py. The "expert policy" scheduler enables graceful degradation of security by dynamically selecting the most appropriate PQC suite from a predefined set. This decision is based on a lookup policy that considers current telemetry data, such as network quality (latency, packet loss), battery state (SOC, predicted flight time), and system load (CPU utilization). For instance, if the battery SOC drops below a critical threshold or CPU load spikes due to an ancillary task, the expert policy can trigger a rekey to a more computationally efficient (and potentially lower-security) PQC suite to conserve energy and reduce latency. This paper focuses exclusively on evaluating the performance characteristics that would inform such an expert lookup policy, rather than the implementation of the decision logic itself.
3.0 Experimental Methodology
To evaluate the performance trade-offs of the PQC C2 stack, a rigorous experimental methodology was designed. This methodology aims to measure key performance indicators across a wide range of cryptographic suites and under realistic computational stress, thereby providing the necessary data to build an effective adaptive scheduling policy. The experiments systematically quantify the impact of PQC on network performance, cryptographic latency, and energy consumption. This section details the testbed configuration, the cryptographic algorithms evaluated, and the specific workload scenarios used to simulate operational conditions.
3.1 Testbed Configuration
The experimental testbed consists of two primary components: a Ground Control Station (GCS) and a UAV, communicating over a network link. The system is configured to capture detailed performance and energy metrics throughout each test run. High-frequency power consumption data is collected on the UAV platform using an INA219 sensor, sampling at 1000 Hz to provide a granular view of the energy cost associated with each cryptographic and computational workload.
3.2 Evaluated Cryptographic Suites
A comprehensive set of cryptographic suites was evaluated to analyze the performance characteristics of different PQC algorithm families. Each suite is a combination of a KEM for key establishment and a digital signature scheme for authentication, categorized by its NIST-defined security level.
Suite ID
KEM
Signature Scheme
NIST Security Level
cs-mlkem512-aesgcm-mldsa44
mlkem512
mldsa44
L1
cs-mlkem512-aesgcm-falcon512
mlkem512
falcon512
L1
cs-mlkem512-aesgcm-sphincs128fsha2
mlkem512
sphincs128fsha2
L1
cs-frodokem640aes-aesgcm-mldsa44
frodokem640aes
mldsa44
L1
cs-classicmceliece348864-aesgcm-sphincs128fsha2
classicmceliece348864
sphincs128fsha2
L1
cs-hqc128-aesgcm-falcon512
hqc128
falcon512
L1
cs-mlkem768-aesgcm-mldsa65
mlkem768
mldsa65
L3
cs-frodokem976aes-aesgcm-mldsa65
frodokem976aes
mldsa65
L3
cs-classicmceliece460896-aesgcm-mldsa65
classicmceliece460896
mldsa65
L3
cs-hqc192-aesgcm-mldsa65
hqc192
mldsa65
L3
cs-mlkem1024-aesgcm-mldsa87
mlkem1024
mldsa87
L5
cs-mlkem1024-aesgcm-falcon1024
mlkem1024
falcon1024
L5
cs-mlkem1024-aesgcm-sphincs256fsha2
mlkem1024
sphincs256fsha2
L5
cs-classicmceliece8192128-aesgcm-sphincs256fsha2
classicmceliece8192128
sphincs256fsha2
L5
cs-hqc256-aesgcm-mldsa87
hqc256
mldsa87
L5
Note: For brevity, only suites utilizing AES-GCM are listed; equivalent suites using ChaCha20Poly1305 were also evaluated and exhibited comparable performance trends in their respective cryptographic classes.
3.3 Computational Workload Scenarios
To simulate the impact of on-board processing tasks on C2 link performance, three distinct experimental scenarios were defined. These scenarios represent varying levels of computational load from a co-located DDOS detection system running on the UAV.
Baseline Scenario: The PQC C2 stack operates with no additional computational load. This scenario establishes the fundamental performance characteristics of each cryptographic suite.
Lightweight Scenario: The system runs concurrently with a lightweight, XGBoost-based DDOS detection model. This represents a typical, low-overhead analytics task.
Heavyweight Scenario: The system runs concurrently with a computationally intensive, Transformer-based DDOS detection model. This scenario simulates a high-stress condition where C2 performance is likely to be impacted by CPU resource contention.
3.4 Performance Metrics
For each 45-second experimental run, a comprehensive set of performance metrics was collected to provide a holistic view of the system's behavior. The key metrics, as documented in the source logs, include:
Network Performance: Throughput (Mb/s), Packet Loss (%), and Round-Trip Time (RTT) in milliseconds (ms), including average, 50th percentile (p50), and 95th percentile (p95) values.
Cryptographic Latency: GCS Handshake Time (ms) and a breakdown of primitive operation latencies for KEM key generation, KEM decapsulation, and Signature signing.
Energy Consumption: Average Power (W) and Total Energy consumed over the 45-second test window (J).
System Resources: Maximum CPU Utilization (%) and Resident Set Size (RSS) memory (MiB).
4.0 Evaluation and Results
This section presents the core empirical findings of the study, detailing the performance of the PQC C2 stack under the three defined computational workload scenarios. The results are organized to first establish a performance baseline for all evaluated PQC suites and then to systematically analyze the impact of increasing computational loads from the co-located DDOS detection models. This analysis reveals the critical performance trade-offs inherent in each cryptographic choice, providing the quantitative data necessary to inform an adaptive security policy.
4.1 Baseline Performance Analysis (No DDOS Load)
Under the baseline scenario with no ancillary computational load, the performance of the various PQC suites is primarily dictated by their inherent algorithmic complexity and the size of their cryptographic artifacts (keys and signatures). The results, summarized in the table below, establish the fundamental cost of each security posture.
Suite
Handshake (ms)
RTT Avg (ms)
Loss (%)
Avg Power (W)
cs-mlkem512-aesgcm-mldsa44
521.90
14.44
0.01
4.202
cs-mlkem512-aesgcm-falcon512
20.31
18.09
1.31
4.215
cs-mlkem768-aesgcm-mldsa65
19.40
16.08
0.02
4.217
cs-mlkem1024-aesgcm-mldsa87
10.62
15.17
0.10
4.352
cs-mlkem1024-aesgcm-falcon1024
15.00
12.63
0.09
4.344
cs-hqc128-aesgcm-falcon512
101.06
17.52
0.10
4.114
cs-hqc192-aesgcm-mldsa65
172.90
12.39
0.17
4.351
cs-hqc256-aesgcm-mldsa87
297.27
21.64
2.39
4.316
cs-frodokem640aes-aesgcm-mldsa44
54.51
19.74
0.15
4.228
cs-frodokem976aes-aesgcm-mldsa65
58.68
18.14
0.45
4.322
cs-classicmceliece348864-aesgcm-sphincs128fsha2
1090.40
13.56
0.04
4.234
cs-classicmceliece460896-aesgcm-mldsa65
293.67
18.56
1.49
4.349
cs-classicmceliece8192128-aesgcm-sphincs256fsha2
902.40
14.63
0.14
4.350
The analysis reveals several key trends. Suites based on ML-KEM (Kyber) and ML-DSA (Dilithium) or Falcon generally exhibit the lowest handshake latencies, with cs-mlkem1024-aesgcm-mldsa87 completing its handshake in just 10.62 ms. In stark contrast, Classic-McEliece variants exhibit the highest handshake latency due to their very large public key sizes, with cs-classicmceliece348864-aesgcm-sphincs128fsha2 requiring over a full second (1090.40 ms). This significant latency makes Classic-McEliece variants poorly suited for scenarios requiring rapid session establishment or frequent rekeying, as each handshake would introduce over a second of control-link downtime. Network metrics like RTT and packet loss remain low across most suites, indicating that at baseline, the primary performance differentiator is the initial cryptographic setup cost.
4.2 Performance under Lightweight Computational Load (XGBoost)
Introducing the lightweight XGBoost-based DDOS detection model creates a scenario of mild resource contention. This workload modestly impacts the performance of most suites, but the changes reveal how different algorithms respond to background CPU activity.
Suite
Handshake (ms)
RTT Avg (ms)
Loss (%)
Avg Power (W)
cs-mlkem512-aesgcm-mldsa44
14.29
17.35
0.53
4.321
cs-mlkem512-aesgcm-falcon512
13.32
15.14
0.25
4.343
cs-mlkem768-aesgcm-mldsa65
35.50
12.17
0.03
4.307
cs-mlkem1024-aesgcm-mldsa87
15.67
21.73
2.02
4.287
cs-mlkem1024-aesgcm-falcon1024
10.96
25.05
1.02
4.314
cs-hqc128-aesgcm-falcon512
65.50
12.54
0.07
4.372
cs-hqc192-aesgcm-mldsa65
291.92
26.21
0.94
4.285
cs-hqc256-aesgcm-mldsa87
345.28
61.16
3.23
4.243
cs-frodokem640aes-aesgcm-mldsa44
33.58
15.20
0.18
4.345
cs-frodokem976aes-aesgcm-mldsa65
59.70
12.75
0.10
4.333
cs-classicmceliece348864-aesgcm-sphincs128fsha2
253.70
12.57
0.10
4.354
cs-classicmceliece460896-aesgcm-mldsa65
641.09
16.15
0.28
4.303
cs-classicmceliece8192128-aesgcm-sphincs256fsha2
913.08
14.27
0.56
4.329
Under the lightweight load, most suites exhibit a moderate increase in network latency and packet loss. Interestingly, several suites showed a decrease in handshake latency and average RTT. For instance, for cs-hqc128-aesgcm-falcon512, its handshake time decreased from 101.06 ms to 65.50 ms, and its RTT dropped from 17.52 ms to 12.54 ms. This counter-intuitive result may be attributable to subtle changes in process scheduling, CPU cache state, or other system-level effects when moving from an idle state to one with consistent, low-level background processing. The most computationally intensive suite, cs-classicmceliece348864-aesgcm-sphincs128fsha2, saw its handshake time improve dramatically from 1090.40 ms to 253.70 ms, suggesting its baseline performance may have been anomalous or subject to cold-start penalties that were mitigated by the continuous workload.
4.3 Performance under Heavyweight Computational Load (Transformer)
The heavyweight Transformer-based DDOS model places the system under significant computational stress, exposing severe performance degradation and highlighting the breaking point for several cryptographic suites. This scenario simulates a mission-critical situation where on-board analytics are essential but directly compete with the C2 link for resources.
Suite
Handshake (ms)
RTT Avg (ms)
Loss (%)
Avg Power (W)
cs-mlkem512-aesgcm-mldsa44
8.14
27.92
2.36
4.620
cs-mlkem512-aesgcm-falcon512
5.33
38.27
3.33
4.610
cs-mlkem768-aesgcm-mldsa65
22.74
34.21
3.07
4.612
cs-mlkem1024-aesgcm-mldsa87
12.45
38.07
4.67
4.672
cs-mlkem1024-aesgcm-falcon1024
15.67
35.07
6.41
4.671
cs-hqc128-aesgcm-falcon512
73.81
31.27
2.11
4.695
cs-hqc192-aesgcm-mldsa65
168.22
92.11
3.96
4.623
cs-hqc256-aesgcm-mldsa87
322.94
38.08
5.56
4.677
cs-frodokem640aes-aesgcm-mldsa44
29.76
54.35
3.34
4.606
cs-frodokem976aes-aesgcm-mldsa65
70.95
34.81
3.15
4.664
cs-classicmceliece348864-aesgcm-sphincs128fsha2
837.13
110.38
6.45
4.585
cs-classicmceliece460896-aesgcm-mldsa65
580.72
35.71
4.82
4.659
cs-classicmceliece8192128-aesgcm-sphincs256fsha2
1637.19
45.14
3.30
4.670
The heavyweight load caused a dramatic decline in network quality for all suites. Packet loss rates increased substantially, often exceeding 3-6%. The cs-classicmceliece348864-aesgcm-sphincs128fsha2 suite experienced a catastrophic increase in average RTT from 13.56 ms (baseline) to 110.38 ms, coupled with a packet loss increase from 0.04% to 6.45%, rendering the C2 link unreliable. Its handshake time regressed to 837.13 ms. The even larger cs-classicmceliece8192128-aesgcm-sphincs256fsha2 saw its handshake time balloon to 1637.19 ms. In contrast, the lattice-based suites demonstrated greater resilience. While suites like cs-mlkem512-aesgcm-falcon512 still suffered increased RTT (18.09 ms to 38.27 ms) and packet loss (1.31% to 3.33%), their handshake latencies remained low, indicating that session re-establishment would be fast even under duress.
4.4 Cross-Scenario Comparative Analysis
A holistic, cross-scenario analysis reveals that the impact of computational load is not uniform across different classes of PQC algorithms. The performance degradation is most pronounced for suites whose primary bottleneck is CPU-intensive computation rather than memory or bandwidth.
Code-based KEMs, specifically the Classic-McEliece variants, suffer the most significant increases in handshake latency. The handshake time for cs-classicmceliece460896-aesgcm-mldsa65 increased by 98% from baseline (293.67 ms) to the heavyweight scenario (580.72 ms). In contrast, the fast, lattice-based cs-mlkem768-aesgcm-mldsa65 saw only a 17% increase in handshake time (from 19.40 ms to 22.74 ms) under the same conditions.
Signature schemes with high computational costs are disproportionately affected by CPU contention. The handshake latency of any suite using SPHINCS+ is dominated by the signing operation. For cs-mlkem1024-aesgcm-sphincs256fsha2, the handshake time increased by 35% from baseline (124.93 ms) to the heavyweight load (168.09 ms). This increase is almost entirely attributable to the sig sign primitive, which must compete for CPU cycles with the Transformer model. Conversely, schemes with extremely fast signing, such as Falcon and ML-DSA, contribute minimally to handshake latency even under heavy load. This disproportionate impact demonstrates that for CPU-constrained platforms, the performance of the signature scheme can become the single greatest limiting factor to C2 resilience, making fast-signing algorithms like Falcon and ML-DSA critically important design choices.
4.5 Power and Energy Consumption Analysis
The energy efficiency of the cryptographic suites is a critical factor for battery-powered UAVs. The analysis of average power consumption across the three scenarios quantifies the energy cost of both the cryptographic operations and the ancillary on-board analytics.
Suite
Avg Power (W) - Baseline
Avg Power (W) - Lightweight
Avg Power (W) - Heavyweight
cs-mlkem512-aesgcm-mldsa44
4.202
4.321
4.620
cs-mlkem512-aesgcm-falcon512
4.215
4.343
4.610
cs-mlkem768-aesgcm-mldsa65
4.217
4.307
4.612
cs-mlkem1024-aesgcm-mldsa87
4.352
4.287
4.672
cs-mlkem1024-aesgcm-falcon1024
4.344
4.314
4.671
cs-hqc128-aesgcm-falcon512
4.114
4.372
4.695
cs-hqc192-aesgcm-mldsa65
4.351
4.285
4.623
cs-hqc256-aesgcm-mldsa87
4.316
4.243
4.677
cs-frodokem640aes-aesgcm-mldsa44
4.228
4.345
4.606
cs-frodokem976aes-aesgcm-mldsa65
4.322
4.333
4.664
cs-classicmceliece348864-aesgcm-sphincs128fsha2
4.234
4.354
4.585
cs-classicmceliece460896-aesgcm-mldsa65
4.349
4.303
4.659
cs-classicmceliece8192128-aesgcm-sphincs256fsha2
4.350
4.329
4.670
Under baseline conditions, most suites consumed between 4.1 W and 4.3 W, with minimal variation between them. The introduction of the lightweight XGBoost model resulted in a consistent but small increase in power consumption to the 4.2 W to 4.4 W range. However, the heavyweight Transformer model imposed a significant energy penalty, uniformly increasing consumption to between 4.5 W and 4.7 W. This near-uniform 0.4W penalty, regardless of the PQC suite, implies that the dominant energy cost under heavy load is the analytic model itself, not the cryptography. This reinforces the strategy of an adaptive scheduler: by selecting a more computationally efficient PQC suite, it can free up CPU cycles for the DDOS model, potentially allowing it to complete its task faster and return the CPU to a lower power state, thereby optimizing overall mission endurance.
5.0 Conclusion
This paper addressed the critical challenge of implementing high-performance, quantum-resistant cryptography on resource-constrained UAV platforms. We presented a complete C2 stack designed for resilience, featuring a modular architecture and an adaptive scheduling policy engine informed by real-time telemetry. Our comprehensive evaluation systematically quantified the performance of a wide range of PQC suites under varying levels of computational stress, simulating the presence of on-board DDOS detection models.
The key findings from our evaluation underscore the significant and non-uniform impact of PQC on system performance. We demonstrated clear trade-offs between the NIST security level, network performance (latency and packet loss), and power consumption. These trade-offs are dramatically exacerbated by ancillary computational loads, where suites with high CPU requirements, such as Classic-McEliece and SPHINCS+, suffer severe degradation in C2 link reliability. Conversely, lattice-based schemes like ML-KEM and ML-DSA proved far more resilient. Our analysis also quantified the significant energy cost associated with advanced on-board analytics, reinforcing the need for intelligent resource management.
These results validate the central thesis of our work: an adaptive security architecture is a necessary mechanism for maintaining operational resilience in secure UAV systems. By enabling the graceful degradation of cryptographic policies, our proposed system allows a UAV to balance its security posture against mission-critical performance requirements in real-time. The findings of this study provide a foundational data set for the design of future secure UAVs and inform the development of sophisticated scheduling policies. Future research will focus on the implementation of machine learning-based scheduling strategies that can learn optimal policies from operational data, further enhancing the autonomy and resilience of next-generation unmanned systems.