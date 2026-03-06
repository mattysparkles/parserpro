import json
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse
import webbrowser

import requests

from app_logging import logger
from helpers import log_once


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = APP_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
HITS_DIR = APP_DIR / "hits"
HITS_DIR.mkdir(parents=True, exist_ok=True)

LEGACY_CONFIG_FILE = APP_DIR / "config.json"
LEGACY_PROCESSED_SITES_FILE = APP_DIR / "processed_sites.json"
CONFIG_FILE = DATA_DIR / "config.json"
PROCESSED_SITES_FILE = DATA_DIR / "processed_sites.json"
GOST_RELEASE_API = "https://api.github.com/repos/ginuerzh/gost/releases/latest"
HYDRA_RELEASE_API = "https://api.github.com/repos/vanhauser-thc/thc-hydra/releases/latest"
HYDRA_WINDOWS_DIR = APP_DIR / "tools" / "hydra"
GOST_ARCHIVE_CACHE = DATA_DIR / "downloads"
GOST_ARCHIVE_CACHE.mkdir(parents=True, exist_ok=True)
HYDRA_WINDOWS_DIR.mkdir(parents=True, exist_ok=True)


def get_gost_binary_path():
    return DATA_DIR / ("gost.exe" if platform.system().lower() == "windows" else "gost")


GOST_EXE = get_gost_binary_path()


def normalize_proxy(value):
    if value is None:
        return None
    if isinstance(value, str):
        server = value.strip()
        return {"server": server} if server else None
    if isinstance(value, dict):
        server = (value.get("server") or "").strip()
        if server:
            out = dict(value)
            out["server"] = server
            return out
    return None


def proxy_is_reachable(proxy_dict, timeout=1.0):
    proxy_cfg = normalize_proxy(proxy_dict)
    if not proxy_cfg:
        return False

    server = proxy_cfg.get("server", "")
    parsed = urlparse(server if "://" in server else f"//{server}")
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return False

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_vpn_control(cfg):
    raw = str((cfg or {}).get("vpn_control", "none")).strip().lower()
    return raw if raw in {"none", "nordvpn"} else "none"


def get_effective_proxy(cfg, runtime_proxy=None, fail_fast=None):
    configured_proxy = runtime_proxy
    if configured_proxy is None:
        configured_proxy = (
            cfg.get("proxy_url")
            or cfg.get("burp_proxy")
            or cfg.get("socks_proxy")
            or cfg.get("proxy")
            or None
        )

    proxy_cfg = normalize_proxy(configured_proxy)
    if not proxy_cfg:
        return None

    if proxy_is_reachable(proxy_cfg):
        return proxy_cfg

    required = bool(cfg.get("proxy_required", False)) if fail_fast is None else bool(fail_fast)
    if required:
        raise RuntimeError(f"Configured proxy is unreachable: {proxy_cfg.get('server', '')}")

    log_once(
        f"proxy-unreachable:{proxy_cfg.get('server', '')}",
        "Proxy configured but unreachable; disabling proxy for this run",
    )
    return None


