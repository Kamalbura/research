# Data Transport Protocol Diagrams

This document contains detailed visualizations of the UDP data transport protocol implementation.

## UDP Data Plane Protocol

### Complete Packet Processing Flow

```mermaid
sequenceDiagram
    participant App as Application
    participant DProxy as Drone Proxy
    participant Network as UDP Network
    participant GProxy as GCS Proxy
    participant GCSApp as GCS Application
    
    Note over App,GCSApp: Post-Handshake Data Flow
    
    App->>DProxy: UDP packet (plaintext data)
    
    Note over DProxy: Outbound Processing
    DProxy->>DProxy: Check session state
    DProxy->>DProxy: Build packet header:<br/>• version=1<br/>• kem_id, kem_param<br/>• sig_id, sig_param<br/>• session_id<br/>• sequence++<br/>• epoch
    
    DProxy->>DProxy: Generate nonce:<br/>nonce = epoch || sequence[11 bytes]
    
    DProxy->>DProxy: AES-256-GCM encrypt:<br/>• key = drone_to_gcs_key<br/>• plaintext = app_data<br/>• aad = header<br/>• nonce = deterministic
    
    DProxy->>Network: UDP packet: header + ciphertext + tag
    Network->>GProxy: Network delivery
    
    Note over GProxy: Inbound Processing
    GProxy->>GProxy: Parse packet header
    GProxy->>GProxy: Validate header format
    GProxy->>GProxy: Check session_id match
    
    GProxy->>GProxy: Replay protection check:<br/>• Extract sequence number<br/>• Check against sliding window<br/>• Update window if valid
    
    alt Valid Packet
        GProxy->>GProxy: Reconstruct nonce:<br/>nonce = epoch || sequence[11 bytes]
        
        GProxy->>GProxy: AES-256-GCM decrypt:<br/>• key = drone_to_gcs_key<br/>• ciphertext = packet_data<br/>• aad = header<br/>• nonce = reconstructed
        
        GProxy->>GProxy: Increment success counter
        GProxy->>GCSApp: Forward plaintext data
        
    else Invalid Packet
        alt Bad Format
            GProxy->>GProxy: Increment drop_format counter
        else Authentication Failure
            GProxy->>GProxy: Increment drop_auth_fail counter
        else Replay/Window
            GProxy->>GProxy: Increment drop_replay counter
        else Decryption Failure
            GProxy->>GProxy: Increment drop_decrypt counter
        end
        GProxy->>GProxy: Silent drop (no response)
    end
    
    Note over App,GCSApp: Bidirectional - Same process for GCS→Drone
```

## Packet Format Details

### Complete Packet Structure

```mermaid
graph TB
    subgraph "UDP Packet (Total: Header + Data + Tag)"
        subgraph "Plaintext Header (22 bytes)"
            A[Version<br/>1 byte<br/>WIRE_VERSION=1]
            B[KEM ID<br/>1 byte<br/>Algorithm identifier]
            C[KEM Param<br/>1 byte<br/>Parameter set]
            D[Signature ID<br/>1 byte<br/>Algorithm identifier]
            E[Signature Param<br/>1 byte<br/>Parameter set]
            F[Session ID<br/>8 bytes<br/>From handshake]
            G[Sequence Number<br/>8 bytes<br/>Monotonic counter]
            H[Epoch<br/>1 byte<br/>Crypto context version]
        end
        
        subgraph "Encrypted Payload"
            I[Application Data<br/>Variable length<br/>AES-256-GCM encrypted]
        end
        
        subgraph "Authentication"
            J[GCM Tag<br/>16 bytes<br/>Authentication tag]
        end
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
    
    style A fill:#e3f2fd
    style F fill:#f3e5f5
    style G fill:#e8f5e8
    style H fill:#fff3e0
    style I fill:#fce4ec
    style J fill:#e8f5e8
```

### Header Encoding Details

