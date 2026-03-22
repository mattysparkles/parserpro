import importlib.util
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback
import webbrowser
import zipfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app_logging import logger

APP_DIR = Path(__file__).resolve().parent
TOOLS_DIR = APP_DIR / "tools"
HYDRA_DIR = TOOLS_DIR / "hydra"
ZAP_DIR = TOOLS_DIR / "zap"
BURP_DIR = TOOLS_DIR / "burp"

HYDRA_RELEASES = "https://api.github.com/repos/maaaaz/thc-hydra-windows/releases/latest"
HYDRA_WINDOWS_FALLBACK = "https://github.com/maaaaz/thc-hydra-windows/releases/download/v9.1/hydra-9.1-win.zip"
ZAP_LATEST_RELEASE_URL = "https://github.com/zaproxy/zaproxy/releases/latest"
BURP_RELEASE_PAGE = "https://portswigger.net/burp/releases/community/latest"
BURP_COMMUNITY_DOWNLOAD_PAGE = "https://portswigger.net/burp/communitydownload"


def _session() -> requests.Session:
    # FIXED: Standardized retries for third-party tool downloads
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _status_code_from_exc(exc: Exception):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", "n/a")


def _log_request_error(action: str, url: str, exc: Exception, log_func=None):
    status_code = _status_code_from_exc(exc)
    message = f"{action} failed for {url} (status={status_code}): {exc}"
    if log_func:
        log_func(message)
        log_func(traceback.format_exc())
    logger.exception(message)


def _show_messagebox(title: str, message: str):
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(title, message)
        root.destroy()
    except Exception:
        logger.info("Unable to show messagebox [%s]: %s", title, message)


def _download_file(url: str, output: Path, timeout: int = 10, log_func=None, retries: int = 3) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            if log_func:
                log_func(f"Downloading: {url} (attempt {attempt}/{retries})")
            with _session().get(url, stream=True, timeout=timeout, allow_redirects=True) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                received = 0
                with output.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
                            received += len(chunk)
                            if log_func and total:
                                pct = int((received / total) * 100)
                                if pct % 20 == 0:
                                    log_func(f"Download progress {output.name}: {pct}%")
            return output
        except Exception as exc:
            last_exc = exc
            _log_request_error("Download", url, exc, log_func=log_func)
            if log_func:
                log_func(f"Download failed ({attempt}/{retries}): {exc}")
            time.sleep(1.0)
    raise RuntimeError(f"failed to download {url}: {last_exc}")


def _append_to_windows_user_path(path: Path, log_func=None):
    path_str = str(path)
    if path_str.lower() in os.environ.get("PATH", "").lower():
        return
    os.environ["PATH"] = f"{path_str}{os.pathsep}{os.environ.get('PATH', '')}"
    try:
        current = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", "Path"], capture_output=True, text=True)
        existing = ""
        if current.returncode == 0:
            match = re.search(r"Path\s+REG_\w+\s+(.+)", current.stdout)
            existing = match.group(1).strip() if match else ""
        if path_str.lower() not in existing.lower():
            new_path = f"{existing};{path_str}".strip(";")
            subprocess.run(["setx", "Path", new_path], capture_output=True, text=True, check=False)
            if log_func:
                log_func(f"Added to Windows user PATH: {path_str}")
    except Exception as exc:
        _log_request_error("PATH update", path_str, exc, log_func=log_func)


