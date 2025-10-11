# Data plane: header and nonce


## Sequence notes

- **Sender (core/aead.Sender)** builds headers via `_build_header()`, increments sequence numbers, and seals payloads.
- **Receiver (core/aead.Receiver)** validates header structure, enforces the replay window, and authenticates/decrypts with AES-GCM.
- **AES-GCM open, drop on auth fail** corresponds to raising `AeadAuthError`, surfacing through structured logs.

```mermaid
sequenceDiagram
  participant S as Sender (core/aead.Sender)
  participant R as Receiver (core/aead.Receiver)
  Note over S,R: Header = 22 bytes: !BBBBB8sQB (wire, aeadId, sigId, kemId, infoId, epoch(8), seq(8), payloadLen)
  S->>S: Nonce = epoch (8) and seq (11 bytes)
  S->>R: Header, ciphertext and tag
  R->>R: Validate header, replay window and derive nonce
  R->>R: AES-GCM open, drop on auth fail
```

## Data flow notes

- `22-byte Header`: layout defined in `core/aead.HEADER_STRUCT` containing wire version, suite IDs, epoch, sequence, and payload length.
- `Nonce`: deterministic concatenation of epoch and padded sequence counter (11 bytes) per `Sender._build_nonce()`.
- `HKDF produces two keys`: `core/handshake.derive_transport_keys()` feeding `Sender`/`Receiver` key material.
- `AES-256-GCM`: default AEAD implementation; suite selection may swap in ChaCha20-Poly1305 or ASCON.

```mermaid
flowchart LR
hdr["22-byte Header"]
nce["Nonce epoch and seq 11"]
kdf["HKDF produces two keys"]
aead["AES-256-GCM"]
hdr-->aead
nce-->aead
kdf-->aead
```
