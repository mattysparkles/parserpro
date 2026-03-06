# ParserPro (Ultimate Combo → Hydra Pipeline)

ParserPro is a modular Python toolkit that ingests combo lists, normalizes/organizes credentials by target site, extracts login form metadata, and prepares Hydra command templates for downstream credential testing workflows.

> ⚠️ Use only on systems and applications you own or are explicitly authorized to test.

## What it does

- Parses one or many `.txt` combo sources with `site:user:pass` formatted lines.
- Produces a consolidated CSV output for data hygiene and auditing.
- Creates per-site combo files (`example_com.txt`) for replay/testing.
- Extracts login form details from target pages (Playwright first, Selenium fallback).
- Skips invalid/non-web targets before browser navigation (including nonstandard ports unless explicitly allowed).
- Builds Hydra `http-post-form` command templates from discovered forms.
- Includes a GUI with two tabs:
  - **Extractor**: import combos, normalize sites, extract forms.
  - **Hydra Runner**: run selected or all prepared targets.
- Supports optional:
  - `vpn_control` (`none` by default, optional `nordvpn`) for VPN automation mode selection.
  - `proxy_url` routing for an already-running SOCKS/HTTP proxy.
  - Burp proxy routing for extraction and runner command composition.
  - CAPTCHA solving integration (DeathByCaptcha / 2Captcha / Anti-Captcha / Capsolver).
  - Proxy-list rotation (`proxies.txt`) for extraction and runner execution.
  - Headless CLI mode (`--headless`) for extraction + optional hydra execution.

## Project structure

- `main.py` – app entry point and Tk bootstrap.
- `gui.py` – primary GUI and orchestration pipeline.
- `runner.py` – Hydra runner mixin and execution UI/actions.
- `extract.py` – form detection/validation/extraction logic.
- `fetch.py` – page fetching backends + CAPTCHA hooks.
- `helpers.py` – URL parsing and misc shared helpers.
- `config.py` – config persistence and gost downloader.
- `parserpro8.py` – legacy single-file script (kept for transition/reference).

## Installation

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Then launch:

```bash
python main.py

# headless mode
python main.py --headless --extract combos.txt --forms-output hydra_forms.csv --run-hydra
```

## Optional dependencies

### Optional: DeathByCaptcha

DeathByCaptcha support is optional and now degrades gracefully when the package or client class is unavailable.

```bash
pip install deathbycaptcha
```

If your environment exposes `HttpClient` but not `SocketClient`, ParserPro now falls back automatically.

### Optional: Proxy / VPN behavior

ParserPro now defaults to `vpn_control: "none"` so it does **not** attempt NordVPN automation unless you explicitly opt in.

On Windows specifically, ParserPro will not launch the NordVPN GUI executable. If a true headless CLI with connect/disconnect support is not available, it logs: `NordVPN automation not supported on Windows; set vpn_control='none' and manage VPN externally.` and continues with no VPN/no proxy automation.

If `proxy_url` is set, ParserPro only uses it when reachable. If unreachable and `proxy_required` is `false`, it logs once and disables proxy for the run. If `proxy_required` is `true`, it fails fast.

When NordVPN mode is enabled and supported, ParserPro auto-detects the latest compatible gost release asset for your OS/CPU via GitHub Releases API and caches the archive under `data/downloads/`.

### System tools

Depending on enabled features you may also need:

- `hydra` (for runner execution)
- `nordvpn` CLI (if using auto NordVPN rotation)
- Chromium/Chrome runtime compatible with Playwright/Selenium



## UI refresh (2026 usability pass)

- Added vertical + horizontal scrollbars to long-content widgets in both tabs:
  - Extractor log
  - Runner results table
  - Runner log
- Mouse wheel scrolling now supports platform-specific behavior, including `Shift+Wheel` for horizontal scrolling.
- Runner tab now uses an adjustable split pane so you can resize table/log space interactively.
- Layout was updated for better resizing at common laptop resolutions (including 1280x720).

**Resize tips**
- Drag the divider in the Runner tab to allocate more room to the results table or the runner log.
- Widen the app window to view more Treeview columns; use the horizontal scrollbar for overflow.

## Runner tab UX controls

The **Hydra Runner** tab now behaves like a generic command orchestrator with explicit row controls:

- **Pause / Resume / Cancel semantics**
  - **Pause** stops launching new subprocesses and waits cooperatively (no busy-spin loops).
  - **Cancel** stops queued launches immediately and terminates any active subprocess (`terminate` then `kill` fallback).
  - UI state returns to idle after worker completion through queue-driven main-thread updates.
