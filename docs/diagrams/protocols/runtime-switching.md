# Runtime Algorithm Switching Diagrams

This document contains detailed visualizations of the runtime cryptographic algorithm switching mechanism.

## Complete Algorithm Switching Protocol

### Two-Phase Commit Sequence

```mermaid
sequenceDiagram
    participant GCS as GCS Proxy
    participant GNet as GCS Network
    participant DNet as Drone Network  
    participant Drone as Drone Proxy
    participant Policy as Policy Engine
    participant Handshake as Handshake Module
    
    Note over GCS,Policy: Phase 1 - Negotiation
    
    GCS->>GCS: Decision to rekey<br/>(periodic, policy, or threat-based)
    GCS->>GCS: Select target_suite<br/>(e.g., "cs-mlkem768-aesgcm-falcon512")
    
    GCS->>GCS: prepare_control_message:<br/>• type = prepare_rekey<br/>• target_suite = suite_id<br/>• nonce = random(8)<br/>• timestamp = current_time
    
    GCS->>GCS: encrypt_packet(control_msg, type=0x02)
    GCS->>GNet: Control packet (encrypted)
    GNet->>DNet: Network transmission
    DNet->>Drone: UDP delivery
    
    Drone->>Drone: decrypt_verify(packet)
    Drone->>Drone: Extract control message<br/>Verify type = 0x02
    Drone->>Policy: handle_control(prepare_rekey, target_suite)
    
    Policy->>Policy: Validate suite availability:<br/>• Check SUPPORTED_SUITES<br/>• Verify OQS library support<br/>• Check resource constraints
    
    Policy->>Policy: Assess compatibility:<br/>• Current system state<br/>• Performance requirements<br/>• Security policies
    
    alt Suite Available and Compatible
        Policy->>Policy: State = NEGOTIATING
        Policy->>Drone: Response: commit_rekey
        
        Drone->>Drone: prepare_control_response:<br/>• type = commit_rekey<br/>• target_suite = confirmed<br/>• nonce = echo_nonce<br/>• ready_time = timestamp
        
        Drone->>Drone: encrypt_packet(response, type=0x02)
        Drone->>DNet: Control response
        DNet->>GNet: Network transmission
        GNet->>GCS: UDP delivery
        
        GCS->>GCS: decrypt_verify(response)
        GCS->>GCS: Verify commit_rekey confirmation
        
        Note over GCS,Policy: Phase 2 - Execution
        
        par Prepare for Transition
            GCS->>GCS: State = SWAPPING<br/>Stop accepting new data<br/>Drain pending packets
        and
            Drone->>Drone: State = SWAPPING<br/>Stop accepting new data<br/>Drain pending packets
        end
        
        Note over GCS,Drone: Initiate New Handshake
        GCS->>Handshake: server_gcs_handshake(target_suite)
        Handshake->>Handshake: Generate new ephemeral keys<br/>for target algorithms
        
        GCS->>Drone: New TCP handshake with target suite
        Drone->>Handshake: client_drone_handshake(target_suite)
        
        Note over GCS,Drone: Complete Key Exchange
        Handshake->>Handshake: ML-KEM key exchange<br/>with new parameters
        Handshake->>Handshake: derive_transport_keys<br/>with target algorithms
        
        par Atomic Switch
            GCS->>GCS: • epoch++<br/>• Install new session keys<br/>• Update suite configuration<br/>• Reset sequence numbers
        and
            Drone->>Drone: • epoch++<br/>• Install new session keys<br/>• Update suite configuration<br/>• Reset sequence numbers
        end
        
        par Resume Operation
            GCS->>GCS: State = RUNNING<br/>Resume data processing<br/>with new algorithms
        and
            Drone->>Drone: State = RUNNING<br/>Resume data processing<br/>with new algorithms
        end
        
        Note over GCS,Drone: Success - New Cryptographic Context Active
        
    else Suite Unavailable or Incompatible
        Policy->>Drone: Response: prepare_fail
        
        Drone->>Drone: prepare_control_response:<br/>• type = prepare_fail<br/>• reason = unavailable/incompatible<br/>• current_suite = maintaining
        
        Drone->>DNet: Control response (failure)
        DNet->>GNet: Network transmission
        GNet->>GCS: UDP delivery
        
        GCS->>GCS: Process prepare_fail<br/>Log rekey failure<br/>Continue with current suite
        
        Note over GCS,Drone: Continue with Current Cryptographic Suite
    end
```

## State Machine Implementation

### Control State Transitions

