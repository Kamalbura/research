"""
Manual script: Receive plaintext UDP packet from proxy.
Run in terminal 4.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import socket
from core.config import CONFIG

if __name__ == "__main__":
    # Listen on Drone RX
    s_drone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_drone.bind(("127.0.0.1", CONFIG["DRONE_PLAINTEXT_RX"]))
    s_drone.settimeout(5.0)
    try:
        data, addr = s_drone.recvfrom(2048)
        print("Received at DRONE_PLAINTEXT_RX:", data)
    except Exception as e:
        print("No packet at DRONE_PLAINTEXT_RX:", e)
    s_drone.close()

    # Listen on GCS RX
    s_gcs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s_gcs.bind(("127.0.0.1", CONFIG["GCS_PLAINTEXT_RX"]))
    s_gcs.settimeout(5.0)
    try:
        data, addr = s_gcs.recvfrom(2048)
        print("Received at GCS_PLAINTEXT_RX:", data)
    except Exception as e:
        print("No packet at GCS_PLAINTEXT_RX:", e)
    s_gcs.close()