def _migrate_legacy_config_once():
    if not CONFIG_FILE.exists() and LEGACY_CONFIG_FILE.exists():
        CONFIG_FILE.write_text(LEGACY_CONFIG_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    if not PROCESSED_SITES_FILE.exists() and LEGACY_PROCESSED_SITES_FILE.exists():
        PROCESSED_SITES_FILE.write_text(LEGACY_PROCESSED_SITES_FILE.read_text(encoding="utf-8"), encoding="utf-8")


def load_config():
    _migrate_legacy_config_once()
    loaded = {}
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
    loaded.setdefault("ignore_https_errors", False)
    loaded.setdefault("vpn_control", "none")
    loaded.setdefault("proxy_url", "")
    loaded.setdefault("proxy_required", False)
    loaded.setdefault("use_burp", False)
    loaded.setdefault("burp_proxy", "")
    loaded.setdefault("proxy_rotation", False)
    loaded.setdefault("proxy_list_file", "")
    loaded.setdefault("anticaptcha_key", "")
    loaded.setdefault("capsolver_key", "")
    loaded.setdefault("captcha_provider_order", ["deathbycaptcha", "2captcha", "anticaptcha", "capsolver"])
    loaded.setdefault("allow_nonstandard_ports", False)
    loaded.setdefault("force_recheck", False)
    loaded.setdefault("cache_ttl_days", 30)
    loaded.setdefault("failed_retry_ttl_days", 1)
    loaded.setdefault("debug_logging", False)
    loaded.setdefault("analysis_mode", "static")
    loaded.setdefault("observation_enable_dummy_interaction", False)
    loaded.setdefault("observation_allowlisted_domains", [])
    loaded.setdefault("startup_dependency_checks", True)
    loaded.setdefault("prefer_wsl_hydra", True)
    loaded.setdefault("auto_install_hydra", True)
    loaded.setdefault("hydra_timeout_seconds", 3600)
    loaded.setdefault("auto_setup_chromedriver", True)
    loaded.setdefault("auto_configure_nordvpn_path", True)
    return loaded


config = load_config()
logger.set_debug(bool(config.get("debug_logging", False)))


def save_config():
    logger.set_debug(bool(config.get("debug_logging", False)))
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _run_cmd(command, timeout=30):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _wsl_available() -> bool:
    if os.name != "nt":
        return False
    try:
        res = _run_cmd(["wsl", "--status"], timeout=10)
        return res.returncode == 0
    except Exception:
        return False


def _hydra_available_native() -> bool:
    if shutil.which("hydra"):
        return True
    win_hydra = HYDRA_WINDOWS_DIR / "hydra.exe"
    return win_hydra.exists()


def _hydra_available_wsl() -> bool:
    if not _wsl_available():
        return False
    try:
        res = _run_cmd(["wsl", "hydra", "--version"], timeout=15)
        return res.returncode == 0
    except Exception:
        return False


def _install_hydra_wsl(log_func=None) -> bool:
    if not _wsl_available():
        return False
    try:
        if log_func:
            log_func("Hydra missing in WSL; attempting automatic install on Ubuntu...")
        res = _run_cmd(["wsl", "-d", "Ubuntu", "bash", "-lc", "sudo apt update && sudo apt install -y hydra"], timeout=600)
        ok = res.returncode == 0
        if log_func:
            log_func("WSL Hydra install completed." if ok else f"WSL Hydra install failed: {(res.stderr or res.stdout).strip()[:220]}")
        return ok
    except Exception as exc:
        if log_func:
            log_func(f"WSL Hydra install error: {exc}")
        return False


def _download_hydra_windows_binary(log_func=None):
    try:
        resp = requests.get(HYDRA_RELEASE_API, timeout=30)
        resp.raise_for_status()
        release = resp.json()
    except Exception as exc:
        if log_func:
            log_func(f"Hydra release lookup failed: {exc}")
        return None

    assets = release.get("assets") or []
    preferred = [a for a in assets if "win" in (a.get("name") or "").lower() and (a.get("name") or "").lower().endswith(".zip")]
    if not preferred:
        if log_func:
            log_func("No official Windows Hydra release asset was found on latest release.")
        return None

    asset = preferred[0]
    archive_path = GOST_ARCHIVE_CACHE / asset["name"]
    try:
        if log_func:
            log_func(f"Downloading Hydra Windows binary: {asset['name']}")
        with requests.get(asset["browser_download_url"], stream=True, timeout=120) as req:
            req.raise_for_status()
            with archive_path.open("wb") as fh:
                for chunk in req.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(HYDRA_WINDOWS_DIR)
        for file in HYDRA_WINDOWS_DIR.rglob("hydra.exe"):
            return file
    except Exception as exc:
        if log_func:
            log_func(f"Hydra Windows install failed: {exc}")
    return None


def _add_dir_to_path_windows(path_obj: Path) -> bool:
    target = str(path_obj)
    path_now = os.environ.get("PATH", "")
    if target.lower() in path_now.lower():
        return False
    try:
        merged = f"{path_now};{target}"
        subprocess.run(["setx", "PATH", merged], capture_output=True, text=True, timeout=30)
        return True
    except Exception:
        return False


def ensure_hydra_available(log_func=None):
    """Ensure Hydra is callable, preferring WSL on Windows when available."""
    prefer_wsl = bool(config.get("prefer_wsl_hydra", True))
    auto_install = bool(config.get("auto_install_hydra", True))

    if prefer_wsl and _hydra_available_wsl():
        return {"available": True, "mode": "wsl", "message": "Hydra available in WSL"}
    if _hydra_available_native():
        return {"available": True, "mode": "native", "message": "Hydra available natively"}

    if auto_install and prefer_wsl and _wsl_available() and _install_hydra_wsl(log_func=log_func) and _hydra_available_wsl():
        return {"available": True, "mode": "wsl", "message": "Hydra installed in WSL"}

    if auto_install and os.name == "nt":
        hydra_exe = _download_hydra_windows_binary(log_func=log_func)
        if hydra_exe:
            restart_needed = _add_dir_to_path_windows(hydra_exe.parent)
            message = "Hydra installed natively on Windows"
            if restart_needed:
                message += "; restart may be required for PATH update"
            return {
                "available": True,
                "mode": "native",
                "message": message,
                "path_updated": restart_needed,
            }

    return {"available": False, "mode": None, "message": "Hydra not found (native or WSL)"}


def ensure_nordvpn_cli(log_func=None):
    """Resolve NordVPN CLI path on Windows and optionally add install directory to PATH."""
    candidates = [
        shutil.which("nordvpn"),
        shutil.which("nordvpncli"),
        r"C:\Program Files\NordVPN\nordvpn.exe",
        r"C:\Program Files\NordVPN\NordVPN.exe",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path_obj = Path(candidate)
        if not path_obj.exists() and os.path.sep in str(candidate):
            continue
        if os.name == "nt" and bool(config.get("auto_configure_nordvpn_path", True)):
            _add_dir_to_path_windows(path_obj.parent)
        return {"available": True, "path": str(path_obj)}

    if log_func:
        log_func("NordVPN CLI not found. Opening download page for installation.")
    try:
        webbrowser.open("https://nordvpn.com/download/windows/")
    except Exception:
        pass
    return {"available": False, "path": None}


def download_gost():
    if GOST_EXE.exists():
        return GOST_EXE

    asset = _get_matching_gost_asset()
    if not asset:
        logger.warn("Could not determine a valid gost release asset for this platform; continuing without proxy.")
        return None

    archive_name = asset["name"]
    archive_path = GOST_ARCHIVE_CACHE / archive_name
    if not archive_path.exists():
        logger.info(f"Downloading gost archive: {archive_name}")
        try:
            with requests.get(asset["browser_download_url"], stream=True, timeout=60) as r:
                r.raise_for_status()
                with archive_path.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            logger.warn(f"Failed to download gost ({e}); continuing without proxy.")
            return None

    try:
        _extract_gost_archive(archive_path)
    except Exception as e:
        logger.warn(f"Failed to extract gost archive ({e}); continuing without proxy.")
        return None

    if GOST_EXE.exists():
        if platform.system().lower() != "windows":
            GOST_EXE.chmod(0o755)
        logger.info(f"gost ready at {GOST_EXE}")
        return GOST_EXE

    logger.warn("gost archive extracted but binary was not found; continuing without proxy.")
    return None


def _get_matching_gost_asset():
    sys_name = platform.system().lower()
    arch = platform.machine().lower()
    arch_pref = "arm64" if arch in {"arm64", "aarch64"} else "amd64"

    try:
        resp = requests.get(GOST_RELEASE_API, timeout=30)
        resp.raise_for_status()
        release = resp.json()
    except Exception as e:
        logger.warn(f"Failed to query gost releases API ({e}); continuing without proxy.")
        return None

    assets = release.get("assets") or []
    if sys_name == "windows":
        candidates = [a for a in assets if _match_asset(a, ["windows", "amd64"], [".zip"])]
    elif sys_name == "darwin":
        candidates = [a for a in assets if _match_asset(a, ["darwin", arch_pref], [".tar.gz", ".gz", ".zip"])]
    else:
        candidates = [a for a in assets if _match_asset(a, ["linux", "amd64"], [".tar.gz", ".gz"])]

    return candidates[0] if candidates else None


def _match_asset(asset, includes, endings):
    name = (asset.get("name") or "").lower()
    if not name:
        return False
    return all(k in name for k in includes) and any(name.endswith(end) for end in endings)


def _extract_gost_archive(archive_path):
    lower = archive_path.name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(DATA_DIR)
    elif lower.endswith(".tar.gz") or lower.endswith(".tgz") or lower.endswith(".gz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(DATA_DIR)
    else:
        raise ValueError(f"unsupported archive format: {archive_path.name}")

    names = ["gost.exe", "gost"] if platform.system().lower() == "windows" else ["gost", "gost.exe"]
    for name in names:
        for file in DATA_DIR.rglob(name):
            if file.is_file():
                if file.resolve() != GOST_EXE.resolve():
                    file.replace(GOST_EXE)
                return
