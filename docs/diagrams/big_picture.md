# Big picture: End-to-end architecture

This diagram shows both sides (GCS and Drone), the control-plane handshake, the data-plane AEAD path, and the internal modules that move data and perform rekeying and policy decisions.

```mermaid
%%{init: {'securityLevel': 'loose', 'flowchart': {'htmlLabels': true}}}%%
flowchart LR
  %% LAYOUT
  classDef comp fill:#eef,stroke:#447,stroke-width:1px
  classDef mod fill:#f7faff,stroke:#6699cc,stroke-width:1px
  classDef io fill:#fffaf0,stroke:#cc9900,stroke-width:1px
  classDef net fill:#f9f9f9,stroke:#888,stroke-dasharray:3 3
  classDef warn fill:#ffecec,stroke:#cc6666
  classDef note fill:#ffffe0,stroke:#999,stroke-dasharray:3 3,color:#333

  %% GCS SIDE
  subgraph GCS[Ground Control Station]
    direction TB
  GCSApps["Traffic/Control Apps<br/>(blasters, tests)"]
  GCSProxy["core/run_proxy.py"]
  GCSAsync["core/async_proxy.py<br/>UDP/TCP orchestration"]
  GCSHS["core/handshake.py<br/>KEM+SIG, HKDF"]
  GCSAEAD["core/aead.py<br/>Sender/Receiver, replay"]
  GCSPolicy["core/policy_engine.py<br/>Rekey 2PC FSM"]
  GCSCfg["core/config.py &amp; suites.py"]
  GCSLog["core/logging_utils.py"]

    GCSApps -->|UDP plaintext| GCSProxy
    GCSProxy --> GCSAsync
    GCSAsync --> GCSHS
    GCSAsync --> GCSAEAD
    GCSAsync --> GCSPolicy
    GCSHS --> GCSCfg
    GCSAEAD --> GCSCfg
    GCSPolicy --> GCSCfg
    GCSProxy --> GCSLog
  end

  %% DRONE SIDE
  subgraph DRN[Drone]
    direction TB
  DroneApps["Flight/Telemetry Apps<br/>(UdpEcho, Telemetry, Power)"]
  DroneProxy["core/run_proxy.py"]
  DroneAsync["core/async_proxy.py<br/>UDP/TCP orchestration"]
  DroneHS["core/handshake.py<br/>KEM+SIG, HKDF"]
  DroneAEAD["core/aead.py<br/>Sender/Receiver, replay"]
  DronePolicy["core/policy_engine.py<br/>Rekey 2PC FSM"]
  DroneCfg["core/config.py &amp; suites.py"]
  DroneLog["core/logging_utils.py"]

    DroneApps <-->|UDP plaintext| DroneProxy
    DroneProxy --> DroneAsync
    DroneAsync --> DroneHS
    DroneAsync --> DroneAEAD
    DroneAsync --> DronePolicy
    DroneHS --> DroneCfg
    DroneAEAD --> DroneCfg
    DronePolicy --> DroneCfg
    DroneProxy --> DroneLog
  end

  %% NETWORK / CONTROL + DATA PLANES
  subgraph NET[Network]
    direction LR
  TCPHS["TCP Handshake<br/>(client&lt;-&gt;server)"]:::net
  UDPAEAD["UDP Encrypted<br/>AES-GCM frames"]:::net
  UDPPlain["UDP Plaintext<br/>app-side"]:::net
  end

  %% Cross links
  GCSAsync -- Client role --> TCPHS
  TCPHS -- Server role --> DroneAsync

  GCSAEAD -- AEAD UDP --> UDPAEAD
  UDPAEAD -- AEAD UDP --> DroneAEAD

  GCSApps -. app traffic .-> UDPPlain
  UDPPlain -. app traffic .-> DroneApps

  %% LABELS & NOTES (flowchart doesn't support "note over"; use dedicated nodes)
  NoteTCPHS["KEM Encaps/Decaps + SIG verify<br/>Derive 2x32B keys via HKDF-SHA256"]
  NoteTCPHS -.-> TCPHS
  NoteUDPAEAD["Header 22B (!BBBBB8sQB), nonce = epoch || seq(11)<br/>Receiver replay window; silent drops on auth fail"]
  NoteUDPAEAD -.-> UDPAEAD
  NoteRekey["Control rekey path:<br/>NEGOTIATING → SWAPPING → RUNNING"]
  NoteRekey -.-> GCSPolicy
  NoteRekey -.-> DronePolicy

  %% DATA PATH DETAIL
  subgraph DATAFLOW[Data-plane flow]
    direction TB
  AppOut["App Payload"] --> Header["Build 22B header<br/>(epoch, seq, suite ids)"] --> Nonce["epoch||seq(11)"] --> Encrypt["AES-GCM seal"] --> Packet["Header||Ciphertext||Tag"]
  Packet --> Verify["Header check + replay window"] --> Open["AES-GCM open"] --> AppIn["Deliver plaintext to app"]
  end

  GCSApps --> AppOut
  AppIn --> DroneApps

  %% CONTROL PATH DETAIL
  subgraph CONTROL[Control-plane rekey]
    direction LR
    Propose[Propose new suite IDs]:::mod --> Neg[NEGOTIATING]:::mod --> Swap[SWAPPING (freeze old, set new epoch)]:::mod --> Run[RUNNING (new keys)]:::mod
  end
  GCSPolicy --> Propose
  Run --> DronePolicy

  %% ERROR/OBSERVABILITY
  GCSAEAD -. AeadAuthError on auth fail .-> GCSLog
  DroneAEAD -. AeadAuthError on auth fail .-> DroneLog
  GCSHS -. HandshakeVerifyError on decaps/verify fail .-> GCSLog
  DroneHS -. HandshakeVerifyError on decaps/verify fail .-> DroneLog

  %% GROUPED CLASS ASSIGNMENTS
  class GCSProxy,DroneProxy comp
  class GCSAsync,DroneAsync,GCSHS,DroneHS,GCSAEAD,DroneAEAD,GCSPolicy,DronePolicy,GCSCfg,DroneCfg,GCSLog,DroneLog,Header,Nonce,Encrypt,Verify,Open,Propose,Neg,Swap,Run mod
  class GCSApps,DroneApps,AppOut,Packet,AppIn io
  class TCPHS,UDPAEAD,UDPPlain net
  class NoteTCPHS,NoteUDPAEAD,NoteRekey note
```