```mermaid
graph LR
    subgraph "Algorithm IDs Encoding"
        A[KEM ID Examples:<br/>0x00: ML-KEM-512<br/>0x01: ML-KEM-768<br/>0x02: ML-KEM-1024]
        
        B[Signature ID Examples:<br/>0x00: ML-DSA-44<br/>0x01: ML-DSA-65<br/>0x02: ML-DSA-87<br/>0x10: Falcon-512<br/>0x11: Falcon-1024<br/>0x20: SLH-DSA-128s<br/>0x21: SLH-DSA-256s]
        
        C[Parameter Encoding:<br/>0x00: Standard parameters<br/>0x01: Custom/future use]
    end
    
    subgraph "Sequence and Epoch"
        D[Sequence Number:<br/>64-bit big-endian<br/>Starts at 0<br/>Increments per packet]
        
        E[Epoch Counter:<br/>8-bit value<br/>Increments on rekey<br/>Prevents nonce reuse]
    end
    
    A --> F[header_ids_for_suite<br/>Function mapping]
    B --> F
    C --> F
    D --> G[Nonce Construction<br/>epoch || sequence[11:]]
    E --> G
    
    style F fill:#e8f5e8
    style G fill:#fff3e0
```

## Nonce Generation and Management

### Deterministic Nonce Construction

```mermaid
flowchart TB
    subgraph "Nonce Requirements"
        A[96-bit AES-GCM nonce<br/>Must be unique per key<br/>Never reuse with same key]
    end
    
    subgraph "Our Construction"
        B[Epoch Counter<br/>8 bits<br/>Increments on rekey]
        C[Sequence Number<br/>64 bits<br/>Monotonic per session]
        D[Truncate to 88 bits<br/>Use lower 11 bytes of sequence]
        E[Concatenate<br/>nonce = epoch || sequence[11:]]
    end
    
    subgraph "Security Properties"
        F[Uniqueness:<br/>Different epoch = different nonce<br/>Same epoch = sequence increment]
        G[No transmission overhead:<br/>Deterministic reconstruction<br/>Both sides compute same nonce]
        H[Rekey safety:<br/>Epoch increment prevents<br/>nonce reuse across sessions]
    end
    
    A --> B
    B --> D
    C --> D
    D --> E
    E --> F
    E --> G
    E --> H
    
    style A fill:#e3f2fd
    style E fill:#e8f5e8
    style F fill:#ccffcc
    style G fill:#ccffcc
    style H fill:#ccffcc
```

### Nonce Space Analysis

```mermaid
graph TB
    subgraph "Nonce Space Partitioning"
        A[Total 96-bit space<br/>2^96 possible nonces]
        B[Epoch partition<br/>8 bits = 256 epochs]
        C[Sequence partition<br/>88 bits ≈ 3.1×10^26 sequences]
    end
    
    subgraph "Practical Limits"
        D[Max packets per epoch:<br/>2^64 = 1.8×10^19<br/>Far exceeds practical use]
        E[Max rekey operations:<br/>256 epochs<br/>Sufficient for operational lifetime]
        F[Safety margin:<br/>Rekey recommended at<br/>2^32 packets ≈ 4 billion]
    end
    
    A --> B
    A --> C
    B --> E
    C --> D
    D --> F
    
    style A fill:#e3f2fd
    style D fill:#e8f5e8
    style E fill:#e8f5e8
    style F fill:#ccffcc
```

## Replay Protection Mechanism

### Sliding Window Algorithm

```mermaid
flowchart TB
    subgraph "Window State"
        A[High Watermark<br/>Highest received sequence]
        B[Window Size<br/>Default: 1024 packets]
        C[Bitmask<br/>Track recent packets<br/>1024 bits = 128 bytes]
    end
    
    subgraph "Packet Reception"
        D[Extract sequence number<br/>from packet header]
        E{Sequence > High Watermark?}
        F{Sequence in window?}
        G{Already received?}
    end
    
    subgraph "Window Update"
        H[Advance high watermark<br/>Shift bitmask left<br/>Set new bit]
        I[Check bitmask<br/>for sequence position]
        J[Set bit in bitmask<br/>for this sequence]
    end
    
    subgraph "Packet Decision"
        K[Accept packet<br/>Continue processing]
        L[Drop packet<br/>Too old or duplicate]
    end
    
    D --> E
    E -->|Yes| H
    E -->|No| F
    F -->|Yes| G
    F -->|No| L
    G -->|Yes| L
    G -->|No| J
    H --> K
    J --> K
    
    style K fill:#ccffcc
    style L fill:#ffcccc
```

### Window Management Implementation

