from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from app_logging import logger

DEFAULT_TOR_BROWSER_RELATIVE = Path("Desktop") / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe"
TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9050

_TOR_PROCESS: subprocess.Popen | None = None


def _candidate_paths() -> list[str]:
    user_profile = Path(os.environ.get("USERPROFILE", ""))
    candidates: list[str] = []
    if user_profile:
        candidates.append(str((user_profile / DEFAULT_TOR_BROWSER_RELATIVE).resolve()))
        candidates.append(str((user_profile / "Downloads" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe").resolve()))
    candidates.extend(
        [
            shutil.which("tor") or "",
            shutil.which("tor.exe") or "",
            r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
            r"C:\Program Files (x86)\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
            r"C:\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
        ]
    )
    unique = []
    seen = set()
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        unique.append(value)
    return unique


def detect_tor_executable() -> str:
    for candidate in _candidate_paths():
        path = Path(candidate)
        if os.path.sep in candidate:
            if path.exists():
                return str(path)
        elif shutil.which(candidate):
            return str(shutil.which(candidate))
    return ""


def is_tor_running(port: int = TOR_SOCKS_PORT, host: str = TOR_SOCKS_HOST, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def start_tor(tor_path: str | None = None, socks_port: int = TOR_SOCKS_PORT) -> tuple[bool, str, subprocess.Popen | None]:
    global _TOR_PROCESS
    if is_tor_running(port=socks_port):
        return True, "[Tor] Using existing Tor instance", _TOR_PROCESS

    path = str(tor_path or "").strip() or detect_tor_executable()
    if not path:
        return False, "[Tor] Failed to start — Tor executable not found", None

    cmd = [path, "--SocksPort", str(socks_port)]
    try:
        logger.info("[Tor] Starting Tor process... (%s)", path)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _TOR_PROCESS = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        return False, f"[Tor] Failed to start — {exc}", None

    for _ in range(20):
        if is_tor_running(port=socks_port):
            return True, f"[Tor] Starting Tor process... ready on 127.0.0.1:{socks_port}", _TOR_PROCESS
        if _TOR_PROCESS and _TOR_PROCESS.poll() is not None:
            return False, "[Tor] Failed to start — process exited early", _TOR_PROCESS
        time.sleep(1)

    return False, "[Tor] Failed to start — SOCKS port not reachable after 20s", _TOR_PROCESS


def stop_tor() -> tuple[bool, str]:
    global _TOR_PROCESS
    proc = _TOR_PROCESS
    if not proc:
        return True, "[Tor] No managed Tor process to stop"
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        return True, "[Tor] Tor process stopped"
    except Exception as exc:
        return False, f"[Tor] Failed to stop Tor process — {exc}"
    finally:
        _TOR_PROCESS = None
