from project_io import build_project_payload, load_project_payload
from run_summary import compute_run_summary, from_dict


def test_compute_run_summary_counts():
    sites_db = {
        "https://a.example": {"status": "success_form", "extracted": {"submit_mode": "native_post", "user_field": "u", "pass_field": "p"}},
        "https://b.example": {"status": "success_loginish", "extracted": {"submit_mode": "js_handled"}},
        "https://c.example": {"status": "no_form"},
        "https://d.example": {"status": "fetch_failed", "last_error_code": "dns_failed"},
        "https://e.example": {"status": "fetch_failed", "last_error_code": "cert_invalid"},
        "https://f.example": {"status": "fetch_failed", "last_error_code": "proxy_down"},
        "https://g.example": {"status": "fetch_failed", "last_error_code": "conn_closed"},
        "https://h.example": {"status": "fetch_failed", "last_error_code": "weird_error"},
    }
    summary = compute_run_summary(
        run_id="run-1",
        started_ts="2026-01-01T00:00:00Z",
        ended_ts="2026-01-01T00:01:00Z",
        mode="extraction",
        notes="",
        processed_sites=list(sites_db.keys()),
        sites_total_seen=12,
        sites_skipped_cached=4,
        sites_db=sites_db,
        fetch_ms_values=[100, 200, 300],
    )

    assert summary.duration_s == 60.0
    assert summary.successes_actionable == 1
    assert summary.successes_loginish == 1
    assert summary.no_form == 1
    assert summary.fetch_failed == 5
    assert summary.dns_failed == 1
    assert summary.tls_failed == 1
    assert summary.proxy_failed == 1
    assert summary.conn_closed == 1
    assert summary.other_failed == 1


def test_project_roundtrip_preserves_run_summaries():
    summary = compute_run_summary(
        run_id="run-2",
        started_ts="2026-01-01T00:00:00Z",
        ended_ts="2026-01-01T00:00:30Z",
        mode="extraction",
        notes="",
        processed_sites=[],
        sites_total_seen=0,
        sites_skipped_cached=0,
        sites_db={},
    )
    payload = build_project_payload(
        project_name="Demo",
        project_path="demo.pproj",
        created_ts="2026-01-01T00:00:00Z",
        sites_db={},
        filters={},
        sort_state={},
        selection=[],
        ui_state={},
        app_settings={},
        timeline_events=[],
        run_summaries=[summary.to_dict()],
    )

    loaded = load_project_payload(payload)
    loaded_summaries = [from_dict(item) for item in loaded["run_summaries"]]
    assert len(loaded_summaries) == 1
    assert loaded_summaries[0].run_id == "run-2"
