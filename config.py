import json
import zipfile
from pathlib import Path


CONFIG_FILE = Path("config.json")
GOST_EXE = Path("gost.exe")
GOST_ZIP_URL = "https://github.com/ginuerzh/gost/releases/download/v3.0.0-rc.10/gost_3.0.0-rc.10_windows_amd64.zip"


def load_config():
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
    zip_path = Path("gost.zip")
    import requests

    with requests.get(GOST_ZIP_URL, stream=True) as r:
        r.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(".")
    for file in Path(".").glob("gost*"):
        if file.name.endswith(".exe"):
            file.rename(GOST_EXE)
            break
    zip_path.unlink(missing_ok=True)
    print("gost downloaded and extracted.")
