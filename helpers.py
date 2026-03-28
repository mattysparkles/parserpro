import hashlib
import os
import random
import re
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from app_logging import log_once
from logging import write_detailed, write_privacy

TOR_SOCKS_PROXY = "socks5h://127.0.0.1:9050"
TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9050
ONION_REDACTION_HOST = "genericonionexample.onion"
SETTING_SCOPE_OPTIONS = ("clear_web", "onion_only", "both")
RELEVANT_SCOPE_KEYS = (
    "proxy_scope",
    "threads_scope",
    "timeout_scope",
    "captcha_scope",
    "validation_scope",
    "random_user_agent_scope",
    "user_agent_scope",
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.90 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.42 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/134.0.3124.68 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edg/135.0.3179.17 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.89 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.41 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15.0; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.88 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.40 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.95 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 16; Pixel 10 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.48 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 15; SM-S938U) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/27.0 Chrome/134.0.6998.90 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 16; SM-S948U) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/135.0.7049.44 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro XL; rv:135.0) Gecko/135.0 Firefox/135.0",
    "Mozilla/5.0 (Linux; Android 16; Pixel 10; rv:136.0) Gecko/136.0 Firefox/136.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.90 YaBrowser/25.1.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.42 Brave/1.76.74 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.41 Brave/1.76.74 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.40 Vivaldi/7.2.3621.45 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.42 OPR/117.0.5408.53 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.89 OPR/116.0.5366.35 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 15; OnePlus 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.95 Mobile Safari/537.36 EdgA/134.0.3124.73",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/135.0.7049.45 Mobile/15E148 Safari/604.1",
]

COMMON_LOGIN_PATHS = ["/login", "/signin", "/account/login", "/auth/login", "/user/login", "/session/new"]


def _strip_domain_suffix_noise(host: str) -> str:
    if not host:
        return host
    if host.lower().endswith(".onion"):
        return host.lower()
    match = re.match(r"^([A-Za-z0-9.-]+\.[A-Za-z]{2,})(\d+)$", host)
    if match:
        return match.group(1)
    return host