## How each side actually works

- GCS box
  - core/run_proxy.py boots listeners and wires components based on `--suite` and config.
  - core/async_proxy.py runs the TCP handshake as client, builds Sender/Receiver, manages UDP sockets, QoS, and rekey orchestration with the policy engine.
  - core/handshake.py negotiates KEM+SIG, verifies identities, and derives transport keys via HKDF.
  - core/aead.py frames packets (22-byte header), constructs nonces from epoch+seq, enforces replay defense, and seals/opens with AES-GCM.
  - core/policy_engine.py performs the two-phase rekey state machine and applies manual/automatic policy.
  - core/config.py and core/suites.py are the source of truth for suite IDs, HKDF salts, and lengths.
  - core/logging_utils.py provides structured JSON logs.

- Drone box
  - Mirrors GCS flow but acts as TCP server in the handshake.
  - The follower utilities (e.g., UdpEcho, TelemetryPublisher, PowerCapture) interact with the proxy via plaintext UDP on the app side.
  - Rekey and AEAD behavior are symmetric to GCS, with the same header/nonce rules and replay window.

Notes
- Wire compatibility: changing header layout or types requires tests updates (tests/test_aead_framing.py, tests/test_packet_types.py).
- Constant-time guardrails: no secret-dependent branching or logging on secrets in core/ crypto paths.
- Control-plane transitions must go through policy_engine.ControlState helpers.
