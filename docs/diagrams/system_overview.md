# System overview

```mermaid
flowchart LR
  subgraph GCS[Ground Control Station]
    GCSApp[Traffic/Control Apps]
    GCSProxy[core/run_proxy.py]
  end

  subgraph Network
    UDPPlain[Plaintext UDP]
    UDPEnc[Encrypted UDP]
    TCPHS[TCP Handshake]
  end

  subgraph Drone
    DroneApp[Flight/Telemetry Apps]
    DroneProxy[core/run_proxy.py]
  end

  GCSApp-- UDP plain -->GCSProxy
  GCSProxy-- TCPHS (KEM+SIG) -->DroneProxy
  GCSProxy-- AEAD UDP -->DroneProxy
  DroneProxy-- UDP plain -->DroneApp

  classDef comp fill:#eef,stroke:#447
  class GCSApp,GCSProxy,DroneApp,DroneProxy comp
```

## Legend

- `Traffic/Control Apps`: suite schedulers and blasters under `gcs/` and `tools/auto/` that emit plaintext telemetry commands.
- `core/run_proxy.py` (GCS): proxy entry point that mediates between plaintext apps and encrypted transport.
- `Plaintext UDP`: local loopback socket used by apps; locked down unless `ALLOW_NON_LOOPBACK_PLAINTEXT=1`.
- `TCP Handshake`: PQC handshake over TCP orchestrated by `core/async_proxy._perform_handshake()`.
- `Encrypted UDP`: AEAD-protected packets carrying the `HEADER_STRUCT` fields defined in `core/aead.py`.
- `core/run_proxy.py` (Drone): mirror proxy forwarding decrypted payloads to flight/telemetry software.
- `Flight/Telemetry Apps`: `drone/` binaries, `tools/auto/drone_follower.py` modules, and power/telemetry collectors.