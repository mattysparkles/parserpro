import json
import zipfile
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LEGACY_CONFIG_FILE = APP_DIR / "config.json"
CONFIG_FILE = DATA_DIR / "config.json"
PROCESSED_SITES_FILE = DATA_DIR / "processed_sites.json"
GOST_EXE = DATA_DIR / "gost.exe"
GOST_ZIP_URL = "https://github.com/ginuerzh/gost/releases/download/v3.0.0-rc.10/gost_3.0.0-rc.10_windows_amd64.zip"


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


def load_config():
    _migrate_legacy_config_once()
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


config = load_config()


def save_config():
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def download_gost():
    if GOST_EXE.exists():
        return
    print("Downloading gost...")
    zip_path = DATA_DIR / "gost.zip"
    import requests

    with requests.get(GOST_ZIP_URL, stream=True) as r:
        r.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(DATA_DIR)
    for file in DATA_DIR.glob("gost*"):
        if file.name.endswith(".exe"):
            file.rename(GOST_EXE)
            break
    zip_path.unlink(missing_ok=True)
    print("gost downloaded and extracted.")
