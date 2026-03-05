import json
import platform
import tarfile
import zipfile
from pathlib import Path

import requests


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LEGACY_CONFIG_FILE = APP_DIR / "config.json"
LEGACY_PROCESSED_SITES_FILE = APP_DIR / "processed_sites.json"
CONFIG_FILE = DATA_DIR / "config.json"
PROCESSED_SITES_FILE = DATA_DIR / "processed_sites.json"
GOST_RELEASE_API = "https://api.github.com/repos/ginuerzh/gost/releases/latest"
GOST_ARCHIVE_CACHE = DATA_DIR / "downloads"
GOST_ARCHIVE_CACHE.mkdir(parents=True, exist_ok=True)


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
    return loaded


config = load_config()


def save_config():
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def download_gost():
    if GOST_EXE.exists():
        return GOST_EXE

    asset = _get_matching_gost_asset()
    if not asset:
        print("Could not determine a valid gost release asset for this platform; continuing without proxy.")
        return None

    archive_name = asset["name"]
    archive_path = GOST_ARCHIVE_CACHE / archive_name
    if not archive_path.exists():
        print(f"Downloading gost archive: {archive_name}")
        try:
            with requests.get(asset["browser_download_url"], stream=True, timeout=60) as r:
                r.raise_for_status()
                with archive_path.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
        except Exception as e:
            print(f"Failed to download gost ({e}); continuing without proxy.")
            return None

    try:
        _extract_gost_archive(archive_path)
    except Exception as e:
        print(f"Failed to extract gost archive ({e}); continuing without proxy.")
        return None

    if GOST_EXE.exists():
        if platform.system().lower() != "windows":
            GOST_EXE.chmod(0o755)
        print(f"gost ready at {GOST_EXE}")
        return GOST_EXE

    print("gost archive extracted but binary was not found; continuing without proxy.")
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
        print(f"Failed to query gost releases API ({e}); continuing without proxy.")
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
