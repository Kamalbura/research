import socket
import threading
import time
import argparse
import sys
from pathlib import Path

# Ensure repository root is on sys.path when executed directly
_HERE = Path(__file__).resolve()
# Prefer the ancestor that contains a 'core' directory
for parent in (_HERE.parent.parent, _HERE.parent):
    try:
        if (parent / "core").exists():
            parent_str = str(parent)
            if parent_str not in sys.path:
                sys.path.insert(0, parent_str)
            break
    except Exception:
        # Best-effort; fall through
        pass

from core.config import CONFIG  # Defines hosts and all plaintext/handshake ports

def _sendto(sock: socket.socket, host: str, port: int, text: str) -> None:
    sock.sendto(text.encode('utf-8'), (host, port))


def run_udp_test(role, local_ip, remote_ip, local_rx_port, remote_tx_port):
    """
    Sets up a UDP listener and sender to test direct plaintext communication.
    :param role: "GCS" or "DRONE"
    :param local_ip: The IP address this machine should bind its receiver to (usually "0.0.0.0")
    :param remote_ip: The IP address of the remote machine to send messages to
    :param local_rx_port: The port this machine listens on
    :param remote_tx_port: The port the remote machine is listening on (which we send to)
    """
    print(f"\n--- {role} Plaintext UDP Test ---")
    print(f"  Listening on: {local_ip}:{local_rx_port}")
    print(f"  Sending to:   {remote_ip}:{remote_tx_port}")
    print(f"  Type a message and press Enter to send. Ctrl+C to exit.")

    # Setup receiver socket
    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.bind((local_ip, local_rx_port))
    rx_sock.setblocking(False) # Non-blocking for concurrent read/write

    # Setup sender socket
    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Thread for receiving messages
    def receiver():
        while True:
            try:
                data, addr = rx_sock.recvfrom(65535)
                msg = data.decode('utf-8', errors='ignore').strip()
                print(f"\n[{time.strftime('%H:%M:%S')}] Received from {addr[0]}:{addr[1]}: {msg}")
                print(f"Type message: ", end='', flush=True) # Prompt again after receiving
            except BlockingIOError:
                time.sleep(0.01) # Small delay to prevent busy-waiting
            except Exception as e:
                print(f"Error in receiver: {e}")
                break

    # Start receiver thread
    receiver_thread = threading.Thread(target=receiver, daemon=True)
    receiver_thread.start()

    # Main thread for sending messages
    try:
        while True:
            message = input(f"Type message: ")
            if message.lower() == 'exit':
                break
            tx_sock.sendto(message.encode('utf-8'), (remote_ip, remote_tx_port))
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        rx_sock.close()
        tx_sock.close()


