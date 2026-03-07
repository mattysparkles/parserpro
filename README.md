# ParserPro

ParserPro is a desktop + CLI workflow tool for **credential-list normalization**, **login-form extraction**, and **Hydra command preparation/execution**.

> ⚠️ **Legal/Ethical Use Only:** Use ParserPro only on systems you own or where you have explicit written authorization to test.

---

## Table of Contents

1. [What ParserPro Does](#what-parserpro-does)
2. [Feature Inventory (Detailed)](#feature-inventory-detailed)
3. [Project Layout](#project-layout)
4. [Requirements](#requirements)
5. [Quick Start for Newbies](#quick-start-for-newbies)
6. [First Run Walkthrough (GUI)](#first-run-walkthrough-gui)
7. [Headless/CLI Usage](#headlesscli-usage)
8. [Input/Output Formats](#inputoutput-formats)
9. [Settings Reference (Every Option)](#settings-reference-every-option)
10. [Hydra Integration Details](#hydra-integration-details)
11. [Proxy, Burp, and ZAP Workflows](#proxy-burp-and-zap-workflows)
12. [Troubleshooting Guide](#troubleshooting-guide)
13. [Testing / Smoke Checks](#testing--smoke-checks)
14. [Safety, Data Handling, and Privacy Notes](#safety-data-handling-and-privacy-notes)
15. [Developer Notes](#developer-notes)

---

## What ParserPro Does

At a high level, ParserPro turns raw combo lists into structured, testable artifacts:

1. Reads combo lines in `site:user:password` format.
2. Normalizes target sites/URLs.
3. Groups credentials by site and writes per-site combo files.
4. Visits targets and attempts login-form extraction.
5. Classifies extraction outcomes (actionable form, login-ish page, no form, fetch failure).
6. Generates Hydra command templates per target.
7. Lets you run Hydra from the GUI runner or from headless mode.
8. Records logs, timeline events, and run summaries for diagnostics/export.

---

## Feature Inventory (Detailed)

This section explicitly lists major features available in the codebase.

### 1) Combo ingestion and normalization

- Accepts a single input file or a folder of `.txt` files.
- Parses lines into three fields (`site`, `username`, `password`).
- Optional cleanup controls:
  - skip blank lines
  - trim whitespace
- Normalizes site hostnames/URLs and computes base URLs.
- Exports consolidated CSV plus per-site combo files (`data/<site>.txt`).

### 2) Extraction pipeline

- Optional login-form extraction per target.
- Uses fetch/extract modules to identify likely user/pass fields.
- Supports strict validation mode.
- Supports cache/TTL logic to skip fresh already-processed sites.
- Supports force recheck override.
- Supports a configurable analysis mode (`static` / `observation`).
- Captures validation reason, confidence, method, warnings, and failure metadata.

### 3) Hydra command generation

- Produces a `hydra_forms.csv` containing discovered form metadata.
- Builds command templates for:
  - `http-post-form` when method is POST
  - `http-get-form` when method is GET
- Emits method warnings for GET forms (manual tuning may be needed).
- Stores runnable command templates with combo placeholder replacement.

### 4) Hydra runner UI (command orchestrator)

- Dedicated **Hydra Runner** tab with table + log pane.
- Row selection with checkbox column (`[ ]` / `[x]`).
- Selection helpers:
  - Select All (Filtered)
  - Deselect All (Filtered)
  - Invert Selection (Filtered)
- Run modes:
  - Run Selected
  - Run All (Filtered)
- Filters:
  - minimum combos
  - status (All/Pending/Running/Failed/Success)
  - minimum hits
  - last run state (All/Never Run/Has Run)
- Sorting by table headers (numeric and text aware).
- Pause/resume and cancel controls.
- Timeout/cleanup support for running subprocesses.

### 5) Startup dependency checks and auto-setup

- Startup checks for Hydra availability.
- Chromedriver auto-setup (via `webdriver_manager`) when enabled.
- NordVPN CLI presence check.
- On Windows, Hydra setup path includes:
  - native hydra detection
  - WSL distro probing and hydra validation
  - optional install path attempts (if enabled)
  - native fallback download/extract path attempts
- Runner can be disabled with clear warning if Hydra remains unavailable.

### 6) Proxy, VPN, and routing controls

- Supports `proxy_url` usage with reachability checks.
- `proxy_required` can fail-fast when proxy is unavailable.
- Optional proxy rotation from a proxy list file.
- Burp routing support (`use_burp`, `burp_proxy`).
- ZAP routing support (`use_zap`, `zap_proxy`, optional API key).
- Intercept-proxy precedence logic (Burp first when enabled).
- VPN mode setting (`none` or `nordvpn`).

### 7) CAPTCHA provider integration

Supports provider keys/order settings for:
- DeathByCaptcha
- 2Captcha
- Anti-Captcha
- Capsolver

Provider availability is optional and degrades gracefully if missing.

### 8) Burp and ZAP tooling helpers

- Burp executable auto-detection + launch helper.
- ZAP executable auto-detection + launch helper (desktop or daemon).
- Export/import helper flows for target data.

### 9) Timeline, run summaries, and diagnostics

- Timeline event recording with categories, levels, actions, metrics.
- Timeline filtering and CSV export.
- Run summary generation with metrics, error category counts, latency stats.
- Troubleshooting panel groups fetch failures by category:
  - DNS
  - TLS
  - proxy
  - connection closed
  - other
- Diagnostic exports (CSV/JSON).

### 10) Project/session management

- New/Open/Save project support.
- Autosave worker + periodic autosave controls.
- Export reports as JSON/CSV.
- Maintains processed-site metadata and app settings snapshot.

### 11) Logging and output organization

- Timestamped logs under `logs/`.
- Hit files under `hits/` and `data/hits_<site>.txt`.
- Config and processed-site state under `data/`.

---

## Project Layout

- `main.py` - app entry point and CLI/headless argument handling.
- `gui.py` - main multi-tab Tkinter interface and extraction orchestration.
- `runner.py` - Hydra runner tab behaviors and execution controls.
- `extract.py` - form extraction logic.
- `fetch.py` - page fetch helpers and provider hooks.
- `helpers.py` - parsing/url helper utilities.
- `config.py` - config loading/defaults + environment/dependency setup.
- `burp.py` - Burp launcher and helpers.
- `zap.py` - ZAP launcher/import helpers.
- `proxies.py` - proxy rotation utility.
- `timeline.py` - timeline event model and utilities.
- `run_summary.py` - run summary model and metric computation.
- `project_io.py` - project/report export/import helpers.
- `tests/` - unit tests.

---

## Requirements

- Python 3.10+
- `pip`
- OS with GUI support for Tkinter (for desktop mode)
- Optional but recommended:
  - Playwright Chromium
  - Selenium dependencies / Chromedriver
  - Hydra
  - Burp Suite / OWASP ZAP

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install Playwright browser:

```bash
python -m playwright install chromium
```

---

## Quick Start for Newbies

If you are brand new, follow this exactly.

### Step 1) Clone and enter project

```bash
git clone <your-repo-url> parserpro
cd parserpro
```

### Step 2) Create virtual environment

**Linux/macOS**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### Step 3) Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### Step 4) Prepare input file

Create a text file (example `combos.txt`) with one item per line:

```text
example.com:alice@example.com:Password123
https://demo.site/login:bob:SuperSecret!
```

### Step 5) Start app

```bash
python main.py
```

### Step 6) Configure output paths in Extractor tab

- Choose input file/folder
- Choose combined CSV output path
- Choose forms CSV output path (`hydra_forms.csv` style)

### Step 7) Run pipeline

Click **Start Pipeline**.

After completion, inspect:
- output CSV
- forms CSV
- `data/*.txt` per-site combo files
- `logs/` for details

---

## First Run Walkthrough (GUI)

### Extractor tab

1. **Input section**
   - `File` for one combo file
   - `Folder` for bulk import of `.txt` files
2. **Output section**
   - Combined CSV path
   - Forms CSV path
3. **Options**
   - Skip blank lines
   - Trim whitespace
   - Create per-site `user:pass` files
   - Extract login forms
   - Strict form validation
   - Force recheck (ignore cache)
   - Show debug details
4. **Control buttons**
   - Start Pipeline
   - Pause
   - Cancel
   - Resume Failed
   - Settings
   - Test Credentials (Selected Site)
   - Clear Log

### Hydra Runner tab

- Click **Refresh List** after extraction.
- Select rows using the checkbox column.
- Use filters (combos/status/hits/last run).
- Use **Run Selected** or **Run All (Filtered)**.
- Observe live command output in runner log area.

### Burp Tester tab

Use this tab when you want routing through Burp and helper launch/import flows.

### ZAP Tester tab

Use this tab to integrate with OWASP ZAP (desktop/daemon depending on settings).

### Troubleshooting tab

Use this tab for categorized fetch-failure diagnostics and exports.

### Timeline tab

Use this tab to inspect event stream, filter by level/category/window, compare runs, and export timeline CSV.

---

## Headless/CLI Usage

ParserPro supports non-GUI mode.

### Command

```bash
python main.py --headless --extract <input.txt> --forms-output <forms.csv> [--run-hydra]
```

### Arguments

- `--headless` : run without GUI
- `--extract` : input combo file path
- `--forms-output` : where to write extracted forms CSV
- `--run-hydra` : optionally execute hydra after extraction

### Example

```bash
python main.py --headless --extract combos.txt --forms-output hydra_forms.csv --run-hydra
```

> `--headless` requires both `--extract` and `--forms-output`.

---

## Input/Output Formats

### Expected input line format

```text
site:user:password
```

Where `site` can be domain or URL.

### Key generated files

- `data/config.json` - persisted settings
- `data/processed_sites.json` - cached per-site processing state
- `data/<site>.txt` - per-site combo files
- `data/hits_<site>.txt` - runner hit captures
- `hits/hits_<domain>.txt` - saved successful hit summaries
- `logs/*.log` - app/run logs
- `combined.csv` (user-defined name/path)
- `hydra_forms.csv` (user-defined name/path)

---

## Settings Reference (Every Option)

These are the core persisted config options exposed in settings/defaults.

- `ignore_https_errors` - allow HTTPS/TLS errors during fetch attempts.
- `vpn_control` - VPN automation mode (`none`, `nordvpn`).
- `proxy_url` - default proxy endpoint.
- `proxy_required` - fail-fast if configured proxy is unreachable.
- `use_burp` - route traffic via Burp proxy setting.
- `burp_proxy` - Burp proxy URL.
- `use_zap` - route traffic via ZAP proxy setting.
- `zap_proxy` - ZAP proxy URL.
- `zap_api_key` - optional ZAP API key.
- `auto_start_zap_daemon` - launch ZAP in daemon mode automatically.
- `proxy_rotation` - enable rotating proxies from list.
- `proxy_list_file` - file path with one proxy endpoint per line.
- `anticaptcha_key` - Anti-Captcha API key.
- `capsolver_key` - Capsolver API key.
- `captcha_provider_order` - provider fallback order list.
- `allow_nonstandard_ports` - permit non-default web ports.
- `force_recheck` - ignore cache freshness and retry extraction.
- `cache_ttl_days` - freshness window for successful cache entries.
- `failed_retry_ttl_days` - freshness window for failed entries.
- `debug_logging` - verbose log output.
- `analysis_mode` - `static` or `observation` extraction mode.
- `observation_enable_dummy_interaction` - allow basic interaction during observation mode.
- `observation_allowlisted_domains` - domains allowed for observation interactions.
- `startup_dependency_checks` - run startup dependency checks.
- `prefer_wsl_hydra` - prefer WSL hydra backend when available.
- `auto_install_hydra` - attempt auto install when Hydra missing.
- `hydra_timeout_seconds` - runner timeout per Hydra process.
- `wsl_username` - optional WSL user for install/commands.
- `wsl_password` - optional password for sudo install flow.
- `auto_setup_chromedriver` - install/setup chromedriver automatically.
- `auto_configure_nordvpn_path` - auto-detect/configure NordVPN path.

---

## Hydra Integration Details

ParserPro can use native Hydra or WSL Hydra (especially on Windows).

Behavior overview:

1. Detect existing hydra.
2. If unavailable, attempt supported install/setup workflows.
3. Store backend mode and distro metadata when found.
4. If still unavailable, runner remains disabled but extractor still works.

Hydra commands are generated from extracted form metadata and combo file placeholders.

---

## Proxy, Burp, and ZAP Workflows

### Basic proxy routing

- Set `proxy_url` in Settings.
- If `proxy_required = false`, unreachable proxy is logged and bypassed.
- If `proxy_required = true`, run fails fast on proxy failure.

### Proxy rotation

1. Create `proxies.txt`:
   ```text
   http://127.0.0.1:8080
   socks5://127.0.0.1:9050
   ```
2. Enable `proxy_rotation`.
3. Set `proxy_list_file` to this path.

### Burp

- Enable `use_burp` and set `burp_proxy`.
- Launch Burp manually or via helper action.
- Install Burp CA cert if intercepting HTTPS.

### ZAP

- Enable `use_zap` and configure `zap_proxy` / `zap_api_key`.
- Optionally use daemon mode for API workflows.

---

## Troubleshooting Guide

### Problem: Hydra runner disabled

- Run extractor mode anyway (data prep still works).
- Install hydra manually and restart app.
- On Windows, ensure WSL is installed/configured if using WSL path.

### Problem: No forms extracted

- Confirm target URL is valid and reachable.
- Disable strict validation temporarily.
- Enable debug logging.
- Try observation mode on JS-heavy pages.

### Problem: Proxy failures

- Verify proxy host/port is reachable.
- Set `proxy_required=false` to continue without proxy.
- If using Burp/ZAP, confirm listener is active.

### Problem: TLS/certificate errors

- Install trusted cert chains for intercept proxies.
- As a last resort for controlled testing only, set `ignore_https_errors=true`.

### Problem: Empty runner hits

- Verify generated command templates are valid for target form behavior.
- Review method warnings for GET forms.
- Tune failure/success conditions in templates if needed.

---

## Testing / Smoke Checks

Run these from repository root:

```bash
python -m compileall .
python -m unittest discover -s tests -p 'test_*.py'
```

---

## Safety, Data Handling, and Privacy Notes

- This tool handles highly sensitive credential data.
- Keep all generated files in secure storage.
- Rotate/delete old logs and hit files regularly.
- Never run against unauthorized targets.
- Follow organizational policy and local laws.

---

## Developer Notes

- Codebase is modularized by responsibility.
- Unit tests exist for validation and utility behavior.
- `parserpro8.py` is retained for legacy/reference context.

