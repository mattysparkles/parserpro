import random
import subprocess
import sys
import time
import traceback
import importlib

from typing import Optional

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from app_logging import logger, log_once
from config import config, get_intercept_proxy
from helpers import normalize_and_validate_target, resolve_user_agent
from logging import write_detailed, write_privacy


def _pick_user_agent(target_url: str | None = None, override: str | None = None) -> str:
    return resolve_user_agent(config, target_url=target_url, override=override)

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service as ChromeService

    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

try:
    from webdriver_manager.chrome import ChromeDriverManager

    HAS_WEBDRIVER_MANAGER = True
except ImportError:
    HAS_WEBDRIVER_MANAGER = False

try:
    import deathbycaptcha
except ImportError:
    deathbycaptcha = None

try:
    import deathbycaptcha_official  # type: ignore
except ImportError:
    deathbycaptcha_official = None

HAS_DEATHBYCAPTCHA = bool(deathbycaptcha or deathbycaptcha_official)

try:
    from twocaptcha import TwoCaptcha

    HAS_2CAPTCHA = True
except ImportError:
    HAS_2CAPTCHA = False

try:
    from anticaptchaofficial.recaptchav2proxyless import recaptchaV2Proxyless
    from anticaptchaofficial.hcaptchaproxyless import hCaptchaProxyless

    HAS_ANTICAPTCHA = True
except ImportError:
    HAS_ANTICAPTCHA = False

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import cloudscraper

    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False


_CHROMEDRIVER_STATUS_CACHE = None
_PROXY_STARTUP_TESTED = False
_PROXY_STARTUP_OK = True
PLAYWRIGHT_TIMEOUT_MS = 300000  # FIXED: per-site timeout 5 minutes
REQUEST_TIMEOUT_SECONDS = 300  # FIXED: per-site timeout 5 minutes
SITE_FETCH_RETRIES = 2  # FIXED: 2 retries + initial attempt
_PLAYWRIGHT_RUNTIME_READY = False


def ensure_playwright_runtime_once() -> tuple[bool, str]:
    # FIXED: auto-install chromium on first runtime use if missing
    global _PLAYWRIGHT_RUNTIME_READY
    if _PLAYWRIGHT_RUNTIME_READY:
        return True, "playwright_runtime_ready"
    if not HAS_PLAYWRIGHT:
        return False, "playwright_not_installed"
    commands = [["playwright", "install", "chromium"], [sys.executable, "-m", "playwright", "install", "chromium"]]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
            if result.returncode == 0:
                _PLAYWRIGHT_RUNTIME_READY = True
                return True, "playwright_chromium_ready"
        except Exception:
            continue
    return False, "playwright_install_failed"


def _proxy_or_none(effective_proxy):
    # FIXED: startup proxy probe once; disable proxy if unreachable
    global _PROXY_STARTUP_TESTED, _PROXY_STARTUP_OK
    if not effective_proxy or not effective_proxy.get("server"):
        return effective_proxy
    if not _PROXY_STARTUP_TESTED:
        _PROXY_STARTUP_TESTED = True
        _PROXY_STARTUP_OK = test_proxy_connection(effective_proxy)
        if not _PROXY_STARTUP_OK:
            write_detailed("[Proxy Fallback] Startup proxy probe failed; using direct connection", level="WARN")
            write_privacy("[Proxy Fallback] Startup proxy probe failed; using direct connection", level="WARN")
    return effective_proxy if _PROXY_STARTUP_OK else None

ERROR_CODE_MAP = {
    "ERR_NAME_NOT_RESOLVED": ("dns_failed", "DNS resolution failed"),
    "ERR_CONNECTION_CLOSED": ("conn_closed", "Connection closed by peer or non-web endpoint"),
    "ERR_SSL_VERSION_OR_CIPHER_MISMATCH": ("tls_mismatch", "TLS handshake failed (proxy/AV may interfere)"),
    "ERR_CERT_AUTHORITY_INVALID": ("cert_invalid", "Untrusted certificate (MITM/captive portal)"),
    "ERR_SOCKS_CONNECTION_FAILED": ("proxy_down", "SOCKS proxy unreachable"),
}


def classify_nav_error(exc_text: str):
    text = str(exc_text or "")
    lowered = text.lower()
    for signature, mapped in ERROR_CODE_MAP.items():
        if signature.lower() in lowered:
            return mapped
    return "fetch_failed", "Navigation failed"


