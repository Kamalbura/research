# Big picture: End-to-end architecture

This diagram shows both sides (GCS and Drone), the control-plane handshake, the data-plane AEAD path, and the internal modules that move data and perform rekeying and policy decisions.

```mermaid
flowchart LR
  %% LAYOUT
  classDef comp fill:#eef,stroke:#447,stroke-width:1px
  classDef mod fill:#f7faff,stroke:#6699cc,stroke-width:1px
  classDef io fill:#fffaf0,stroke:#cc9900,stroke-width:1px
  classDef net fill:#f9f9f9,stroke:#888,stroke-dasharray:3 3
  classDef warn fill:#ffecec,stroke:#cc6666

  %% GCS SIDE
  subgraph GCS[Ground Control Station]
    direction TB
    GCSApps[Traffic/Control Apps\n(blasters, tests)]:::io
    GCSProxy[core/run_proxy.py]:::comp
    GCSAsync[core/async_proxy.py\nUDP/TCP orchestration]:::mod
    GCSHS[core/handshake.py\nKEM+SIG, HKDF]:::mod
    GCSAEAD[core/aead.py\nSender/Receiver, replay]:::mod
    GCSPolicy[core/policy_engine.py\nRekey 2PC FSM]:::mod
    GCSCfg[core/config.py & suites.py]:::mod
    GCSLog[core/logging_utils.py]:::mod

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
    DroneApps[Flight/Telemetry Apps\n(UdpEcho, Telemetry, Power)]:::io
    DroneProxy[core/run_proxy.py]:::comp
    DroneAsync[core/async_proxy.py\nUDP/TCP orchestration]:::mod
    DroneHS[core/handshake.py\nKEM+SIG, HKDF]:::mod
    DroneAEAD[core/aead.py\nSender/Receiver, replay]:::mod
    DronePolicy[core/policy_engine.py\nRekey 2PC FSM]:::mod
    DroneCfg[core/config.py & suites.py]:::mod
    DroneLog[core/logging_utils.py]:::mod

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
    TCPHS[TCP Handshake\n(client<->server)]:::net
    UDPAEAD[UDP Encrypted\nAES-GCM frames]:::net
    UDPPlain[UDP Plaintext\napp-side]:::net
  end

  %% Cross links
  GCSAsync -- Client role --> TCPHS
  TCPHS -- Server role --> DroneAsync

  GCSAEAD -- AEAD UDP --> UDPAEAD
  UDPAEAD -- AEAD UDP --> DroneAEAD

  GCSApps -. app traffic .-> UDPPlain
  UDPPlain -. app traffic .-> DroneApps

  %% LABELS & NOTES
  note over TCPHS: KEM Encaps/Decaps + SIG verify\nDerive 2x32B keys via HKDF-SHA256
  note over UDPAEAD: Header 22B (!BBBBB8sQB), nonce=epoch||seq(11)\nReplay window in Receiver; drops are silent
  note over GCSPolicy,DronePolicy: Control messages drive NEGOTIATING→SWAPPING→RUNNING

  %% DATA PATH DETAIL
  subgraph DATAFLOW[Data-plane flow]
    direction TB
    AppOut[App Payload]:::io --> Header[Build 22B header\n(epoch, seq, suite ids)]:::mod --> Nonce[epoch||seq(11)]:::mod --> Encrypt[AES-GCM seal]:::mod --> Packet[Header||Ciphertext||Tag]:::io
    Packet --> Verify[Header check + replay window]:::mod --> Open[AES-GCM open]:::mod --> AppIn[Deliver plaintext to app]:::io
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
  GCSAEAD -. AeadAuthError on auth fail .-> GCSLog:::warn
  DroneAEAD -. AeadAuthError on auth fail .-> DroneLog:::warn
  GCSHS -. HandshakeVerifyError on decaps/verify fail .-> GCSLog:::warn
  DroneHS -. HandshakeVerifyError on decaps/verify fail .-> DroneLog:::warn
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
