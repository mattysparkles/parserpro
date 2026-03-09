import re
import hashlib
from urllib.parse import urlparse, urlunparse

from app_logging import log_once


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

COMMON_LOGIN_PATHS = ["/login", "/signin", "/account/login", "/auth/login", "/user/login", "/session/new"]


def _strip_domain_suffix_noise(host: str) -> str:
    if not host:
        return host
    match = re.match(r"^([A-Za-z0-9.-]+\.[A-Za-z]{2,})(\d+)$", host)
    if match:
        return match.group(1)
    return host


def _clean_target_candidate(raw) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""

    # Drop noisy source prefixes like "SA example.com" / "BR example.com".
    s = re.sub(r"^[A-Z]{2}\s+", "", s, flags=re.IGNORECASE)
    s = s.strip("\"'`[](){}<>|,; ")
    s = re.sub(r"^(https?://\s*)+", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.strip("/.")
    return s


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
            s = "https://" + s.lstrip("/")
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
            s = f"https://{s.lstrip('/')}"
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
            candidate = f"https:{candidate}"
        elif "." in candidate and not candidate.startswith("/"):
            candidate = f"https://{candidate.lstrip('/')}"
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

    netloc = host if not port else f"{host}:{port}"
    sanitized = parsed._replace(netloc=netloc)
    return urlunparse(sanitized), None
