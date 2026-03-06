import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import time
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
HYDRA_WINDOWS_RELEASES_API = "https://api.github.com/repos/maaaaz/thc-hydra-windows/releases/latest"
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



def get_intercept_proxy(cfg, runtime_proxy=None, fail_fast=None):
    """Resolve Burp/ZAP/general proxy with Burp taking precedence when both are enabled."""
    if bool((cfg or {}).get("use_burp", False)) and str((cfg or {}).get("burp_proxy", "")).strip():
        return {"server": str(cfg.get("burp_proxy")).strip()}
    if bool((cfg or {}).get("use_zap", False)) and str((cfg or {}).get("zap_proxy", "")).strip():
        return {"server": str(cfg.get("zap_proxy")).strip()}
    return get_effective_proxy(cfg, runtime_proxy=runtime_proxy, fail_fast=fail_fast)

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
    loaded.setdefault("burp_proxy", "http://127.0.0.1:8080")
    loaded.setdefault("use_zap", False)
    loaded.setdefault("zap_proxy", "http://127.0.0.1:8080")
    loaded.setdefault("zap_api_key", "")
    loaded.setdefault("auto_start_zap_daemon", False)
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
    loaded.setdefault("wsl_username", "")
    loaded.setdefault("wsl_password", "")
    loaded.setdefault("auto_setup_chromedriver", True)
    loaded.setdefault("auto_configure_nordvpn_path", True)
    return loaded


config = load_config()
logger.set_debug(bool(config.get("debug_logging", False)))


def save_config():
    logger.set_debug(bool(config.get("debug_logging", False)))
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _run_cmd(command, timeout=30):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def build_wsl_command(inner_command: str, distro: str = "", username: str = "") -> list[str]:
    cmd = ["wsl"]
    if distro:
        cmd.extend(["-d", distro])
    if username:
        cmd.extend(["-u", username])
    cmd.extend(["bash", "-lc", inner_command])
    return cmd


def build_wsl_sudo_command(base_command: str, password: str = "", non_interactive: bool = False) -> str:
    if password:
        return f"echo {shlex.quote(password)} | sudo -S {base_command}"
    if non_interactive:
        return f"sudo -n {base_command}"
    return f"sudo {base_command}"


def _is_apt_lock_error(output: str) -> bool:
    message = (output or "").lower()
    return (
        "could not get lock" in message
        or "unable to lock" in message
        or "could not open lock file" in message
        or "lock-frontend" in message
    )


def _is_sudo_auth_error(output: str) -> bool:
    message = (output or "").lower()
    return (
        "sudo: a password is required" in message
        or "[sudo] password for" in message
        or "are you root?" in message
        or "permission denied" in message
    )


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


def _normalize_wsl_distro_name(raw_name: str) -> str:
    # FIX: Normalize malformed `wsl --list --quiet` entries like `k a l i - l i n u x`.
    candidate = " ".join((raw_name or "").strip().split())
    if not candidate:
        return ""
    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"\s*-\s*", "-", candidate)
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9\- ]*[A-Za-z0-9])?", candidate) and " " in candidate:
        pieces = [p for p in candidate.split(" ") if p]
        if pieces and all(len(p) == 1 or p == "-" for p in pieces):
            candidate = "".join(pieces).replace("--", "-")
    return candidate.strip()


def _list_wsl_distros(log_func=None) -> list[str]:
    # FIX: Parse distro names robustly and drop empty/garbled lines.
    if not _wsl_available():
        return []
    try:
        res = _run_cmd(["wsl", "--list", "--quiet"], timeout=20)
        if res.returncode != 0:
            if log_func:
                log_func(f"WSL distro listing failed: {(res.stderr or res.stdout).strip()[:220]}")
            return []
        seen = set()
        distros = []
        for line in (res.stdout or "").splitlines():
            clean = _normalize_wsl_distro_name(line)
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                distros.append(clean)
        if log_func:
            log_func(f"Detected WSL distros: {', '.join(distros) if distros else 'none'}")
        return distros
    except Exception as exc:
        if log_func:
            log_func(f"WSL distro discovery error: {exc}")
        return []


