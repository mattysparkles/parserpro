from __future__ import annotations

import requests

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from config import config
from helpers import TOR_SOCKS_PROXY, redact_onion_value, resolve_user_agent
from logging import write_detailed, write_privacy
from tor_manager import is_tor_running, start_tor



def _log(message: str, level: str = "INFO"):
    write_detailed(message, level=level)
    write_privacy(redact_onion_value(message), level=level)


def ensure_tor_for_onion() -> tuple[bool, str]:
    port = int(config.get("tor_socks_port", 9050) or 9050)
    if is_tor_running(port=port):
        return True, "[Tor] Using existing Tor instance"
    if not bool(config.get("auto_launch_tor", True)):
        return False, "[Tor] Failed to start — auto-launch disabled"
    _log("[Tor] Starting Tor process...")
    ok, msg, _ = start_tor(tor_path=str(config.get("tor_executable_path", "")).strip() or None, socks_port=port)
    level = "INFO" if ok else "WARN"
    _log(msg, level=level)
    return ok, msg


def fetch_onion_requests(url: str, *, timeout: int = 90, user_agent: str | None = None):
    ua = user_agent or resolve_user_agent(config, target_url=url)
    ok, msg = ensure_tor_for_onion()
    if not ok:
        return None, {"code": "tor_error", "hint": "Tor is not running", "detail": msg}, ua
    try:
        response = requests.get(
            url,
            headers={"User-Agent": ua},
            proxies={"http": TOR_SOCKS_PROXY, "https": TOR_SOCKS_PROXY},
            timeout=max(45, timeout),
            verify=False,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            return None, {"code": "http_error", "hint": f"HTTP {response.status_code}", "detail": f"{url} returned {response.status_code}"}, ua
        _log(f"Tor requests fetch OK {url} ua={ua}")
        return response.text, None, ua
    except Exception as exc:
        _log(f"Tor requests fetch failed {url} detail={exc} ua={ua}", level="WARN")
        return None, {"code": "tor_fetch_failed", "hint": "Tor requests fetch failed", "detail": str(exc)}, ua


def fetch_onion_playwright(url: str, *, timeout_ms: int = 90000, user_agent: str | None = None):
    ua = user_agent or resolve_user_agent(config, target_url=url)
    if not HAS_PLAYWRIGHT:
        return None, {"code": "playwright_not_installed", "hint": "Playwright not installed", "detail": "Install playwright to fetch onion sites"}, ua
    ok, msg = ensure_tor_for_onion()
    if not ok:
        return None, {"code": "tor_error", "hint": "Tor is not running", "detail": msg}, ua
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, proxy={"server": TOR_SOCKS_PROXY}, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                user_agent=ua,
                ignore_https_errors=bool(config.get("ignore_https_errors", False)),
                viewport={"width": 1600, "height": 900},
                locale="en-US",
                java_script_enabled=True,
                bypass_csp=True,
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=max(90000, timeout_ms))
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
        _log(f"Tor Playwright fetch OK {url} ua={ua}")
        return html, None, ua
    except Exception as exc:
        _log(f"Tor Playwright fetch failed {url} detail={exc} ua={ua}", level="WARN")
        return None, {"code": "tor_playwright_failed", "hint": "Tor Playwright fetch failed", "detail": str(exc)}, ua


def fetch_onion_html(url: str, *, timeout_seconds: int = 90, user_agent: str | None = None):
    html, error, ua = fetch_onion_playwright(url, timeout_ms=max(timeout_seconds, 90) * 1000, user_agent=user_agent)
    if html:
        return html, None, ua, True
    html, error2, ua = fetch_onion_requests(url, timeout=max(timeout_seconds, 90), user_agent=ua)
    if html:
        return html, None, ua, False
    return None, (error2 or error), ua, False