def install_hydra(log_func=None):
    """Install hydra on Linux/WSL or unpack Windows zip into tools/hydra."""
    if platform.system().lower() != "windows":
        try:
            subprocess.run(["sudo", "apt", "update"], check=True, capture_output=True, text=True)
            subprocess.run(["sudo", "apt", "install", "-y", "hydra"], check=True, capture_output=True, text=True)
            hydra_path = shutil.which("hydra") or "hydra"
            return {"ok": True, "path": hydra_path, "message": "Hydra installed via apt"}
        except Exception as exc:
            return {"ok": False, "path": None, "message": f"Hydra apt install failed: {exc}"}

    hydra_existing = HYDRA_DIR / "hydra.exe"
    if hydra_existing.exists():
        _append_to_windows_user_path(hydra_existing.parent, log_func=log_func)
        return {"ok": True, "path": str(hydra_existing), "message": f"Hydra already installed at {hydra_existing}"}

    try:
        # FIXED: community Hydra Windows build fallback (no official v9.5+ Windows release)
        release = _session().get(HYDRA_RELEASES, timeout=10).json()
        assets = release.get("assets") or []
        zip_url = next((a.get("browser_download_url") for a in assets if str(a.get("name", "")).lower().endswith(".zip") and "win" in str(a.get("name", "")).lower()), None)
        zip_url = zip_url or HYDRA_WINDOWS_FALLBACK
        if not zip_url:
            _show_messagebox("Hydra", "No official Windows binary; use WSL Kali Hydra instead")
            return {"ok": False, "path": None, "message": "No official Windows binary; use WSL Kali Hydra instead"}
        archive = _download_file(zip_url, HYDRA_DIR / "hydra.zip", log_func=log_func)
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(HYDRA_DIR)
        hydra_exe = next(HYDRA_DIR.rglob("hydra.exe"), None)
        if not hydra_exe:
            _show_messagebox("Hydra", "No official Windows binary; use WSL Kali Hydra instead")
            return {"ok": False, "path": None, "message": "Hydra Windows archive extracted but hydra.exe missing"}
        hydra_exe.chmod(hydra_exe.stat().st_mode | stat.S_IEXEC)
        _append_to_windows_user_path(hydra_exe.parent, log_func=log_func)
        return {"ok": True, "path": str(hydra_exe), "message": f"Hydra extracted to {hydra_exe}"}
    except Exception as exc:
        _show_messagebox("Hydra", "No official Windows binary; use WSL Kali Hydra instead")
        _log_request_error("Hydra install", HYDRA_RELEASES, exc, log_func=log_func)
        return {"ok": False, "path": None, "message": f"Hydra Windows install failed: {exc}"}


TOR_DEPENDENCY_MODULES = {"stem": "stem", "requests[socks]": "requests", "pysocks": "socks"}


def get_missing_tor_dependencies(module_finder=None):
    finder = module_finder or importlib.util.find_spec
    missing = []
    for package_name, module_name in TOR_DEPENDENCY_MODULES.items():
        if finder(module_name) is None:
            missing.append(package_name)
    return missing


def install_tor_dependencies(log_func=None, missing_packages=None):
    missing = list(missing_packages) if missing_packages is not None else get_missing_tor_dependencies()
    if not missing:
        message = "Tor Python dependencies already installed"
        if log_func:
            log_func(message)
        return {"ok": True, "message": message, "installed": False, "missing": []}

    commands = [
        [sys.executable, "-m", "pip", "install", *missing],
    ]
    last_message = ""
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
            if result.returncode == 0:
                message = f"Installed Tor Python dependencies ({', '.join(missing)})"
                if log_func:
                    log_func(message)
                return {"ok": True, "message": message, "installed": True, "missing": missing}
            last_message = (result.stderr or result.stdout or "pip failed").strip()
        except Exception as exc:
            last_message = str(exc)
    return {"ok": False, "message": f"Tor dependency install failed: {last_message}", "installed": False, "missing": missing}


def ensure_tor_dependencies(log_func=None):
    missing = get_missing_tor_dependencies()
    if not missing:
        return {"ok": True, "message": "Tor Python dependencies already installed", "installed": False, "missing": []}
    if log_func:
        log_func(f"Missing Tor Python dependencies detected: {', '.join(missing)}")
    return install_tor_dependencies(log_func=log_func, missing_packages=missing)


def detect_tor_installation():
    candidates = [
        shutil.which("tor"),
        shutil.which("tor.exe"),
        r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
    ]
    for candidate in candidates:
        if candidate and (os.path.sep not in str(candidate) or Path(candidate).exists()):
            return {"ok": True, "path": str(candidate), "message": f"Tor found at {candidate}"}
    return {"ok": False, "path": None, "message": "Tor not found. Download Tor Browser: https://www.torproject.org/download/"}


