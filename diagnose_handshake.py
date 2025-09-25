import threading
import socket
from core.suites import get_suite
from core.handshake import server_gcs_handshake, client_drone_handshake
from oqs.oqs import Signature
from core.config import CONFIG

suite = get_suite("cs-kyber768-aesgcm-dilithium3")

keys = {}
errors = {}

ready = threading.Event()

def server_thread():
    sig = Signature(suite["sig_name"])
    pub = sig.generate_keypair()
    keys['pub'] = pub
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(('127.0.0.1', CONFIG['TCP_HANDSHAKE_PORT']))
    srv.listen(1)
    ready.set()
    conn, addr = srv.accept()
    with conn:
        k_recv, k_send, *_ = server_gcs_handshake(conn, suite, sig)
        keys['server_recv'] = k_recv
        keys['server_send'] = k_send
    srv.close()


def client_thread():
    if not ready.wait(timeout=3):
        errors['client'] = 'timeout'
        return
    pub = keys['pub']
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(('127.0.0.1', CONFIG['TCP_HANDSHAKE_PORT']))
    k_send, k_recv, *_ = client_drone_handshake(sock, suite, pub)
    keys['client_send'] = k_send
    keys['client_recv'] = k_recv
    sock.close()

threads = [threading.Thread(target=server_thread), threading.Thread(target=client_thread)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print('errors', errors)
for name, value in keys.items():
    if isinstance(value, bytes):
        print(name, len(value), value[:8].hex())
    else:
        print(name, type(value))
