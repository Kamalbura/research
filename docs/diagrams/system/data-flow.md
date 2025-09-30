# Data Flow Diagrams

This document contains detailed data flow visualizations for the post-quantum cryptographic framework.

## Application Data Flow

### End-to-End Data Flow

```mermaid
sequenceDiagram
    participant App as UAV Application
    participant DLocal as Drone Local (47003)
    participant DProxy as Drone Proxy
    participant DEnc as Drone Encrypted (46012)
    participant Net as Network
    participant GEnc as GCS Encrypted (46011)
    participant GProxy as GCS Proxy
    participant GLocal as GCS Local (47001)
    participant GCS as GCS Application
    
    Note over App,GCS: Normal Data Flow (Post-Handshake)
    
    App->>DLocal: UDP packet (plaintext)
    DLocal->>DProxy: Forward to proxy
    
    Note over DProxy: AES-256-GCM Encryption
    DProxy->>DProxy: encrypt_packet(plaintext, type=0x01)
    DProxy->>DProxy: header + aes_gcm(data) + tag
    
    DProxy->>DEnc: Encrypted packet
    DEnc->>Net: UDP transmission
    Net->>GEnc: Network delivery
    
    GEnc->>GProxy: Receive encrypted
    
    Note over GProxy: AES-256-GCM Decryption
    GProxy->>GProxy: decrypt_verify(packet)
    GProxy->>GProxy: Replay protection check
    GProxy->>GProxy: Extract plaintext
    
    GProxy->>GLocal: Forward plaintext
    GLocal->>GCS: UDP packet (restored)
    
    Note over App,GCS: Bidirectional - Same process in reverse
```

### Packet Processing Pipeline

```mermaid
flowchart TB
    subgraph "Outbound Processing (Drone → GCS)"
        A1[Application Packet] --> B1[async_proxy.py]
        B1 --> C1{Handshake Complete?}
        C1 -->|No| D1[Queue/Drop]
        C1 -->|Yes| E1[aead.encrypt_packet]
        E1 --> F1[Add Header + Sequence]
        F1 --> G1[AES-256-GCM]
        G1 --> H1[Network Transmission]
    end
    
    subgraph "Inbound Processing (GCS ← Drone)"
        A2[Network Reception] --> B2[aead.decrypt_verify]
        B2 --> C2{Valid Packet?}
        C2 -->|No| D2[Drop + Counter]
        C2 -->|Yes| E2[Replay Check]
        E2 --> F2{In Window?}
        F2 -->|No| D2
        F2 -->|Yes| G2[Extract Plaintext]
        G2 --> H2[Forward to Application]
    end
    
    D1 --> I[ProxyCounters.queue_full]
    D2 --> J[ProxyCounters.drop_replay]
    
    style C1 fill:#e3f2fd
    style C2 fill:#e3f2fd
    style F2 fill:#fff3e0
    style D1 fill:#ffcccc
    style D2 fill:#ffcccc
```

## Control Plane Data Flow

### Runtime Algorithm Switching Flow

```mermaid
sequenceDiagram
    participant GCS as GCS Proxy
    participant DNet as Drone (Network)
    participant Drone as Drone Proxy
    participant Policy as Policy Engine
    
    Note over GCS,Drone: Phase 1 - Control Message Flow
    
    GCS->>GCS: Trigger rekey decision
    GCS->>GCS: prepare_control_message(prepare_rekey, target_suite)
    GCS->>GCS: encrypt_packet(control_msg, type=0x02)
    
    GCS->>DNet: Control packet (encrypted)
    DNet->>Drone: Network delivery
    
    Drone->>Drone: decrypt_verify(packet)
    Drone->>Drone: Extract control message
    Drone->>Policy: handle_control(prepare_rekey, target_suite)
    
    Policy->>Policy: Validate suite availability
    Policy->>Policy: Check compatibility
    
    alt Suite Available
        Policy->>Drone: commit_rekey response
        Drone->>Drone: encrypt_packet(commit_rekey, type=0x02)
        Drone->>DNet: Control response
        DNet->>GCS: Network delivery
        
        Note over GCS,Drone: Phase 2 - Algorithm Switch
        GCS->>GCS: State = SWAPPING
        Drone->>Drone: State = SWAPPING
        
        Note over GCS,Drone: New handshake with target algorithms
        GCS->>Drone: New TCP handshake
        
        Note over GCS,Drone: Atomic switch to new keys
        GCS->>GCS: epoch++, update keys
        Drone->>Drone: epoch++, update keys
        
        Note over GCS,Drone: Resume with new crypto
        
    else Suite Unavailable
        Policy->>Drone: prepare_fail response
        Drone->>DNet: Control response (fail)
        Note over GCS,Drone: Continue with current suite
    end
```

