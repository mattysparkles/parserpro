"""ParserPro logging helpers + stdlib logging compatibility shim."""

from __future__ import annotations

import importlib.util
import re
import sysconfig
from datetime import datetime
from pathlib import Path


def _load_stdlib_logging():
    stdlib_dir = Path(sysconfig.get_paths()["stdlib"])
    logging_init = stdlib_dir / "logging" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_parserpro_stdlib_logging", logging_init)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


_STDLIB_LOGGING = _load_stdlib_logging()


for _name in dir(_STDLIB_LOGGING):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_STDLIB_LOGGING, _name))


_APP_ROOT = Path(__file__).resolve().parent
_LOG_DIR = _APP_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_DOMAIN_RE = re.compile(r"https?://[^/]+")


def _log_file(prefix: str) -> Path:
    return _LOG_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d')}.log"


def _sanitize_domains(message: str) -> str:
    return _DOMAIN_RE.sub("genericexamplewebsite.com", message or "")


# FIXED: required helper names and log file format
def write_detailed(message: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _log_file("detailed").open("a", encoding="utf-8") as handle:
        handle.write(f"[{ts}] {level.upper()}: {message}\n")


def write_privacy(message: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_message = _sanitize_domains(message)
    with _log_file("privacy").open("a", encoding="utf-8") as handle:
        handle.write(f"[{ts}] {level.upper()}: {safe_message}\n")


# Backward-compatible aliases.
def write_detailed_log(message: str, level: str = "INFO") -> None:
    write_detailed(message, level)


def write_privacy_log(message: str, level: str = "INFO") -> None:
    write_privacy(message, level)
