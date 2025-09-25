"""
Minimal PolicyEngine stub for control-plane messages.

Control packet format (first byte already stripped by async_proxy):
    0x02 | cmd_id(1) | payload...
For now we just log and ack-by-dropping. Later this will drive rekey/scheduler knobs.
"""
from typing import Optional

def handle_control(buf: bytes) -> Optional[bytes]:
    """
    Handle control-plane message and return optional response packet.
    
    Args:
        buf: Control payload with type byte (0x02) already stripped by caller
        
    Returns:
        Optional response packet to send back, or None for no response
    """
    if not buf:
        return None
    # First byte was 0x02 (packet type), async_proxy passes the rest in `buf`.
    # Reserve cmd_id for future expansion.
    cmd_id = buf[0] if len(buf) >= 1 else 0
    # TODO: add dispatch on cmd_id (e.g., 0x01=rekey, 0x02=set-DSCP, 0x10=telemetry request).
    # For now, do nothing and return no response.
    return None