import os
import platform
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import requests

from app_logging import logger

APP_DIR = Path(__file__).resolve().parent
TOOLS_DIR = APP_DIR / "tools"
HYDRA_DIR = TOOLS_DIR / "hydra"
ZAP_DIR = TOOLS_DIR / "zap"
BURP_DIR = TOOLS_DIR / "burp"

HYDRA_RELEASES = "https://api.github.com/repos/vanhauser-thc/thc-hydra/releases/latest"
HYDRA_WINDOWS_FALLBACK = "https://github.com/vanhauser-thc/thc-hydra/releases/download/v9.5/hydra-9.5-win.zip"
ZAP_JAR_URL = "https://github.com/zaproxy/zaproxy/releases/latest/download/ZAP_2_16_1_Crossplatform.zip"
BURP_RELEASE_PAGE = "https://portswigger.net/burp/releases/community/latest"


def _download_file(url: str, output: Path, timeout: int = 180) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
        response.raise_for_status()
        with output.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)
    return output


def install_hydra(log_func=None):
    """Install hydra on Linux/WSL or unpack Windows zip into tools/hydra."""
    if platform.system().lower() != "windows":
        try:
            subprocess.run(["sudo", "apt", "update"], check=True)
            subprocess.run(["sudo", "apt", "install", "-y", "hydra"], check=True)
            hydra_path = shutil.which("hydra") or "hydra"
            return {"ok": True, "path": hydra_path, "message": "Hydra installed via apt"}
        except Exception as exc:
            return {"ok": False, "path": None, "message": f"Hydra apt install failed: {exc}"}

    try:
        release = requests.get(HYDRA_RELEASES, timeout=40).json()
        assets = release.get("assets") or []
        zip_url = next((a.get("browser_download_url") for a in assets if str(a.get("name", "")).lower().endswith(".zip") and "win" in str(a.get("name", "")).lower()), None)
        zip_url = zip_url or HYDRA_WINDOWS_FALLBACK
        archive = _download_file(zip_url, HYDRA_DIR / "hydra.zip")
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(HYDRA_DIR)
        hydra_exe = next(HYDRA_DIR.rglob("hydra.exe"), None)
        if not hydra_exe:
            return {"ok": False, "path": None, "message": "Hydra Windows archive extracted but hydra.exe missing"}
        return {"ok": True, "path": str(hydra_exe), "message": f"Hydra extracted to {hydra_exe}"}
    except Exception as exc:
        return {"ok": False, "path": None, "message": f"Hydra Windows install failed: {exc}"}


def install_zap(log_func=None):
    ZAP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        archive = _download_file(ZAP_JAR_URL, ZAP_DIR / "zap.zip")
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(ZAP_DIR)
        jar = next(ZAP_DIR.rglob("*.jar"), None)
        if not jar:
            return {"ok": False, "path": None, "message": "ZAP archive downloaded but no jar found"}
        target = ZAP_DIR / "ZAP.jar"
        if jar != target:
            target.write_bytes(jar.read_bytes())
        if os.name == "nt":
            launcher = ZAP_DIR / "zap.bat"
            launcher.write_text("@echo off\r\njava -jar \"%~dp0ZAP.jar\" -daemon -host 127.0.0.1 -port 8080\r\n", encoding="utf-8")
        else:
            launcher = ZAP_DIR / "zap.sh"
            launcher.write_text("#!/usr/bin/env bash\njava -jar \"$(dirname \"$0\")/ZAP.jar\" -daemon -host 127.0.0.1 -port 8080\n", encoding="utf-8")
            launcher.chmod(0o755)
        return {"ok": True, "path": str(target), "launcher": str(launcher), "message": f"Installed ZAP jar at {target}"}
    except Exception as exc:
        return {"ok": False, "path": None, "message": f"ZAP install failed: {exc}"}


def _resolve_burp_jar_url() -> str:
    response = requests.get(BURP_RELEASE_PAGE, timeout=40)
    response.raise_for_status()
    match = re.search(r'https://portswigger-cdn\.net/burp/releases/download\?product=community[^"\']+', response.text)
    if not match:
        raise RuntimeError("Could not parse Burp community download URL")
    return match.group(0).replace("&amp;", "&")


def install_burp(log_func=None):
    BURP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        jar_url = _resolve_burp_jar_url()
        target = _download_file(jar_url, BURP_DIR / "burpsuite_community.jar")
        if os.name == "nt":
            launcher = BURP_DIR / "burp.bat"
            launcher.write_text("@echo off\r\njava -jar \"%~dp0burpsuite_community.jar\"\r\n", encoding="utf-8")
        else:
            launcher = BURP_DIR / "burp.sh"
            launcher.write_text("#!/usr/bin/env bash\njava -jar \"$(dirname \"$0\")/burpsuite_community.jar\"\n", encoding="utf-8")
            launcher.chmod(0o755)
        return {"ok": True, "path": str(target), "launcher": str(launcher), "message": f"Installed Burp jar at {target}"}
    except Exception as exc:
        return {"ok": False, "path": None, "message": f"Burp install failed: {exc}"}
