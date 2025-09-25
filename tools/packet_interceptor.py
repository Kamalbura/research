# tools/packet_interceptor.py
"""
A packet interceptor that sits between proxy components to monitor encrypted traffic.
This acts as a transparent UDP forwarder that logs all packets passing through.
"""
import socket
import sys
import time
import threading

def main():
    if len(sys.argv) != 4:
        print(f"Usage: python {sys.argv[0]} <listen_port> <forward_to_host> <forward_to_port>")
        print("Example: python packet_interceptor.py 45899 127.0.0.1 45801")
        print("  This listens on 45899 and forwards everything to 127.0.0.1:45801")
        sys.exit(1)

    try:
        listen_port = int(sys.argv[1])
        forward_host = sys.argv[2]
        forward_port = int(sys.argv[3])
    except ValueError as e:
        print(f"Error: Invalid arguments: {e}")
        sys.exit(1)

    print(f"--- 🔍 Packet Interceptor ---")
    print(f"Listening on 0.0.0.0:{listen_port}")
    print(f"Forwarding all traffic to {forward_host}:{forward_port}")
    print("Press Ctrl+C to stop.")
    print()

    packet_count = 0

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(('0.0.0.0', listen_port))
            
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as forwarder:
                while True:
                    data, addr = listener.recvfrom(2048)
                    packet_count += 1
                    timestamp = time.strftime("%H:%M:%S")
                    
                    print(f"[{timestamp}] INTERCEPTED Packet #{packet_count}:")
                    print(f"  From: {addr[0]}:{addr[1]}")
                    print(f"  Size: {len(data)} bytes")
                    print(f"  Data (hex): {data[:32].hex()}...")
                    print(f"  Forwarding to {forward_host}:{forward_port}")
                    
                    # Forward the packet
                    try:
                        forwarder.sendto(data, (forward_host, forward_port))
                        print(f"  ✅ Forwarded successfully")
                    except Exception as e:
                        print(f"  ❌ Forward failed: {e}")
                    print()

    except OSError as e:
        print(f"\n❌ Error binding to port {listen_port}: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\nInterceptor stopped. Processed {packet_count} packets.")
        sys.exit(0)

if __name__ == "__main__":
    main()