def run_auto(role: str, *, verbose: bool = False, delay: float = 0.05) -> int:
    """Automatic cross-direction UDP smoke test using CONFIG hosts/ports.

    Flow:
      - Drone listens on DRONE_PLAINTEXT_RX; GCS listens on GCS_PLAINTEXT_RX.
      - Drone sends trigger "HELLO_FROM_DRONE" to GCS_PLAINTEXT_RX.
      - GCS replies "HELLO_FROM_GCS" to DRONE_PLAINTEXT_RX.
      - Each side then sends 5 numbered messages to the other's RX port.
      - Returns 0 on success; non-zero on failure.
    """

    gcs_host = CONFIG.get("GCS_HOST", "127.0.0.1")
    drone_host = CONFIG.get("DRONE_HOST", "127.0.0.1")
    gcs_rx = int(CONFIG["GCS_PLAINTEXT_RX"])  # local RX for GCS
    drone_rx = int(CONFIG["DRONE_PLAINTEXT_RX"])  # local RX for Drone

    # Sockets
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    if role == "gcs":
        rx.bind(("0.0.0.0", gcs_rx))
        peer_host, peer_port = drone_host, drone_rx
    else:
        rx.bind(("0.0.0.0", drone_rx))
        peer_host, peer_port = gcs_host, gcs_rx

    rx.setblocking(False)

    received = []
    stop = False

    def recv_loop():
        nonlocal stop
        while not stop:
            try:
                data, addr = rx.recvfrom(65535)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except Exception as e:
                if verbose:
                    print(f"recv error: {e}")
                break
            received.append((time.time(), addr, data))
            if verbose:
                print(f"RX {addr}: {data[:64]!r}")

    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    try:
        if role == "drone":
            _sendto(tx, gcs_host, gcs_rx, "HELLO_FROM_DRONE")
        # Wait briefly for trigger; then GCS replies
        deadline = time.time() + 2.0
        replied = False
        while time.time() < deadline:
            if any(b"HELLO_FROM_DRONE" in it[2] for it in received) and role == "gcs":
                _sendto(tx, drone_host, drone_rx, "HELLO_FROM_GCS")
                replied = True
                break
            if any(b"HELLO_FROM_GCS" in it[2] for it in received) and role == "drone":
                replied = True
                break
            time.sleep(0.01)

        if not replied:
            print("Timeout waiting for trigger exchange")
            return 2

        # Now fire 5 numbered messages from both sides
        for i in range(1, 6):
            if role == "gcs":
                _sendto(tx, drone_host, drone_rx, f"GCS_MSG_{i}")
            else:
                _sendto(tx, gcs_host, gcs_rx, f"DRONE_MSG_{i}")
            time.sleep(delay)

        # Allow receive
        time.sleep(0.5)

        # Basic assertions
        rx_texts = [pkt[2].decode("utf-8", errors="ignore") for pkt in received]
        if role == "gcs":
            ok = any("HELLO_FROM_DRONE" in s for s in rx_texts) and sum(1 for s in rx_texts if s.startswith("DRONE_MSG_")) >= 1
        else:
            ok = any("HELLO_FROM_GCS" in s for s in rx_texts) and sum(1 for s in rx_texts if s.startswith("GCS_MSG_")) >= 1
        print(f"Auto test {'PASSED' if ok else 'FAILED'}; received {len(received)} datagrams")
        return 0 if ok else 1
    finally:
        stop = True
        t.join(timeout=0.2)
        try:
            rx.close()
            tx.close()
        except Exception:
            pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test direct UDP plaintext communication between GCS and Drone.")
    parser.add_argument("--role", choices=["gcs", "drone"], required=True, help="Specify if this is the 'gcs' or 'drone' side.")
    parser.add_argument("--local_ip", default="0.0.0.0", help="Local IP to bind the receiver socket to.")
    parser.add_argument("--remote_gcs_ip", help="IP of the GCS machine (required for drone role).")
    parser.add_argument("--remote_drone_ip", help="IP of the Drone machine (required for gcs role).")
    parser.add_argument("--auto", action="store_true", help="Run automatic cross-direction smoke test using CONFIG hosts/ports.")
    args = parser.parse_args()

    if args.auto:
        rc = run_auto(args.role)
        raise SystemExit(rc)

    if args.role == "gcs":
        if not args.remote_drone_ip:
            parser.error("--remote_drone_ip is required for 'gcs' role.")
        run_udp_test(
            role="GCS",
            local_ip=args.local_ip,
            remote_ip=args.remote_drone_ip,
            local_rx_port=CONFIG["GCS_PLAINTEXT_RX"],
            remote_tx_port=CONFIG["DRONE_PLAINTEXT_RX"]
        )
    elif args.role == "drone":
        if not args.remote_gcs_ip:
            parser.error("--remote_gcs_ip is required for 'drone' role.")
        run_udp_test(
            role="DRONE",
            local_ip=args.local_ip,
            remote_ip=args.remote_gcs_ip,
            local_rx_port=CONFIG["DRONE_PLAINTEXT_RX"],
            remote_tx_port=CONFIG["GCS_PLAINTEXT_RX"]
        )