def short_error_detail(exc_text: str, max_len=220):
    detail = " ".join(str(exc_text or "").split())
    return detail[:max_len]




def test_proxy_connection(proxy_cfg) -> bool:
    if not (HAS_REQUESTS and proxy_cfg and proxy_cfg.get("server")):
        return False
    server = proxy_cfg["server"]
    try:
        write_detailed(f"Proxy test attempt: {server}")
        write_privacy(f"Proxy test attempt: {server}")
        requests.get("https://httpbin.org/ip", proxies={"http": server, "https": server}, timeout=5, verify=False)
        return True
    except Exception as exc:
        write_detailed(f"Proxy unreachable {server}: {exc}", level="WARN")
        write_privacy(f"Proxy unreachable {server}: {exc}", level="WARN")
        return False


def build_error_payload(code, hint, detail, stacktrace=None):
    payload = {"code": code, "hint": hint, "detail": short_error_detail(detail)}
    if stacktrace:
        payload["stacktrace"] = stacktrace
    return payload


def _debug_stack(prefix, exc):
    if not bool(config.get("debug_logging", False)):
        return None
    stack = traceback.format_exc()
    logger.debug(f"{prefix}: {exc}")
    logger.debug(stack)
    return stack


def get_dbc_client(user, password):
    if not user or not password:
        return None

    for module in (deathbycaptcha, deathbycaptcha_official):
        if not module:
            continue

        module_variants = [module]
        nested_module_name = f"{module.__name__}.{module.__name__.split('.')[-1]}"
        try:
            nested_module = importlib.import_module(nested_module_name)
            module_variants.append(nested_module)
        except Exception:
            pass

        for module_variant in module_variants:
            socket_client = getattr(module_variant, "SocketClient", None)
            if socket_client:
                return socket_client(user, password)
            http_client = getattr(module_variant, "HttpClient", None)
            if http_client:
                return http_client(user, password)
    return None


