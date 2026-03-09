import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from config import DATA_DIR, config
from install_tools import BURP_RELEASE_PAGE, BURP_DIR, install_burp

BURP_DOWNLOAD_URL = BURP_RELEASE_PAGE


def _resolve_burp_command(auto_install=False):
    jar = Path(str(config.get("BURP_JAR", BURP_DIR / "burpsuite_community.jar")))
    if not jar.exists() and auto_install:
        state = install_burp()
        if state.get("ok"):
            jar = Path(state["path"])
            config["BURP_JAR"] = str(jar)
    return jar if jar.exists() else None


def launch_burp(auto_install=False):
    jar = _resolve_burp_command(auto_install=auto_install)
    if not jar:
        return False, f"Burp executable not found. Install from {BURP_DOWNLOAD_URL}"
    try:
        subprocess.Popen(["java", "-jar", str(jar)])
        return True, f"Launched Burp: {jar}"
    except Exception as exc:
        return False, f"Failed to launch Burp: {exc}"


def export_data_for_burp(site_data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "burp_import.json"
    out.write_text(site_data, encoding="utf-8")
    return out


def build_intruder_request_template(site_row):
    action = (site_row or {}).get("action") or (site_row or {}).get("action_url") or "https://target/login"
    payload = (site_row or {}).get("post_data") or "username=^USER^&password=^PASS^"
    host = urlparse(action).hostname or "target"
    return f"POST {action} HTTP/1.1\nHost: {host}\nContent-Type: application/x-www-form-urlencoded\n\n{payload}"


def run_burp_with_project(site_rows, auto_install=False):
    req_file = DATA_DIR / "burp_intruder_requests.txt"
    req_file.write_text("\n\n".join(build_intruder_request_template(row) for row in (site_rows or [])[:50]), encoding="utf-8")
    project = DATA_DIR / "burp_project.json"
    project.write_text(json.dumps({"proxy": "127.0.0.1:8080", "requests": str(req_file)}, indent=2), encoding="utf-8")
    ok, msg = launch_burp(auto_install=auto_install)
    return ok, f"{msg}. Burp runner prepared template: {req_file}"
