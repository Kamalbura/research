import socket, time, sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from core.config import CONFIG

CFG = {**CONFIG}
CFG.update({
    "GCS_PLAINTEXT_TX": 45805,
    "GCS_PLAINTEXT_RX": 45806,
})
GCS_PLAINTEXT_TX = CFG["GCS_PLAINTEXT_TX"]
GCS_PLAINTEXT_RX = CFG["GCS_PLAINTEXT_RX"]
COUNT = 15
SEND = b"Hello from GCS Control Station - Message"
EXPECT = b"Hello from Drone Aircraft - Response"

def recv_loop(expected: bytes, count: int) -> int:
    n = 0
    print(f"[GCS APP] Waiting for first packet... (timeout: 10 seconds)")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as r:
        r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        r.bind(("127.0.0.1", GCS_PLAINTEXT_RX))
        r.settimeout(10.0)  # 10 seconds for first packet
        first_packet_received = False
        
        while n < count:
            try:
                data, _ = r.recvfrom(2048)
                if data == expected:
                    n += 1
                    if not first_packet_received:
                        print(f"[GCS APP] ✓ First packet received! Continuing...")
                        first_packet_received = True
                        r.settimeout(2.0)  # Shorter timeout for remaining packets
                    print(f"[GCS APP] Got packet {n}/{count}: {data.decode()}")
                else:
                    print(f"[GCS APP] Unexpected packet: {data}")
            except socket.timeout:
                if not first_packet_received:
                    print(f"[GCS APP] ✗ No packets received within 10 seconds")
                    break
                else:
                    print(f"[GCS APP] Timeout waiting for more packets")
                    break
    return n

def send_loop(payload: bytes, count: int):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        print(f"[GCS APP] Starting to send {count} packets...")
        for i in range(count):
            s.sendto(payload, ("127.0.0.1", GCS_PLAINTEXT_TX))
            print(f"[GCS APP] Sent packet {i+1}/{count}: {payload.decode()}")
            time.sleep(0.05)  # Slightly slower to be more readable

if __name__ == "__main__":
    print("[GCS APP] Ready. Controls:")
    print("  1 = Send 15 packets to drone")
    print("  2 = Receive 15 packets from drone")
    print("  q = Quit")
    
    while True:
        try:
            cmd = input("[GCS APP] Enter command (1/2/q): ").strip()
            if cmd == '1':
                print(f"[GCS APP] Sending {COUNT} × {SEND!r}")
                send_loop(SEND, COUNT)
                print(f"[GCS APP] Sent {COUNT} packets")
            elif cmd == '2':
                print(f"[GCS APP] Receiving up to {COUNT} × {EXPECT!r}")
                got = recv_loop(EXPECT, COUNT)
                print(f"[GCS APP] Received {got}/{COUNT}")
            elif cmd.lower() == 'q':
                print("[GCS APP] Exiting")
                break
            else:
                print("[GCS APP] Invalid command. Use 1, 2, or q")
        except KeyboardInterrupt:
            print("\n[GCS APP] Interrupted, exiting")
            break
