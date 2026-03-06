import argparse
import csv
import logging
import os
import shutil
import subprocess
import sys
import webbrowser
import warnings
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox


try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:
    InsecureRequestWarning = Warning

from config import DATA_DIR, LOGS_DIR, check_and_setup_hydra, config
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




# FIX: startup checks for headless and GUI entry
def _log_note(notes: list[str], text: str, logger: logging.Logger | None = None) -> None:
    notes.append(text)
    if logger:
        logger.info(text)
    else:
        print(f"[startup] {text}")


def check_and_setup_prerequisites(logger: logging.Logger | None = None) -> list[str]:
    # FIX: Call centralized Hydra setup before UI loop/headless extraction.
    notes: list[str] = []
    hydra_status = check_and_setup_hydra(log_func=(logger.info if logger else None))
    _log_note(notes, f"Hydra check: {hydra_status.get('message')}", logger)

    if not hydra_status.get("available"):
        warn = "Hydra is still unavailable. GUI will load, but Hydra Runner is disabled."
        notes.append(warn)
        config["runner_enabled"] = False
        config["hydra_unavailable_message"] = warn
        if not logger:
            messagebox.showwarning("Hydra unavailable", warn)

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
