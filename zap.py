import json
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import requests

ZAP_DOWNLOAD_URL = "https://www.zaproxy.org/download/"


def _candidate_zap_paths():
    candidates = []
    if sys.platform == "win32":
        candidates.extend([
            Path(r"C:\\Program Files\\OWASP\\Zed Attack Proxy\\zap.bat"),
            Path(r"C:\\Program Files\\OWASP\\Zed Attack Proxy\\zap.exe"),
            Path(r"C:\\Program Files\\ZAP\\Zed Attack Proxy\\zap.bat"),
            Path(r"C:\\Program Files\\ZAP\\Zed Attack Proxy\\zap.exe"),
        ])
    else:
        for cmd in ("zaproxy", "zap.sh"):
            p = shutil.which(cmd)
            if p:
                candidates.append(Path(p))
    return candidates


def find_zap_executable():
    for path in _candidate_zap_paths():
        if path.exists():
            return str(path)
    return None


def launch_zap(daemon=False, proxy_url="http://127.0.0.1:8080", api_key=""):
    exe = find_zap_executable()
    if not exe:
        webbrowser.open(ZAP_DOWNLOAD_URL)
        return False, f"ZAP executable not found. Opened download page: {ZAP_DOWNLOAD_URL}"
    try:
        if daemon:
            host, port = parse_host_port(proxy_url)
            if sys.platform == "win32":
                cmd = f'"{exe}" -daemon -host {host} -port {port}'
                if api_key:
                    cmd += f" -config api.key={api_key}"
                subprocess.Popen(cmd, shell=True)
            else:
                args = [exe, "-daemon", "-host", host, "-port", str(port)]
                if api_key:
                    args += ["-config", f"api.key={api_key}"]
                subprocess.Popen(args)
        else:
            if sys.platform == "win32":
                subprocess.Popen(f'"{exe}"', shell=True)
            else:
                subprocess.Popen([exe])
        return True, f"Launched ZAP: {exe}"
    except Exception as exc:
        return False, f"Failed to launch ZAP: {exc}"


def parse_host_port(proxy_url: str):
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 8080


def import_data_to_zap(proxy_url, api_key, targets):
    host, port = parse_host_port(proxy_url)
    base = f"http://{host}:{port}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    for target in targets:
        url = target.get("action_url") or target.get("original_url")
        if not url:
            continue
        params = {"url": url}
        if api_key:
            params["apikey"] = api_key
        requests.get(f"{base}/JSON/core/action/accessUrl/", params=params, headers=headers, timeout=8)
        requests.get(f"{base}/JSON/ascan/action/scan/", params=params, headers=headers, timeout=8)
    return True, "Imported targets into ZAP active scan queue"


def export_data_for_zap(path, targets):
    payload = {"targets": targets}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