- **Sorting**
  - Click any table header (`Combos`, `Status`, `Site`, `Hits`, `Last Run`) to toggle ascending/descending sorting.
  - Numeric columns are sorted numerically; text columns are case-insensitive.
- **Filtering**
  - Minimum combos
  - Status (`All / Pending / Running / Failed / Success`)
  - Minimum hits
  - Last run (`All / Never Run / Has Run`)
- **Selection model**
  - First column checkbox (`[ ]` / `[x]`) persists selection through sort/filter refreshes.
  - Buttons: **Select All (Filtered)**, **Deselect All (Filtered)**, **Invert Selection (Filtered)**.
  - **Run Selected** executes only rows currently marked selected.

## Smoke check

```bash
python -m compileall .
python -m unittest tests.test_url_validation
```

## How the pipeline works

1. **Load input** (single file or folder of `.txt` files).
2. **Parse and clean lines** (`split_three_fields`, whitespace handling).
3. **Normalize targets** (`normalize_site` + `get_base_url`).
4. **Write outputs**:
   - Combined CSV
   - Per-site combo files
5. **Extract forms** (optional):
   - Fetch page (Playwright, then Selenium fallback)
   - Validate candidate forms (`POST`, password field, confidence checks)
   - Build `hydra_command_template`
6. **Persist state** in `processed_sites.json` for retries/runner list.
7. **Run Hydra** from Runner tab on selected/all sites.

## Login flow analyzer modes

ParserPro now supports two safety-focused analysis modes:

- **Static mode** (default): parse HTML and classify outcomes as:
  - `✅ actionable native POST form`
  - `🟨 login-ish (JS-handled / non-POST / missing action)`
  - `❌ no login form`
- **Observation mode** (optional): loads target page with Playwright and records observed auth-like requests/cookies as telemetry (`observed_login_flow`).

Observation mode keeps boundaries explicit:
- Dummy interaction is **off by default**.
- Dummy interaction only runs when explicitly enabled **and** the domain matches `observation_allowlisted_domains`.
- Captured data is stored as observed flow metadata (endpoint/method/content-type/status + cookies), not attack instructions.

Configuration keys in `data/config.json`:
- `analysis_mode`: `"static"` or `"observation"`
- `observation_enable_dummy_interaction`: `true/false`
- `observation_allowlisted_domains`: `["example.com"]`

## Settings

GUI Settings includes:

- DeathByCaptcha username/password
- 2Captcha API key
- Anti-Captcha API key
- Capsolver API key
- NordVPN token
- VPN control (`none` or `nordvpn`)
- Proxy URL (`proxy_url`, optional socks5/http endpoint)
- Proxy required (`proxy_required`, fail fast if proxy is unreachable)
- Allow nonstandard ports (`allow_nonstandard_ports`, default `false`)
- Cache TTL days (`cache_ttl_days`, default `30`)
- Failed-fetch retry TTL days (`failed_retry_ttl_days`, default `1`)
- Force recheck toggle (`force_recheck`, default `false`)
- Burp Proxy (example: `http://127.0.0.1:8080`)
- Route all requests through Burp (`use_burp`)
- Enable proxy rotation + proxy list file (`proxy_rotation`, `proxy_list_file`)
- `ignore_https_errors` in `data/config.json` (default `false`)

When Burp proxy is configured, extraction fetchers are routed through it and runner command composition appends proxy args.

## Burp integration notes

- Start Burp and ensure proxy listener is active.
- Set `Burp Proxy` in Settings.
- If inspecting HTTPS traffic, install Burp CA certificate in the system/browser context used by your tooling.
- For Python HTTPS clients, add Burp CA to your cert bundle (or set `REQUESTS_CA_BUNDLE=/path/to/cacert.pem`).
- Playwright/Selenium traffic is proxied when **Route all requests through Burp** is enabled.
- Hydra commands also append `-p <burp_proxy>` when Burp routing is enabled.


### Windows recommendation

NordVPN GUI on Windows may steal foreground focus. To avoid disruptions, ParserPro does not control NordVPN by default.

Recommended setup:
- Connect NordVPN manually at the OS level, then run ParserPro with `vpn_control: "none"`.
- Or run your own SOCKS/HTTP proxy separately and set `proxy_url` to that listener.

## Common outputs

- `combined.csv` (name user-defined)
- `hydra_forms.csv` (name user-defined)
- `data/<site>.txt` per-site combos
- `data/hits_<site>.txt` runner output hits
- `hits/<domain>.txt` normalized hit output with timestamps
- `logs/*.log` timestamped extraction / runner / headless session logs
- `data/processed_sites.json` run metadata
- `data/config.json` settings

## CHANGELOG

