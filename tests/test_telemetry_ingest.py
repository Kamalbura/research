import json
from pathlib import Path

from tools.auto.simulate_session import write_trace
from tools.auto.telemetry_ingest import process_input_file
from tools.auto.consolidate_results import consolidate_session


def test_telemetry_ingest_roundtrip(tmp_path):
    # prepare simulated trace
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    trace_file = data_dir / "sim_trace.ldjson"
    write_trace(trace_file, session_id="sim-test-1", events=12)

    # ensure output base overridden to tmp_path/output
    # monkeypatch module-level OUT_BASE
    from importlib import reload

    import tools.auto.telemetry_ingest as ingest_mod
    import tools.auto.consolidate_results as cons_mod

    ingest_mod.OUT_BASE = tmp_path / "output" / "gcs"
    cons_mod.OUT_BASE = tmp_path / "output" / "gcs"

    # run ingestion
    process_input_file(trace_file)

    session_dir = ingest_mod.OUT_BASE / "sim-test-1"
    assert session_dir.exists()
    # check telemetry_events.csv exists and has lines
    ev = session_dir / "telemetry_events.csv"
    assert ev.exists()
    content = ev.read_text(encoding="utf-8")
    assert "kind" in content

    # check one of the flattened CSVs exists
    ps = session_dir / "power_summaries.csv"
    assert ps.exists()

    # run consolidation
    manifest = consolidate_session("sim-test-1")
    assert "telemetry_events.csv" in manifest["files"]
