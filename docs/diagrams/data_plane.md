# Data plane: header and nonce

```mermaid
sequenceDiagram
  participant S as Sender (core/aead.Sender)
  participant R as Receiver (core/aead.Receiver)
  Note over S,R: Header = 22 bytes: !BBBBB8sQB (wire, aeadId, sigId, kemId, infoId, epoch(8), seq(8), payloadLen)
  S->>S: Nonce = epoch(8) || seq(11 bytes)
  S->>R: [Header || Ciphertext || Tag]
  R->>R: Validate header, replay window, derive nonce
  R->>R: AES-GCM open; drop on auth fail
```

```mermaid
flowchart LR
  hdr[22-byte Header]
  nce[Nonce: epoch || seq(11)]
  kdf[HKDF-SHA256 -> 2x32B keys]
  aead[AES-256-GCM]
  hdr --> aead
  nce --> aead
  kdf --> aead
```
