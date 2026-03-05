import re
import hashlib
from urllib.parse import urlparse, urlunparse

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
]

COMMON_LOGIN_PATHS = ["/login", "/signin", "/account/login", "/auth/login", "/user/login", "/session/new"]


def normalize_site(raw):
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"^[A-Z]{2}\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(https?://\s*)+", "", s)
    s = re.sub(r"\s+", "", s)
    s = s.strip("/.")

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
        if not p.netloc or len(p.netloc) < 4:
            return None
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