def _prioritize_wsl_distros(distros: list[str]) -> list[str]:
    # FIX: Prefer Kali distro first while still checking all installed distros.
    kali = [d for d in distros if d.lower() == "kali-linux"]
    others = [d for d in distros if d.lower() != "kali-linux"]
    ubuntu = [d for d in others if d.lower() == "ubuntu"]
    remaining = [d for d in others if d.lower() != "ubuntu"]
    return kali + ubuntu + remaining


def _hydra_available_wsl_distro(distro: str, log_func=None) -> bool:
    try:
        res = _run_cmd(["wsl", "-d", distro, "hydra", "--version"], timeout=20)
        if res.returncode == 0:
            if log_func:
                log_func(f"Found Hydra in WSL {distro}")
            return True
        if log_func:
            log_func(f"Hydra not found in WSL {distro}")
        return False
    except Exception as exc:
        if log_func:
            log_func(f"Hydra check failed for WSL {distro}: {exc}")
        return False


def _install_hydra_wsl(log_func=None) -> bool:
    # FIX: Install into preferred available distro (Kali first), not hard-coded Ubuntu.
    distros = _prioritize_wsl_distros(_list_wsl_distros(log_func=log_func))
    if not distros:
        return False
    target = distros[0]
    wsl_user = str(config.get("wsl_username", "")).strip()
    wsl_pass = str(config.get("wsl_password", ""))
    try:
        if log_func:
            log_func(f"Hydra missing in WSL; attempting automatic install on {target}...")
        install_command = (
            "DEBIAN_FRONTEND=noninteractive "
            "apt-get update -y && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y hydra"
        )
        sudo_install = build_wsl_sudo_command(
            install_command,
            password=wsl_pass,
            non_interactive=not bool(wsl_pass),
        )

        attempts = 3
        res = None
        for attempt in range(1, attempts + 1):
            res = _run_cmd(build_wsl_command(sudo_install, distro=target, username=wsl_user), timeout=600)
            if res.returncode == 0:
                break
            output = f"{res.stderr or ''}\n{res.stdout or ''}"
            if _is_apt_lock_error(output) and attempt < attempts:
                if log_func:
                    log_func(f"WSL apt is busy (attempt {attempt}/{attempts}); retrying in 5s...")
                time.sleep(5)
                continue
            break

        ok = bool(res) and res.returncode == 0
        if log_func:
            output = f"{res.stderr or ''}\n{res.stdout or ''}" if res else ""
            if ok:
                log_func("WSL Hydra install completed.")
            elif _is_sudo_auth_error(output):
                log_func(
                    "WSL Hydra install failed: sudo authentication/permissions required in WSL "
                    "(set wsl_password in settings or install hydra manually with sudo)."
                )
            elif not wsl_pass:
                log_func("WSL Hydra install failed: sudo password required for auto-install (set wsl_password in settings).")
            else:
                log_func(f"WSL Hydra install failed: {(res.stderr or res.stdout).strip()[:220]}")
        if ok:
            config["wsl_hydra_distro"] = target
        return ok
    except Exception as exc:
        if log_func:
            log_func(f"WSL Hydra install error: {exc}")
        return False


def _download_hydra_windows_binary(log_func=None):
    try:
        # FIX: Use maintained Windows Hydra releases feed and latest ZIP asset.
        resp = requests.get(HYDRA_WINDOWS_RELEASES_API, timeout=30)
        resp.raise_for_status()
        release = resp.json()
    except Exception as exc:
        if log_func:
            log_func(f"Hydra release lookup failed: {exc}")
        return None

    assets = release.get("assets") or []
    preferred = [a for a in assets if (a.get("name") or "").lower().endswith(".zip")]
    if not preferred:
        if log_func:
            log_func("No Windows Hydra ZIP asset found on latest maaaaz/thc-hydra-windows release.")
        return None

    preferred.sort(key=lambda a: ("hydra" not in (a.get("name") or "").lower(), len(a.get("name") or "")))
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
            if log_func:
                log_func(f"Hydra extracted to {file.parent}")
            return file
    except Exception as exc:
        if log_func:
            log_func(f"Hydra Windows install failed: {exc}")
    return None


