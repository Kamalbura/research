import socket
import threading
import time
import argparse
from core.config import CONFIG # Assuming CONFIG defines GCS_PLAINTEXT_TX/RX and DRONE_PLAINTEXT_TX/RX

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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test direct UDP plaintext communication between GCS and Drone.")
    parser.add_argument("--role", choices=["gcs", "drone"], required=True, help="Specify if this is the 'gcs' or 'drone' side.")
    parser.add_argument("--local_ip", default="0.0.0.0", help="Local IP to bind the receiver socket to.")
    parser.add_argument("--remote_gcs_ip", help="IP of the GCS machine (required for drone role).")
    parser.add_argument("--remote_drone_ip", help="IP of the Drone machine (required for gcs role).")
    args = parser.parse_args()

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