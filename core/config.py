"""
Core configuration constants for PQC drone-GCS secure proxy.
"""

CONFIG = {
    "TCP_HANDSHAKE_PORT": 5800,
    "UDP_DRONE_RX": 5810,   # drone receives encrypted from GCS here
    "UDP_GCS_RX": 5811,     # gcs receives encrypted from drone here
    
    # Plaintext (local app / flight controller) ports
    "DRONE_PLAINTEXT_TX": 14550,  # drone -> app sends plaintext to proxy here (will be encrypted out)
    "DRONE_PLAINTEXT_RX": 14551,  # drone <- app receives decrypted here  
    "GCS_PLAINTEXT_TX": 14551,    # gcs -> app sends plaintext to proxy here
    "GCS_PLAINTEXT_RX": 14550,    # gcs <- app receives decrypted here
    
    # Hosts (loopback acceptable in tests)
    "DRONE_HOST": "127.0.0.1",
    "GCS_HOST": "127.0.0.1",
    
    # Crypto runtime parameters
    "REPLAY_WINDOW": 1024,
    "REKEY_SECONDS": 600,
}