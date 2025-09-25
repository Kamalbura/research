from core.suites import get_suite, header_ids_for_suite
from core.aead import Sender, Receiver, AeadIds
from diagnose_handshake import keys  # reuse from handshake script
from core.config import CONFIG

suite = get_suite("cs-kyber768-aesgcm-dilithium3")
ids = AeadIds(*header_ids_for_suite(suite))

session_id = b'ABCDEFGH'

sender = Sender(CONFIG["WIRE_VERSION"], ids, session_id, 0, keys['client_send'])
receiver = Receiver(CONFIG["WIRE_VERSION"], ids, session_id, 0, keys['server_recv'], CONFIG['REPLAY_WINDOW'])

wire = sender.encrypt(b"hello")
plain = receiver.decrypt(wire)
print("decrypt", plain)