```mermaid
sequenceDiagram
    participant Packet as Incoming Packet
    participant Parser as Header Parser
    participant Window as ReplayWindow
    participant Counter as ProxyCounters
    
    Packet->>Parser: UDP packet received
    Parser->>Parser: Extract sequence number
    Parser->>Window: is_valid(sequence)
    
    alt Future packet (seq > high_watermark)
        Window->>Window: Advance window:<br/>• new_high = sequence<br/>• shift = new_high - old_high<br/>• bitmask <<= shift<br/>• bitmask |= 1
        Window->>Parser: Return True
        Parser->>Counter: Increment forward_success
        
    else Within window
        Window->>Window: Check bitmask:<br/>bit_pos = high_watermark - sequence<br/>already_received = bitmask & (1 << bit_pos)
        
        alt Not yet received
            Window->>Window: Set bit:<br/>bitmask |= (1 << bit_pos)
            Window->>Parser: Return True
            Parser->>Counter: Increment accept_window
        else Already received
            Window->>Parser: Return False
            Parser->>Counter: Increment drop_replay
        end
        
    else Too old (outside window)
        Window->>Parser: Return False
        Parser->>Counter: Increment drop_window
    end
```

## AES-GCM Encryption Details

### Encryption Process

```mermaid
flowchart TB
    subgraph "Input Preparation"
        A[Plaintext data<br/>Application payload]
        B[Session key<br/>32 bytes from HKDF]
        C[Nonce<br/>epoch || sequence[11:]<br/>96 bits total]
        D[AAD<br/>22-byte packet header<br/>Authenticated but not encrypted]
    end
    
    subgraph "AES-256-GCM Encryption"
        E[Initialize AES-256-GCM<br/>with key and nonce]
        F[Set Additional Authenticated Data<br/>AAD = packet header]
        G[Encrypt plaintext<br/>Generate ciphertext]
        H[Compute authentication tag<br/>16-byte GCM tag]
    end
    
    subgraph "Output Construction"
        I[Final packet:<br/>header || ciphertext || tag]
        J[Increment sequence number<br/>for next packet]
    end
    
    A --> E
    B --> E
    C --> E
    D --> F
    E --> G
    F --> H
    G --> I
    H --> I
    I --> J
    
    style E fill:#e3f2fd
    style I fill:#e8f5e8
```

### Decryption and Verification

```mermaid
sequenceDiagram
    participant Receiver as Packet Receiver
    participant Parser as Header Parser
    participant AES as AES-GCM Engine
    participant Window as Replay Window
    participant App as Application
    
    Receiver->>Parser: Received packet
    Parser->>Parser: Parse header (22 bytes)
    Parser->>Parser: Extract ciphertext and tag
    
    Parser->>Window: Check replay protection
    
    alt Valid sequence
        Parser->>Parser: Reconstruct nonce:<br/>epoch || sequence[11:]
        
        Parser->>AES: Initialize decryption:<br/>• key = session_key<br/>• nonce = reconstructed<br/>• aad = header
        
        AES->>AES: Verify authentication tag
        
        alt Tag valid
            AES->>AES: Decrypt ciphertext
            AES->>Parser: Return plaintext
            Parser->>Window: Update window state
            Parser->>App: Forward plaintext
        else Tag invalid
            AES->>Parser: Authentication failure
            Parser->>Parser: Increment drop_auth_fail<br/>Silent drop
        end
        
    else Invalid sequence
        Parser->>Parser: Increment drop_replay<br/>Silent drop
    end
```

## Performance Optimizations

### Packet Processing Pipeline

```mermaid
graph LR
    subgraph "Fast Path (Normal Case)"
        A[UDP Receive] --> B[Header Parse]
        B --> C[Sequence Check]
        C --> D[AES-GCM Decrypt]
        D --> E[Forward to App]
    end
    
    subgraph "Optimization Techniques"
        F[Header Caching<br/>Avoid repeated parsing]
        G[AES Hardware Acceleration<br/>Use CPU crypto instructions]
        H[Batch Processing<br/>Multiple packets per loop]
        I[Zero-Copy Forwarding<br/>Minimize memory copies]
    end
    
    B -.-> F
    D -.-> G
    A -.-> H
    E -.-> I
    
    style A fill:#e3f2fd
    style E fill:#e8f5e8
    style F fill:#ccffcc
    style G fill:#ccffcc
    style H fill:#ccffcc
    style I fill:#ccffcc
```

---

**Navigation**: 
- **Back to**: [Diagrams Index](../README.md)
- **Related**: [Handshake Protocol](handshake.md) | [Runtime Switching](runtime-switching.md)
- **Technical Docs**: [Data Transport](../../technical/data-transport.md)