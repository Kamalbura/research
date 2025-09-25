# tools/encrypted_sniffer.py
"""
A simple UDP sniffer to verify that encrypted packets are being sent
by the proxies. Listens on a specified port and prints details of
any received datagrams.
"""
import socket
import sys
import time

def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <port_to_listen_on>")
        sys.exit(1)

    try:
        listen_port = int(sys.argv[1])
    except ValueError:
        print(f"Error: Invalid port '{sys.argv[1]}'. Please provide a number.")
        sys.exit(1)

    print(f"--- 🕵️ Encrypted Packet Sniffer ---")
    print(f"Listening for UDP packets on 0.0.0.0:{listen_port}...")
    print("Press Ctrl+C to stop.")

    count = 0
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(('0.0.0.0', listen_port))
            while True:
                data, addr = s.recvfrom(2048)
                count += 1
                timestamp = time.strftime("%H:%M:%S")
                print(
                    f"[{timestamp}] Packet #{count}: Received {len(data)} bytes from {addr[0]}:{addr[1]}"
                    f" | Data (hex): {data[:16].hex()}..."
                )
    except OSError as e:
        print(f"\n❌ Error binding to port {listen_port}: {e}")
        print("   Is another application already using this port?")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\nSniffer stopped. Received a total of {count} packets.")
        sys.exit(0)

if __name__ == "__main__":
    main()