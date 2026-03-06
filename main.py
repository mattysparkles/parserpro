import argparse
import csv
import logging
import os
import shutil
import subprocess
import sys
import webbrowser
import warnings
import zipfile
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

import requests

try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:
    InsecureRequestWarning = Warning

from config import APP_DIR, DATA_DIR, LOGS_DIR, config
from extract import extract_login_form
from fetch import ensure_chromedriver_available
from helpers import get_base_url, get_site_filename, normalize_site, split_three_fields
from gui import CombinedParserGUI

warnings.simplefilter("ignore", InsecureRequestWarning)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _build_headless_logger() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    session = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOGS_DIR / f"headless_{session}.log"
    logger = logging.getLogger("parserpro-headless")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh.setFormatter(fmt)
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    logger.info("headless log file: %s", log_file)
    return logger




# NEW: startup checks for headless and GUI entry
def _log_note(notes: list[str], text: str, logger: logging.Logger | None = None) -> None:
    # FIX: Centralized startup logging for prerequisite auto-setup.
    notes.append(text)
    if logger:
        logger.info(text)
    else:
        print(f"[startup] {text}")


def _list_wsl_distros(logger: logging.Logger | None = None) -> list[str]:
    # NEW: Hydra auto-setup - discover installed WSL distros.
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            _log_note([], f"wsl --list --quiet failed: {(result.stderr or result.stdout or '').strip()[:200]}", logger)
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception as exc:
        if logger:
            logger.warning("WSL distro discovery failed: %s", exc)
        return []


def _hydra_in_wsl_distro(distro: str, logger: logging.Logger | None = None) -> bool:
    # NEW: Hydra auto-setup - check hydra in a specific distro.
    try:
        result = subprocess.run(["wsl", "-d", distro, "hydra", "--version"], capture_output=True, text=True, timeout=20)
        if logger and result.returncode != 0:
            logger.info("Hydra not found in WSL distro '%s'.", distro)
        return result.returncode == 0
    except Exception as exc:
        if logger:
            logger.info("Hydra check failed for WSL distro '%s': %s", distro, exc)
        return False


def _install_hydra_wsl_distro(distro: str, logger: logging.Logger | None = None) -> bool:
    # NEW: Hydra auto-setup - install Hydra in first available distro.
    command = ["wsl", "-d", distro, "--", "bash", "-lc", "sudo apt update && sudo apt install -y hydra"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=1200)
        if result.returncode == 0:
            if logger:
                logger.info("Hydra install succeeded in WSL distro '%s'.", distro)
            return True
        err = (result.stderr or result.stdout or "").strip()
        if logger:
            logger.warning("Hydra install failed in WSL distro '%s': %s", distro, err[:320])
    except Exception as exc:
        if logger:
            logger.warning("Hydra install exception in WSL distro '%s': %s", distro, exc)
    return False


def _install_hydra_windows(logger: logging.Logger | None = None) -> bool:
    # NEW: Hydra auto-setup - fallback to native Windows Hydra package.
    tools_dir = APP_DIR / "tools" / "hydra"
    tools_dir.mkdir(parents=True, exist_ok=True)
    zip_url = "https://github.com/maaaaz/thc-hydra-windows/releases/download/v9.1/hydra-9.1-win.zip"
    try:
        archive = tools_dir / "hydra-9.1-win.zip"
        with requests.get(zip_url, stream=True, timeout=120) as req:
            req.raise_for_status()
            with archive.open("wb") as fh:
                for chunk in req.iter_content(8192):
                    if chunk:
                        fh.write(chunk)
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(tools_dir)
    except Exception as exc:
        if logger:
            logger.warning("Native Windows Hydra install failed: %s", exc)
        return False

    hydra_exe = next(tools_dir.rglob("hydra.exe"), None)
    if not hydra_exe:
        if logger:
            logger.warning("Hydra install archive extracted but hydra.exe was not found.")
        return False
    hydra_dir = str(hydra_exe.parent)
    if hydra_dir.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = f"{hydra_dir};{os.environ.get('PATH', '')}" if os.environ.get("PATH") else hydra_dir
    return True


