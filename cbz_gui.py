"""
CBZ Automation Suite — GUI Launcher
Run any suite tool without touching the command line.
Double-click this file or run: python cbz_gui.py
"""

import os
import sys
import json
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent / "scripts"
LOG_DIR     = Path(__file__).parent
ROUTING_JSON = Path(__file__).parent / "routing.json"

# ── Colour palette (works on both light and dark Windows themes) ───────────────
BG        = "#1a1a2e"   # deep navy
PANEL     = "#16213e"   # slightly lighter panel
CARD      = "#0f3460"   # card background
ACCENT    = "#e94560"   # coral-red accent
ACCENT2   = "#533483"   # purple accent
TEXT      = "#eaeaea"
MUTED     = "#888"
SUCCESS   = "#4caf50"
WARNING   = "#ff9800"
ERROR     = "#f44336"
LOG_BG    = "#0d0d1a"
LOG_FG    = "#c8ffc8"   # green terminal text

FONT_HEAD = ("Segoe UI", 22, "bold")
FONT_SUB  = ("Segoe UI", 12)
FONT_BODY = ("Segoe UI", 10)
FONT_MONO = ("Consolas", 9)

# ── Tool definitions ────────────────────────────────────────────────────────────
# scan_folder_flag: how the folder is passed to each script.
#   "positional"  — appended as a bare arg (default, works for most tools)
#   "--scan"      — passed as --scan=<path> (used by cbz_sanitizer.py)
TOOLS = [
    {
        "id": "sanitizer",
        "label": "CBZ Sanitizer",
        "script": "cbz_sanitizer.py",
        "description": "Clean filenames and fix ComicInfo.xml metadata in-place across a library folder.",
        "icon": "\u2726",
        "color": ACCENT,
        "scan_folder_flag": "--scan",
        "options": [
            {"type": "folder",       "key": "scan_folder", "label": "Scan folder",             "default": r"\\tower\media\comics\manga"},
            {"type": "select",       "key": "sort",        "label": "Sort order",              "choices": ["newest", "oldest", "alpha", "alpha-reverse"], "default": "newest"},
            {"type": "checkbox",     "key": "dry_run",     "label": "Dry run (preview only)",  "default": False},
            {"type": "checkbox",     "key": "restart",     "label": "Restart (clear progress)","default": False},
            {"type": "checkbox",     "key": "resume",      "label": "Resume from last run",    "default": False},
            {"type": "multi_select", "key": "rules",       "label": "Active rules",
             "choices": ["brackets", "comicinfo", "leading_nums", "non_latin",
                          "normalize_stem", "number_tokens", "scan_groups", "trailing_junk", "url"],
             "default": [],
             "note": "Leave all unchecked to run every rule. Check specific rules to run only those."},
        ],
    },
    {
        "id": "watcher",
        "label": "CBZ Watcher",
        "script": "cbz_watcher.py",
        "description": "Monitor a folder for incoming .cbz files, process them, and route to the correct destination.",
        "icon": "\u25ce",
        "color": "#2196F3",
        "options": [],
        "note": "Runs continuously \u2014 click Stop to shut it down.",
    },
    {
        "id": "folder_merger",
        "label": "Folder Merger",
        "script": "cbz_folder_merger.py",
        "description": "Merge sibling folders that represent the same series split across chapter-numbered directories.",
        "icon": "\u2295",
        "color": "#9C27B0",
        "options": [
            {"type": "folder",   "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run",     "label": "Dry run (preview only)", "default": True},
        ],
    },
    {
        "id": "compilation_resolver",
        "label": "Compilation Resolver",
        "script": "cbz_compilation_resolver.py",
        "description": "Detect compilation archives that overlap with individual chapters and rebuild them with the best pages.",
        "icon": "\u229e",
        "color": "#00BCD4",
        "options": [
            {"type": "folder",   "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run",     "label": "Dry run (preview only)", "default": True},
        ],
    },
    {
        "id": "deduplicator",
        "label": "Deduplicator",
        "script": "cbz_deduplicator.py",
        "description": "Remove duplicate .cbz/.cbr files and pack loose image folders into archives.",
        "icon": "\u2297",
        "color": "#FF5722",
        "options": [
            {"type": "folder",   "key": "scan_folder",    "label": "Library folder",          "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run",        "label": "Dry run (preview only)",  "default": True},
            {"type": "checkbox", "key": "no_recursive",   "label": "Single-level only (--no-recursive)", "default": False},
        ],
    },
    {
        "id": "number_tagger",
        "label": "Number Tagger",
        "script": "cbz_number_tagger.py",
        "description": "Retroactively set <Number> and <Volume> ComicInfo.xml tags from filenames.",
        "icon": "\u229f",
        "color": "#8BC34A",
        "options": [
            {"type": "folder",   "key": "scan_folder", "label": "Library folder",         "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run",     "label": "Dry run (preview only)", "default": True},
        ],
    },
    {
        "id": "series_matcher",
        "label": "Series Matcher",
        "script": "cbz_series_matcher.py",
        "description": "Detect near-duplicate series folder names and auto-merge above the similarity threshold.",
        "icon": "\u2248",
        "color": "#FF9800",
        "options": [
            {"type": "checkbox", "key": "dry_run", "label": "Dry run (preview only)", "default": True},
        ],
        "note": "Scans the SCAN_FOLDERS configured inside the script.",
    },
    {
        "id": "gap_checker",
        "label": "Gap Checker",
        "script": "cbz_gap_checker.py",
        "description": "Scan library for missing chapter numbers and write a timestamped CSV report.",
        "icon": "\u2298",
        "color": "#009688",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
        ],
        "note": "Read-only \u2014 never modifies files.",
    },
    {
        "id": "strip_duplicates",
        "label": "Strip Duplicates",
        "script": "strip_duplicates.py",
        "description": "Remove duplicate number tokens from filenames (e.g. 'ch. 5 ch.5') and fix spaced punctuation.",
        "icon": "\u229c",
        "color": "#607D8B",
        "options": [
            {"type": "folder",   "key": "scan_folder",  "label": "Library folder",          "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run",      "label": "Dry run (preview only)",  "default": True},
            {"type": "checkbox", "key": "no_recursive", "label": "Single-level only (--no-recursive)", "default": False},
        ],
    },
    {
        "id": "uncensored_dupes",
        "label": "Uncensored Dupes",
        "script": "find_uncensored_dupes.py",
        "description": "Find folders that are censored/uncensored duplicates of each other and move them to a _Check folder for manual review.",
        "icon": "\u26b2",
        "color": "#E91E63",
        "scan_folder_flag": "--library",
        "options": [
            {"type": "folder",   "key": "scan_folder", "label": "Library folder",         "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run",     "label": "Dry run (preview only)", "default": True},
            {"type": "select",   "key": "move_which",  "label": "Move which folder(s)",   "choices": ["both", "uncensored", "censored"], "default": "both"},
        ],
        "note": "Moves matched pairs into a _Check subfolder inside the library. Unmatched uncensored-only folders are left alone.",
    },
]


class CBZLauncherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CBZ Automation Suite")
        self.geometry("1100x720")
        self.minsize(900, 600)
        self.configure(bg=BG)

        self._proc = None
        self._log_queue = queue.Queue()
        self._active_tool = None
        self._option_vars = {}
        self._running = False

        self._build_ui()
        self._select_tool(TOOLS[0])
        self._poll_log_queue()

    # ── Layout ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Left sidebar
        sidebar = tk.Frame(self, bg=PANEL, width=220)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        # App title
        header = tk.Frame(sidebar, bg=PANEL, pady=16)
        header.pack(fill=tk.X)
        tk.Label(header, text="CBZ Suite", font=("Segoe UI", 16, "bold"),
                 bg=PANEL, fg=TEXT).pack(padx=16, anchor="w")
        tk.Label(header, text="Automation Launcher", font=FONT_BODY,
                 bg=PANEL, fg=MUTED).pack(padx=16, anchor="w")

        tk.Frame(sidebar, bg=ACCENT, height=1).pack(fill=tk.X, padx=12)

        # Tool buttons
        self._sidebar_buttons = {}
        tools_frame = tk.Frame(sidebar, bg=PANEL)
        tools_frame.pack(fill=tk.BOTH, expand=True, pady=8)

        for tool in TOOLS:
            btn = self._make_sidebar_btn(tools_frame, tool)
            self._sidebar_buttons[tool["id"]] = btn

        # Main content area
        self._main = tk.Frame(self, bg=BG)
        self._main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Top bar
        self._topbar = tk.Frame(self._main, bg=BG, pady=0)
        self._topbar.pack(fill=tk.X, padx=24, pady=(20, 0))

        self._tool_icon_lbl = tk.Label(self._topbar, text="", font=("Segoe UI", 28),
                                        bg=BG, fg=ACCENT)
        self._tool_icon_lbl.pack(side=tk.LEFT)

        title_block = tk.Frame(self._topbar, bg=BG)
        title_block.pack(side=tk.LEFT, padx=(10, 0))
        self._tool_name_lbl = tk.Label(title_block, text="", font=FONT_HEAD,
                                        bg=BG, fg=TEXT)
        self._tool_name_lbl.pack(anchor="w")
        self._tool_desc_lbl = tk.Label(title_block, text="", font=FONT_BODY,
                                        bg=BG, fg=MUTED, wraplength=600, justify="left")
        self._tool_desc_lbl.pack(anchor="w")

        # Options panel
        self._opts_frame = tk.Frame(self._main, bg=BG)
        self._opts_frame.pack(fill=tk.X, padx=24, pady=12)

        # Run / Stop buttons
        btn_row = tk.Frame(self._main, bg=BG)
        btn_row.pack(fill=tk.X, padx=24, pady=(0, 12))

        self._run_btn = tk.Button(btn_row, text="\u25b6  Run", font=("Segoe UI", 11, "bold"),
                                   bg=ACCENT, fg="white", relief="flat", cursor="hand2",
                                   padx=24, pady=8, command=self._run_tool)
        self._run_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._stop_btn = tk.Button(btn_row, text="\u25a0  Stop", font=("Segoe UI", 11),
                                    bg=CARD, fg=MUTED, relief="flat", cursor="hand2",
                                    padx=20, pady=8, command=self._stop_tool, state="disabled")
        self._stop_btn.pack(side=tk.LEFT)

        self._status_lbl = tk.Label(btn_row, text="", font=FONT_BODY, bg=BG, fg=MUTED)
        self._status_lbl.pack(side=tk.LEFT, padx=16)

        # Divider
        tk.Frame(self._main, bg="#2a2a4a", height=1).pack(fill=tk.X, padx=24)

        # Log output
        log_label = tk.Label(self._main, text="Output", font=("Segoe UI", 10, "bold"),
                              bg=BG, fg=MUTED)
        log_label.pack(anchor="w", padx=24, pady=(8, 2))

        log_frame = tk.Frame(self._main, bg=LOG_BG, relief="flat",
                              highlightbackground="#2a2a4a", highlightthickness=1)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=24, pady=(0, 16))

        self._log = scrolledtext.ScrolledText(
            log_frame, bg=LOG_BG, fg=LOG_FG,
            font=FONT_MONO, relief="flat", bd=0,
            state="disabled", wrap=tk.WORD,
            insertbackground=LOG_FG
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Colour tags for the log
        self._log.tag_config("info",    foreground=LOG_FG)
        self._log.tag_config("warn",    foreground=WARNING)
        self._log.tag_config("error",   foreground=ERROR)
        self._log.tag_config("success", foreground=SUCCESS)
        self._log.tag_config("muted",   foreground=MUTED)

        clear_btn = tk.Button(self._main, text="Clear log", font=FONT_BODY,
                               bg=BG, fg=MUTED, relief="flat", cursor="hand2",
                               command=self._clear_log)
        clear_btn.pack(anchor="e", padx=24, pady=(0, 8))

    def _make_sidebar_btn(self, parent, tool):
        color = tool["color"]
        frame = tk.Frame(parent, bg=PANEL, cursor="hand2")
        frame.pack(fill=tk.X, padx=8, pady=1)

        accent_bar = tk.Frame(frame, bg=PANEL, width=4)
        accent_bar.pack(side=tk.LEFT, fill=tk.Y)

        inner = tk.Frame(frame, bg=PANEL, pady=10)
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8)

        icon_lbl = tk.Label(inner, text=tool["icon"], font=("Segoe UI", 14),
                             bg=PANEL, fg=color)
        icon_lbl.pack(side=tk.LEFT)

        name_lbl = tk.Label(inner, text=tool["label"], font=("Segoe UI", 10),
                             bg=PANEL, fg=TEXT, anchor="w")
        name_lbl.pack(side=tk.LEFT, padx=6)

        def on_click(t=tool, ab=accent_bar, f=frame, il=icon_lbl, nl=name_lbl):
            self._select_tool(t)

        for widget in [frame, inner, icon_lbl, name_lbl, accent_bar]:
            widget.bind("<Button-1>", lambda e, fn=on_click: fn())
            widget.bind("<Enter>", lambda e, f=frame, il=icon_lbl, nl=name_lbl:
                        self._sidebar_hover(f, il, nl, True))
            widget.bind("<Leave>", lambda e, f=frame, il=icon_lbl, nl=name_lbl:
                        self._sidebar_hover(f, il, nl, False))

        return {"frame": frame, "accent": accent_bar, "icon": icon_lbl,
                "name": name_lbl, "color": color}

    def _sidebar_hover(self, frame, icon_lbl, name_lbl, entering):
        if self._active_tool and frame == self._sidebar_buttons[self._active_tool["id"]]["frame"]:
            return
        bg = "#1e2d50" if entering else PANEL
        for w in [frame, icon_lbl, name_lbl]:
            w.configure(bg=bg)

    def _select_tool(self, tool):
        self._active_tool = tool

        # Update sidebar highlight
        for tid, widgets in self._sidebar_buttons.items():
            is_active = tid == tool["id"]
            bg = CARD if is_active else PANEL
            ac = widgets["color"] if is_active else PANEL
            for w in [widgets["frame"], widgets["icon"], widgets["name"]]:
                w.configure(bg=bg)
            widgets["accent"].configure(bg=ac)

        # Update top bar
        self._tool_icon_lbl.configure(text=tool["icon"], fg=tool["color"])
        self._tool_name_lbl.configure(text=tool["label"])
        self._tool_desc_lbl.configure(text=tool.get("description", ""))

        # Rebuild options
        for widget in self._opts_frame.winfo_children():
            widget.destroy()
        self._option_vars = {}

        options = tool.get("options", [])
        if not options:
            note = tool.get("note", "No configurable options \u2014 uses SCAN_FOLDERS from the script.")
            tk.Label(self._opts_frame, text=note, font=FONT_BODY,
                     bg=BG, fg=MUTED).pack(anchor="w", pady=4)
        else:
            for opt in options:
                self._build_option_row(self._opts_frame, opt)

        if "note" in tool:
            tk.Label(self._opts_frame, text=f"\u2139  {tool['note']}", font=FONT_BODY,
                     bg=BG, fg="#5599cc").pack(anchor="w", pady=(8, 0))

        self._log_line(f"Selected: {tool['label']}", "muted")

    def _build_option_row(self, parent, opt):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill=tk.X, pady=3)

        tk.Label(row, text=opt["label"], font=FONT_BODY, bg=BG, fg=MUTED,
                 width=30, anchor="w").pack(side=tk.LEFT)

        if opt["type"] == "folder":
            var = tk.StringVar(value=opt.get("default", ""))
            self._option_vars[opt["key"]] = var
            entry = tk.Entry(row, textvariable=var, font=FONT_BODY,
                             bg="#12122a", fg=TEXT, insertbackground=TEXT,
                             relief="flat", highlightbackground="#3a3a6a",
                             highlightthickness=1, width=40)
            entry.pack(side=tk.LEFT, padx=(0, 4))
            tk.Button(row, text="Browse\u2026", font=FONT_BODY, bg=CARD, fg=TEXT,
                      relief="flat", cursor="hand2",
                      command=lambda v=var: self._browse(v)
                      ).pack(side=tk.LEFT)

        elif opt["type"] == "checkbox":
            var = tk.BooleanVar(value=opt.get("default", False))
            self._option_vars[opt["key"]] = var
            cb = tk.Checkbutton(row, variable=var, bg=BG, fg=TEXT,
                                 activebackground=BG, activeforeground=TEXT,
                                 selectcolor="#12122a", relief="flat")
            cb.pack(side=tk.LEFT)

        elif opt["type"] == "select":
            var = tk.StringVar(value=opt.get("default", opt["choices"][0]))
            self._option_vars[opt["key"]] = var
            om = tk.OptionMenu(row, var, *opt["choices"])
            om.configure(bg="#12122a", fg=TEXT, activebackground=CARD,
                          activeforeground=TEXT, relief="flat", highlightthickness=0,
                          font=FONT_BODY)
            om["menu"].configure(bg="#12122a", fg=TEXT, activebackground=CARD,
                                  activeforeground=TEXT, font=FONT_BODY)
            om.pack(side=tk.LEFT)

        elif opt["type"] == "multi_select":
            # Renders each choice as an individual checkbox; stores a list var
            choices  = opt["choices"]
            defaults = set(opt.get("default") or [])
            check_vars = {c: tk.BooleanVar(value=(c in defaults)) for c in choices}
            self._option_vars[opt["key"]] = check_vars   # dict[str, BooleanVar]
            # Use a sub-frame so checkboxes wrap naturally
            cb_frame = tk.Frame(row, bg=BG)
            cb_frame.pack(side=tk.LEFT, fill=tk.X)
            for c in choices:
                tk.Checkbutton(
                    cb_frame, text=c, variable=check_vars[c],
                    bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                    selectcolor="#12122a", relief="flat", font=FONT_BODY,
                ).pack(side=tk.LEFT, padx=(0, 6))
            if opt.get("note"):
                tk.Label(cb_frame, text=opt["note"], font=("Segoe UI", 8),
                         bg=BG, fg=MUTED).pack(side=tk.LEFT, padx=(8, 0))

    def _browse(self, var):
        path = filedialog.askdirectory(initialdir=var.get() or "C:\\")
        if path:
            var.set(path)

    # ── Run / Stop ──────────────────────────────────────────────────────────────
    def _build_command(self, tool):
        script = SCRIPT_DIR / tool["script"]
        cmd = [sys.executable, str(script)]
        opts = self._option_vars

        # Pass the scan folder using the method this script expects.
        # Most scripts take a bare positional path; cbz_sanitizer uses --scan=<path>;
        # find_uncensored_dupes uses --library <path>.
        folder_flag = tool.get("scan_folder_flag", "positional")
        scan_folder = opts.get("scan_folder")
        if scan_folder and scan_folder.get():
            folder_path = scan_folder.get()
            if folder_flag == "--scan":
                cmd.append(f"--scan={folder_path}")
            elif folder_flag == "--library":
                cmd.extend(["--library", folder_path])
            else:
                cmd.append(folder_path)

        if opts.get("dry_run") and opts["dry_run"].get():
            # Most scripts use --dry-run; find_uncensored_dupes defaults to dry-run
            # and uses --live to opt in, so we just omit any flag in that case.
            if folder_flag != "--library":
                cmd.append("--dry-run")
        else:
            # Live run: scripts that use --live instead of absence-of-dry-run
            if folder_flag == "--library":
                cmd.append("--live")
        if opts.get("restart") and opts["restart"].get():
            cmd.append("--restart")
        if opts.get("resume") and opts["resume"].get():
            cmd.append("--resume")
        if opts.get("no_recursive") and opts["no_recursive"].get():
            cmd.append("--no-recursive")

        sort_var = opts.get("sort")
        if sort_var:
            cmd.append(f"--sort={sort_var.get()}")

        move_var = opts.get("move_which")
        if move_var:
            cmd.extend(["--move", move_var.get()])

        # multi_select rules: only pass --rules= if at least one box is checked;
        # all-unchecked means "run everything" (no flag needed)
        rules_var = opts.get("rules")
        if rules_var and isinstance(rules_var, dict):
            selected = [r for r, v in rules_var.items() if v.get()]
            if selected:
                cmd.append(f"--rules={','.join(sorted(selected))}")

        return cmd

    def _run_tool(self):
        if self._running:
            return
        tool = self._active_tool
        if not tool:
            return

        script_path = SCRIPT_DIR / tool["script"]
        if not script_path.exists():
            self._log_line(f"ERROR: Script not found: {script_path}", "error")
            return

        cmd = self._build_command(tool)
        self._log_line("\u2500" * 60, "muted")
        self._log_line(f"Running: {' '.join(cmd)}", "muted")
        self._log_line("\u2500" * 60, "muted")

        self._running = True
        self._run_btn.configure(state="disabled", bg="#555", fg=MUTED)
        self._stop_btn.configure(state="normal", bg=ERROR, fg="white")
        self._status_lbl.configure(text="Running\u2026", fg=WARNING)

        def target():
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(SCRIPT_DIR.parent),
                )
                for line in self._proc.stdout:
                    self._log_queue.put(line.rstrip())
                self._proc.wait()
                rc = self._proc.returncode
                if rc == 0:
                    self._log_queue.put("__DONE_OK__")
                else:
                    self._log_queue.put(f"__DONE_ERR__{rc}")
            except Exception as exc:
                self._log_queue.put(f"__ERROR__{exc}")

        threading.Thread(target=target, daemon=True).start()

    def _stop_tool(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._log_line("Process stopped by user.", "warn")
        self._set_idle()

    def _set_idle(self):
        self._running = False
        self._proc = None
        self._run_btn.configure(state="normal", bg=ACCENT, fg="white")
        self._stop_btn.configure(state="disabled", bg=CARD, fg=MUTED)
        self._status_lbl.configure(text="")

    # ── Log ─────────────────────────────────────────────────────────────────────
    def _poll_log_queue(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                if line == "__DONE_OK__":
                    self._log_line("Done \u2014 completed successfully.", "success")
                    self._set_idle()
                    self._status_lbl.configure(text="Finished", fg=SUCCESS)
                elif line.startswith("__DONE_ERR__"):
                    rc = line[len("__DONE_ERR__"):]
                    self._log_line(f"Process exited with code {rc}.", "error")
                    self._set_idle()
                    self._status_lbl.configure(text=f"Exit code {rc}", fg=ERROR)
                elif line.startswith("__ERROR__"):
                    msg = line[len("__ERROR__"):]
                    self._log_line(f"Launch error: {msg}", "error")
                    self._set_idle()
                else:
                    tag = "info"
                    lo = line.lower()
                    if "error" in lo or "failed" in lo:
                        tag = "error"
                    elif "warning" in lo or "warn" in lo or "skipping" in lo:
                        tag = "warn"
                    elif "complete" in lo or "done" in lo or "success" in lo:
                        tag = "success"
                    self._log_line(line, tag)
        except queue.Empty:
            pass
        self.after(80, self._poll_log_queue)

    def _log_line(self, text, tag="info"):
        self._log.configure(state="normal")
        self._log.insert(tk.END, text + "\n", tag)
        self._log.see(tk.END)
        self._log.configure(state="disabled")

    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.configure(state="disabled")


def main():
    app = CBZLauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
