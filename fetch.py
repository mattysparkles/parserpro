import random
import time
import traceback

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from app_logging import logger, log_once
from config import config, get_effective_proxy
from helpers import USER_AGENTS, normalize_and_validate_target

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service as ChromeService

    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

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
        socket_client = getattr(module, "SocketClient", None)
        if socket_client:
            return socket_client(user, password)
        http_client = getattr(module, "HttpClient", None)
        if http_client:
            return http_client(user, password)
    return None


def _fetch_page_playwright_once(clean_url, effective_proxy):
    launch_args = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
    if effective_proxy:
        launch_args["proxy"] = {"server": effective_proxy["server"]}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            ignore_https_errors=bool(config.get("ignore_https_errors", False)),
            java_script_enabled=True,
            bypass_csp=True,
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        page = context.new_page()
        page.goto(clean_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
        return html


def fetch_page_playwright(url, proxy=None):
    if not HAS_PLAYWRIGHT:
        return None, "playwright_not_installed"

    clean_url, reason = normalize_and_validate_target(url, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
    if not clean_url:
        return None, build_error_payload("invalid_target", reason or "invalid target", reason or "invalid target")

    try:
        effective_proxy = get_effective_proxy(config, proxy)
        if bool(config.get("use_burp", False)) and config.get("burp_proxy", "").strip():
            effective_proxy = {"server": config.get("burp_proxy", "").strip()}
    except RuntimeError as e:
        return None, build_error_payload("proxy_down", "SOCKS proxy unreachable", str(e))

    retried_without_proxy = False
    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            return _fetch_page_playwright_once(clean_url, effective_proxy), None
        except Exception as e:
            stack = _debug_stack(f"Playwright failed {clean_url}", e)
            code, hint = classify_nav_error(str(e))
            detail = str(e)

            if code == "proxy_down" and effective_proxy and not retried_without_proxy:
                log_once("proxy-down", "Proxy appears unreachable; retrying once without proxy", level="WARN")
                effective_proxy = None
                retried_without_proxy = True
                continue

            if code == "conn_closed" and attempts < 2:
                time.sleep(1.0)
                continue

            if code == "dns_failed":
                log_once("dns-failed", "DNS failures detected; recording without immediate retry", level="WARN")

            if code in {"tls_mismatch", "cert_invalid"} and effective_proxy:
                log_once("proxy-tls-hint-playwright", "TLS error detected while proxy is enabled; proxy may be breaking TLS", level="WARN")
            return None, build_error_payload(code, hint, detail, stacktrace=stack)

    return None, build_error_payload("fetch_failed", "Navigation failed", "unknown playwright failure")


def fetch_page_selenium(url, proxy=None):
    if not HAS_SELENIUM:
        return None, "selenium_not_installed"

    clean_url, reason = normalize_and_validate_target(url, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
    if not clean_url:
        return None, build_error_payload("invalid_target", reason or "invalid target", reason or "invalid target")

    try:
        effective_proxy = get_effective_proxy(config, proxy)
        if bool(config.get("use_burp", False)) and config.get("burp_proxy", "").strip():
            effective_proxy = {"server": config.get("burp_proxy", "").strip()}
    except RuntimeError as e:
        return None, build_error_payload("proxy_down", "SOCKS proxy unreachable", str(e))

    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        if effective_proxy and effective_proxy.get("server"):
            options.add_argument(f"--proxy-server={effective_proxy['server']}")

        if bool(config.get("ignore_https_errors", False)):
            options.add_argument("--ignore-certificate-errors")

        driver_path = (config.get("chrome_driver_path") or "").strip()
        if driver_path:
            service = ChromeService(executable_path=driver_path)
            driver = webdriver.Chrome(service=service, options=options)
        else:
            driver = webdriver.Chrome(options=options)

        driver.get(clean_url)
        time.sleep(5)
        html = driver.page_source
        driver.quit()
        return html, None
    except Exception as e:
        stack = _debug_stack(f"Selenium failed {clean_url}", e)
        code, hint = classify_nav_error(str(e))
        message = str(e)
        if "driver" in message.lower() or "chromedriver" in message.lower():
            return None, build_error_payload(
                "driver_error",
                "browser driver is missing or misconfigured",
                f"{message}. Ensure Chrome/Chromium is installed or set chrome_driver_path in config.json",
                stacktrace=stack,
            )
        if code in {"tls_mismatch", "cert_invalid"} and effective_proxy:
            log_once("proxy-tls-hint-selenium", "TLS error detected while proxy is enabled; proxy may be breaking TLS", level="WARN")
        return None, build_error_payload(code, hint, message, stacktrace=stack)


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
        create = requests.post("https://api.capsolver.com/createTask", json=payload, timeout=30).json()
        task_id = create.get("taskId")
        if not task_id:
            logger.warn(f"Capsolver failed to create task: {create}")
            return None
        for _ in range(25):
            result = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": config.get("capsolver_key"), "taskId": task_id},
                timeout=30,
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