def _add_dir_to_path_windows(path_obj: Path) -> dict:
    """Add a directory to PATH for current process and try persisting via setx."""
    target = str(path_obj)
    path_now = os.environ.get("PATH", "")
    if target.lower() in path_now.lower():
        return {"session_updated": False, "persisted": False}

    merged = f"{path_now};{target}" if path_now else target
    os.environ["PATH"] = merged

    persisted = False
    try:
        res = subprocess.run(
            ["setx", "PATH", merged],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        persisted = res.returncode == 0
    except Exception:
        persisted = False
    return {"session_updated": True, "persisted": persisted}


def ensure_hydra_available(log_func=None):
    """Ensure Hydra is callable, preferring WSL on Windows when available."""
    # FIX: Delegate to unified startup/setup flow so GUI and main share robust detection.
    status = check_and_setup_hydra(log_func=log_func)
    return {"available": status.get("available", False), "mode": status.get("mode"), "message": status.get("message", "")}


def check_and_setup_hydra(log_func=None):
    # FIX: Robust Hydra auto-detection/install for WSL + native Windows fallback.
    prefer_wsl = bool(config.get("prefer_wsl_hydra", True))
    auto_install = bool(config.get("auto_install_hydra", True))
    os.environ.pop("PARSERPRO_WSL_DISTRO", None)
    os.environ.pop("PARSERPRO_HYDRA_MODE", None)
    config["runner_enabled"] = True

    if prefer_wsl and _wsl_available():
        distros = _prioritize_wsl_distros(_list_wsl_distros(log_func=log_func))
        for distro in distros:
            if _hydra_available_wsl_distro(distro, log_func=log_func):
                config["wsl_hydra_distro"] = distro
                os.environ["PARSERPRO_HYDRA_MODE"] = "wsl"
                os.environ["PARSERPRO_WSL_DISTRO"] = distro
                return {"available": True, "mode": "wsl", "message": f"Found Hydra in WSL {distro}", "wsl_hydra_distro": distro}

        if auto_install and distros:
            install_target = distros[0]
            if log_func:
                log_func(f"Hydra not found in WSL distros; installing in {install_target}")
            if _install_hydra_wsl(log_func=log_func) and _hydra_available_wsl_distro(install_target, log_func=log_func):
                config["wsl_hydra_distro"] = install_target
                os.environ["PARSERPRO_HYDRA_MODE"] = "wsl"
                os.environ["PARSERPRO_WSL_DISTRO"] = install_target
                return {"available": True, "mode": "wsl", "message": f"Hydra installed in WSL {install_target}", "wsl_hydra_distro": install_target}

    if _hydra_available_native():
        os.environ["PARSERPRO_HYDRA_MODE"] = "native"
        return {"available": True, "mode": "native", "message": "Hydra available natively"}

    if auto_install and os.name == "nt":
        hydra_exe = _download_hydra_windows_binary(log_func=log_func)
        if hydra_exe:
            path_result = _add_dir_to_path_windows(hydra_exe.parent)
            verified = shutil.which("hydra") is not None
            message = "Hydra installed natively on Windows"
            if path_result.get("session_updated") and not path_result.get("persisted"):
                message += "; session PATH updated (run setx PATH to persist and restart shells)"
            elif path_result.get("persisted"):
                message += "; PATH persisted via setx (restart shells may be required)"
            if log_func:
                log_func(f"Native Hydra verification via which('hydra'): {verified}")
            if verified:
                os.environ["PARSERPRO_HYDRA_MODE"] = "native"
            else:
                message = "Hydra extract completed but hydra command was not found on PATH"
            return {
                "available": verified,
                "mode": "native",
                "message": message,
                "path_updated": bool(path_result.get("session_updated")),
                "path_persisted": bool(path_result.get("persisted")),
            }

    config["runner_enabled"] = False
    config["hydra_unavailable_message"] = "Hydra not found (native or WSL)"
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
            path_result = _add_dir_to_path_windows(path_obj.parent)
            if log_func and path_result.get("session_updated") and not path_result.get("persisted"):
                log_func("NordVPN path added for current session; run setx PATH for a permanent update.")
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
