import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from config import DATA_DIR

BURP_DOWNLOAD_URL = "https://portswigger.net/burp/communitydownload"


def _candidate_burp_paths():
    candidates = []
    if sys.platform == "win32":
        candidates.extend([
            Path(r"C:\\Program Files\\BurpSuiteCommunity\\BurpSuiteCommunity.exe"),
            Path(r"C:\\Program Files\\BurpSuiteCommunity\\burpsuite_community.exe"),
            Path(r"C:\\Program Files\\BurpSuite\\BurpSuiteCommunity.exe"),
        ])
    else:
        for cmd in ("burpsuite", "burp"):
            p = shutil.which(cmd)
            if p:
                candidates.append(Path(p))
    return candidates


def find_burp_executable():
    for path in _candidate_burp_paths():
        if path.exists():
            return str(path)
    return None


def launch_burp():
    exe = find_burp_executable()
    if not exe:
        webbrowser.open(BURP_DOWNLOAD_URL)
        return False, f"Burp executable not found. Opened download page: {BURP_DOWNLOAD_URL}"
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'"{exe}"', shell=True)
        else:
            subprocess.Popen([exe])
        return True, f"Launched Burp: {exe}"
    except Exception as exc:
        return False, f"Failed to launch Burp: {exc}"


def export_data_for_burp(site_data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "burp_import.json"
    out.write_text(site_data, encoding="utf-8")
    return out


def parse_host_port(proxy_url: str):
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 8080
