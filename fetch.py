import random
import time

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from config import config, normalize_proxy
from helpers import USER_AGENTS, validate_url

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


def fetch_page_playwright(url, proxy=None):
    if not HAS_PLAYWRIGHT:
        return None, "playwright_not_installed"

    clean_url = validate_url(url)
    if not clean_url:
        return None, "invalid_url"

    for attempt in range(2):
        try:
            launch_args = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
            proxy_cfg = normalize_proxy(proxy)
            if proxy_cfg:
                launch_args["proxy"] = {"server": proxy_cfg["server"]}

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
                return html, None
        except Exception as e:
            err_str = str(e).lower()
            reason = "unknown"
            if "name not resolved" in err_str:
                reason = "dns"
            elif "connection refused" in err_str:
                reason = "refused"
            elif "timed out" in err_str:
                reason = "timeout"
            elif "ssl" in err_str or "tls" in err_str:
                reason = "tls_error"
            if attempt == 1:
                proxy_hint = " (proxy may be breaking TLS)" if normalize_proxy(proxy) and reason == "tls_error" else ""
                print(f"Playwright failed {clean_url}: {e}{proxy_hint}")
            time.sleep(2)
    return None, reason


def fetch_page_selenium(url, proxy=None):
    if not HAS_SELENIUM:
        return None, "selenium_not_installed"

    clean_url = validate_url(url)
    if not clean_url:
        return None, "invalid_url"

    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        proxy_cfg = normalize_proxy(proxy)
        if proxy_cfg and proxy_cfg.get("server"):
            options.add_argument(f"--proxy-server={proxy_cfg['server']}")

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
        message = str(e)
        if "driver" in message.lower() or "chromedriver" in message.lower():
            message = f"{message}. Ensure Chrome/Chromium is installed or set chrome_driver_path in config.json"
        print(f"Selenium failed {clean_url}: {message}")
        return None, "selenium_error"


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

    token = None
    if HAS_DEATHBYCAPTCHA and config.get("dbc_user") and config.get("dbc_pass"):
        try:
            client = get_dbc_client(config["dbc_user"], config["dbc_pass"])
            if client:
                captcha = client.decode(sitekey=sitekey, url=url, type=captcha_type)
                token = captcha["text"]
            else:
                print("DeathByCaptcha client unavailable (SocketClient/HttpClient not found); skipping DBC.")
        except Exception as e:
            print(f"DeathByCaptcha failed: {e}")

    if not token and HAS_2CAPTCHA and config.get("twocaptcha_key"):
        try:
            solver = TwoCaptcha(config["twocaptcha_key"])
            if captcha_type == "recaptcha":
                token = solver.recaptcha(sitekey=sitekey, url=url)["code"]
            elif captcha_type == "hcaptcha":
                token = solver.hcaptcha(sitekey=sitekey, url=url)["code"]
        except Exception as e:
            print(f"2Captcha failed: {e}")

    return token
