import random
import time

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from config import config
from helpers import USER_AGENTS

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager

    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False

try:
    import deathbycaptcha

    HAS_DEATHBYCAPTCHA = True
except ImportError:
    HAS_DEATHBYCAPTCHA = False

try:
    from twocaptcha import TwoCaptcha

    HAS_2CAPTCHA = True
except ImportError:
    HAS_2CAPTCHA = False


def fetch_page_playwright(url, proxy=None):
    if not HAS_PLAYWRIGHT:
        return None, "playwright_not_installed"

    for attempt in range(2):
        try:
            launch_args = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
            if proxy:
                launch_args["proxy"] = proxy

            with sync_playwright() as p:
                browser = p.chromium.launch(**launch_args)
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=random.choice(USER_AGENTS),
                    locale="en-US",
                    ignore_https_errors=True,
                    java_script_enabled=True,
                    bypass_csp=True,
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
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
            if attempt == 1:
                print(f"Playwright failed {url}: {e}")
            time.sleep(2)
    return None, reason


def fetch_page_selenium(url):
    if not HAS_SELENIUM:
        return None, "selenium_not_installed"

    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        driver = webdriver.Chrome(ChromeDriverManager().install(), options=options)
        driver.get(url)
        time.sleep(5)
        html = driver.page_source
        driver.quit()
        return html, None
    except Exception as e:
        print(f"Selenium failed {url}: {e}")
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
            client = deathbycaptcha.SocketClient(config["dbc_user"], config["dbc_pass"])
            captcha = client.decode(sitekey=sitekey, url=url, type=captcha_type)
            token = captcha["text"]
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
