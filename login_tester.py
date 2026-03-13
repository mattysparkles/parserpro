from pathlib import Path
from urllib.parse import urlparse

from config import HITS_DIR


def hydra_module_for_method(method: str):
    m = (method or "post").lower()
    if m == "get":
        return "http-get-form"
    if m == "post":
        return "http-post-form"
    return None


def hydra_runtime_flags_for_method(method: str):
    """Return sane default Hydra runtime flags per form method."""
    m = (method or "post").lower()
    if m == "get":
        # GET workflows are often redirect-heavy/noisy; conservative defaults improve stability.
        return "-u -I -t 2 -w 10 -f -V"
    return "-t 4 -f -V"


def save_hit(domain, username, password, method):
    safe = (domain or "unknown").replace(":", "_")
    out = HITS_DIR / f"hits_{safe}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(f"{username}:{password} method={method}\n")
    return out


def domain_from_url(url):
    return (urlparse(url).netloc or "unknown").replace(".", "_")
