from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import uuid


LEVELS = {"INFO", "WARN", "ERROR"}


@dataclass
class TimelineEvent:
    event_id: str
    ts: str
    level: str
    category: str
    action: str
    message: str
    metrics: dict | None = None

    def to_dict(self):
        data = asdict(self)
        if self.metrics is None:
            data["metrics"] = {}
        return data



def utc_now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def normalize_event(raw: dict) -> dict:
    event_id = str(raw.get("event_id") or uuid.uuid4())
    ts = str(raw.get("ts") or utc_now_iso())
    level = str(raw.get("level") or "INFO").upper()
    if level not in LEVELS:
        level = "INFO"
    category = str(raw.get("category") or "ui")
    action = str(raw.get("action") or "update")
    message = str(raw.get("message") or "")
    metrics = raw.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    return TimelineEvent(event_id, ts, level, category, action, message, metrics).to_dict()


def make_event(level: str, category: str, action: str, message: str, metrics: dict | None = None) -> dict:
    return normalize_event(
        {
            "event_id": str(uuid.uuid4()),
            "ts": utc_now_iso(),
            "level": (level or "INFO").upper(),
            "category": category,
            "action": action,
            "message": message,
            "metrics": metrics or {},
        }
    )


def parse_ts(value: str):
    text = str(value or "")
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        return None


def in_time_window(event_ts: str, window_key: str) -> bool:
    if window_key == "All":
        return True
    dt = parse_ts(event_ts)
    if not dt:
        return True
    now = datetime.now(dt.tzinfo)
    if window_key == "Last 10m":
        return dt >= now - timedelta(minutes=10)
    if window_key == "Last hour":
        return dt >= now - timedelta(hours=1)
    if window_key == "Today":
        return dt.date() == now.date()
    return True
