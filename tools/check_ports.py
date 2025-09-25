# tools/check_ports.py
"""
A utility to check if the network ports required by the PQC proxy
are available on localhost. Supports both default and manual_4term port profiles.
"""
import argparse
import os
import socket
import sys

# Add the project root to the Python path to allow importing the 'core' module
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

try:
    from core.config import CONFIG as BASE_CONFIG
except ImportError:
    print("❌ Error: Could not import CONFIG from core/config.py.")
    print("   Please run this script from the project's root directory.")
    sys.exit(1)

# Manual 4-terminal testing port configuration
MANUAL_4TERM_CONFIG = {
    "TCP_HANDSHAKE_PORT": 45800,
    "UDP_DRONE_RX": 45801,
    "UDP_GCS_RX": 45802,
    "DRONE_PLAINTEXT_TX": 45803,
    "DRONE_PLAINTEXT_RX": 45804,
    "GCS_PLAINTEXT_TX": 45805,
    "GCS_PLAINTEXT_RX": 45806,
    "WIRE_VERSION": 1,
}

def check_bind(addr: str, proto: str, port: int) -> bool:
    """Attempts to bind to a port to check its availability. Returns True if available."""
    socket_type = socket.SOCK_STREAM if proto == "TCP" else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, socket_type) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((addr, port))
            return True
        except OSError:
            return False

def main():
    parser = argparse.ArgumentParser(description="Check PQC proxy port availability")
    parser.add_argument("--profile", choices=["default", "manual4term"], default="default",
                        help="Which port profile to check (default: default)")
    parser.add_argument("--include-app-ports", action="store_true",
                        help="Also check the app listener ports (Plaintext RX)")
    args = parser.parse_args()

    # Select configuration based on profile
    config = dict(BASE_CONFIG) if args.profile == "default" else MANUAL_4TERM_CONFIG

    print(f"--- Checking ports on 127.0.0.1 for profile: {args.profile} ---")

    # Define ports to check with their bind addresses
    port_checks = [
        ("TCP", config["TCP_HANDSHAKE_PORT"], "GCS Handshake Listener", "0.0.0.0"),
        ("UDP", config["UDP_DRONE_RX"],       "Drone Encrypted Ingress", "0.0.0.0"),
        ("UDP", config["UDP_GCS_RX"],         "GCS Encrypted Ingress",   "0.0.0.0"),
        ("UDP", config["DRONE_PLAINTEXT_TX"], "Drone Plaintext Ingress", "127.0.0.1"),
        ("UDP", config["GCS_PLAINTEXT_TX"],   "GCS Plaintext Ingress",   "127.0.0.1"),
    ]
    
    if args.include_app_ports:
        port_checks += [
            ("UDP", config["DRONE_PLAINTEXT_RX"], "Drone Plaintext RX (app listener)", "127.0.0.1"),
            ("UDP", config["GCS_PLAINTEXT_RX"],   "GCS Plaintext RX (app listener)",   "127.0.0.1"),
        ]

    all_available = True
    for proto, port, label, addr in port_checks:
        is_available = check_bind(addr, proto, port)
        if is_available:
            status = "✅ Available"
        else:
            status = "❌ IN USE"
            all_available = False
            
        print(f"{proto:4} {port:<5} {label:<40} {addr:<9} : {status}")

    print("-" * 70)
    
    if all_available:
        print("✅ All required ports are available.")
        sys.exit(0)
    else:
        print("❌ One or more required ports are in use. Please close the conflicting application.")
        sys.exit(1)

if __name__ == "__main__":
    main()