- Fixed gost download logic to use GitHub Releases API asset metadata per-platform and cache archives under `data/downloads/`; failures now gracefully continue with no proxy.
- Updated Selenium initialization for Selenium 4 (`service` + `options`) and improved missing-driver error messaging.
- Made DeathByCaptcha integration optional and client-compatible (`SocketClient` fallback to `HttpClient`).
- Added URL validation guardrails before browser fetchers to reject non-URL garbage and avoid runtime crashes.
- Improved form action normalization (`blank -> same page`, relative -> `urljoin`, invalid action skipped with reason).
- Added optional `ignore_https_errors` config (default `false`) and better TLS/proxy diagnostics.
- Added tests for URL validation and form action normalization plus documented smoke checks.

## Notes on modularization

This repository is now split by responsibility (GUI, fetch, extract, config, runner, helpers), which reduces coupling and makes testing/evolution easier.

Suggested next improvements:

- Add automated tests under `tests/` (`pytest`) for helper parsing and form validation.
- Add CI lint/test workflow.
- Migrate modules into a package directory (`src/parserpro/`) if distribution is planned.



## Auto-setup and runtime safety (new)

- **Hydra auto-detection/install**: `main.py` runs startup checks before GUI/headless flow and follows this order on Windows:
  1. Run `wsl --list --quiet` and enumerate installed distros.
  2. For each distro, run `wsl -d <distro> hydra --version`.
  3. If Hydra exists in a distro, that distro is selected for runner execution (`wsl -d <found_distro> bash -lc ...`).
  4. If Hydra is missing in all distros, it attempts install in the first distro with `wsl -d <distro> -- bash -lc "sudo apt update && sudo apt install -y hydra"`.
  5. If WSL is unavailable or install fails, it falls back to native Windows Hydra by downloading `https://github.com/maaaaz/thc-hydra-windows/releases/download/v9.1/hydra-9.1-win.zip`, extracting under `tools/hydra`, and appending that directory to process `PATH`.
- **Runner-safe behavior**: if Hydra is still unavailable after all attempts, GUI still loads but Hydra Runner is disabled and shows a warning message when user tries to run commands.
- **User prompts**: startup uses `messagebox.showinfo` for successful install notifications and restart guidance when native PATH changes might require a fresh shell/app session.
- **Selenium/Chromedriver auto-setup**: uses `webdriver_manager.chrome` to provision Chromedriver when not manually configured.
- **NordVPN on Windows**: checks PATH and default install locations (`C:\Program Files\NordVPN`). If missing, it opens the NordVPN download page and raises a startup warning dialog with manual steps.
- **Form method handling**: non-POST forms are retained, and Hydra templates use:
  - `http-post-form` for POST
  - `http-get-form` for GET
  GET forms are marked with a warning that manual tuning may be required.
- **Runner timeout/termination**: each Hydra process has a default 1-hour timeout (`hydra_timeout_seconds`, default `3600`) and is cleanly terminated on timeout/cancel/exit.
- **Graceful shutdown**: window close now attempts to stop running subprocesses, terminate gost, and disconnect NordVPN CLI sessions.


### New config flags

In `data/config.json`:

- `startup_dependency_checks` (default `true`)
- `prefer_wsl_hydra` (default `true`)
- `auto_install_hydra` (default `true`)
- `hydra_timeout_seconds` (default `3600`)
- `auto_setup_chromedriver` (default `true`)
- `auto_configure_nordvpn_path` (default `true`)

## Troubleshooting

- **Selenium: `WebDriver.__init__() got multiple values for argument options`**
  ParserPro now uses Selenium 4 style initialization (`webdriver.Chrome(service=..., options=...)`) and avoids passing `options` twice.
- **Missing Chrome/Chromedriver**
  If Selenium cannot start, install Chrome/Chromium or set `chrome_driver_path` in `data/config.json`.
- **TLS/SSL failures through proxy**
  Playwright/Selenium now log a TLS hint when a proxy is active (`proxy may be breaking TLS`). Keep `ignore_https_errors` disabled unless you intentionally need it.
- **`ERR_SOCKS_CONNECTION_FAILED`**
  Your SOCKS proxy endpoint is not reachable (commonly `127.0.0.1:1080` when gost is not running). Disable proxy settings (`burp_proxy`, `socks_proxy`, `proxy`) or start the proxy process.
- **Verify proxy health quickly**
  Confirm the listener is up before extraction, e.g. `python -c "import socket;print(socket.create_connection(('127.0.0.1',1080),1))"` (should connect without exception).
## Extraction cache behavior

