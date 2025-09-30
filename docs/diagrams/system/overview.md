# System Overview Diagrams

This document contains comprehensive system architecture diagrams for the post-quantum cryptographic framework.

## Complete System Architecture

### High-Level System Overview

```mermaid
graph TB
    subgraph "Application Layer"
        UA[UAV Application<br/>Flight Controller<br/>Telemetry System]
        GCS_APP[GCS Application<br/>Control Software<br/>Mission Planning]
    end
    
    subgraph "Proxy Layer - Core Modules"
        DRONE_PROXY[Drone Proxy<br/>async_proxy.py --drone<br/>Port Management]
        GCS_PROXY[GCS Proxy<br/>async_proxy.py --gcs<br/>Connection Handling]
    end
    
    subgraph "Cryptographic Core (core/)"
        HANDSHAKE[handshake.py<br/>• server_gcs_handshake<br/>• client_drone_handshake<br/>• derive_transport_keys]
        AEAD[aead.py<br/>• AeadSender/Receiver<br/>• encrypt_packet<br/>• decrypt_verify]
        SUITES[suites.py<br/>• get_suite<br/>• header_ids_for_suite<br/>• SUPPORTED_SUITES]
        CONFIG[config.py<br/>• TCP_HANDSHAKE_PORT<br/>• UDP_GCS_RX/UDP_DRONE_RX<br/>• WIRE_VERSION]
    end
    
    subgraph "Transport Layer"
        TCP_HS[TCP Handshake<br/>Port 46000<br/>Reliable Authentication]
        UDP_ENC[Encrypted UDP<br/>GCS: 46011, Drone: 46012<br/>Low-latency Data]
        UDP_PLAIN[Plaintext UDP<br/>Loopback: 47001-47004<br/>Application Interface]
    end
    
    subgraph "Security Features"
        POLICY[policy_engine.py<br/>• handle_control<br/>• record_rekey_result<br/>• ControlState FSM]
        DDOS[ddos/<br/>• xgb_stage1.py<br/>• mitigations.py<br/>• features.py]
        RL[rl/<br/>• linucb.py<br/>• agent_runtime.py<br/>• safety.py]
    end
    
    subgraph "Monitoring & Tools"
        LOGGING[logging_utils.py<br/>• get_logger<br/>• session_id tracking]
        COUNTERS[ProxyCounters<br/>• drop_auth_fail<br/>• drop_replay<br/>• successful_rekey]
        TOOLS[tools/<br/>• counter_utils.py<br/>• encrypted_sniffer.py<br/>• audit_endpoints.py]
    end
    
    subgraph "PQC Algorithm Layer"
        OQS[Open Quantum Safe<br/>• ML-KEM: 512/768/1024<br/>• ML-DSA: 44/65/87<br/>• Falcon: 512/1024<br/>• SLH-DSA: 128s/192s/256s]
    end
    
    %% Data Flow Connections
    UA ---|Plaintext UDP| UDP_PLAIN
    UDP_PLAIN ---|Bridge| DRONE_PROXY
    DRONE_PROXY ---|Encrypted UDP| UDP_ENC
    UDP_ENC ---|Network| GCS_PROXY
    GCS_PROXY ---|Bridge| UDP_PLAIN
    UDP_PLAIN ---|Application Data| GCS_APP
    
    %% Handshake Flow
    DRONE_PROXY ---|Key Exchange| TCP_HS
    TCP_HS ---|Mutual Auth| GCS_PROXY
    
    %% Core Dependencies
    DRONE_PROXY --> HANDSHAKE
    GCS_PROXY --> HANDSHAKE
    HANDSHAKE --> SUITES
    HANDSHAKE --> OQS
    DRONE_PROXY --> AEAD
    GCS_PROXY --> AEAD
    AEAD --> CONFIG
    
    %% Security Integration
    DRONE_PROXY --> POLICY
    GCS_PROXY --> POLICY
    POLICY --> RL
    DRONE_PROXY --> DDOS
    GCS_PROXY --> DDOS
    
    %% Monitoring Integration
    DRONE_PROXY --> LOGGING
    GCS_PROXY --> LOGGING
    DRONE_PROXY --> COUNTERS
    GCS_PROXY --> COUNTERS
    LOGGING --> TOOLS
    COUNTERS --> TOOLS
    
    %% Configuration Dependencies
    DRONE_PROXY --> CONFIG
    GCS_PROXY --> CONFIG
    HANDSHAKE --> CONFIG
    POLICY --> CONFIG
    
    classDef coreModule fill:#e1f5fe,stroke:#0277bd,stroke-width:2px
    classDef cryptoLayer fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px
    classDef networkLayer fill:#e8f5e8,stroke:#2e7d32,stroke-width:2px
    classDef securityLayer fill:#fff3e0,stroke:#ef6c00,stroke-width:2px
    classDef appLayer fill:#fce4ec,stroke:#c2185b,stroke-width:2px
    
    class DRONE_PROXY,GCS_PROXY,HANDSHAKE,AEAD,SUITES,CONFIG coreModule
    class TCP_HS,UDP_ENC,UDP_PLAIN cryptoLayer
    class POLICY,DDOS,RL securityLayer
    class UA,GCS_APP appLayer
    class LOGGING,COUNTERS,TOOLS networkLayer
```

