"""
CBZ Automation Suite — GUI Launcher
Run any suite tool without touching the command line.
Double-click this file or run: python cbz_gui.py
"""

import os
import sys
import json
import queue
import re
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

SIDEBAR_W = 244
MIN_WINDOW_W = 1180
MIN_WINDOW_H = 760
PAD_X = 24
PAD_Y = 12
SMALL_GAP = 6
ROW_GAP = 8
SECTION_GAP = 14
FIELD_BG = "#12122a"
BORDER = "#2a2a4a"

# ── Tool definitions ────────────────────────────────────────────────────────────
# scan_folder_flag: how the folder is passed to each script.
#   "positional"  — appended as a bare arg (default, works for most tools)
#   "--scan"      — passed as --scan=<path> (used by cbz_sanitizer.py)
TOOLS = [
    {
        "id": "sanitizer",
        "label": "CBZ Sanitizer",
        "script": "cbz_sanitizer.py",
        "description": "Batch clean filenames and repair ComicInfo metadata in-place.",
        "icon": "\u2726",
        "color": ACCENT,
        "scan_folder_flag": "--scan",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Scan folder", "default": r"\\tower\media\comics"},
            {"type": "select", "key": "sort", "label": "Sort order", "choices": ["newest", "oldest", "alpha", "alpha-reverse"], "default": "newest"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run (preview only)", "default": False},
            {"type": "checkbox", "key": "restart", "label": "Restart (clear progress)", "default": False},
            {"type": "checkbox", "key": "resume", "label": "Resume from last run", "default": False},
        ],
    },
    {
        "id": "watcher",
        "label": "CBZ Watcher",
        "script": "cbz_watcher.py",
        "description": "Monitor incoming folders, normalize files, update ComicInfo, and route automatically.",
        "icon": "\u25ce",
        "color": "#2196F3",
        "options": [],
        "note": "Runs continuously — click Stop to shut it down.",
    },
    {
        "id": "library_archive_clean",
        "label": "Archive Cleaner",
        "script": "cbz_library_maintenance.py",
        "subcommand": "archive-clean",
        "description": "Clean duplicate archives, strip duplicate filename tokens, and pack loose image folders.",
        "icon": "\u2297",
        "color": "#FF5722",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run (preview only)", "default": True},
            {"type": "checkbox", "key": "no_recursive", "label": "Single-level only (--no-recursive)", "default": False},
            {"type": "select", "key": "workers", "label": "Workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "library_organizer",
        "label": "Series Organizer",
        "script": "cbz_library_maintenance.py",
        "subcommand": "organize-series",
        "description": "Merge split chapter folders, fuzzy-match duplicate series, and optionally find uncensored pairs.",
        "icon": "\u2295",
        "color": "#9C27B0",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run (preview only)", "default": True},
            {"type": "checkbox", "key": "uncensored_check", "label": "Also check uncensored/decensored pairs", "default": False},
            {"type": "checkbox", "key": "possible_series_check", "label": "Move likely same-series folders to _Check", "default": False},
            {"type": "select", "key": "series_common_words", "label": "Common title words", "choices": ["1", "2", "3", "4"], "default": "2"},
            {"type": "select", "key": "series_min_group_size", "label": "Minimum group size", "choices": ["2", "3", "4", "5"], "default": "2"},
            {"type": "select", "key": "workers", "label": "Workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "metadata_repair",
        "label": "Metadata Repair",
        "script": "cbz_library_maintenance.py",
        "subcommand": "metadata",
        "description": "Retroactively repair ComicInfo title, series, number, and volume using cbz_core.",
        "icon": "\u229f",
        "color": "#8BC34A",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run (preview only)", "default": True},
            {"type": "select", "key": "workers", "label": "Workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "library_all",
        "label": "Full Maintenance",
        "script": "cbz_library_maintenance.py",
        "subcommand": "all",
        "description": "Run archive cleanup, series organization, and metadata repair in one pass.",
        "icon": "\u2605",
        "color": "#E91E63",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run (preview only)", "default": True},
            {"type": "select", "key": "workers", "label": "Workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "compilation_resolver",
        "label": "Compilation Resolver",
        "script": "cbz_compilation_resolver.py",
        "description": "Resolve compilation archives that overlap with individual chapters.",
        "icon": "\u229e",
        "color": "#00BCD4",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run (preview only)", "default": True},
        ],
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
        "note": "Read-only — never modifies files.",
    },
]



class CBZLauncherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CBZ Automation Suite")
        self.geometry(f"{MIN_WINDOW_W}x{MIN_WINDOW_H}")
        self.minsize(MIN_WINDOW_W, MIN_WINDOW_H)
        self.configure(bg=BG)
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._proc = None
        self._log_queue = queue.Queue()
        self._active_tool = None
        self._option_vars = {}
        self._option_traces = []
        self._preview_after = None
        self._progress_seen = 0
        self._progress_mode = "idle"
        self._running = False

        self._build_ui()
        self._select_tool(TOOLS[0])
        self._poll_log_queue()

    # ── Layout ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Left sidebar
        sidebar = tk.Frame(self, bg=PANEL, width=SIDEBAR_W)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(2, weight=1)

        # App title
        header = tk.Frame(sidebar, bg=PANEL, pady=16)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="CBZ Suite", font=("Segoe UI", 16, "bold"),
                 bg=PANEL, fg=TEXT).pack(padx=16, anchor="w")
        tk.Label(header, text="Automation Launcher", font=FONT_BODY,
                 bg=PANEL, fg=MUTED).pack(padx=16, anchor="w")

        tk.Frame(sidebar, bg=ACCENT, height=1).grid(row=1, column=0, sticky="ew", padx=12)

        # Tool buttons
        self._sidebar_buttons = {}
        sidebar_scroll, tools_frame = self._make_scrollable_frame(sidebar, PANEL)
        sidebar_scroll.grid(row=2, column=0, sticky="nsew", pady=8)

        for tool in TOOLS:
            btn = self._make_sidebar_btn(tools_frame, tool)
            self._sidebar_buttons[tool["id"]] = btn

        # Main content area
        self._main = tk.Frame(self, bg=BG)
        self._main.grid(row=0, column=1, sticky="nsew")
        self._main.grid_columnconfigure(0, weight=1)
        self._main.grid_rowconfigure(1, weight=1)
        self._main.grid_rowconfigure(7, weight=3)

        self._style = ttk.Style(self)
        self._style.configure(
            "CBZ.Horizontal.TProgressbar",
            troughcolor=FIELD_BG,
            background=ACCENT,
            bordercolor=BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

        # Top bar
        self._topbar = tk.Frame(self._main, bg=BG, pady=0)
        self._topbar.grid(row=0, column=0, sticky="ew", padx=PAD_X, pady=(20, 0))
        self._topbar.grid_columnconfigure(1, weight=1)
        self._topbar.bind("<Configure>", self._sync_wraplengths)

        self._tool_icon_lbl = tk.Label(self._topbar, text="", font=("Segoe UI", 28),
                                        bg=BG, fg=ACCENT)
        self._tool_icon_lbl.grid(row=0, column=0, sticky="nw")

        title_block = tk.Frame(self._topbar, bg=BG)
        title_block.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        title_block.grid_columnconfigure(0, weight=1)
        self._tool_name_lbl = tk.Label(title_block, text="", font=FONT_HEAD,
                                        bg=BG, fg=TEXT, anchor="w", justify="left")
        self._tool_name_lbl.grid(row=0, column=0, sticky="ew")
        self._tool_desc_lbl = tk.Label(title_block, text="", font=FONT_BODY,
                                        bg=BG, fg=MUTED, wraplength=600, justify="left")
        self._tool_desc_lbl.grid(row=1, column=0, sticky="ew")

        # Options panel
        opts_shell = tk.Frame(self._main, bg=BG)
        opts_shell.grid(row=1, column=0, sticky="nsew", padx=PAD_X, pady=PAD_Y)
        opts_shell.grid_columnconfigure(0, weight=1)
        opts_shell.grid_rowconfigure(1, weight=1)
        tk.Label(opts_shell, text="Options", font=("Segoe UI", 10, "bold"),
                 bg=BG, fg=MUTED).grid(row=0, column=0, sticky="w", pady=(0, 4))
        opts_scroll, self._opts_frame = self._make_scrollable_frame(opts_shell, BG)
        opts_scroll.grid(row=1, column=0, sticky="nsew")
        self._opts_frame.grid_columnconfigure(0, weight=0, minsize=210)
        self._opts_frame.grid_columnconfigure(1, weight=1)

        # Command preview
        preview_label = tk.Label(self._main, text="Command Preview",
                                 font=("Segoe UI", 10, "bold"), bg=BG, fg=MUTED)
        preview_label.grid(row=2, column=0, sticky="w", padx=PAD_X, pady=(0, 2))

        preview_frame = tk.Frame(self._main, bg=LOG_BG, relief="flat",
                                 highlightbackground=BORDER, highlightthickness=1)
        preview_frame.grid(row=3, column=0, sticky="ew", padx=PAD_X, pady=(0, SECTION_GAP))
        preview_frame.grid_columnconfigure(0, weight=1)
        self._command_preview = tk.Text(
            preview_frame, bg=LOG_BG, fg=TEXT, font=FONT_MONO,
            height=3, relief="flat", bd=0, wrap=tk.WORD,
            state="disabled", insertbackground=TEXT
        )
        self._command_preview.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        # Run / Stop buttons
        btn_row = tk.Frame(self._main, bg=BG)
        btn_row.grid(row=4, column=0, sticky="ew", padx=PAD_X, pady=(0, SECTION_GAP))
        btn_row.grid_columnconfigure(2, weight=1)

        self._run_btn = tk.Button(btn_row, text="\u25b6  Run", font=("Segoe UI", 11, "bold"),
                                   bg=ACCENT, fg="white", relief="flat", cursor="hand2",
                                   padx=24, pady=8, command=self._run_tool)
        self._run_btn.grid(row=0, column=0, sticky="w", padx=(0, 8))

        self._stop_btn = tk.Button(btn_row, text="\u25a0  Stop", font=("Segoe UI", 11),
                                    bg=CARD, fg=MUTED, relief="flat", cursor="hand2",
                                    padx=20, pady=8, command=self._stop_tool, state="disabled")
        self._stop_btn.grid(row=0, column=1, sticky="w")

        self._status_lbl = tk.Label(btn_row, text="", font=FONT_BODY, bg=BG, fg=MUTED)
        self._status_lbl.grid(row=0, column=2, sticky="w", padx=16)

        progress_frame = tk.Frame(self._main, bg=BG)
        progress_frame.grid(row=5, column=0, sticky="ew", padx=PAD_X, pady=(0, SECTION_GAP))
        progress_frame.grid_columnconfigure(1, weight=1)

        tk.Label(progress_frame, text="Progress", font=("Segoe UI", 10, "bold"),
                 bg=BG, fg=MUTED).grid(row=0, column=0, sticky="w", padx=(0, SECTION_GAP))

        self._progress = ttk.Progressbar(
            progress_frame,
            mode="determinate",
            maximum=100,
            style="CBZ.Horizontal.TProgressbar",
        )
        self._progress.grid(row=0, column=1, sticky="ew")

        self._progress_lbl = tk.Label(
            progress_frame, text="Idle", font=FONT_BODY, bg=BG, fg=MUTED,
            anchor="e", width=24
        )
        self._progress_lbl.grid(row=0, column=2, sticky="e", padx=(SECTION_GAP, 0))

        # Divider
        tk.Frame(self._main, bg=BORDER, height=1).grid(row=6, column=0, sticky="ew", padx=PAD_X)

        # Log output
        log_label = tk.Label(self._main, text="Output", font=("Segoe UI", 10, "bold"),
                              bg=BG, fg=MUTED)
        log_label.grid(row=7, column=0, sticky="nw", padx=PAD_X, pady=(8, 2))

        log_frame = tk.Frame(self._main, bg=LOG_BG, relief="flat",
                              highlightbackground=BORDER, highlightthickness=1)
        log_frame.grid(row=7, column=0, sticky="nsew", padx=PAD_X, pady=(28, 16))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)

        self._log = scrolledtext.ScrolledText(
            log_frame, bg=LOG_BG, fg=LOG_FG,
            font=FONT_MONO, relief="flat", bd=0,
            state="disabled", wrap=tk.WORD,
            insertbackground=LOG_FG
        )
        self._log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        # Colour tags for the log
        self._log.tag_config("info",    foreground=LOG_FG)
        self._log.tag_config("warn",    foreground=WARNING)
        self._log.tag_config("error",   foreground=ERROR)
        self._log.tag_config("success", foreground=SUCCESS)
        self._log.tag_config("muted",   foreground=MUTED)

        clear_btn = tk.Button(self._main, text="Clear log", font=FONT_BODY,
                               bg=BG, fg=MUTED, relief="flat", cursor="hand2",
                               command=self._clear_log)
        clear_btn.grid(row=8, column=0, sticky="e", padx=PAD_X, pady=(0, 8))

    def _make_scrollable_frame(self, parent, bg):
        shell = tk.Frame(parent, bg=bg)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        canvas = tk.Canvas(shell, bg=bg, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        content = tk.Frame(canvas, bg=bg)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width)
        )
        self._bind_mousewheel(canvas)
        return shell, content

    def _bind_mousewheel(self, canvas):
        def on_wheel(event):
            if event.delta:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

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

        return {"frame": frame, "inner": inner, "accent": accent_bar, "icon": icon_lbl,
                "name": name_lbl, "color": color}

    def _sidebar_hover(self, frame, icon_lbl, name_lbl, entering):
        if self._active_tool and frame == self._sidebar_buttons[self._active_tool["id"]]["frame"]:
            return
        bg = "#1e2d50" if entering else PANEL
        inner = next((widgets["inner"] for widgets in self._sidebar_buttons.values()
                      if widgets["frame"] == frame), None)
        for w in [frame, inner, icon_lbl, name_lbl]:
            if not w:
                continue
            w.configure(bg=bg)

    def _select_tool(self, tool):
        self._active_tool = tool
        self._option_traces = []

        # Update sidebar highlight
        for tid, widgets in self._sidebar_buttons.items():
            is_active = tid == tool["id"]
            bg = CARD if is_active else PANEL
            ac = widgets["color"] if is_active else PANEL
            for w in [widgets["frame"], widgets["inner"], widgets["icon"], widgets["name"]]:
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
                     bg=BG, fg=MUTED, wraplength=760, justify="left").grid(
                         row=0, column=0, columnspan=2, sticky="ew", pady=4)
        else:
            for row_num, opt in enumerate(options):
                self._build_option_row(self._opts_frame, opt, row_num)

        if "note" in tool:
            tk.Label(self._opts_frame, text=f"\u2139  {tool['note']}", font=FONT_BODY,
                     bg=BG, fg="#5599cc", wraplength=760, justify="left").grid(
                         row=len(options) + 1, column=0, columnspan=2,
                         sticky="ew", pady=(8, 0))

        self._log_line(f"Selected: {tool['label']}", "muted")
        self._refresh_command_preview()

    def _build_option_row(self, parent, opt, row_num):
        label = tk.Label(parent, text=opt["label"], font=FONT_BODY, bg=BG, fg=MUTED,
                         anchor="w", justify="left", wraplength=200)
        label.grid(row=row_num, column=0, sticky="nw", padx=(0, SECTION_GAP), pady=ROW_GAP)

        if opt["type"] == "folder":
            var = tk.StringVar(value=opt.get("default", ""))
            self._option_vars[opt["key"]] = var
            field = tk.Frame(parent, bg=BG)
            field.grid(row=row_num, column=1, sticky="ew", pady=ROW_GAP)
            field.grid_columnconfigure(0, weight=1)
            entry = tk.Entry(field, textvariable=var, font=FONT_BODY,
                             bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
                             relief="flat", highlightbackground="#3a3a6a",
                             highlightthickness=1)
            entry.grid(row=0, column=0, sticky="ew", padx=(0, SMALL_GAP))
            tk.Button(field, text="Browse\u2026", font=FONT_BODY, bg=CARD, fg=TEXT,
                      relief="flat", cursor="hand2",
                      command=lambda v=var: self._browse(v)
                      ).grid(row=0, column=1, sticky="e")
            self._trace_option(var)

        elif opt["type"] == "checkbox":
            var = tk.BooleanVar(value=opt.get("default", False))
            self._option_vars[opt["key"]] = var
            cb = tk.Checkbutton(parent, variable=var, bg=BG, fg=TEXT,
                                 activebackground=BG, activeforeground=TEXT,
                                 selectcolor=FIELD_BG, relief="flat")
            cb.grid(row=row_num, column=1, sticky="w", pady=ROW_GAP)
            self._trace_option(var)

        elif opt["type"] == "select":
            var = tk.StringVar(value=opt.get("default", opt["choices"][0]))
            self._option_vars[opt["key"]] = var
            om = tk.OptionMenu(parent, var, *opt["choices"])
            om.configure(bg=FIELD_BG, fg=TEXT, activebackground=CARD,
                          activeforeground=TEXT, relief="flat", highlightthickness=0,
                          font=FONT_BODY)
            om["menu"].configure(bg=FIELD_BG, fg=TEXT, activebackground=CARD,
                                  activeforeground=TEXT, font=FONT_BODY)
            om.grid(row=row_num, column=1, sticky="w", pady=ROW_GAP)
            self._trace_option(var)

        elif opt["type"] == "multi_select":
            # Renders each choice as an individual checkbox; stores a list var
            choices  = opt["choices"]
            defaults = set(opt.get("default") or [])
            check_vars = {c: tk.BooleanVar(value=(c in defaults)) for c in choices}
            self._option_vars[opt["key"]] = check_vars   # dict[str, BooleanVar]
            cb_frame = tk.Frame(parent, bg=BG)
            cb_frame.grid(row=row_num, column=1, sticky="ew", pady=ROW_GAP)
            checkbox_widgets = []
            for c in choices:
                cb = tk.Checkbutton(
                    cb_frame, text=c, variable=check_vars[c],
                    bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
                    selectcolor=FIELD_BG, relief="flat", font=FONT_BODY,
                    anchor="w"
                )
                checkbox_widgets.append(cb)
                self._trace_option(check_vars[c])
            if opt.get("note"):
                note = tk.Label(cb_frame, text=opt["note"], font=("Segoe UI", 8),
                                bg=BG, fg=MUTED, wraplength=360, justify="left")
            else:
                note = None

            def arrange(_event=None):
                width = max(cb_frame.winfo_width(), 320)
                columns = max(1, min(4, width // 190))
                for index, checkbox in enumerate(checkbox_widgets):
                    checkbox.grid(row=index // columns, column=index % columns,
                                  sticky="w", padx=(0, 10), pady=(0, 4))
                for col in range(columns):
                    cb_frame.grid_columnconfigure(col, weight=1, uniform=f"{opt['key']}_choices")
                if note:
                    note.grid(row=(len(checkbox_widgets) + columns - 1) // columns,
                              column=0, columnspan=columns, sticky="ew", pady=(2, 0))

            cb_frame.bind("<Configure>", arrange)
            arrange()

    def _trace_option(self, var):
        token = var.trace_add("write", lambda *_args: self._schedule_command_preview())
        self._option_traces.append((var, token))

    def _schedule_command_preview(self):
        if self._preview_after:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(80, self._refresh_command_preview)

    def _refresh_command_preview(self):
        self._preview_after = None
        if not getattr(self, "_command_preview", None) or not self._active_tool:
            return
        cmd = self._build_command(self._active_tool)
        preview = subprocess.list2cmdline(cmd)
        self._command_preview.configure(state="normal")
        self._command_preview.delete("1.0", tk.END)
        self._command_preview.insert("1.0", preview)
        self._command_preview.configure(state="disabled")

    def _sync_wraplengths(self, event=None):
        width = max(320, (event.width if event else self._topbar.winfo_width()) - 70)
        self._tool_name_lbl.configure(wraplength=width)
        self._tool_desc_lbl.configure(wraplength=width)

    def _browse(self, var):
        path = filedialog.askdirectory(initialdir=var.get() or "C:\\")
        if path:
            var.set(path)

    # ── Run / Stop ──────────────────────────────────────────────────────────────
    def _build_command(self, tool):
        script = SCRIPT_DIR / tool["script"]
        cmd = [sys.executable, str(script)]
        if tool.get("subcommand"):
            cmd.append(tool["subcommand"])
        opts = self._option_vars

        # Pass the scan folder using the method this script expects.
        # Most scripts take a bare positional path; cbz_sanitizer uses --scan=<path>.
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
            # Most scripts use --dry-run.
            if folder_flag != "--library":
                cmd.append("--dry-run")
        else:
            # Live run for any future scripts that use --live instead of absence-of-dry-run.
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

        workers_var = opts.get("workers")
        if workers_var:
            cmd.extend(["--workers", workers_var.get()])

        if opts.get("uncensored_check") and opts["uncensored_check"].get():
            cmd.append("--uncensored-check")

        if opts.get("possible_series_check") and opts["possible_series_check"].get():
            cmd.append("--possible-series-check")

            common_words_var = opts.get("series_common_words")
            if common_words_var:
                cmd.extend(["--series-common-words", common_words_var.get()])

            min_group_size_var = opts.get("series_min_group_size")
            if min_group_size_var:
                cmd.extend(["--series-min-group-size", min_group_size_var.get()])

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
        self._start_progress(tool)

        def target():
            try:
                env = os.environ.copy()
                env["CBZ_PROGRESS"] = "1"
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(SCRIPT_DIR.parent),
                    env=env,
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
        self._stop_progress("Stopped", WARNING, value=0)
        self._set_idle()

    def _set_idle(self):
        self._running = False
        self._proc = None
        self._run_btn.configure(state="normal", bg=ACCENT, fg="white")
        self._stop_btn.configure(state="disabled", bg=CARD, fg=MUTED)
        self._status_lbl.configure(text="")

    def _start_progress(self, tool):
        self._progress_seen = 0
        self._progress_mode = "activity"
        self._progress.configure(mode="indeterminate", maximum=100, value=0)
        self._progress.start(12)
        self._progress_lbl.configure(text=f"{tool['label']} running", fg=WARNING)

    def _stop_progress(self, text, color, value=None):
        self._progress.stop()
        self._progress.configure(mode="determinate")
        if value is not None:
            self._progress.configure(value=value)
        self._progress_lbl.configure(text=text, fg=color)
        self._progress_mode = "idle"

    def _update_progress_from_line(self, line):
        progress_match = re.search(r"CBZ_PROGRESS\s+(\d+)/(\d+)\s+(\d+)%\s*(.*)", line)
        if progress_match:
            current = int(progress_match.group(1))
            total = max(1, int(progress_match.group(2)))
            percent = max(0, min(100, int(progress_match.group(3))))
            label = progress_match.group(4).strip()
            if self._progress_mode != "percent":
                self._progress.stop()
                self._progress.configure(mode="determinate", maximum=100)
                self._progress_mode = "percent"
            self._progress.configure(value=percent)
            detail = f"{current}/{total}"
            if label:
                detail = f"{detail} {label}"
            self._progress_lbl.configure(text=detail, fg=WARNING)
            return True

        percent_match = re.search(r"(?<!\d)(100|[1-9]?\d)(?:\.\d+)?\s*%", line)
        if percent_match:
            percent = int(percent_match.group(1))
            if self._progress_mode != "percent":
                self._progress.stop()
                self._progress.configure(mode="determinate", maximum=100)
                self._progress_mode = "percent"
            self._progress.configure(value=percent)
            self._progress_lbl.configure(text=f"{percent}% complete", fg=WARNING)
            return False

        lo = line.lower()
        if any(marker in lo for marker in ("processing", "scanning", "renamed", "packed", "moved", "deleted", "updated")):
            self._progress_seen += 1
            if self._progress_mode == "activity":
                self._progress_lbl.configure(text=f"{self._progress_seen} update(s)", fg=WARNING)
        return False

    # ── Log ─────────────────────────────────────────────────────────────────────
    def _poll_log_queue(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                if line == "__DONE_OK__":
                    self._log_line("Done \u2014 completed successfully.", "success")
                    self._stop_progress("Complete", SUCCESS, value=100)
                    self._set_idle()
                    self._status_lbl.configure(text="Finished", fg=SUCCESS)
                elif line.startswith("__DONE_ERR__"):
                    rc = line[len("__DONE_ERR__"):]
                    self._log_line(f"Process exited with code {rc}.", "error")
                    self._stop_progress(f"Failed (exit {rc})", ERROR, value=0)
                    self._set_idle()
                    self._status_lbl.configure(text=f"Exit code {rc}", fg=ERROR)
                elif line.startswith("__ERROR__"):
                    msg = line[len("__ERROR__"):]
                    self._log_line(f"Launch error: {msg}", "error")
                    self._stop_progress("Launch error", ERROR, value=0)
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
                    if self._running and self._update_progress_from_line(line):
                        continue
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
