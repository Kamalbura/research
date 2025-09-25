import queue

from core.policy_engine import (
    create_control_state,
    handle_control,
    record_rekey_result,
    request_prepare,
)


def _drain_outbox(state):
    items = []
    while True:
        try:
            items.append(state.outbox.get_nowait())
        except queue.Empty:
            break
    return items


def test_gcs_prepare_commit_success():
    state = create_control_state("gcs", "cs-kyber768-aesgcm-dilithium3")
    rid = request_prepare(state, "cs-kyber512-aesgcm-dilithium2")
    queued = _drain_outbox(state)
    assert queued and queued[0]["type"] == "prepare_rekey"
    assert state.state == "NEGOTIATING"

    result = handle_control({"type": "prepare_ok", "rid": rid, "t_ms": 123}, "gcs", state)
    assert result.start_handshake == ("cs-kyber512-aesgcm-dilithium2", rid)
    assert result.send and result.send[0]["type"] == "commit_rekey"
    assert state.state == "SWAPPING"

    record_rekey_result(state, rid, "cs-kyber512-aesgcm-dilithium2", success=True)
    status = _drain_outbox(state)
    assert any(msg["type"] == "status" and msg["result"] == "ok" for msg in status)
    assert state.state == "RUNNING"
    assert state.stats["rekeys_ok"] == 1
    assert state.current_suite == "cs-kyber512-aesgcm-dilithium2"


def test_gcs_prepare_fail_resets_state():
    state = create_control_state("gcs", "cs-kyber768-aesgcm-dilithium3")
    rid = request_prepare(state, "cs-kyber512-aesgcm-dilithium2")
    _ = _drain_outbox(state)
    result = handle_control({"type": "prepare_fail", "rid": rid, "reason": "unsafe", "t_ms": 10}, "gcs", state)
    assert not result.send
    assert state.state == "RUNNING"
    assert state.stats["rekeys_fail"] == 1


def test_drone_prepare_and_commit_flow():
    state = create_control_state("drone", "cs-kyber768-aesgcm-dilithium3")
    msg = {"type": "prepare_rekey", "suite": "cs-kyber512-aesgcm-dilithium2", "rid": "abcd", "t_ms": 50}
    result = handle_control(msg, "drone", state)
    assert result.send and result.send[0]["type"] == "prepare_ok"
    assert state.state == "NEGOTIATING"

    commit = {"type": "commit_rekey", "rid": "abcd", "t_ms": 60}
    result2 = handle_control(commit, "drone", state)
    assert result2.start_handshake == ("cs-kyber512-aesgcm-dilithium2", "abcd")
    assert state.state == "SWAPPING"

    record_rekey_result(state, "abcd", "cs-kyber512-aesgcm-dilithium2", success=True)
    status = _drain_outbox(state)
    assert any(msg["type"] == "status" and msg["result"] == "ok" for msg in status)
    assert state.state == "RUNNING"
    assert state.current_suite == "cs-kyber512-aesgcm-dilithium2"


def test_drone_prepare_fail_when_guard_blocks():
    state = create_control_state("drone", "cs-kyber768-aesgcm-dilithium3", safe_guard=lambda: False)
    msg = {"type": "prepare_rekey", "suite": "cs-kyber512-aesgcm-dilithium2", "rid": "ffff", "t_ms": 5}
    result = handle_control(msg, "drone", state)
    assert result.send and result.send[0]["type"] == "prepare_fail"
    assert state.state == "RUNNING"
