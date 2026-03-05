import os
import subprocess
import threading
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from helpers import get_site_filename


class RunnerMixin:
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
            combo_count = data.get("combo_count", 0)
            last_run = data.get("last_processed", "Never")
            status = "Form Found" if data.get("form_found", False) else "Failed / Pending"
            hits_file = Path(f"hits_{base.replace('.', '_')}.txt")
            hits_count = len(hits_file.read_text(encoding="utf-8").splitlines()) if hits_file.exists() else 0

            self.runner_tree.insert("", "end", values=(base, combo_count, last_run, status, hits_count))

        self._write_log_threadsafe(f"Runner list refreshed: {len(self.processed_data)} sites loaded")

    def run_selected_hydra(self):
        selected = [self.runner_tree.item(iid)["values"][0] for iid in self.runner_tree.selection()]
        if not selected:
            messagebox.showinfo("No Selection", "Select at least one site.")
            return

        self._write_log_threadsafe(f"Starting Hydra on {len(selected)} selected sites...")
        threading.Thread(target=self.execute_hydra, args=(selected,), daemon=True).start()

    def run_all_hydra(self):
        all_sites = [self.runner_tree.item(iid)["values"][0] for iid in self.runner_tree.get_children()]
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

            cmd_template = self.processed_data.get(site, {}).get("hydra_command_template")
            if not cmd_template:
                self._write_log_threadsafe(f"No Hydra command found for {site} — skipping.")
                continue

            combo_file = get_site_filename(site)
            cmd = cmd_template.replace("{{combo_file}}", combo_file)

            self.hydra_log.insert(tk.END, f"\n=== Starting Hydra for {site} ===\n")
            self.hydra_log.insert(tk.END, f"Command: {cmd}\n")
            self.hydra_log.see(tk.END)

            try:
                if os.system("wsl --version >nul 2>&1") == 0:
                    process = subprocess.Popen(["wsl", "bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                else:
                    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

                for line in iter(process.stdout.readline, ""):
                    self.hydra_log.insert(tk.END, line)
                    self.hydra_log.see(tk.END)
                    if "[DATA]" in line and "password" in line.lower():
                        with open(f"hits_{site.replace('.', '_')}.txt", "a", encoding="utf-8") as hf:
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