### State Machine Data Flow

```mermaid
stateDiagram-v2
    [*] --> INITIALIZING : System startup
    
    INITIALIZING --> HANDSHAKING : Begin TCP handshake
    HANDSHAKING --> RUNNING : Handshake success
    HANDSHAKING --> FAILED : Handshake failure
    
    RUNNING --> NEGOTIATING : Receive prepare_rekey
    NEGOTIATING --> RUNNING : Send prepare_fail
    NEGOTIATING --> SWAPPING : Send commit_rekey
    
    SWAPPING --> RUNNING : New handshake success
    SWAPPING --> FAILED : New handshake failure
    
    FAILED --> INITIALIZING : Restart/reconnect
    FAILED --> [*] : System shutdown
    
    note right of RUNNING
        Normal data flow:
        • Application packets
        • AES-256-GCM encryption
        • Sequence number increment
        • Replay protection
    end note
    
    note right of NEGOTIATING
        Control flow:
        • Validate target suite
        • Check availability
        • Make commit decision
    end note
    
    note right of SWAPPING
        Transition flow:
        • New TCP handshake
        • Fresh key derivation
        • Epoch increment
        • Atomic switch
    end note
```

## Error Handling Data Flow

### Packet Drop Scenarios

```mermaid
flowchart TB
    A[Incoming Packet] --> B{Valid Format?}
    B -->|No| C[Drop: Invalid Format]
    B -->|Yes| D{Authentication Valid?}
    D -->|No| E[Drop: Auth Failure]
    D -->|Yes| F{Replay Check}
    F -->|Duplicate| G[Drop: Replay]
    F -->|Too Old| H[Drop: Window]
    F -->|Valid| I{Decryption Success?}
    I -->|No| J[Drop: Decrypt Fail]
    I -->|Yes| K[Forward to Application]
    
    C --> L[ProxyCounters.drop_format]
    E --> M[ProxyCounters.drop_auth_fail]
    G --> N[ProxyCounters.drop_replay]
    H --> O[ProxyCounters.drop_window]
    J --> P[ProxyCounters.drop_decrypt]
    K --> Q[ProxyCounters.forward_success]
    
    style C fill:#ffcccc
    style E fill:#ffcccc
    style G fill:#ffcccc
    style H fill:#ffcccc
    style J fill:#ffcccc
    style K fill:#ccffcc
```

### Network Failure Handling

```mermaid
sequenceDiagram
    participant App as Application
    participant Proxy as Proxy
    participant Net as Network
    participant Remote as Remote Proxy
    
    Note over App,Remote: Normal Operation
    App->>Proxy: Send packet
    Proxy->>Net: Encrypted packet
    Net->>Remote: Delivery
    
    Note over Net: Network Failure Occurs
    App->>Proxy: Send packet
    Proxy->>Net: Encrypted packet
    Net--xRemote: Delivery fails
    
    Note over Proxy: Timeout Detection
    Proxy->>Proxy: Detect timeout
    Proxy->>Proxy: Increment failure counter
    
    alt Recoverable Failure
        Proxy->>Proxy: Retry transmission
        Proxy->>Net: Retransmit
        Net->>Remote: Successful delivery
        Note over Proxy: Reset failure counter
    else Persistent Failure
        Proxy->>Proxy: Trigger reconnection
        Proxy->>Remote: New handshake
        Note over Proxy,Remote: Re-establish session
    end
```

---

**Navigation**: 
- **Back to**: [System Overview](overview.md)
- **Related**: [Protocol Flows](../protocols/handshake.md) | [State Machines](../implementation/state-machines.md)
- **Technical Docs**: [System Architecture](../../technical/system-overview.md)