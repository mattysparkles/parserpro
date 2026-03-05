# ParserPro (Ultimate Combo → Hydra Pipeline)

ParserPro is a modular Python toolkit that ingests combo lists, normalizes/organizes credentials by target site, extracts login form metadata, and prepares Hydra command templates for downstream credential testing workflows.

> ⚠️ Use only on systems and applications you own or are explicitly authorized to test.

## What it does

- Parses one or many `.txt` combo sources with `site:user:pass` formatted lines.
- Produces a consolidated CSV output for data hygiene and auditing.
- Creates per-site combo files (`example_com.txt`) for replay/testing.
- Extracts login form details from target pages (Playwright first, Selenium fallback).
- Builds Hydra `http-post-form` command templates from discovered forms.
- Includes a GUI with two tabs:
  - **Extractor**: import combos, normalize sites, extract forms.
  - **Hydra Runner**: run selected or all prepared targets.
- Supports optional:
  - `vpn_control` (`none` by default, optional `nordvpn`) for VPN automation mode selection.
  - `proxy_url` routing for an already-running SOCKS/HTTP proxy.
  - Burp proxy routing for extraction and runner command composition.
  - CAPTCHA solving integration (DeathByCaptcha / 2Captcha).

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

## Settings

GUI Settings includes:

- DeathByCaptcha username/password
- 2Captcha API key
- NordVPN token
- VPN control (`none` or `nordvpn`)
- Proxy URL (`proxy_url`, optional socks5/http endpoint)
- Proxy required (`proxy_required`, fail fast if proxy is unreachable)
- Burp Proxy (example: `http://127.0.0.1:8080`)
- `ignore_https_errors` in `data/config.json` (default `false`)

When Burp proxy is configured, extraction fetchers are routed through it and runner command composition appends proxy args.

## Burp integration notes

- Start Burp and ensure proxy listener is active.
- Set `Burp Proxy` in Settings.
- If inspecting HTTPS traffic, install Burp CA certificate in the system/browser context used by your tooling.
- Some environments may also need certificate overrides for Python requests / browser contexts.


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
