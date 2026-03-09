import json
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from config import config
from install_tools import ZAP_DIR, install_zap

ZAP_DOWNLOAD_URL = "https://github.com/zaproxy/zaproxy/releases/latest"

try:
    from zapv2 import ZAPv2
except Exception:  # optional runtime dependency
    ZAPv2 = None


def _resolve_zap_jar(auto_install=False):
    jar = Path(str(config.get("ZAP_JAR", ZAP_DIR / "ZAP.jar")))
    if not jar.exists() and auto_install:
        state = install_zap()
        if state.get("ok"):
            jar = Path(state["path"])
            config["ZAP_JAR"] = str(jar)
    return jar if jar.exists() else None


def parse_host_port(proxy_url: str):
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    return parsed.hostname or "127.0.0.1", parsed.port or 8080


def launch_zap(daemon=False, proxy_url="http://127.0.0.1:8080", api_key="", auto_install=False):
    jar = _resolve_zap_jar(auto_install=auto_install)
    if not jar:
        return False, f"ZAP not found. Install from {ZAP_DOWNLOAD_URL}"
    host, port = parse_host_port(proxy_url)
    args = ["java", "-jar", str(jar)]
    if daemon:
        args += ["-daemon", "-host", host, "-port", str(port)]
        if api_key:
            args += ["-config", f"api.key={api_key}"]
    try:
        subprocess.Popen(args)
        return True, f"Launched ZAP: {jar}"
    except Exception as exc:
        return False, f"Failed to launch ZAP: {exc}"


def import_data_to_zap(proxy_url, api_key, targets):
    if ZAPv2 is None:
        return False, "python-owasp-zap-v2.4 not installed"
    zap = ZAPv2(apikey=api_key, proxies={"http": proxy_url, "https": proxy_url})
    for target in targets:
        url = target.get("action_url") or target.get("original_url")
        if not url:
            continue
        zap.core.access_url(url)
        zap.ascan.scan(url)
    return True, "Imported targets into ZAP active scan queue"


def export_data_for_zap(path, targets):
    Path(path).write_text(json.dumps({"targets": targets}, indent=2), encoding="utf-8")


def run_zap_active_scan(targets, proxy_url, api_key, auto_install=False):
    ok, msg = launch_zap(daemon=True, proxy_url=proxy_url, api_key=api_key, auto_install=auto_install)
    if not ok:
        return False, msg
    time.sleep(2)
    return import_data_to_zap(proxy_url=proxy_url, api_key=api_key, targets=targets)