def _clean_target_candidate(raw) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"^[A-Z]{2}\s+", "", s, flags=re.IGNORECASE)
    s = s.strip("\"'`[](){}<>|,; ")
    s = re.sub(r"^(https?://\s*)+", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.strip("/.")
    return s


def is_onion_host(host: str | None) -> bool:
    return bool(host and str(host).lower().strip().endswith(".onion"))


def is_onion_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        candidate = str(value).strip()
        if candidate.startswith(("http://", "https://")):
            return is_onion_host(urlparse(candidate).hostname)
        return ".onion" in candidate.lower()
    except Exception:
        return False


def redact_onion_value(message: str) -> str:
    return re.sub(r"([a-z2-7]{16,56}\.onion)", ONION_REDACTION_HOST, message or "", flags=re.IGNORECASE)


def get_user_agent_library(cfg=None):
    loaded = list(USER_AGENTS)
    custom = list((cfg or {}).get("user_agent_library") or [])
    for ua in custom:
        value = str(ua or "").strip()
        if value and value not in loaded:
            loaded.append(value)
    return loaded


def resolve_user_agent(cfg=None, *, target_url: str | None = None, override: str | None = None):
    cfg = cfg or {}
    if override and str(override).strip():
        return str(override).strip()
    if not scope_applies(cfg.get("random_user_agent_scope", "both"), target_url):
        random_enabled = False
    else:
        random_enabled = bool(cfg.get("random_user_agent", False))
    library = get_user_agent_library(cfg)
    if random_enabled and library:
        return random.choice(library)
    if not scope_applies(cfg.get("user_agent_scope", "both"), target_url):
        return library[0] if library else "ParserPro/1.0"
    selected = str(cfg.get("selected_user_agent", "")).strip()
    if selected:
        return selected
    custom = str(cfg.get("custom_user_agent", "")).strip()
    if custom:
        return custom
    return library[0] if library else "ParserPro/1.0"


def scope_applies(scope_value: str | None, target_url: str | None) -> bool:
    scope = str(scope_value or "both").strip().lower()
    if scope not in SETTING_SCOPE_OPTIONS:
        scope = "both"
    target_is_onion = is_onion_url(target_url)
    if scope == "both":
        return True
    if scope == "onion_only":
        return target_is_onion
    return not target_is_onion


def get_scoped_value(cfg, key, default, target_url: str | None = None, scope_key: str | None = None):
    if scope_key and not scope_applies((cfg or {}).get(scope_key, "both"), target_url):
        return default
    return (cfg or {}).get(key, default)


def tor_proxy_dict():
    return {"server": TOR_SOCKS_PROXY}


def is_tor_running(host: str = TOR_SOCKS_HOST, port: int = TOR_SOCKS_PORT, timeout: float = 1.5) -> bool:
    from tor_manager import is_tor_running as _is_tor_running

    return _is_tor_running(port=port, host=host, timeout=timeout)


def get_tor_launch_candidates(cfg=None):
    from tor_manager import detect_tor_executable

    cfg = cfg or {}
    candidates = []
    configured = str(cfg.get("tor_executable_path", "")).strip()
    if configured:
        candidates.append(configured)
    detected = detect_tor_executable()
    if detected:
        candidates.append(detected)
    candidates.extend([
        shutil_which("tor"),
        shutil_which("tor.exe"),
        r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
        r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.real.exe",
    ])
    out = []
    seen = set()
    for c in candidates:
        if c and str(c).lower() not in seen:
            seen.add(str(c).lower())
            out.append(c)
    return out


def shutil_which(name: str):
    try:
        from shutil import which
        return which(name)
    except Exception:
        return None


def start_tor_process(cfg=None):
    from tor_manager import start_tor

    cfg = cfg or {}
    tor_path = str(cfg.get("tor_executable_path", "")).strip() or None
    return start_tor(tor_path=tor_path, socks_port=TOR_SOCKS_PORT)


def classify_onion_reachability(url: str, user_agent: str | None = None, timeout: int = 45):
    ua = user_agent or resolve_user_agent({}, target_url=url)
    if not is_tor_running():
        message = f"TOR STATUS {url} => tor_error (Tor not running, expected {TOR_SOCKS_PROXY}) ua={ua}"
        write_detailed(message, level="WARN")
        write_privacy(redact_onion_value(message), level="WARN")
        return {"status": "tor_error", "code": None, "detail": "Tor not running", "user_agent": ua}
    try:
        response = requests.get(
            url,
            headers={"User-Agent": ua},
            proxies={"http": TOR_SOCKS_PROXY, "https": TOR_SOCKS_PROXY},
            timeout=timeout,
            allow_redirects=True,
            verify=False,
        )
        code = int(response.status_code)
        status = "live" if 200 <= code <= 399 else "seized/down" if code in {403, 404} else "seized/down"
        message = f"TOR STATUS {url} => {status} http={code} ua={ua}"
        level = "INFO" if status == "live" else "WARN"
        write_detailed(message, level=level)
        write_privacy(redact_onion_value(message), level=level)
        return {"status": status, "code": code, "detail": f"HTTP {code}", "user_agent": ua}
    except requests.exceptions.ProxyError as exc:
        message = f"TOR STATUS {url} => tor_error proxy={TOR_SOCKS_PROXY} detail={exc} ua={ua}"
        write_detailed(message, level="WARN")
        write_privacy(redact_onion_value(message), level="WARN")
        return {"status": "tor_error", "code": None, "detail": str(exc), "user_agent": ua}
    except requests.exceptions.ConnectionError as exc:
        text = str(exc).lower()
        status = "seized/down" if any(flag in text for flag in ("refused", "name or service not known", "failed to resolve", "name resolution")) else "tor_error"
        message = f"TOR STATUS {url} => {status} detail={exc} ua={ua}"
        level = "WARN"
        write_detailed(message, level=level)
        write_privacy(redact_onion_value(message), level=level)
        return {"status": status, "code": None, "detail": str(exc), "user_agent": ua}
    except Exception as exc:
        message = f"TOR STATUS {url} => seized/down detail={exc} ua={ua}"
        write_detailed(message, level="WARN")
        write_privacy(redact_onion_value(message), level="WARN")
        return {"status": "seized/down", "code": None, "detail": str(exc), "user_agent": ua}


def normalize_site(raw):
    s = _clean_target_candidate(raw)
    if not s:
        return None
    if re.match(r"^[^@]+@[^:]+:[^:]+$", s):
        return None
    match = re.search(r"(https?://[^\s\'\"]+)", s)
    if match:
        s = match.group(1)
    if not s.startswith(("http://", "https://")):
        if s.startswith("//"):
            s = "https:" + s
        elif "." in s and not s.startswith("/"):
            s = "http://" + s.lstrip("/") if s.lower().endswith(".onion") else "https://" + s.lstrip("/")
        else:
            return None
    try:
        p = urlparse(s)
        host = (p.hostname or "").strip(".")
        if not host or len(host) < 4:
            return None
        host = _strip_domain_suffix_noise(host)
        netloc = host
        if p.port:
            netloc = f"{host}:{p.port}"
        elif ":" in p.netloc and p.netloc.rsplit(":", 1)[-1].isdigit():
            netloc = p.netloc
        p = p._replace(netloc=netloc)
        if "referer" in p.query.lower() or len(p.query) > 150:
            p = p._replace(query="", fragment="")
        return urlunparse(p)
    except Exception:
        return None


def get_base_url(url):
    if not url:
        return None
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def get_site_filename(base_url):
    domain = base_url.split("//")[-1].split("/")[0].strip().lower()
    domain = domain.replace("www.", "")
    safe_domain = re.sub(r"[^a-z0-9._-]", "_", domain).strip("._-")
    if not safe_domain:
        fallback = hashlib.sha1(base_url.encode("utf-8", errors="ignore")).hexdigest()[:12]
        safe_domain = f"site_{fallback}"
    return f"{safe_domain}.txt"


def split_three_fields(line):
    parts = line.rsplit(":", 2)
    if len(parts) != 3:
        return None
    return [p.strip() for p in parts]


def validate_url(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    lowered = s.lower()
    bad_markers = ["{", "}", "\n", "tostring:function", "[object", "major:"]
    if any(marker in lowered for marker in bad_markers):
        return None
    if " " in s:
        return None
    if not s.startswith(("http://", "https://")):
        if "." in s and not s.startswith("/"):
            s = f"{'http' if s.lower().endswith('.onion') else 'https'}://{s.lstrip('/')}"
        else:
            return None
    try:
        parsed = urlparse(s)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunparse(parsed)


def normalize_and_validate_target(raw, allow_nonstandard_ports=False):
    if raw is None:
        return None, "empty target"
    raw_text = str(raw)
    if "\n" in raw_text or "\r" in raw_text:
        return None, "invalid target format"
    candidate = _clean_target_candidate(raw)
    if not candidate:
        return None, "empty target"
    lowered = candidate.lower()
    bad_markers = ["{", "}", "\n", "tostring:function", "[object", "major:"]
    if any(marker in lowered for marker in bad_markers):
        return None, "invalid target format"
    if "://" in candidate and not candidate.startswith(("http://", "https://")):
        return None, "unsupported scheme"
    if not candidate.startswith(("http://", "https://")):
        if candidate.startswith("//"):
            candidate = f"{'http' if '.onion' in candidate.lower() else 'https'}:{candidate}"
        elif "." in candidate and not candidate.startswith("/"):
            candidate = f"{'http' if candidate.lower().endswith('.onion') else 'https'}://{candidate.lstrip('/')}"
        else:
            return None, "missing http/https scheme"
    try:
        parsed = urlparse(candidate)
    except Exception:
        return None, "invalid URL parse"
    if parsed.scheme not in {"http", "https"}:
        return None, "unsupported scheme"
    if not parsed.netloc:
        return None, "missing host"
    host = (parsed.hostname or "").strip(".")
    if not host:
        return None, "missing host"
    host = _strip_domain_suffix_noise(host)
    if "." not in host:
        return None, "invalid host"
    try:
        port = parsed.port
    except ValueError:
        return None, "invalid port"
    if port and port not in {80, 443} and not allow_nonstandard_ports:
        return None, "nonstandard port (likely non-web service)"
    scheme = parsed.scheme
    if is_onion_host(host) and scheme == "https":
        scheme = "http"
    netloc = host if not port else f"{host}:{port}"
    sanitized = parsed._replace(scheme=scheme, netloc=netloc)
    return urlunparse(sanitized), None