```mermaid
stateDiagram-v2
    [*] --> INITIALIZING : System startup
    
    INITIALIZING --> HANDSHAKING : Begin initial handshake
    HANDSHAKING --> RUNNING : Handshake success
    HANDSHAKING --> FAILED : Handshake failure
    
    RUNNING --> NEGOTIATING : Receive prepare_rekey
    RUNNING --> NEGOTIATING : Initiate rekey request
    
    NEGOTIATING --> RUNNING : Send/receive prepare_fail
    NEGOTIATING --> SWAPPING : Send/receive commit_rekey
    NEGOTIATING --> TIMEOUT : Negotiation timeout
    
    SWAPPING --> RUNNING : New handshake success
    SWAPPING --> FAILED : New handshake failure
    SWAPPING --> TIMEOUT : Swap timeout
    
    TIMEOUT --> RUNNING : Fallback to previous suite
    TIMEOUT --> FAILED : Recovery impossible
    
    FAILED --> INITIALIZING : Restart connection
    FAILED --> [*] : System shutdown
    
    note right of RUNNING
        Normal Operation:
        • Process application data
        • Monitor for rekey triggers
        • Handle control messages
        • Maintain session state
    end note
    
    note right of NEGOTIATING
        Rekey Negotiation:
        • Validate target suite
        • Check resource availability
        • Coordinate timing
        • Prepare for transition
    end note
    
    note right of SWAPPING
        Algorithm Transition:
        • New handshake execution
        • Key derivation and installation
        • Atomic context switch
        • State synchronization
    end note
```

### Detailed State Behaviors

```mermaid
flowchart TB
    subgraph "RUNNING State"
        A1[Process Data Packets]
        A2[Monitor Rekey Triggers]
        A3[Handle Control Messages]
        A4[Update Performance Metrics]
    end
    
    subgraph "NEGOTIATING State"
        B1[Validate Target Suite]
        B2[Check Resource Constraints]
        B3[Assess Compatibility]
        B4[Make Commit Decision]
        B5[Send Response]
    end
    
    subgraph "SWAPPING State"
        C1[Stop Data Processing]
        C2[Drain Pending Packets]
        C3[Execute New Handshake]
        C4[Derive New Keys]
        C5[Install New Context]
        C6[Resume Processing]
    end
    
    subgraph "Trigger Conditions"
        D1[Periodic Rekey Timer]
        D2[Packet Count Threshold]
        D3[Security Policy Change]
        D4[Performance Degradation]
        D5[External Command]
    end
    
    D1 --> A2
    D2 --> A2
    D3 --> A2
    D4 --> A2
    D5 --> A3
    
    A2 --> B1
    A3 --> B1
    B5 --> C1
    C6 --> A1
    
    style A2 fill:#e3f2fd
    style B4 fill:#f3e5f5
    style C5 fill:#e8f5e8
```

## Control Message Format

### Control Packet Structure

```mermaid
graph TB
    subgraph "Control Packet (Type 0x02)"
        subgraph "Standard Header (22 bytes)"
            A[Version, KEM_ID, KEM_Param<br/>Sig_ID, Sig_Param<br/>Session_ID, Sequence, Epoch]
        end
        
        subgraph "Control Payload (Encrypted)"
            B[Message Type<br/>1 byte<br/>prepare_rekey=0x01<br/>commit_rekey=0x02<br/>prepare_fail=0x03]
            
            C[Target Suite ID<br/>Variable length<br/>String identifier<br/>example cs-mlkem768-aesgcm-falcon512]
            
            D[Nonce<br/>8 bytes<br/>Replay protection<br/>Echo in response]
            
            E[Timestamp<br/>8 bytes<br/>Unix timestamp<br/>Coordination timing]
            
            F[Additional Data<br/>Variable<br/>Message-specific<br/>payload]
        end
        
        subgraph "Authentication"
            G[GCM Tag<br/>16 bytes<br/>Message authentication]
        end
    end
    
    A --> B
    B --> C
    C --> D
    D --> E
    E --> F
    F --> G
    
    style A fill:#e3f2fd
    style B fill:#f3e5f5
    style D fill:#e8f5e8
    style G fill:#fce4ec
```

### Message Type Specifications

```mermaid
graph LR
    subgraph "prepare_rekey (0x01)"
        A1[Initiates rekey process<br/>Contains target suite<br/>Requests availability check]
        A2[Additional Data:<br/>• reason code<br/>• preferred timing<br/>• compatibility flags]
    end
    
    subgraph "commit_rekey (0x02)"
        B1[Confirms rekey agreement<br/>Authorizes transition<br/>Establishes timing]
        B2[Additional Data:<br/>• ready timestamp<br/>• resource allocation<br/>• synchronization info]
    end
    
    subgraph "prepare_fail (0x03)"
        C1[Rejects rekey request<br/>Explains failure reason<br/>Suggests alternatives]
        C2[Additional Data:<br/>• failure reason<br/>• alternative suites<br/>• retry timing]
    end
    
    A1 --> A2
    B1 --> B2
    C1 --> C2
    
    style A1 fill:#e3f2fd
    style B1 fill:#ccffcc
    style C1 fill:#ffcccc
```

