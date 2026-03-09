import json
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import requests

from install import ZAP_DOWNLOAD_URL, ensure_zap_installed


def find_zap_executable(auto_install=False):
    state = ensure_zap_installed(auto_install=auto_install)
    if state.get("available") and state.get("path"):
        return str(state["path"])
    return None


def launch_zap(daemon=False, proxy_url="http://127.0.0.1:8080", api_key="", auto_install=False):
    exe = find_zap_executable(auto_install=auto_install)
    if not exe:
        webbrowser.open(ZAP_DOWNLOAD_URL)
        return False, f"ZAP executable not found. Opened download page: {ZAP_DOWNLOAD_URL}"
    try:
        host, port = parse_host_port(proxy_url)
        if daemon:
            args = [exe, "-daemon", "-host", host, "-port", str(port)]
            if api_key:
                args += ["-config", f"api.key={api_key}"]
        else:
            args = [exe]
        if sys.platform == "win32":
            subprocess.Popen(" ".join(f'"{a}"' if " " in a else a for a in args), shell=True)
        else:
            subprocess.Popen(args)
        return True, f"Launched ZAP: {exe}"
    except Exception as exc:
        return False, f"Failed to launch ZAP: {exc}"


def parse_host_port(proxy_url: str):
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 8080


def import_data_to_zap(proxy_url, api_key, targets):
    host, port = parse_host_port(proxy_url)
    base = f"http://{host}:{port}"
    for target in targets:
        url = target.get("action_url") or target.get("original_url")
        if not url:
            continue
        params = {"url": url}
        if api_key:
            params["apikey"] = api_key
        requests.get(f"{base}/JSON/core/action/accessUrl/", params=params, timeout=8)
        requests.get(f"{base}/JSON/ascan/action/scan/", params=params, timeout=8)
    return True, "Imported targets into ZAP active scan queue"


def export_data_for_zap(path, targets):
    payload = {"targets": targets}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_zap_active_scan(targets, proxy_url, api_key, auto_install=False):
    ok, msg = launch_zap(daemon=True, proxy_url=proxy_url, api_key=api_key, auto_install=auto_install)
    if not ok:
        return False, msg
    return import_data_to_zap(proxy_url=proxy_url, api_key=api_key, targets=targets)
