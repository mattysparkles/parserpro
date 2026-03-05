import os
import subprocess
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from config import DATA_DIR, config
from helpers import get_site_filename, log_once


class RunnerMixin:
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

        log_section = ttk.LabelFrame(paned, text="Runner Log")
        log_section.columnconfigure(0, weight=1)
        log_section.rowconfigure(1, weight=1)

        btn_frame = ttk.Frame(log_section)
        btn_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(btn_frame, text="Refresh List", command=self.refresh_runner_list).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Run Selected", command=self.run_selected_hydra).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Run All (Filtered)", command=self.run_all_hydra).pack(side="left", padx=4)
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

        paned.add(table_section, weight=3)
        paned.add(log_section, weight=2)

        for var in (self.min_combos_var, self.min_hits_var, self.status_filter_var, self.last_run_filter_var):
            var.trace_add("write", lambda *_: self.apply_runner_filters_and_sort())

    def _coerce_int(self, value):
        try:
            return int(str(value).strip())
        except Exception:
            return 0

    def _normalize_status(self, data):
        status = str(data.get("status", "")).strip().lower()
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
            hits_file = DATA_DIR / f"hits_{base.replace('.', '_')}.txt"
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
        selected = self._get_selected_sites()
        if not selected:
            messagebox.showinfo("No Selection", "Select at least one row using the checkbox column.")
            return
        self.start_runner_execution(selected)

    def run_all_hydra(self):
        if not self.runner_rows_view:
            messagebox.showinfo("No Sites", "No filtered sites available.")
            return
        self.select_all_filtered()
        self.start_runner_execution([row["site"] for row in self.runner_rows_view if row.get("selected")])

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

            combo_path = self.processed_data.get(site, {}).get("combo_path")
            combo_file = Path(combo_path) if combo_path else DATA_DIR / get_site_filename(site)
            if not combo_file.exists():
                self._append_hydra_log_threadsafe(f"Combo file missing for {site}: {combo_file}\n")
                self._set_row_status(site, "Failed")
                continue

            cmd = cmd_template.replace("{{combo_file}}", str(combo_file.resolve()))
            burp_proxy = config.get("burp_proxy", "").strip()
            if burp_proxy and " -p " not in f" {cmd} ":
                cmd = f"{cmd} -p {burp_proxy}"

            self._append_hydra_log_threadsafe(f"\n=== Starting command for {site} ===\n")
            self._append_hydra_log_threadsafe(f"Command: {cmd}\n")

            try:
                if os.name == "nt" and os.system("wsl --version >nul 2>&1") == 0:
                    process = subprocess.Popen(["wsl", "bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                else:
                    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

                self.runner_active_process = process
                for line in iter(process.stdout.readline, ""):
                    self._append_hydra_log_threadsafe(line)
                    if self.cancel_event.is_set():
                        self.terminate_active_runner_process()
                        break
                    if "[DATA]" in line and "password" in line.lower():
                        with (DATA_DIR / f"hits_{site.replace('.', '_')}.txt").open("a", encoding="utf-8") as hf:
                            hf.write(line.strip() + "\n")

                process.wait(timeout=3)
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
                self.runner_active_process = None

            self._append_hydra_log_threadsafe(f"=== Finished {site} ===\n\n")

        msg = "Runner cancelled." if self.cancel_event.is_set() else "Runner complete."
        self.ui_queue.put(("runner_done", msg))

    def _set_row_status(self, site, status):
        for row in self.runner_rows_all:
            if row["site"] == site:
                row["status"] = status
                break
        self.ui_queue.put(("runner_refresh", None))

    def terminate_active_runner_process(self):
        proc = self.runner_active_process
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
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
