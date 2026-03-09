import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from config import DATA_DIR
from install import BURP_DOWNLOAD_URL, ensure_burp_installed


def find_burp_executable(auto_install=False):
    state = ensure_burp_installed(auto_install=auto_install)
    if state.get("available") and state.get("path"):
        return str(state["path"])
    return None


def launch_burp(auto_install=False):
    exe = find_burp_executable(auto_install=auto_install)
    if not exe:
        webbrowser.open(BURP_DOWNLOAD_URL)
        return False, f"Burp executable not found. Opened download page: {BURP_DOWNLOAD_URL}"
    try:
        if exe.endswith(".jar"):
            subprocess.Popen(["java", "-jar", exe])
        elif sys.platform == "win32":
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


def generate_intruder_payloads_xml(site_rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "burp_intruder_payloads.xml"
    items = []
    for row in site_rows or []:
        user = str((row or {}).get("username") or "").strip()
        pwd = str((row or {}).get("password") or "").strip()
        if not (user or pwd):
            continue
        items.append(f"    <payload>{user}:{pwd}</payload>")
    out.write_text("<?xml version='1.0' encoding='UTF-8'?>\n<intruderPayloads>\n" + "\n".join(items) + "\n</intruderPayloads>\n", encoding="utf-8")
    return out


def build_intruder_request_template(site_row):
    action = (site_row or {}).get("action") or (site_row or {}).get("action_url") or "https://target/login"
    payload = (site_row or {}).get("post_data") or "username=^USER^&password=^PASS^"
    host = urlparse(action).hostname or "target"
    return f"POST {action} HTTP/1.1\nHost: {host}\nContent-Type: application/x-www-form-urlencoded\n\n{payload}"


def run_burp_with_project(site_rows, auto_install=False):
    exe = find_burp_executable(auto_install=auto_install)
    if not exe:
        return False, "Burp not installed"
    req_file = DATA_DIR / "burp_intruder_requests.txt"
    req_file.write_text("\n\n".join(build_intruder_request_template(row) for row in (site_rows or [])[:50]), encoding="utf-8")
    return launch_burp(auto_install=auto_install)[0], f"Burp runner prepared payload template: {req_file}"
