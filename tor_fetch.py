from __future__ import annotations

from typing import Any

import requests

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from config import config
from helpers import TOR_SOCKS_PROXY, is_tor_running, redact_onion_value, resolve_user_agent
from logging import write_detailed, write_privacy


def _log(message: str, level: str = "INFO"):
    write_detailed(message, level=level)
    write_privacy(redact_onion_value(message), level=level)


def fetch_onion_requests(url: str, *, timeout: int = 45, user_agent: str | None = None):
    ua = user_agent or resolve_user_agent(config, target_url=url)
    if not is_tor_running():
        return None, {"code": "tor_error", "hint": "Tor is not running", "detail": f"Expected {TOR_SOCKS_PROXY}"}, ua
    try:
        response = requests.get(
            url,
            headers={"User-Agent": ua},
            proxies={"http": TOR_SOCKS_PROXY, "https": TOR_SOCKS_PROXY},
            timeout=timeout,
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


def fetch_onion_playwright(url: str, *, timeout_ms: int = 180000, user_agent: str | None = None):
    ua = user_agent or resolve_user_agent(config, target_url=url)
    if not HAS_PLAYWRIGHT:
        return None, {"code": "playwright_not_installed", "hint": "Playwright not installed", "detail": "Install playwright to fetch onion sites"}, ua
    if not is_tor_running():
        return None, {"code": "tor_error", "hint": "Tor is not running", "detail": f"Expected {TOR_SOCKS_PROXY}"}, ua
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
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
        _log(f"Tor Playwright fetch OK {url} ua={ua}")
        return html, None, ua
    except Exception as exc:
        _log(f"Tor Playwright fetch failed {url} detail={exc} ua={ua}", level="WARN")
        return None, {"code": "tor_playwright_failed", "hint": "Tor Playwright fetch failed", "detail": str(exc)}, ua


def fetch_onion_html(url: str, *, timeout_seconds: int = 45, user_agent: str | None = None):
    html, error, ua = fetch_onion_playwright(url, timeout_ms=max(timeout_seconds, 45) * 1000, user_agent=user_agent)
    if html:
        return html, None, ua, True
    html, error2, ua = fetch_onion_requests(url, timeout=max(timeout_seconds, 45), user_agent=ua)
    if html:
        return html, None, ua, False
    return None, (error2 or error), ua, False