def check_nordvpn_onion_support(log_func=None):
    candidate = shutil.which("nordvpn") or shutil.which("nordvpncli")
    if not candidate:
        return {"ok": False, "message": "NordVPN CLI not found"}
    try:
        result = subprocess.run([candidate, "groups"], capture_output=True, text=True, check=False, timeout=20)
        text = f"{result.stdout}\n{result.stderr}".lower()
        ok = "onion" in text
        message = "NordVPN Onion group available" if ok else "NordVPN CLI detected, but Onion group not listed"
        if log_func:
            log_func(message)
        return {"ok": ok, "message": message}
    except Exception as exc:
        return {"ok": False, "message": f"NordVPN Onion group check failed: {exc}"}


def install_zap(log_func=None):
    ZAP_DIR.mkdir(parents=True, exist_ok=True)
    existing_jar = ZAP_DIR / "ZAP.jar"
    if existing_jar.exists():
        return {"ok": True, "path": str(existing_jar), "message": f"ZAP already installed at {existing_jar}"}
    try:
        # FIXED: Dynamic latest ZAP URL
        latest = _session().get(ZAP_LATEST_RELEASE_URL, timeout=10, allow_redirects=True)
        latest.raise_for_status()
        match = re.search(r"/tag/(v\d+\.\d+\.\d+)", latest.url)
        if not match:
            raise RuntimeError(f"Unable to parse latest ZAP version from URL: {latest.url}")
        tag = match.group(1)
        version = tag.lstrip("v")
        archive_name = f"ZAP_{version}_Crossplatform.zip"
        zap_url = f"https://github.com/zaproxy/zaproxy/releases/download/{tag}/{archive_name}"
        archive = _download_file(zap_url, ZAP_DIR / "zap.zip", log_func=log_func)
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
        _log_request_error("ZAP install", ZAP_LATEST_RELEASE_URL, exc, log_func=log_func)
        return {"ok": False, "path": None, "message": f"ZAP install failed: {exc}"}


def _resolve_burp_jar_url() -> str:
    # FIXED: Burp parse fix using official latest endpoint and scrape fallback
    latest = _session().get(BURP_RELEASE_PAGE, timeout=10, allow_redirects=True)
    latest.raise_for_status()
    if ".jar" in latest.url or latest.url.endswith(".exe"):
        return latest.url

    download_page = _session().get(BURP_COMMUNITY_DOWNLOAD_PAGE, timeout=10)
    download_page.raise_for_status()
    match = re.search(r'https://[^"\']+burp[^"\']+\.jar[^"\']*', download_page.text)
    if not match:
        raise RuntimeError("Could not parse Burp community download URL")
    return match.group(0).replace("&amp;", "&")


def install_burp(log_func=None):
    BURP_DIR.mkdir(parents=True, exist_ok=True)
    existing_jar = BURP_DIR / "burpsuite_community.jar"
    if existing_jar.exists():
        return {"ok": True, "path": str(existing_jar), "message": f"Burp already installed at {existing_jar}"}
    try:
        jar_url = _resolve_burp_jar_url()
        target = _download_file(jar_url, BURP_DIR / "burpsuite_community.jar", log_func=log_func)
        if os.name == "nt":
            launcher = BURP_DIR / "burp.bat"
            launcher.write_text("@echo off\r\njava -jar \"%~dp0burpsuite_community.jar\"\r\n", encoding="utf-8")
        else:
            launcher = BURP_DIR / "burp.sh"
            launcher.write_text("#!/usr/bin/env bash\njava -jar \"$(dirname \"$0\")/burpsuite_community.jar\"\n", encoding="utf-8")
            launcher.chmod(0o755)
        return {"ok": True, "path": str(target), "launcher": str(launcher), "message": f"Installed Burp jar at {target}"}
    except Exception as exc:
        _log_request_error("Burp install", BURP_RELEASE_PAGE, exc, log_func=log_func)
        _show_messagebox("Burp", "Manual download required")
        try:
            webbrowser.open(BURP_COMMUNITY_DOWNLOAD_PAGE)
        except Exception:
            logger.warning("Could not open browser for Burp manual download page")
        return {"ok": False, "path": None, "message": f"Burp install failed: {exc}"}
