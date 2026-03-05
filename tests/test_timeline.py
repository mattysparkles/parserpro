from collections import defaultdict

from gui import CombinedParserGUI
from project_io import build_project_payload, load_project_payload


def test_record_event_adds_event():
    gui = CombinedParserGUI.__new__(CombinedParserGUI)
    gui.timeline_events = []
    gui.timeline_coalesce = defaultdict(list)
    gui.timeline_coalesce_last_summary = {}
    gui.refresh_timeline_view = lambda: None

    CombinedParserGUI.record_event(gui, "INFO", "run", "start", "Extraction run started", {"sites": 3})

    assert len(gui.timeline_events) == 1
    event = gui.timeline_events[0]
    assert event["category"] == "run"
    assert event["action"] == "start"
    assert event["metrics"]["sites"] == 3


def test_project_roundtrip_preserves_timeline_fields():
    events = [
        {
            "event_id": "evt-1",
            "ts": "2026-02-02T02:02:02Z",
            "level": "WARN",
            "category": "dns",
            "action": "failure",
            "message": "dns_failed x2 in last 5m",
            "metrics": {"count": 2},
        }
    ]
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
        timeline_events=events,
    )
    loaded = load_project_payload(payload)

    assert len(loaded["timeline_events"]) == 1
    assert loaded["timeline_events"][0]["event_id"] == "evt-1"
    assert loaded["timeline_events"][0]["metrics"]["count"] == 2