def check_and_setup_prerequisites(logger: logging.Logger | None = None) -> list[str]:
    # NEW: Hydra auto-setup
    notes: list[str] = []

    os.environ.pop("PARSERPRO_WSL_DISTRO", None)
    os.environ.pop("PARSERPRO_HYDRA_MODE", None)
    config["runner_enabled"] = True

    hydra_ready = False
    if os.name == "nt":
        distros = _list_wsl_distros(logger=logger)
        _log_note(notes, f"Detected WSL distros: {', '.join(distros) if distros else 'none'}", logger)
        selected_distro = None
        for distro in distros:
            _log_note(notes, f"Checking Hydra in WSL distro '{distro}'", logger)
            if _hydra_in_wsl_distro(distro, logger=logger):
                selected_distro = distro
                break

        if selected_distro:
            os.environ["PARSERPRO_HYDRA_MODE"] = "wsl"
            os.environ["PARSERPRO_WSL_DISTRO"] = selected_distro
            hydra_ready = True
            _log_note(notes, f"Hydra detected in WSL distro '{selected_distro}'", logger)
        elif distros:
            target_distro = distros[0]
            _log_note(notes, f"Hydra not found in any distro; attempting install in '{target_distro}'", logger)
            if _install_hydra_wsl_distro(target_distro, logger=logger) and _hydra_in_wsl_distro(target_distro, logger=logger):
                os.environ["PARSERPRO_HYDRA_MODE"] = "wsl"
                os.environ["PARSERPRO_WSL_DISTRO"] = target_distro
                hydra_ready = True
                _log_note(notes, f"Hydra installed in WSL distro '{target_distro}'", logger)
                if not logger:
                    messagebox.showinfo("Hydra setup", f"Hydra was installed in WSL distro '{target_distro}'.")

        if not hydra_ready:
            _log_note(notes, "Falling back to native Windows Hydra setup", logger)
            hydra_ready = _install_hydra_windows(logger=logger) or shutil.which("hydra") is not None
            if hydra_ready:
                os.environ["PARSERPRO_HYDRA_MODE"] = "native"
                _log_note(notes, "Native Windows Hydra is ready", logger)
                if not logger:
                    messagebox.showinfo("Hydra setup", "Hydra native binary is installed. Restart the app if command shell PATH was stale.")
    else:
        hydra_ready = shutil.which("hydra") is not None
        if hydra_ready:
            os.environ["PARSERPRO_HYDRA_MODE"] = "native"
            _log_note(notes, "Hydra available natively", logger)

    if not hydra_ready:
        warn = "Hydra is still unavailable. GUI will load, but Hydra Runner is disabled."
        notes.append(warn)
        config["runner_enabled"] = False
        config["hydra_unavailable_message"] = warn

    try:
        import webdriver_manager.chrome  # noqa: F401

        _log_note(notes, "Selenium/WebDriverManager import check passed.", logger)
    except Exception as exc:
        notes.append(f"webdriver_manager not available: {exc}. Install via `pip install webdriver-manager selenium`.")

    chromedriver_ok, chromedriver_msg, _ = ensure_chromedriver_available()
    if chromedriver_ok:
        _log_note(notes, "Chromedriver check passed.", logger)
    else:
        notes.append(f"Chromedriver setup warning: {chromedriver_msg}")

    nord_candidates = [
        shutil.which("nordvpn"),
        shutil.which("nordvpncli"),
        r"C:\Program Files\NordVPN\nordvpn.exe",
        r"C:\Program Files\NordVPN\NordVPN.exe",
    ]
    nord_ok = any(candidate and (os.path.sep not in str(candidate) or Path(candidate).exists()) for candidate in nord_candidates)
    if nord_ok:
        _log_note(notes, "NordVPN CLI check passed.", logger)
    else:
        notes.append("NordVPN CLI not found. Download from https://nordvpn.com/download/windows/ if VPN control is required.")
        try:
            webbrowser.open("https://nordvpn.com/download/windows/")
        except Exception:
            pass

    return notes

