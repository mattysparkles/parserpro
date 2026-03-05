import json
from pathlib import Path

from project_io import atomic_write_json, build_project_payload, load_project_payload


def test_project_roundtrip(tmp_path):
    project_file = tmp_path / "sample.pproj"
    payload = build_project_payload(
        project_name="Demo",
        project_path=project_file,
        created_ts="2026-01-01T00:00:00Z",
        sites_db={"https://example.com": {"status": "fetch_failed", "last_error_code": "dns_failed"}},
        filters={"min_combos": "2", "status": "Failed"},
        sort_state={"column": "Status", "reverse": True},
        selection=["https://example.com"],
        ui_state={"input_path": "sites.txt"},
        app_settings={"autosave_enabled": True, "autosave_interval_minutes": 2},
    )
    atomic_write_json(project_file, payload)

    loaded = load_project_payload(json.loads(Path(project_file).read_text(encoding="utf-8")))
    assert loaded["schema_version"] == 1
    assert loaded["project_name"] == "Demo"
    assert loaded["ui_filters"]["status"] == "Failed"
    assert "https://example.com" in loaded["results"]
