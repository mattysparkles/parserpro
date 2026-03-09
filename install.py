import os
import sys
import shutil
from pathlib import Path

import requests

from config import APP_DIR, config, save_config

TOOLS_DIR = APP_DIR / "tools"
TOOLS_DIR.mkdir(parents=True, exist_ok=True)

ZAP_DOWNLOAD_URL = "https://www.zaproxy.org/download/"
BURP_DOWNLOAD_URL = "https://portswigger.net/burp/releases/community/latest"


def _download_file(url: str, destination: Path, timeout: int = 120):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" in content_type and destination.suffix not in {".html"}:
            raise RuntimeError(f"unexpected HTML response from {url}; manual install may be required")
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    output.write(chunk)
    return destination


def _default_zap_paths():
    if sys.platform == "win32":
        return [
            Path(r"C:\\Program Files\\OWASP\\Zed Attack Proxy\\zap.bat"),
            Path(r"C:\\Program Files\\ZAP\\Zed Attack Proxy\\zap.bat"),
        ]
    return [Path("/usr/share/zaproxy/zap.sh"), Path("/snap/bin/zaproxy")]


def _default_burp_paths():
    if sys.platform == "win32":
        return [Path(r"C:\\Program Files\\BurpSuiteCommunity\\BurpSuiteCommunity.exe")]
    return [Path("/usr/bin/burpsuite"), Path("/opt/BurpSuiteCommunity/BurpSuiteCommunity")]


def ensure_zap_installed(auto_install=False):
    configured = Path(str(config.get("zap_executable_path", "")).strip()) if config.get("zap_executable_path") else None
    if configured and configured.exists():
        return {"available": True, "path": str(configured), "installed": False}

    for candidate in _default_zap_paths():
        if candidate.exists():
            config["zap_executable_path"] = str(candidate)
            save_config()
            return {"available": True, "path": str(candidate), "installed": False}

    which_candidate = shutil.which("zaproxy") or shutil.which("zap.sh")
    if which_candidate:
        config["zap_executable_path"] = which_candidate
        save_config()
        return {"available": True, "path": which_candidate, "installed": False}

    if not auto_install:
        return {"available": False, "path": None, "installed": False, "message": "ZAP executable not found"}

    zap_target = TOOLS_DIR / ("zap-installer.exe" if sys.platform == "win32" else "zap-installer.sh")
    try:
        _download_file(ZAP_DOWNLOAD_URL, zap_target)
    except Exception as exc:
        return {"available": False, "path": None, "installed": False, "message": f"ZAP download failed: {exc}"}

    if sys.platform != "win32":
        zap_target.chmod(0o755)
    config["zap_executable_path"] = str(zap_target)
    save_config()
    return {"available": True, "path": str(zap_target), "installed": True, "message": f"Downloaded ZAP installer to {zap_target}"}


def ensure_burp_installed(auto_install=False):
    configured = Path(str(config.get("burp_executable_path", "")).strip()) if config.get("burp_executable_path") else None
    if configured and configured.exists():
        return {"available": True, "path": str(configured), "installed": False}

    for candidate in _default_burp_paths():
        if candidate.exists():
            config["burp_executable_path"] = str(candidate)
            save_config()
            return {"available": True, "path": str(candidate), "installed": False}

    which_candidate = shutil.which("burpsuite") or shutil.which("burp")
    if which_candidate:
        config["burp_executable_path"] = which_candidate
        save_config()
        return {"available": True, "path": which_candidate, "installed": False}

    if not auto_install:
        return {"available": False, "path": None, "installed": False, "message": "Burp executable not found"}

    suffix = ".exe" if sys.platform == "win32" else ".jar"
    burp_target = TOOLS_DIR / f"burpsuite_community{suffix}"
    try:
        _download_file(BURP_DOWNLOAD_URL, burp_target)
    except Exception as exc:
        return {"available": False, "path": None, "installed": False, "message": f"Burp download failed: {exc}"}

    if sys.platform != "win32" and burp_target.suffix != ".jar":
        burp_target.chmod(0o755)
    config["burp_executable_path"] = str(burp_target)
    save_config()
    return {"available": True, "path": str(burp_target), "installed": True, "message": f"Downloaded Burp package to {burp_target}"}