def run_headless_extract(input_path: Path, forms_output: Path, run_hydra: bool) -> int:
    logger = _build_headless_logger()
    for warning in check_and_setup_prerequisites(logger):
        if "failed" in warning.lower() or "warning" in warning.lower() or "not found" in warning.lower():
            logger.warning(warning)
    rows = []
    site_combos = {}
    for raw in input_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = split_three_fields(raw.strip())
        if not parts:
            continue
        rows.append(parts)
        site = normalize_site(parts[0])
        base = get_base_url(site) if site else None
        if base:
            site_combos.setdefault(base, set()).add(f"{parts[1]}:{parts[2]}")

    for base, combos in site_combos.items():
        combo_path = DATA_DIR / get_site_filename(base)
        combo_path.write_text("\n".join(sorted(combos)) + "\n", encoding="utf-8")

    forms = []
    for base in sorted(site_combos.keys()):
        form_data, error = extract_login_form(base, strict_validation=True, mode=str(config.get("analysis_mode", "static")))
        if form_data and form_data.get("hydra_command_template"):
            forms.append(
                {
                    "original_url": form_data.get("original_url"),
                    "base_url": base,
                    "used_url": base,
                    "used_type": "base",
                    "action": form_data.get("action"),
                    "post_data": form_data.get("post_data"),
                    "failure_condition": form_data.get("failure_condition"),
                    "hydra_command_template": form_data.get("hydra_command_template"),
                    "combo_file": get_site_filename(base),
                    "full_hydra_command": form_data.get("hydra_command_template", "").replace("{{combo_file}}", get_site_filename(base)),
                    "confidence": form_data.get("confidence"),
                    "validation_reason": form_data.get("validation_reason"),
                    "method": form_data.get("method", "post"),
                    "method_warning": form_data.get("method_warning", ""),
                }
            )
            logger.info("success: %s", base)
        else:
            logger.warning("failed/no-form: %s :: %s", base, error)

    forms_output.parent.mkdir(parents=True, exist_ok=True)
    with forms_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "original_url",
                "base_url",
                "used_url",
                "used_type",
                "action",
                "post_data",
                "failure_condition",
                "hydra_command_template",
                "combo_file",
                "full_hydra_command",
                "confidence",
                "validation_reason",
                "method",
                "method_warning",
            ],
        )
        writer.writeheader()
        writer.writerows(forms)

    if run_hydra:
        for form in forms:
            cmd = form["hydra_command_template"].replace("{{combo_file}}", str((DATA_DIR / form["combo_file"]).resolve()))
            intercept_proxy = ""
            if bool(config.get("use_burp", False)):
                intercept_proxy = config.get("burp_proxy", "").strip()
            elif bool(config.get("use_zap", False)):
                intercept_proxy = config.get("zap_proxy", "").strip()
            if intercept_proxy and " -p " not in f" {cmd} ":
                cmd = f"{cmd} -p {intercept_proxy}"
            logger.info("running hydra: %s", cmd)
            subprocess.run(cmd, shell=True, check=False)

    logger.info("headless complete: %s forms", len(forms))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ParserPro GUI/headless runner")
    parser.add_argument("--extract", help="Path to combo input file")
    parser.add_argument("--forms-output", help="Output path for extracted forms CSV")
    parser.add_argument("--run-hydra", action="store_true", help="Execute hydra after extraction")
    parser.add_argument("--headless", action="store_true", help="Run extraction without GUI")
    args = parser.parse_args()

    if args.headless:
        if not args.extract or not args.forms_output:
            raise SystemExit("--headless requires --extract and --forms-output")
        raise SystemExit(run_headless_extract(Path(args.extract), Path(args.forms_output), args.run_hydra))

    startup_notes = check_and_setup_prerequisites()
    startup_warnings = [n for n in startup_notes if any(flag in n.lower() for flag in ("failed", "warning", "not found"))]
    if startup_warnings:
        root_warn = tk.Tk()
        root_warn.withdraw()
        messagebox.showwarning("ParserPro startup prerequisites", "\n".join(startup_warnings))
        root_warn.destroy()
    root = tk.Tk()
    CombinedParserGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
