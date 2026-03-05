from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
import math
import uuid


TLS_CODES = {"tls_mismatch", "cert_invalid"}
DNS_CODES = {"dns_failed", "ERR_NAME_NOT_RESOLVED"}
PROXY_CODES = {"proxy_down", "proxy_failed", "ERR_SOCKS_CONNECTION_FAILED"}
CONN_CLOSED_CODES = {"conn_closed", "ERR_CONNECTION_CLOSED"}


@dataclass
class RunSummary:
    run_id: str
    started_ts: str
    ended_ts: str
    duration_s: float
    mode: str
    notes: str = ""
    sites_total_seen: int = 0
    sites_processed_this_run: int = 0
    sites_skipped_cached: int = 0
    successes_actionable: int = 0
    successes_loginish: int = 0
    no_form: int = 0
    fetch_failed: int = 0
    dns_failed: int = 0
    tls_failed: int = 0
    proxy_failed: int = 0
    conn_closed: int = 0
    other_failed: int = 0
    top_domains_failed: list[tuple[str, int]] | None = None
    top_error_codes: list[tuple[str, int]] | None = None
    avg_fetch_ms: float | None = None
    p95_fetch_ms: float | None = None
    avg_extract_ms: float | None = None
    p95_extract_ms: float | None = None
    environment_snapshot: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["top_domains_failed"] = data.get("top_domains_failed") or []
        data["top_error_codes"] = data.get("top_error_codes") or []
        data["environment_snapshot"] = data.get("environment_snapshot") or {}
        return data


def _iso_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil((p / 100.0) * len(ordered)) - 1))
    return round(float(ordered[idx]), 2)


def from_dict(raw: dict[str, Any]) -> RunSummary:
    data = raw or {}
    return RunSummary(
        run_id=str(data.get("run_id") or uuid.uuid4()),
        started_ts=str(data.get("started_ts") or _iso_now()),
        ended_ts=str(data.get("ended_ts") or _iso_now()),
        duration_s=float(data.get("duration_s") or 0.0),
        mode=str(data.get("mode") or "extraction"),
        notes=str(data.get("notes") or ""),
        sites_total_seen=int(data.get("sites_total_seen") or 0),
        sites_processed_this_run=int(data.get("sites_processed_this_run") or 0),
        sites_skipped_cached=int(data.get("sites_skipped_cached") or 0),
        successes_actionable=int(data.get("successes_actionable") or 0),
        successes_loginish=int(data.get("successes_loginish") or 0),
        no_form=int(data.get("no_form") or 0),
        fetch_failed=int(data.get("fetch_failed") or 0),
        dns_failed=int(data.get("dns_failed") or 0),
        tls_failed=int(data.get("tls_failed") or 0),
        proxy_failed=int(data.get("proxy_failed") or 0),
        conn_closed=int(data.get("conn_closed") or 0),
        other_failed=int(data.get("other_failed") or 0),
        top_domains_failed=[tuple(x) for x in (data.get("top_domains_failed") or [])][:10],
        top_error_codes=[tuple(x) for x in (data.get("top_error_codes") or [])][:10],
        avg_fetch_ms=float(data["avg_fetch_ms"]) if data.get("avg_fetch_ms") is not None else None,
        p95_fetch_ms=float(data["p95_fetch_ms"]) if data.get("p95_fetch_ms") is not None else None,
        avg_extract_ms=float(data["avg_extract_ms"]) if data.get("avg_extract_ms") is not None else None,
        p95_extract_ms=float(data["p95_extract_ms"]) if data.get("p95_extract_ms") is not None else None,
        environment_snapshot=data.get("environment_snapshot") or {},
    )


def compute_run_summary(*, started_ts: str, ended_ts: str, mode: str, notes: str, processed_sites: list[str], sites_total_seen: int, sites_skipped_cached: int, sites_db: dict[str, dict], fetch_ms_values: list[float] | None = None, extract_ms_values: list[float] | None = None, environment_snapshot: dict[str, Any] | None = None, run_id: str | None = None) -> RunSummary:
    actionable = 0
    loginish = 0
    no_form = 0
    fetch_failed = 0
    dns_failed = 0
    tls_failed = 0
    proxy_failed = 0
    conn_closed = 0
    other_failed = 0
    domain_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()

    for site in processed_sites:
        entry = (sites_db or {}).get(site) or {}
        status = str(entry.get("status") or "")
        extracted = entry.get("extracted") or {}
        code = str(entry.get("last_error_code") or "")

        if status == "success_form" and extracted.get("submit_mode") == "native_post" and extracted.get("user_field") and extracted.get("pass_field"):
            actionable += 1
        elif status in {"success_form", "success_loginish"}:
            loginish += 1
        elif status == "no_form":
            no_form += 1
        elif status == "fetch_failed":
            fetch_failed += 1
            error_counts[code or "fetch_failed"] += 1
            domain = urlparse(site).netloc or site
            domain_counts[domain] += 1
            if code in DNS_CODES:
                dns_failed += 1
            elif code in TLS_CODES:
                tls_failed += 1
            elif code in PROXY_CODES:
                proxy_failed += 1
            elif code in CONN_CLOSED_CODES:
                conn_closed += 1
            else:
                other_failed += 1

    fetch_ms = [float(v) for v in (fetch_ms_values or []) if v is not None]
    extract_ms = [float(v) for v in (extract_ms_values or []) if v is not None]

    try:
        started_dt = datetime.fromisoformat(started_ts.replace("Z", "+00:00"))
        ended_dt = datetime.fromisoformat(ended_ts.replace("Z", "+00:00"))
        duration_s = max(0.0, (ended_dt - started_dt).total_seconds())
    except Exception:
        duration_s = 0.0

    return RunSummary(
        run_id=run_id or str(uuid.uuid4()),
        started_ts=started_ts,
        ended_ts=ended_ts,
        duration_s=round(duration_s, 2),
        mode=mode or "extraction",
        notes=notes or "",
        sites_total_seen=int(sites_total_seen or 0),
        sites_processed_this_run=len(processed_sites or []),
        sites_skipped_cached=int(sites_skipped_cached or 0),
        successes_actionable=actionable,
        successes_loginish=loginish,
        no_form=no_form,
        fetch_failed=fetch_failed,
        dns_failed=dns_failed,
        tls_failed=tls_failed,
        proxy_failed=proxy_failed,
        conn_closed=conn_closed,
        other_failed=other_failed,
        top_domains_failed=domain_counts.most_common(10),
        top_error_codes=error_counts.most_common(10),
        avg_fetch_ms=round(sum(fetch_ms) / len(fetch_ms), 2) if fetch_ms else None,
        p95_fetch_ms=_pct(fetch_ms, 95),
        avg_extract_ms=round(sum(extract_ms) / len(extract_ms), 2) if extract_ms else None,
        p95_extract_ms=_pct(extract_ms, 95),
        environment_snapshot=environment_snapshot or {},
    )
