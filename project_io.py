import csv
import json
import threading
from json import JSONDecodeError
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

PROJECT_SCHEMA_VERSION = 3


def utc_now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(target.parent), suffix=".tmp") as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        temp_name = tmp.name
    Path(temp_name).replace(target)


def build_project_payload(*, project_name, project_path, created_ts, sites_db, filters, sort_state, selection, ui_state, app_settings, timeline_events=None, run_summaries=None):
    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "project_name": project_name or "Untitled",
        "created_ts": created_ts or utc_now_iso(),
        "last_saved_ts": utc_now_iso(),
        "input_sources": {
            "input_path": ui_state.get("input_path", ""),
            "embedded_sites": sorted(list(sites_db.keys())),
        },
        "ui_filters": filters or {},
        "table_sort": sort_state or {},
        "row_selection": selection or [],
        "results": sites_db or {},
        "timeline_events": timeline_events or [],
        "run_summaries": run_summaries or [],
        "ui_state": ui_state or {},
        "session_settings": {
            "ignore_https_errors": bool(app_settings.get("ignore_https_errors", False)),
            "allow_nonstandard_ports": bool(app_settings.get("allow_nonstandard_ports", False)),
            "proxy_url": app_settings.get("proxy_url", ""),
            "autosave_enabled": bool(app_settings.get("autosave_enabled", True)),
            "autosave_interval_minutes": int(app_settings.get("autosave_interval_minutes", 2)),
        },
        "project_path": str(project_path) if project_path else "",
    }


def load_project_payload(payload):
    data = payload or {}
    return {
        "schema_version": int(data.get("schema_version", 1)),
        "project_name": data.get("project_name", "Untitled"),
        "created_ts": data.get("created_ts") or utc_now_iso(),
        "last_saved_ts": data.get("last_saved_ts") or utc_now_iso(),
        "input_sources": data.get("input_sources") or {},
        "ui_filters": data.get("ui_filters") or {},
        "table_sort": data.get("table_sort") or {},
        "row_selection": data.get("row_selection") or [],
        "results": data.get("results") or {},
        "timeline_events": data.get("timeline_events") or [],
        "run_summaries": data.get("run_summaries") or [],
        "ui_state": data.get("ui_state") or {},
        "session_settings": data.get("session_settings") or {},
        "project_path": data.get("project_path", ""),
    }


def _strip_trailing_commas(text):
    out = []
    in_string = False
    escape = False
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
                continue

        out.append(ch)
        i += 1

    return "".join(out)


def parse_project_json(text):
    cleaned = text.lstrip("\ufeff").replace("\x00", "")
    try:
        return json.loads(cleaned), []
    except JSONDecodeError as exc:
        repaired = _strip_trailing_commas(cleaned)
        if repaired != cleaned:
            try:
                payload = json.loads(repaired)
                return payload, [
                    (
                        "Recovered project file by removing trailing commas. "
                        "Please re-save this project to keep a clean JSON format."
                    )
                ]
            except JSONDecodeError:
                pass
        raise exc


def summarize_status_counts(rows):
    out = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        out[status] = out.get(status, 0) + 1
    return out


def site_report_rows(sites_db):
    rows = []
    for site, entry in (sites_db or {}).items():
        extracted = entry.get("extracted") or {}
        rows.append({
            "site_url": site,
            "status": entry.get("status", "pending"),
            "confidence": extracted.get("confidence"),
            "action_url": extracted.get("action_url"),
            "method": extracted.get("method"),
            "user_field": extracted.get("user_field"),
            "pass_field": extracted.get("pass_field"),
            "submit_mode": extracted.get("submit_mode", "unknown"),
            "last_checked_ts": entry.get("last_checked_ts"),
            "error_code": entry.get("last_error_code"),
            "error_hint": entry.get("last_error_hint"),
        })
    return rows


def export_rows_json(path, *, project_meta, rows, summary, timeline_events=None, run_summaries=None):
    payload = {"project": project_meta, "summary": summary, "entries": rows}
    if timeline_events is not None:
        payload["timeline_events"] = timeline_events
    if run_summaries is not None:
        payload["run_summaries"] = run_summaries
    atomic_write_json(path, payload)


def export_timeline_csv(path, events):
    fieldnames = ["event_id", "ts", "level", "category", "action", "message", "metrics"]
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for event in events or []:
            row = dict(event)
            row["metrics"] = json.dumps(row.get("metrics") or {}, ensure_ascii=False)
            writer.writerow(row)


def export_rows_csv(path, rows):
    fieldnames = ["site_url", "status", "confidence", "action_url", "method", "user_field", "pass_field", "submit_mode", "last_checked_ts", "error_code", "error_hint"]
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)


def export_run_summaries_csv(path, run_summaries):
    fieldnames = [
        "run_id",
        "started_ts",
        "ended_ts",
        "duration_s",
        "mode",
        "notes",
        "sites_total_seen",
        "sites_processed_this_run",
        "sites_skipped_cached",
        "successes_actionable",
        "successes_loginish",
        "no_form",
        "fetch_failed",
        "dns_failed",
        "tls_failed",
        "proxy_failed",
        "conn_closed",
        "other_failed",
        "top_error_code",
        "top_error_count",
        "top_domain_failed",
        "top_domain_count",
        "avg_fetch_ms",
        "p95_fetch_ms",
        "avg_extract_ms",
        "p95_extract_ms",
    ]
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for summary in run_summaries or []:
            top_error = (summary.get("top_error_codes") or [("", "")])[0]
            top_domain = (summary.get("top_domains_failed") or [("", "")])[0]
            writer.writerow({
                **{k: summary.get(k) for k in fieldnames if k not in {"top_error_code", "top_error_count", "top_domain_failed", "top_domain_count"}},
                "top_error_code": top_error[0],
                "top_error_count": top_error[1],
                "top_domain_failed": top_domain[0],
                "top_domain_count": top_domain[1],
            })


DIAG_CATEGORIES = {
    "DNS failures": {"dns_failed", "ERR_NAME_NOT_RESOLVED"},
    "TLS failures": {"tls_mismatch", "cert_invalid"},
    "Proxy failures": {"proxy_down", "ERR_SOCKS_CONNECTION_FAILED"},
    "Connection closed": {"conn_closed", "ERR_CONNECTION_CLOSED"},
}


def diagnostics_summary(sites_db):
    result = {k: [] for k in DIAG_CATEGORIES}
    result["Other fetch failures"] = []
    for site, entry in (sites_db or {}).items():
        if entry.get("status") != "fetch_failed":
            continue
        code = str(entry.get("last_error_code") or "")
        placed = False
        for cat, codes in DIAG_CATEGORIES.items():
            if code in codes:
                result[cat].append((site, entry))
                placed = True
                break
        if not placed:
            result["Other fetch failures"].append((site, entry))
    return result


def top_failing_domains(entries, limit=10):
    counts = {}
    for site, _entry in entries:
        domain = urlparse(site).netloc or site
        counts[domain] = counts.get(domain, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]


class AutosaveWorker:
    def __init__(self, save_fn):
        self.save_fn = save_fn
        self.q = []
        self.cv = threading.Condition()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self):
        with self.cv:
            self.q.append(1)
            self.cv.notify()

    def _run(self):
        while self.running:
            with self.cv:
                if not self.q:
                    self.cv.wait(timeout=1)
                if not self.running:
                    return
                self.q.clear()
            self.save_fn()

    def stop(self):
        with self.cv:
            self.running = False
            self.cv.notify_all()