## Synchronization and Timing

### Coordination Timeline

```mermaid
gantt
    title Runtime Algorithm Switch Timeline
    dateFormat X
    axisFormat %s
    
    section Negotiation
    Send prepare_rekey     :done, neg1, 0, 1s
    Process request        :done, neg2, 1s, 2s
    Send commit_rekey      :done, neg3, 2s, 3s
    
    section Preparation
    Drain packets         :done, prep1, 3s, 4s
    Prepare handshake     :done, prep2, 4s, 5s
    
    section Transition
    New TCP handshake     :done, trans1, 5s, 6s
    Key derivation        :done, trans2, 6s, 7s
    Atomic switch         :crit, trans3, 7s, 8s
    
    section Resume
    Resume processing     :done, resume1, 8s, 9s
    Normal operation      :done, resume2, 9s, 10s
```

### Packet Flow During Transition

```mermaid
sequenceDiagram
    participant App as Application
    participant Proxy as Proxy
    participant Network as Network
    participant Remote as Remote Proxy
    
    Note over App,Remote: Normal Operation (Epoch N)
    App->>Proxy: Data packets
    Proxy->>Network: Encrypted with current suite
    Network->>Remote: Delivery
    
    Note over Proxy,Remote: Begin Transition
    Proxy->>Proxy: State = NEGOTIATING
    App->>Proxy: Data packets
    Proxy->>Proxy: Continue with current suite
    
    Note over Proxy,Remote: Prepare Transition
    Proxy->>Proxy: State = SWAPPING
    App->>Proxy: Data packets
    Proxy->>Proxy: Queue packets (temporary)
    
    Note over Proxy,Remote: Execute Transition
    Proxy->>Remote: New handshake
    Proxy->>Proxy: Install new keys (Epoch N+1)
    Remote->>Remote: Install new keys (Epoch N+1)
    
    Note over App,Remote: Resume Operation (Epoch N+1)
    Proxy->>Proxy: Process queued packets
    Proxy->>Network: Encrypted with new suite
    App->>Proxy: New data packets
    Proxy->>Network: Continue with new suite
```

## Error Handling and Recovery

### Failure Scenarios and Recovery

```mermaid
flowchart TB
    A[Begin Algorithm Switch] --> B{Negotiation Success?}
    
    B -->|No| C[Receive prepare_fail]
    C --> D[Log failure reason]
    D --> E[Continue current suite]
    E --> F[Schedule retry<br/>with backoff]
    
    B -->|Yes| G[Begin SWAPPING state]
    G --> H{New Handshake Success?}
    
    H -->|No| I[Handshake failure]
    I --> J[Revert to previous suite]
    J --> K[State = RUNNING]
    K --> L[Log switch failure]
    
    H -->|Yes| M[Key installation]
    M --> N{Atomic Switch Success?}
    
    N -->|No| O[Synchronization failure]
    O --> P[Emergency fallback]
    P --> Q[Reset connection]
    
    N -->|Yes| R[Success: New suite active]
    R --> S[Update metrics]
    S --> T[Log successful switch]
    
    style C fill:#ffcccc
    style I fill:#ffcccc
    style O fill:#ff9999
    style R fill:#ccffcc
```

### Timeout and Recovery Logic

```mermaid
sequenceDiagram
    participant GCS as GCS
    participant Drone as Drone
    participant Timer as Timeout Timer
    
    GCS->>Drone: prepare_rekey
    GCS->>Timer: Start negotiation timer (30s)
    
    alt Normal Response
        Drone->>GCS: commit_rekey (within timeout)
        GCS->>Timer: Cancel timer
        Note over GCS,Drone: Proceed with switch
        
    else Timeout Scenario
        Timer->>GCS: Negotiation timeout
        GCS->>GCS: Log timeout event
        GCS->>GCS: Increment failure counter
        
        alt Retry Logic
            GCS->>GCS: Wait exponential backoff
            GCS->>Drone: Retry prepare_rekey
        else Max Retries
            GCS->>GCS: Mark drone unreachable
            GCS->>GCS: Continue with current suite
        end
    end
    
    Note over GCS,Drone: Handshake Phase
    GCS->>Drone: New TCP handshake
    GCS->>Timer: Start handshake timer (60s)
    
    alt Handshake Success
        Drone->>GCS: Handshake complete
        GCS->>Timer: Cancel timer
        Note over GCS,Drone: Switch complete
        
    else Handshake Timeout
        Timer->>GCS: Handshake timeout
        GCS->>GCS: Revert to previous suite
        Note over GCS,Drone: Fallback to old suite
    end
```

---

**Navigation**: 
- **Back to**: [Diagrams Index](../README.md)
- **Related**: [Handshake Protocol](handshake.md) | [State Machines](../implementation/state-machines.md)
- **Technical Docs**: [Runtime Switching](../../technical/runtime-switching.md)