- Cache is stored in `data/processed_sites.json` and reused on later runs.
- By default, sites with recent `success` or `no_form` status are skipped until TTL expires.
- Sites with `fetch_failed` are retried after a shorter TTL, or immediately with **Retry Failed**.
- The Extractor log reports per-site outcome: login form found, no form found, cached skip, invalid-target skip, or fetch failure code.
- Root-level legacy `processed_sites.json` is migrated once into `data/processed_sites.json`.

## Why targets may be skipped

Some combo lists contain host:port endpoints that are not browser login pages (for example mining dashboard/service ports). ParserPro validates targets before Playwright/Selenium navigation and skips likely non-web entries to reduce noisy browser errors such as `ERR_CONNECTION_CLOSED`.


- **Navigation error codes**
  - `dns_failed`: DNS resolution failed. ParserPro records this and waits for retry TTL instead of immediate retry.
  - `conn_closed`: Remote side closed connection or target is not a web endpoint. ParserPro retries once with short backoff.
  - `tls_mismatch`: TLS negotiation failed (often proxy/AV interception issues).
  - `cert_invalid`: Certificate is untrusted (possible MITM/captive portal).
  - `proxy_down`: Configured SOCKS proxy is unreachable; ParserPro retries once without proxy for that run.
  - `fetch_failed`: Generic navigation failure fallback when no known signature is matched.

- **What “login-ish” means**
  ParserPro now stores forms with password fields even if they are not strict POST login forms. These entries are marked `success_loginish` with metadata (`method`, `action_url`, `user_field`, `pass_field`, `submit_mode`, confidence/reasons) so reruns can skip redundant fetches while still surfacing useful extraction context.


## Project files (.pproj / .parserproproj.json)

ParserPro now supports portable **project files** with schema versioning (`schema_version: 1`). A project captures session context without storing secrets:

- Project name, created/last-saved timestamps
- Input references and embedded site list snapshot
- Runner filter state + sort state + selected rows
- Results snapshot per site (status, extraction metadata, error code/hint/detail, timestamps)
- Session-safe settings (`proxy_url`, `allow_nonstandard_ports`, `ignore_https_errors`, autosave preferences)

Use **File → New/Open/Save/Save As** in the GUI. Project writes use atomic temp-file replacement to reduce corruption risk.

## Report export

Use **File → Export → JSON/CSV**.

- JSON includes project metadata, summary counts, and per-site records.
- CSV flattens key per-site fields using UTF-8 with standard CSV quoting.
- Exports can target either full data or the current filtered view.

## Troubleshooting dashboard

A new **Troubleshooting** tab summarizes failures by category:

- DNS failures (`dns_failed`, `ERR_NAME_NOT_RESOLVED`)
- TLS failures (`tls_mismatch`, `cert_invalid`)
- Proxy failures (`proxy_down`, `ERR_SOCKS_CONNECTION_FAILED`)
- Connection closed (`conn_closed`, `ERR_CONNECTION_CLOSED`)
- Other fetch failures

The tab shows category counts, top failing domains, per-site details, static recommended actions, and includes:

- **Retry Failed** (re-queues failed sites using existing controls)
- **Export Diagnostics CSV**

## Burp + ZAP + Credential Testing (new)

- Added dedicated GUI tabs:
  - **Burp Tester**: launch Burp Suite Community and export current extracted target data (`data/burp_import.json`).
  - **ZAP Tester**: launch OWASP ZAP (optionally daemon mode) and import current targets into ZAP API active scans.
- Settings now include:
  - `use_burp`, `burp_proxy` (default `http://127.0.0.1:8080`)
  - `use_zap`, `zap_proxy` (default `http://127.0.0.1:8080`)
  - `zap_api_key`, `auto_start_zap_daemon`
- Unified proxy routing: Playwright, Selenium, extraction pipeline, and Hydra runner now respect interceptor preference (`Burp` first if enabled, otherwise `ZAP`, otherwise normal proxy settings).
- Login flow analyzer now supports GET/POST/other methods:
  - Generates Hydra `http-get-form`/`http-post-form` when supported.
  - Marks unsupported methods for custom testing.
- Added GUI action **Test Credentials (Selected Site)**:
  - Uses per-site combos against extracted form fields.
  - Saves successful entries to `hits/hits_<domain>.txt` including method used.

### Burp CA certificate note

For HTTPS interception, install Burp's CA certificate into the trust store used by your browser/tooling environment. Without this, TLS requests may fail or be blocked. On Python clients you can also point to a trusted CA file using `REQUESTS_CA_BUNDLE`.

### Windows-first notes

- Burp/ZAP launch uses Windows executable/batch detection first and `shell=True` where appropriate.
- Linux/macOS paths fall back to `burpsuite` / `zaproxy` style commands when available.