def _fetch_page_playwright_once(clean_url, effective_proxy):
    # FIXED: Proxy fallback + single chromedriver check
    launch_args = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
    if effective_proxy:
        launch_args["proxy"] = {"server": effective_proxy["server"]}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=_pick_user_agent(clean_url),
            locale="en-US",
            ignore_https_errors=bool(config.get("ignore_https_errors", False)),
            java_script_enabled=True,
            bypass_csp=True,
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        page = context.new_page()
        page.goto(clean_url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
        return html


def fetch_page_playwright(url, proxy=None):
    ready, ready_msg = ensure_playwright_runtime_once()
    if not ready:
        return None, ready_msg

    clean_url, reason = normalize_and_validate_target(url, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
    if not clean_url:
        return None, build_error_payload("invalid_target", reason or "invalid target", reason or "invalid target")

    try:
        effective_proxy = _proxy_or_none(get_intercept_proxy(config, proxy))
    except RuntimeError as e:
        return None, build_error_payload("proxy_down", "SOCKS proxy unreachable", str(e))

    retried_without_proxy = False
    attempts = 0
    max_attempts = SITE_FETCH_RETRIES + 1
    while attempts < max_attempts:
        attempts += 1
        route = "proxy" if effective_proxy else "direct"
        write_detailed(f"Playwright fetch attempt {attempts} via {route}: {clean_url}")
        write_privacy(f"Playwright fetch attempt {attempts} via {route}: {clean_url}")
        try:
            return _fetch_page_playwright_once(clean_url, effective_proxy), None
        except Exception as e:
            stack = _debug_stack(f"Playwright failed {clean_url}", e)
            code, hint = classify_nav_error(str(e))
            detail = str(e)

            if effective_proxy and not retried_without_proxy:
                logger.warn(f"[Proxy Fallback] Playwright proxy failed ({detail}); retrying direct connection")
                log_once("proxy-fallback-playwright", "[Proxy Fallback] Using direct connection", level="WARN")
                effective_proxy = None
                retried_without_proxy = True
                continue

            if code == "proxy_down" and effective_proxy and not retried_without_proxy:
                log_once("proxy-down", "Proxy appears unreachable; retrying once without proxy", level="WARN")
                effective_proxy = None
                retried_without_proxy = True
                continue

            if attempts < max_attempts:
                backoff = 2 ** (attempts - 1)
                time.sleep(backoff)
                continue

            if code == "dns_failed":
                log_once("dns-failed", "DNS failures detected; recording without immediate retry", level="WARN")

            if code in {"tls_mismatch", "cert_invalid"} and effective_proxy:
                log_once("proxy-tls-hint-playwright", "TLS error detected while proxy is enabled; proxy may be breaking TLS", level="WARN")
            return None, build_error_payload(code, hint, detail, stacktrace=stack)

    return None, build_error_payload("fetch_failed", "Navigation failed", "unknown playwright failure")




# NEW: Selenium + Chromedriver auto-setup check
def ensure_chromedriver_available() -> tuple[bool, str, Optional[str]]:
    """Try webdriver_manager install once and cache availability/message/driver_path."""
    global _CHROMEDRIVER_STATUS_CACHE
    if _CHROMEDRIVER_STATUS_CACHE is not None:
        return _CHROMEDRIVER_STATUS_CACHE
    if not HAS_SELENIUM:
        _CHROMEDRIVER_STATUS_CACHE = (False, "selenium_not_installed", None)
        return _CHROMEDRIVER_STATUS_CACHE
    if not HAS_WEBDRIVER_MANAGER:
        _CHROMEDRIVER_STATUS_CACHE = (False, "webdriver_manager_not_installed", None)
        return _CHROMEDRIVER_STATUS_CACHE
    try:
        driver_path = ChromeDriverManager().install()
        _CHROMEDRIVER_STATUS_CACHE = (True, "chromedriver_ready", driver_path)
    except Exception as exc:
        _CHROMEDRIVER_STATUS_CACHE = (False, f"chromedriver_auto_setup_failed: {exc}", None)
    return _CHROMEDRIVER_STATUS_CACHE


def fetch_page_selenium(url, proxy=None):
    """Fetch page HTML using Selenium with webdriver_manager fallback."""
    if not HAS_SELENIUM:
        return None, "selenium_not_installed"

    clean_url, reason = normalize_and_validate_target(url, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
    if not clean_url:
        return None, build_error_payload("invalid_target", reason or "invalid target", reason or "invalid target")

    try:
        effective_proxy = _proxy_or_none(get_intercept_proxy(config, proxy))
    except RuntimeError as e:
        return None, build_error_payload("proxy_down", "SOCKS proxy unreachable", str(e))

    # FIXED: Proxy fallback + single chromedriver check
    driver = None
    retried_without_proxy = False
    current_proxy = effective_proxy
    attempts = 0
    max_attempts = SITE_FETCH_RETRIES + 1
    while attempts < max_attempts:
        attempts += 1
        driver = None
        route = "proxy" if current_proxy else "direct"
        write_detailed(f"Selenium fetch attempt {attempts} via {route}: {clean_url}")
        write_privacy(f"Selenium fetch attempt {attempts} via {route}: {clean_url}")
        try:
            options = Options()
            options.add_argument("--headless")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument(f"user-agent={_pick_user_agent(clean_url)}")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            if current_proxy and current_proxy.get("server"):
                options.add_argument(f"--proxy-server={current_proxy['server']}")

            if bool(config.get("ignore_https_errors", False)):
                options.add_argument("--ignore-certificate-errors")

            driver_path = (config.get("chrome_driver_path") or "").strip()
            if driver_path:
                service = ChromeService(executable_path=driver_path)
                driver = webdriver.Chrome(service=service, options=options)
            else:
                driver = webdriver.Chrome(options=options)

            # Prevent edge-case pages from blocking extraction indefinitely.
            driver.set_page_load_timeout(180)

            driver.get(clean_url)
            time.sleep(5)
            html = driver.page_source
            return html, None
        except Exception as e:
            stack = _debug_stack(f"Selenium failed {clean_url}", e)
            code, hint = classify_nav_error(str(e))
            message = str(e)

            if current_proxy and not retried_without_proxy:
                logger.warn(f"[Proxy Fallback] Selenium proxy failed ({message}); retrying direct connection")
                log_once("proxy-fallback-selenium", "[Proxy Fallback] Using direct connection", level="WARN")
                current_proxy = None
                retried_without_proxy = True
                continue

            if "timeout" in message.lower():
                return None, build_error_payload(
                    "fetch_timeout",
                    "Page load timed out",
                    "Selenium timed out while loading this page; skipped to keep extraction moving",
                    stacktrace=stack,
                )
            if "driver" in message.lower() or "chromedriver" in message.lower():
                return None, build_error_payload(
                    "driver_error",
                    "browser driver is missing or misconfigured",
                    f"{message}. Ensure Chrome/Chromium is installed or set chrome_driver_path in config.json",
                    stacktrace=stack,
                )
            if code in {"tls_mismatch", "cert_invalid"} and current_proxy:
                log_once("proxy-tls-hint-selenium", "TLS error detected while proxy is enabled; proxy may be breaking TLS", level="WARN")
            return None, build_error_payload(code, hint, message, stacktrace=stack)
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    return None, build_error_payload("fetch_failed", "Navigation failed", "unknown selenium failure")




def fetch_page_requests(url, proxy=None, timeout=REQUEST_TIMEOUT_SECONDS):
    if not HAS_REQUESTS:
        return None, build_error_payload("fetch_failed", "requests not installed", "requests dependency missing")

    clean_url, reason = normalize_and_validate_target(url, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
    if not clean_url:
        return None, build_error_payload("invalid_target", reason or "invalid target", reason or "invalid target")

    try:
        effective_proxy = _proxy_or_none(get_intercept_proxy(config, proxy))
    except RuntimeError as e:
        return None, build_error_payload("proxy_down", "SOCKS proxy unreachable", str(e))

    headers = {"User-Agent": _pick_user_agent(clean_url)}
    proxy_map = None
    if effective_proxy and effective_proxy.get("server"):
        server = effective_proxy["server"]
        proxy_map = {"http": server, "https": server}

    route_sequence = [proxy_map, None] if proxy_map else [None]
    max_attempts = SITE_FETCH_RETRIES + 1
    last_exc = None
    for idx in range(1, max_attempts + 1):
        attempt_proxy = route_sequence[min(idx - 1, len(route_sequence) - 1)]
        route = "proxy" if attempt_proxy else "direct"
        write_detailed(f"Requests fetch attempt {idx} via {route}: {clean_url}")
        write_privacy(f"Requests fetch attempt {idx} via {route}: {clean_url}")
        try:
            resp = requests.get(clean_url, headers=headers, timeout=timeout, verify=False, proxies=attempt_proxy, allow_redirects=True)
            if resp.status_code == 403 and HAS_CLOUDSCRAPER and bool(config.get("enable_cloudscraper_fallback", True)):
                try:
                    logger.info(f"[cloudscraper] 403 from {clean_url}; retrying with cloudscraper")
                    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
                    resp = scraper.get(clean_url, headers=headers, timeout=timeout, verify=False, proxies=attempt_proxy, allow_redirects=True)
                except Exception as cloud_exc:
                    stack = _debug_stack(f"Cloudscraper failed {clean_url}", cloud_exc)
                    return None, build_error_payload("fetch_failed", "cloudscraper failed", str(cloud_exc), stacktrace=stack)
            if resp.status_code >= 400:
                return None, build_error_payload("http_error", f"HTTP {resp.status_code}", f"{clean_url} returned {resp.status_code}")
            return resp.text, None
        except requests.exceptions.Timeout as e:
            last_exc = e
            write_detailed(f"Timeout via {route}: {clean_url} :: {e}", level="WARN")
            write_privacy(f"Timeout via {route}: {clean_url} :: {e}", level="WARN")
        except requests.exceptions.RequestException as e:
            last_exc = e
            write_detailed(f"Request failure via {route}: {clean_url} :: {e}", level="WARN")
            write_privacy(f"Request failure via {route}: {clean_url} :: {e}", level="WARN")

        if idx < max_attempts:
            time.sleep(2 ** (idx - 1))

    if isinstance(last_exc, requests.exceptions.Timeout):
        return None, build_error_payload("fetch_timeout", "request timed out", str(last_exc))
    if last_exc is not None:
        stack = _debug_stack(f"Requests failed {clean_url}", last_exc)
        code, hint = classify_nav_error(str(last_exc))
        return None, build_error_payload(code, hint, str(last_exc), stacktrace=stack)
    return None, build_error_payload("fetch_failed", "request failed", "all attempts exhausted")


def _solve_with_anticaptcha(captcha_type, sitekey, url):
    if not HAS_ANTICAPTCHA or not config.get("anticaptcha_key"):
        return None
    key = config.get("anticaptcha_key")
    try:
        if captcha_type == "recaptcha":
            solver = recaptchaV2Proxyless()
            solver.set_verbose(0)
            solver.set_key(key)
            solver.set_website_url(url)
            solver.set_website_key(sitekey)
            return solver.solve_and_return_solution()
        if captcha_type == "hcaptcha":
            solver = hCaptchaProxyless()
            solver.set_verbose(0)
            solver.set_key(key)
            solver.set_website_url(url)
            solver.set_website_key(sitekey)
            return solver.solve_and_return_solution()
    except Exception as e:
        logger.warn(f"Anti-Captcha failed: {e}")
    return None


def _solve_with_capsolver(captcha_type, sitekey, url):
    if not HAS_REQUESTS or not config.get("capsolver_key"):
        return None
    task_type = {
        "recaptcha": "ReCaptchaV2TaskProxyLess",
        "hcaptcha": "HCaptchaTaskProxyLess",
        "turnstile": "TurnstileTaskProxyLess",
    }.get(captcha_type)
    if not task_type:
        return None

    try:
        payload = {
            "clientKey": config.get("capsolver_key"),
            "task": {
                "type": task_type,
                "websiteURL": url,
                "websiteKey": sitekey,
            },
        }
        create = requests.post("https://api.capsolver.com/createTask", json=payload, timeout=30, verify=False).json()
        task_id = create.get("taskId")
        if not task_id:
            logger.warn(f"Capsolver failed to create task: {create}")
            return None
        for _ in range(25):
            result = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": config.get("capsolver_key"), "taskId": task_id},
                timeout=30,
                verify=False,
            ).json()
            if result.get("status") == "ready":
                return (result.get("solution") or {}).get("gRecaptchaResponse") or (result.get("solution") or {}).get("token")
            time.sleep(2)
    except Exception as e:
        logger.warn(f"Capsolver failed: {e}")
    return None


def solve_captcha(soup, url):
    captcha_type = None
    sitekey = None

    recaptcha_div = soup.find("div", {"class": "g-recaptcha"})
    hcaptcha_div = soup.find("div", {"class": "h-captcha"})
    turnstile_div = soup.find("div", {"class": "cf-turnstile"})

    if recaptcha_div:
        captcha_type = "recaptcha"
        sitekey = recaptcha_div.get("data-sitekey")
    elif "hcaptcha" in str(soup).lower() and hcaptcha_div:
        captcha_type = "hcaptcha"
        sitekey = hcaptcha_div.get("data-sitekey")
    elif "turnstile" in str(soup).lower() and turnstile_div:
        captcha_type = "turnstile"
        sitekey = turnstile_div.get("data-sitekey")

    if not captcha_type or not sitekey:
        return None

    def _solve_with_dbc():
        if not (HAS_DEATHBYCAPTCHA and config.get("dbc_user") and config.get("dbc_pass")):
            return None
        try:
            client = get_dbc_client(config["dbc_user"], config["dbc_pass"])
            if client:
                captcha = client.decode(sitekey=sitekey, url=url, type=captcha_type)
                return captcha["text"]
            logger.warn("DeathByCaptcha client unavailable (SocketClient/HttpClient not found); skipping DBC.")
        except Exception as e:
            logger.warn(f"DeathByCaptcha failed: {e}")
        return None

    def _solve_with_2captcha():
        if not (HAS_2CAPTCHA and config.get("twocaptcha_key")):
            return None
        try:
            solver = TwoCaptcha(config["twocaptcha_key"])
            if captcha_type == "recaptcha":
                return solver.recaptcha(sitekey=sitekey, url=url)["code"]
            if captcha_type == "hcaptcha":
                return solver.hcaptcha(sitekey=sitekey, url=url)["code"]
        except Exception as e:
            logger.warn(f"2Captcha failed: {e}")
        return None

    solvers = {
        "deathbycaptcha": _solve_with_dbc,
        "2captcha": _solve_with_2captcha,
        "anticaptcha": lambda: _solve_with_anticaptcha(captcha_type, sitekey, url),
        "capsolver": lambda: _solve_with_capsolver(captcha_type, sitekey, url),
    }

    token = None
    provider_order = config.get("captcha_provider_order") or ["deathbycaptcha", "2captcha", "anticaptcha", "capsolver"]
    for provider in provider_order:
        solver = solvers.get(str(provider).strip().lower())
        if not solver:
            continue
        token = solver()
        if token:
            break

    return token