### Simplified Architecture (For Academic Papers)

```mermaid
graph TB
    subgraph "Novel Contributions"
        A["🔄 Runtime Algorithm Switching<br/>Live cryptographic transitions<br/>No service interruption"]
        B["🚁 Drone-Optimized Protocol<br/>Hybrid TCP/UDP design<br/>Latency vs reliability separation"]
        C["📊 Comprehensive Algorithm Matrix<br/>21 NIST suite combinations<br/>ML-KEM + ML-DSA/Falcon/SLH-DSA"]
    end
    
    subgraph "Implementation Framework"
        D["TCP Handshake<br/>Post-quantum key exchange<br/>Mutual authentication"]
        E["UDP Data Plane<br/>AES-256-GCM encryption<br/>Optimized for real-time"]
        F["Control Plane<br/>Algorithm negotiation<br/>Two-phase commit protocol"]
    end
    
    A --> D
    B --> E
    C --> F
    
    D --> G["Quantum-Resistant<br/>UAV Communication"]
    E --> G
    F --> G
    
    style A fill:#99ff99
    style B fill:#99ff99
    style C fill:#99ff99
    style G fill:#e1f5fe
```

## Core Module Dependencies

### Module Interaction Flow

```mermaid
graph TB
    subgraph "Core Module Stack"
        A["core/suites.py<br/>Algorithm Registry<br/>21 Suite Combinations"]
        B["core/handshake.py<br/>Post-Quantum Handshake<br/>ML-KEM + Signatures"]
        C["core/aead.py<br/>AES-256-GCM Encryption<br/>Replay Protection"]
        D["core/policy_engine.py<br/>Runtime Switching<br/>State Machine"]
        E["core/async_proxy.py<br/>Network Coordination<br/>TCP/UDP Management"]
    end
    
    subgraph "External Dependencies"
        F["Open Quantum Safe (OQS)<br/>NIST Algorithm Implementations<br/>Constant-time Operations"]
        G["Python Cryptography<br/>AES-GCM, HKDF<br/>Hash Functions"]
    end
    
    subgraph "Network Interfaces"
        H["TCP Handshake<br/>Port 46000"]
        I["UDP Encrypted Data<br/>Ports 46011/46012"]
        J["UDP Plaintext Loopback<br/>Ports 47001-47004"]
    end
    
    A --> B
    B --> C
    C --> D
    D --> E
    
    B --> F
    C --> G
    
    E --> H
    E --> I
    E --> J
    
    style A fill:#e3f2fd
    style B fill:#f3e5f5
    style C fill:#e8f5e8
    style D fill:#fff3e0
    style E fill:#fce4ec
```

## Data Flow Visualization

### Complete Data Flow Path

```mermaid
flowchart LR
    subgraph "Drone Side"
        APP1[UAV Application]
        PLAIN1[UDP 47003/47004]
        PROXY1[Drone Proxy]
        ENC1[UDP 46012]
    end
    
    subgraph "Network"
        NET[Encrypted Channel]
    end
    
    subgraph "GCS Side"
        ENC2[UDP 46011]
        PROXY2[GCS Proxy]
        PLAIN2[UDP 47001/47002]
        APP2[GCS Application]
    end
    
    subgraph "Handshake Channel"
        TCP[TCP 46000<br/>Key Exchange]
    end
    
    APP1 -->|Plaintext| PLAIN1
    PLAIN1 -->|Bridge| PROXY1
    PROXY1 -->|AES-256-GCM| ENC1
    ENC1 -->|Encrypted UDP| NET
    NET -->|Encrypted UDP| ENC2
    ENC2 -->|AES-256-GCM| PROXY2
    PROXY2 -->|Bridge| PLAIN2
    PLAIN2 -->|Plaintext| APP2
    
    PROXY1 -.->|Key Exchange| TCP
    TCP -.->|Mutual Auth| PROXY2
    
    style APP1 fill:#e8f5e8
    style APP2 fill:#e8f5e8
    style NET fill:#ffcccc
    style TCP fill:#cce5ff
```

---

**Navigation**: 
- **Back to**: [Diagrams Index](../README.md)
- **Related**: [Protocol Flows](../protocols/handshake.md) | [Algorithm Matrix](../algorithms/algorithm-matrix.md)
- **Technical Docs**: [System Overview](../../technical/system-overview.md)