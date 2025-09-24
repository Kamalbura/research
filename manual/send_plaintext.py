"""
Manual script: Send plaintext UDP packet to proxy.
Run in terminal 3.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import socket
import time
from core.config import CONFIG

if __name__ == "__main__":
    # Send to Drone proxy
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"Hello from manual sender", ("127.0.0.1", CONFIG["DRONE_PLAINTEXT_TX"]))
    print("Sent to DRONE_PLAINTEXT_TX:", CONFIG["DRONE_PLAINTEXT_TX"])
    time.sleep(0.5)
    # Send to GCS proxy
    s.sendto(b"Hello from manual sender", ("127.0.0.1", CONFIG["GCS_PLAINTEXT_TX"]))
    print("Sent to GCS_PLAINTEXT_TX:", CONFIG["GCS_PLAINTEXT_TX"])
    s.close()
