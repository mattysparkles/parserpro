import argparse
import csv
import logging
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path

import tkinter as tk

try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:
    InsecureRequestWarning = Warning

from config import DATA_DIR, LOGS_DIR, config
from extract import extract_login_form
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


def run_headless_extract(input_path: Path, forms_output: Path, run_hydra: bool) -> int:
    logger = _build_headless_logger()
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
            ],
        )
        writer.writeheader()
        writer.writerows(forms)

    if run_hydra:
        for form in forms:
            cmd = form["hydra_command_template"].replace("{{combo_file}}", str((DATA_DIR / form["combo_file"]).resolve()))
            if bool(config.get("use_burp", False)) and config.get("burp_proxy", "").strip() and " -p " not in f" {cmd} ":
                cmd = f"{cmd} -p {config.get('burp_proxy').strip()}"
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

    root = tk.Tk()
    CombinedParserGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
