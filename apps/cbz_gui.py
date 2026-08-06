r"""
CBZ Automation Suite — GUI Launcher
Run any suite tool without touching the command line.
Double-click this file or run: python apps\cbz_gui.py
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
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
LOG_DIR = REPO_ROOT / "Logs"
ROUTING_JSON = REPO_ROOT / "routing.json"

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

CATEGORIES = [
    ("core", "Core"),
    ("review", "Review"),
    ("utility", "Utilities"),
]

# ── Tool definitions ────────────────────────────────────────────────────────────
# scan_folder_flag: how the folder is passed to each script.
#   "positional"  — appended as a bare arg (default, works for most tools)
#   "--scan"      — passed as --scan=<path> (used by cbz_sanitizer.py)
TOOLS = [
    {
        "id": "sanitizer",
        "label": "CBZ Sanitizer",
        "script": "cbz_sanitizer.py",
        "description": "Clean up messy filenames, fix ComicInfo metadata, and normalize file structure across your entire collection.",
        "category": "core",
        "icon": "\u2726",
        "color": ACCENT,
        "scan_folder_flag": "--scan",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Scan folder", "default": r"\\tower\media\comics"},
            {"type": "select", "key": "sort", "label": "Sort order", "choices": ["newest", "oldest", "alpha", "alpha-reverse"], "default": "newest", "description": "Order for processing: newest first (last modified), oldest, alphabetical, or reverse alphabetical"},
            {"type": "checkbox", "key": "full_rescan", "label": "Full rescan — process all files (normally skips files modified since last run)", "default": False},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run — preview changes without modifying files", "default": False},
            {"type": "checkbox", "key": "restart", "label": "Restart — clear progress tracker and re-process everything", "default": False},
            {
                "type": "multi_select",
                "key": "rules",
                "label": "Select cleanup rules to apply",
                "choices": ["brackets", "comicinfo", "leading_nums", "non_latin", "normalize_stem", "number_tokens", "scan_groups", "trailing_junk", "url"],
                "default": [],
                "note": "Leave all unchecked to run every available rule. Brackets = remove [brackets], Leading_nums = strip leading numbers, Non_latin = transliterate non-ASCII, URL = strip URLs from names.",
            },
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12", "20"], "default": "8", "description": "Higher = faster processing but more CPU usage"},
        ],
    },
    {
        "id": "watcher",
        "label": "CBZ Watcher",
        "script": "cbz_watcher.py",
        "description": "Keep a folder continuously monitored for new files. Automatically sanitize, organize, update metadata, and route files as they arrive.",
        "category": "core",
        "icon": "\u25ce",
        "color": "#2196F3",
        "options": [],
        "note": "Runs as a background service — click Stop to shut it down. Set up your incoming folder in the watcher config.",
    },
    {
        "id": "library_archive_clean",
        "label": "Archive Cleaner",
        "script": "cbz_library_maintenance.py",
        "subcommand": "archive-clean",
        "description": "Remove duplicate archives, strip redundant filename tokens, and compress loose image folders into CBZ files.",
        "category": "core",
        "icon": "\u2297",
        "color": "#FF5722",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to clean", "default": r"\\tower\media\comics\Comix"},
            {"type": "select", "key": "run_mode", "label": "Run mode", "choices": ["Dry run", "Normal run"], "default": "Dry run"},
            {"type": "checkbox", "key": "apply_saved_plan", "label": "Apply previous dry run plan", "default": False},
            {"type": "checkbox", "key": "metadata_dedupe", "label": "Match duplicates by ComicInfo metadata (slower but more accurate)", "default": True},
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "library_organizer",
        "label": "Series Organizer",
        "script": "cbz_library_maintenance.py",
        "subcommand": "organize-series",
        "description": "Merge split chapter folders, detect duplicate series using fuzzy matching, and optionally flag uncensored/censored pairs for review.",
        "category": "core",
        "icon": "\u2295",
        "color": "#9C27B0",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to organize", "default": r"\\tower\media\comics\Comix"},
            {"type": "select", "key": "run_mode", "label": "Run mode", "choices": ["Dry run", "Normal run"], "default": "Dry run"},
            {"type": "checkbox", "key": "apply_saved_plan", "label": "Apply previous dry run plan", "default": False},
            {"type": "checkbox", "key": "metadata_dedupe", "label": "Match duplicates by ComicInfo metadata (slower but more accurate)", "default": True},
            {"type": "checkbox", "key": "uncensored_check", "label": "Also detect uncensored/censored variant pairs", "default": False},
            {"type": "select", "key": "move_which", "label": "When uncensored pair found, stage:", "choices": ["both", "uncensored", "censored"], "default": "both", "description": "Which version to flag for manual review"},
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "metadata_repair",
        "label": "Metadata Repair",
        "script": "cbz_library_maintenance.py",
        "subcommand": "metadata",
        "description": "Fix ComicInfo title, series name, chapter numbers, and volume tags by parsing filenames using the suite's core naming rules.",
        "category": "core",
        "icon": "\u229f",
        "color": "#8BC34A",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to repair", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run — preview metadata changes before applying", "default": True},
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "repair_names",
        "label": "Repair Names",
        "script": "cbz_library_maintenance.py",
        "subcommand": "repair-names",
        "description": "Fix mojibake from the downloader where punctuation was written as literal UTF-8 hex (e.g. Player\u2019s saved as 'Playere28099s'). Repairs file names, folder names, and ComicInfo Title/Series inside archives.",
        "category": "core",
        "icon": "\u2692",
        "color": "#00BCD4",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to repair", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run \u2014 preview repairs before applying", "default": True},
            {"type": "checkbox", "key": "names_only", "label": "Names only \u2014 skip rewriting ComicInfo metadata inside archives", "default": False},
        ],
    },
    {
        "id": "library_all",
        "label": "Full Maintenance",
        "script": "cbz_library_maintenance.py",
        "subcommand": "all",
        "description": "Run a complete library cleanup: archives, series organization, and metadata repair all in one pass.",
        "category": "core",
        "icon": "\u2605",
        "color": "#E91E63",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to maintain", "default": r"\\tower\media\comics\Comix"},
            {"type": "select", "key": "run_mode", "label": "Run mode", "choices": ["Dry run", "Normal run"], "default": "Dry run"},
            {"type": "checkbox", "key": "apply_saved_plan", "label": "Apply previous dry run plan", "default": False},
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "compilation_resolver",
        "label": "Compilation Resolver",
        "script": "cbz_compilation_resolver.py",
        "description": "Identify and resolve conflicts where large compilation archives overlap with individual chapter files in your library.",
        "category": "core",
        "icon": "\u229e",
        "color": "#00BCD4",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to analyze", "default": r"\\tower\media\comics\Comix"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run — preview resolution plan before applying", "default": True},
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
    },
    {
        "id": "gap_checker",
        "label": "Gap Checker",
        "script": "cbz_gap_checker.py",
        "description": "Scan your library for missing chapter numbers in each series and generate a detailed CSV report of gaps.",
        "category": "review",
        "icon": "\u2298",
        "color": "#009688",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to scan", "default": r"\\tower\media\comics\Comix"},
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
        "note": "Read-only operation — analyzes your library without making any changes. Report saved to Logs folder.",
    },
    {
        "id": "series_review",
        "label": "Series Review",
        "script": "cbz_library_maintenance.py",
        "subcommand": "propose-series",
        "description": "Find likely same-series folders in your library, review each match with Yes/No checkboxes, and merge the ones you confirm.",
        "category": "review",
        "icon": "\u2611",
        "color": "#7E57C2",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to scan", "default": r"\\tower\media\comics\Comix"},
            {"type": "text", "key": "out", "label": "Proposal file path", "default": str(LOG_DIR / "series_proposal.json"), "description": "Where to save the analysis results"},
            {"type": "select", "key": "series_common_words", "label": "Common title words to ignore", "choices": ["1", "2", "3", "4"], "default": "1", "description": "How many common words to skip (e.g., 'The', 'A', etc.)"},
            {"type": "select", "key": "series_min_group_size", "label": "Minimum group size", "choices": ["2", "3", "4", "5"], "default": "2", "description": "Only flag groups with at least this many folders"},
            {"type": "button", "key": "review_btn", "label": "\u2611  Review & apply matches\u2026", "action": "open_series_review"},
        ],
        "note": "Step 1: Click Run to analyze and generate the proposal. Step 2: Click 'Review & apply matches' to open the interactive checklist and merge confirmed series.",
    },
    {
        "id": "stage_possible_series",
        "label": "Stage Similar Series",
        "script": "cbz_library_maintenance.py",
        "subcommand": "organize-series",
        "description": "Run the series organizer and also stage likely same-series folders into a _Check folder for manual review.",
        "category": "review",
        "icon": "\u25a3",
        "color": "#AB47BC",
        "options": [
            {"type": "folder", "key": "scan_folder", "label": "Library folder to process", "default": r"\\tower\media\comics\Comix"},
            {"type": "select", "key": "run_mode", "label": "Run mode", "choices": ["Dry run", "Normal run"], "default": "Dry run"},
            {"type": "checkbox", "key": "apply_saved_plan", "label": "Apply previous dry run plan", "default": False},
            {"type": "checkbox", "key": "metadata_dedupe", "label": "Match duplicates by ComicInfo metadata (slower but more accurate)", "default": True},
            {"type": "select", "key": "series_common_words", "label": "Common title words to ignore", "choices": ["1", "2", "3", "4"], "default": "1"},
            {"type": "select", "key": "series_min_group_size", "label": "Minimum group size", "choices": ["2", "3", "4", "5"], "default": "2"},
            {"type": "select", "key": "workers", "label": "Parallel workers", "choices": ["1", "2", "4", "8", "12"], "default": "8"},
        ],
        "fixed_flags": ["--possible-series-check"],
        "note": "Use Series Review for an interactive approve/reject checklist. This runs the normal organizer and adds automatic staging of suspect series to _Check.",
    },
    {
        "id": "apply_plan",
        "label": "Apply Dry-Run Plan",
        "script": "cbz_library_maintenance.py",
        "subcommand": "apply-plan",
        "description": "Execute actions from a previously saved dry-run plan without re-scanning. Safe: each file is re-checked before modification.",
        "category": "utility",
        "icon": "\u25b6",
        "color": "#26A69A",
        "options": [
            {"type": "text", "key": "plan_file", "label": "Plan file path", "default": str(LOG_DIR / "plan.json"), "description": "Path to the plan.json saved by a dry run"},
            {"type": "checkbox", "key": "dry_run", "label": "Dry run — test the plan without making changes", "default": False},
        ],
        "note": "Load a plan file saved by running another tool with 'Save plan to file' enabled. The plan will be replayed safely (files are re-checked first).",
    },
    {
        "id": "clear_exclusions",
        "label": "Exclusions Log",
        "script": "cbz_library_maintenance.py",
        "subcommand": "clear-exclusions",
        "description": "View or clear pairs that were marked as 'not matching' during series reviews (prevents re-proposing the same pairs).",
        "category": "utility",
        "icon": "\u2297",
        "color": WARNING,
        "options": [
            {"type": "checkbox", "key": "dry_run", "label": "Dry run — list exclusions without removing them", "default": True},
            {"type": "text", "key": "filter", "label": "Filter (optional — only remove pairs containing this text)", "default": ""},
        ],
        "note": "Run with dry run ON to preview what would be deleted. Turn it OFF to actually remove matching entries from the exclusions log.",
    },
]



_CONSOLIDATED_TOOL_IDS = {
    "sanitizer",
    "library_archive_clean",
    "library_organizer",
    "metadata_repair",
    "repair_names",
    "library_all",
    "compilation_resolver",
    "series_review",
    "stage_possible_series",
}

CONSOLIDATED_TOOLS = [
    {
        "id": "series_workflow",
        "label": "Series Workflow",
        "script": "cbz_workflows.py",
        "subcommand": "series",
        "description": (
            "Organize series folders, stage or review similar titles, and resolve "
            "compilation overlaps from one workflow."
        ),
        "category": "core",
        "icon": "\u2295",
        "color": "#9C27B0",
        "options": [
            {
                "type": "folder",
                "key": "scan_folder",
                "label": "Library folder",
                "default": r"\\tower\media\comics\Comix",
            },
            {
                "type": "select",
                "key": "run_mode",
                "label": "Run mode",
                "choices": ["Dry run", "Normal run"],
                "default": "Dry run",
            },
            {
                "type": "multi_select",
                "key": "stages",
                "label": "Series stages",
                "choices": ["organize", "stage", "review", "compilations"],
                "default": ["organize"],
                "note": (
                    "Organize merges and normalizes folders. Stage moves likely "
                    "matches to _Check. Review writes the interactive proposal. "
                    "Compilations performs page-level overlap resolution."
                ),
            },
            {
                "type": "checkbox",
                "key": "metadata_dedupe",
                "label": "Match duplicates by ComicInfo metadata",
                "default": True,
            },
            {
                "type": "checkbox",
                "key": "uncensored_check",
                "label": "Detect censored/uncensored variants",
                "default": False,
            },
            {
                "type": "select",
                "key": "move_which",
                "label": "When a variant pair is found, stage:",
                "choices": ["both", "uncensored", "censored"],
                "default": "both",
            },
            {
                "type": "select",
                "key": "series_common_words",
                "label": "Common title words",
                "choices": ["1", "2", "3", "4"],
                "default": "1",
            },
            {
                "type": "select",
                "key": "series_min_group_size",
                "label": "Minimum review group",
                "choices": ["2", "3", "4", "5"],
                "default": "2",
            },
            {
                "type": "text",
                "key": "out",
                "label": "Proposal file",
                "default": str(LOG_DIR / "series_proposal.json"),
            },
            {
                "type": "button",
                "key": "review_btn",
                "label": "\u2611  Review & apply matches\u2026",
                "action": "open_series_review",
            },
            {
                "type": "select",
                "key": "workers",
                "label": "Parallel workers",
                "choices": ["1", "2", "4", "8", "12"],
                "default": "8",
            },
        ],
        "note": (
            "Generate the review proposal by selecting Review and clicking Run, "
            "then open the interactive checklist with Review & apply matches."
        ),
    },
    {
        "id": "maintenance_workflow",
        "label": "Library Maintenance",
        "script": "cbz_workflows.py",
        "subcommand": "maintenance",
        "description": (
            "Run sanitization, archive cleanup, series organization, metadata "
            "repair, and name repair as one configurable workflow."
        ),
        "category": "core",
        "icon": "\u2605",
        "color": "#E91E63",
        "options": [
            {
                "type": "folder",
                "key": "scan_folder",
                "label": "Library folder",
                "default": r"\\tower\media\comics\Comix",
            },
            {
                "type": "select",
                "key": "run_mode",
                "label": "Run mode",
                "choices": ["Dry run", "Normal run"],
                "default": "Dry run",
            },
            {
                "type": "multi_select",
                "key": "stages",
                "label": "Maintenance stages",
                "choices": ["sanitize", "archive", "organize", "metadata", "names"],
                "default": ["sanitize", "archive", "organize", "metadata", "names"],
                "note": "Uncheck stages to run a targeted maintenance pass.",
            },
            {
                "type": "select",
                "key": "sort",
                "label": "Sanitizer sort order",
                "choices": ["newest", "oldest", "alpha", "alpha-reverse"],
                "default": "newest",
            },
            {
                "type": "checkbox",
                "key": "full_rescan",
                "label": "Sanitizer full rescan",
                "default": False,
            },
            {
                "type": "checkbox",
                "key": "restart",
                "label": "Clear sanitizer progress before running",
                "default": False,
            },
            {
                "type": "multi_select",
                "key": "rules",
                "label": "Sanitizer rules",
                "choices": [
                    "brackets",
                    "comicinfo",
                    "leading_nums",
                    "non_latin",
                    "normalize_stem",
                    "number_tokens",
                    "scan_groups",
                    "trailing_junk",
                    "translate",
                    "url",
                ],
                "default": [],
                "note": "Leave all unchecked to use every sanitizer rule.",
            },
            {
                "type": "checkbox",
                "key": "metadata_dedupe",
                "label": "Match duplicates by ComicInfo metadata",
                "default": True,
            },
            {
                "type": "checkbox",
                "key": "names_only",
                "label": "Name repair: skip ComicInfo metadata",
                "default": False,
            },
            {
                "type": "select",
                "key": "workers",
                "label": "Parallel workers",
                "choices": ["1", "2", "4", "8", "12", "20"],
                "default": "8",
            },
        ],
    },
]

TOOLS = CONSOLIDATED_TOOLS + [
    tool for tool in TOOLS if tool["id"] not in _CONSOLIDATED_TOOL_IDS
]

OPTION_GUIDANCE = {
    ("series_workflow", "scan_folder"): (
        "Root folder containing the series directories to organize and review.",
        r"\\tower\media\comics\Comix",
        "Only series beneath this folder are scanned or changed.",
    ),
    ("series_workflow", "run_mode"): (
        "Choose whether to preview actions or apply them to the library.",
        "Dry run",
        "Dry run logs planned work; Normal run performs selected stages.",
    ),
    ("series_workflow", "stages"): (
        "Select any combination of organizing, staging, review proposal, and page-level compilation work.",
        "organize + review",
        "Stages run in a safe fixed order and stop if one fails.",
    ),
    ("series_workflow", "metadata_dedupe"): (
        "Use ComicInfo Series, Volume, and Number when filenames alone do not identify duplicates.",
        "Enabled",
        "Finds more duplicate chapters, with additional archive reads.",
    ),
    ("series_workflow", "uncensored_check"): (
        "Look for censored and uncensored directory variants that need manual comparison.",
        "Series and Series Uncensored",
        "Matching variants are staged according to the next setting.",
    ),
    ("series_workflow", "move_which"): (
        "Choose which side of a censored/uncensored pair is moved for review.",
        "both",
        "The selected variant folders are moved into the review area.",
    ),
    ("series_workflow", "series_common_words"): (
        "Minimum shared leading title words used to group likely related series.",
        "2 for 'Batman Superman ...'",
        "Higher values reduce false matches but may miss short titles.",
    ),
    ("series_workflow", "series_min_group_size"): (
        "Minimum number of matching folders required to create a review group.",
        "2",
        "Groups smaller than this number are ignored.",
    ),
    ("series_workflow", "out"): (
        "File where the similar-series review proposal is written.",
        r"Logs\series_proposal.json",
        "The Review button opens this file after the review stage runs.",
    ),
    ("series_workflow", "review_btn"): (
        "Open the latest proposal and approve or reject each suggested merge.",
        "Run the review stage, then click this button.",
        "Approved groups are merged; rejected pairs are added to exclusions.",
    ),
    ("series_workflow", "workers"): (
        "Maximum number of series folders processed concurrently.",
        "8",
        "More workers improve speed but increase disk, network, and CPU load.",
    ),
    ("maintenance_workflow", "scan_folder"): (
        "Root folder containing the CBZ library to maintain.",
        r"\\tower\media\comics\Comix",
        "Every selected maintenance stage operates beneath this folder.",
    ),
    ("maintenance_workflow", "run_mode"): (
        "Choose whether to preview the complete workflow or apply changes.",
        "Dry run",
        "Dry run writes no archive, filename, folder, or progress changes.",
    ),
    ("maintenance_workflow", "stages"): (
        "Choose which maintenance passes run in the consolidated workflow.",
        "sanitize + archive + metadata",
        "Selected stages run in the displayed canonical order.",
    ),
    ("maintenance_workflow", "sort"): (
        "Order in which the sanitizer visits top-level directories.",
        "newest",
        "Changes processing priority only; it does not change results.",
    ),
    ("maintenance_workflow", "full_rescan"): (
        "Ignore the sanitizer's incremental cutoff and inspect every CBZ.",
        "Enable after changing cleanup rules.",
        "Previously processed and older files become eligible for scanning.",
    ),
    ("maintenance_workflow", "restart"): (
        "Clear sanitizer history before a normal run.",
        "Enable to rebuild progress tracking from scratch.",
        "All eligible files are processed again; dry run only reports the reset.",
    ),
    ("maintenance_workflow", "rules"): (
        "Limit sanitizer work to selected filename and ComicInfo operations.",
        "comicinfo + translate",
        "Unchecked means all rules; checked means only those rules execute.",
    ),
    ("maintenance_workflow", "metadata_dedupe"): (
        "Compare ComicInfo identifiers in addition to normalized filenames.",
        "Enabled",
        "Catches differently named duplicate chapters at the cost of more reads.",
    ),
    ("maintenance_workflow", "names_only"): (
        "Repair mojibake in file and folder names without changing embedded XML.",
        "Playere28099s becomes Player's",
        "Names are repaired while ComicInfo metadata remains untouched.",
    ),
    ("maintenance_workflow", "workers"): (
        "Maximum number of independent folders processed concurrently.",
        "8",
        "Higher values finish sooner but place more load on storage and CPU.",
    ),
    ("gap_checker", "scan_folder"): (
        "Root library folder whose series are checked for missing chapter numbers.",
        r"\\tower\media\comics\Comix",
        "A CSV gap report is generated for series beneath this folder.",
    ),
    ("gap_checker", "workers"): (
        "Number of series scanned concurrently.",
        "8",
        "Higher values speed up read-only analysis on capable storage.",
    ),
    ("apply_plan", "plan_file"): (
        "Previously generated maintenance plan to validate and replay.",
        r"Logs\plan.json",
        "Actions in this file are rechecked before they are applied.",
    ),
    ("apply_plan", "dry_run"): (
        "Validate and describe the saved plan without changing files.",
        "Enabled",
        "The plan is tested and logged, but no action is executed.",
    ),
    ("clear_exclusions", "dry_run"): (
        "List matching exclusion entries without removing them.",
        "Enabled",
        "Shows what would be cleared while preserving the exclusions file.",
    ),
    ("clear_exclusions", "filter"): (
        "Restrict removal to exclusion pairs containing this text.",
        "Batman",
        "Only matching pairs are listed or removed; blank targets all pairs.",
    ),
}

for _tool in TOOLS:
    for _option in _tool.get("options", []):
        _guidance = OPTION_GUIDANCE.get((_tool["id"], _option["key"]))
        if _guidance:
            _option["description"], _option["example"], _option["expected_result"] = _guidance


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
        self._option_widgets = {}  # Track widgets for conditional enabling/disabling
        self._preview_after = None
        self._progress_seen = 0
        self._progress_mode = "idle"
        self._running = False
        self._active_category = CATEGORIES[0][0]

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
        sidebar.grid_rowconfigure(3, weight=1)

        # App title
        header = tk.Frame(sidebar, bg=PANEL, pady=16)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="CBZ Suite", font=("Segoe UI", 16, "bold"),
                 bg=PANEL, fg=TEXT).pack(padx=16, anchor="w")
        tk.Label(header, text="Automation Launcher", font=FONT_BODY,
                 bg=PANEL, fg=MUTED).pack(padx=16, anchor="w")

        tk.Frame(sidebar, bg=ACCENT, height=1).grid(row=1, column=0, sticky="ew", padx=12)

        category_frame = tk.Frame(sidebar, bg=PANEL)
        category_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(10, 2))
        self._category_buttons = {}
        for col, (category_id, label) in enumerate(CATEGORIES):
            category_frame.grid_columnconfigure(col, weight=1, uniform="category")
            btn = tk.Button(
                category_frame,
                text=label,
                font=("Segoe UI", 8, "bold"),
                bg=CARD if category_id == self._active_category else PANEL,
                fg=TEXT if category_id == self._active_category else MUTED,
                activebackground=CARD,
                activeforeground=TEXT,
                relief="flat",
                cursor="hand2",
                command=lambda cid=category_id: self._select_category(cid),
            )
            btn.grid(row=0, column=col, sticky="ew", padx=1)
            self._category_buttons[category_id] = btn

        # Tool buttons
        self._sidebar_buttons = {}
        sidebar_scroll, tools_frame = self._make_scrollable_frame(sidebar, PANEL)
        sidebar_scroll.grid(row=3, column=0, sticky="nsew", pady=8)
        self._tools_frame = tools_frame

        self._rebuild_sidebar_tools()

        # Main content area
        self._main = tk.Frame(self, bg=BG)
        self._main.grid(row=0, column=1, sticky="nsew")
        self._main.grid_columnconfigure(0, weight=1)
        self._main.grid_rowconfigure(1, weight=0)
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
        self._topbar.grid(row=0, column=0, sticky="ew", padx=PAD_X, pady=(10, 0))
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
        opts_shell = tk.Frame(self._main, bg=BG, height=280)
        opts_shell.grid(row=1, column=0, sticky="nsew", padx=PAD_X, pady=(6, 8))
        opts_shell.grid_columnconfigure(0, weight=1)
        opts_shell.grid_rowconfigure(1, weight=1)
        opts_shell.grid_propagate(False)
        tk.Label(opts_shell, text="Options", font=("Segoe UI", 10, "bold"),
                 bg=BG, fg=MUTED).grid(row=0, column=0, sticky="w", pady=(0, 2))
        options_scroll, self._opts_frame = self._make_scrollable_frame(opts_shell, BG)
        options_scroll.grid(row=1, column=0, sticky="nsew")
        self._options_scroll_shell = options_scroll

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
        shell._scroll_canvas = canvas
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

    def _rebuild_sidebar_tools(self):
        for child in self._tools_frame.winfo_children():
            child.destroy()
        self._sidebar_buttons = {}
        for tool in TOOLS:
            if tool.get("category", "core") != self._active_category:
                continue
            btn = self._make_sidebar_btn(self._tools_frame, tool)
            self._sidebar_buttons[tool["id"]] = btn

    def _select_category(self, category_id):
        if category_id == self._active_category:
            return
        self._active_category = category_id
        for cid, btn in self._category_buttons.items():
            active = cid == category_id
            btn.configure(bg=CARD if active else PANEL, fg=TEXT if active else MUTED)
        self._rebuild_sidebar_tools()
        first_tool = next(
            (tool for tool in TOOLS if tool.get("category", "core") == category_id),
            None,
        )
        if first_tool:
            self._select_tool(first_tool)

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
        for var, token in self._option_traces:
            var.trace_remove("write", token)
        self._active_tool = tool
        self._option_traces = []
        self._option_widgets = {}

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
            note = "No configurable options \u2014 uses the script's default settings."
            tk.Label(self._opts_frame, text=note, font=FONT_BODY,
                     bg=BG, fg=MUTED, wraplength=760, justify="left").grid(
                         row=0, column=0, columnspan=3, sticky="ew", pady=4)
            note_row = 1
        else:
            note_row = self._build_options_grid(self._opts_frame, options)

        if "note" in tool:
            tk.Label(self._opts_frame, text=f"\u2139  {tool['note']}", font=FONT_BODY,
                     bg=BG, fg="#5599cc", wraplength=760, justify="left").grid(
                         row=note_row, column=0, columnspan=3,
                         sticky="ew", pady=(8, 0))

        self.update_idletasks()
        self._options_scroll_shell._scroll_canvas.yview_moveto(0)

        self._log_line(f"Selected: {tool['label']}", "muted")
        self._refresh_command_preview()

    def _build_options_grid(self, parent, options):
        for col in range(3):
            parent.grid_columnconfigure(col, weight=1, uniform="option_cols")

        row = 0
        compact: list[dict] = []
        for opt in options:
            if opt["type"] in {"checkbox", "select"}:
                compact.append(opt)
                continue
            if compact:
                row = self._flush_compact_options(parent, compact, row)
                compact = []
            row += self._build_full_option(parent, opt, row)
        if compact:
            row = self._flush_compact_options(parent, compact, row)

        # Set up conditional enabling of apply_saved_plan based on run_mode
        if "run_mode" in self._option_vars and "apply_saved_plan" in self._option_vars:
            run_mode_var = self._option_vars["run_mode"]
            apply_plan_widget = self._option_widgets.get("apply_saved_plan")
            if apply_plan_widget:
                def update_apply_plan_state(*_args):
                    state = "normal" if run_mode_var.get() == "Normal run" else "disabled"
                    apply_plan_widget.configure(state=state)
                run_mode_var.trace_add("write", update_apply_plan_state)
                update_apply_plan_state()  # Set initial state

        return row

    def _flush_compact_options(self, parent, options, row):
        for index, opt in enumerate(options):
            self._build_compact_option(parent, opt, row + index // 2, index % 2)
        return row + ((len(options) + 1) // 2)

    def _add_option_guidance(self, parent, opt, row, columnspan=1, wraplength=390):

        tk.Label(
            parent,
            text=(
                f"Description: {opt['description']}\n"
                f"Example: {opt['example']}\n"
                f"Expected: {opt['expected_result']}"
            ),
            font=("Segoe UI", 8),
            bg=BG,
            fg=MUTED,
            anchor="nw",
            justify="left",
            wraplength=wraplength,
        ).grid(
            row=row,
            column=0,
            columnspan=columnspan,
            sticky="ew",
            pady=(3, 7),
        )



    def _build_compact_option(self, parent, opt, row_num, col_num):
        cell = tk.Frame(parent, bg=BG)
        cell.grid(row=row_num, column=col_num, sticky="ew", padx=(0, 14), pady=3)
        cell.grid_columnconfigure(0, weight=1)

        if opt["type"] == "checkbox":
            var = tk.BooleanVar(value=opt.get("default", False))
            self._option_vars[opt["key"]] = var
            cb = tk.Checkbutton(
                cell,
                text=opt["label"],
                variable=var,
                bg=BG,
                fg=TEXT,
                activebackground=BG,
                activeforeground=TEXT,
                selectcolor=FIELD_BG,
                relief="flat",
                font=FONT_BODY,
                anchor="w",
                justify="left",
                wraplength=260,
            )
            cb.grid(row=0, column=0, sticky="w")

            self._add_option_guidance(cell, opt, 1)
            self._option_widgets[opt["key"]] = cb  # Store widget reference
            self._trace_option(var)
            return

        var = tk.StringVar(value=opt.get("default", opt["choices"][0]))
        self._option_vars[opt["key"]] = var
        tk.Label(
            cell,
            text=opt["label"],
            font=FONT_BODY,
            bg=BG,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=210,
        ).grid(row=0, column=0, sticky="w", pady=(0, 2))
        om = tk.OptionMenu(cell, var, *opt["choices"])
        om.configure(bg=FIELD_BG, fg=TEXT, activebackground=CARD,
                     activeforeground=TEXT, relief="flat", highlightthickness=0,
                     font=FONT_BODY)
        om["menu"].configure(bg=FIELD_BG, fg=TEXT, activebackground=CARD,
                             activeforeground=TEXT, font=FONT_BODY)
        om.grid(row=1, column=0, sticky="w")

        self._add_option_guidance(cell, opt, 2)
        self._trace_option(var)

    def _build_full_option(self, parent, opt, row_num):
        if opt["type"] == "button":
            field = tk.Frame(parent, bg=BG)
            field.grid(row=row_num, column=0, columnspan=3, sticky="ew", pady=(4, 6))
            field.grid_columnconfigure(0, weight=1)
            btn = tk.Button(
                field, text=opt["label"], font=("Segoe UI", 10, "bold"),
                bg=ACCENT2, fg="white", relief="flat", cursor="hand2",
                padx=16, pady=6,
                command=lambda a=opt.get("action"): self._invoke_option_action(a),
            )
            btn.grid(row=0, column=0, sticky="w")
            self._add_option_guidance(field, opt, 1, wraplength=760)
            return 1

        label = tk.Label(parent, text=opt["label"], font=FONT_BODY, bg=BG, fg=MUTED,
                         anchor="w", justify="left", wraplength=520)
        label.grid(row=row_num, column=0, columnspan=3, sticky="ew", pady=(5, 2))

        if opt["type"] == "folder":
            var = tk.StringVar(value=opt.get("default", ""))
            self._option_vars[opt["key"]] = var
            field = tk.Frame(parent, bg=BG)
            field.grid(row=row_num + 1, column=0, columnspan=3, sticky="ew", pady=(0, 5))
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

            self._add_option_guidance(field, opt, 1, columnspan=2, wraplength=760)
            self._trace_option(var)
            return 2

        elif opt["type"] == "checkbox":
            self._build_compact_option(parent, opt, row_num, 0)
            return 1

        elif opt["type"] == "text":
            var = tk.StringVar(value=opt.get("default", ""))
            self._option_vars[opt["key"]] = var
            field = tk.Frame(parent, bg=BG)
            field.grid(row=row_num + 1, column=0, columnspan=3, sticky="ew", pady=(0, 5))
            field.grid_columnconfigure(0, weight=1)

            entry = tk.Entry(field, textvariable=var, font=FONT_BODY,
                             bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
                             relief="flat", highlightbackground="#3a3a6a",
                             highlightthickness=1)
            entry.grid(row=0, column=0, sticky="ew")
            self._add_option_guidance(field, opt, 1, wraplength=760)
            self._trace_option(var)
            return 2

        elif opt["type"] == "select":
            self._build_compact_option(parent, opt, row_num, 0)
            return 1

        elif opt["type"] == "multi_select":
            choices = opt["choices"]
            defaults = set(opt.get("default") or [])
            check_vars = {c: tk.BooleanVar(value=(c in defaults)) for c in choices}
            self._option_vars[opt["key"]] = check_vars   # dict[str, BooleanVar]
            cb_frame = tk.Frame(parent, bg=BG)
            cb_frame.grid(row=row_num + 1, column=0, columnspan=3, sticky="ew", pady=(0, 5))
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

            guidance = tk.Label(
                cb_frame,
                text=(
                    f"Description: {opt['description']}\n"
                    f"Example: {opt['example']}\n"
                    f"Expected: {opt['expected_result']}"
                ),
                font=("Segoe UI", 8),
                bg=BG,
                fg=MUTED,
                anchor="nw",
                justify="left",
                wraplength=760,
            )

            def arrange(_event=None):
                width = max(cb_frame.winfo_width(), 320)
                columns = max(1, min(4, width // 190))
                for index, checkbox in enumerate(checkbox_widgets):
                    checkbox.grid(row=index // columns, column=index % columns,
                                  sticky="w", padx=(0, 10), pady=(0, 4))
                for col in range(columns):
                    cb_frame.grid_columnconfigure(col, weight=1, uniform=f"{opt['key']}_choices")
                help_row = (len(checkbox_widgets) + columns - 1) // columns

                if note:
                    note.grid(row=help_row,
                              column=0, columnspan=columns, sticky="ew", pady=(2, 0))

                    help_row += 1

                guidance.grid(
                    row=help_row,
                    column=0,
                    columnspan=columns,
                    sticky="ew",
                    pady=(3, 7),
                )

            cb_frame.bind("<Configure>", arrange)
            arrange()
            return 2

        return 1

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
    @staticmethod
    def _script_command(script_name):
        """Invoke a scripts/ tool as a module, never by path.

        Running `python scripts/cbz_watcher.py` puts scripts/ itself on
        sys.path[0], so the package root is absent and any module-level
        `from scripts...` import dies with ModuleNotFoundError before the
        tool prints anything. Popen already runs with cwd=REPO_ROOT, so
        `-m scripts.<module>` resolves correctly.

        This is not hypothetical: cbz_watcher.py gained such an import in
        113985f and the GUI could not launch it from that commit until this
        fix. tests/test_gui_tool_commands.py asserts every tool in TOOLS can
        actually start, which is the check that was missing.
        """
        return [sys.executable, "-m", f"scripts.{Path(script_name).stem}"]

    def _build_command(self, tool):
        opts = self._option_vars

        # Check if we're applying a saved plan (only relevant for tools with run_mode)
        apply_plan = opts.get("apply_saved_plan") and opts["apply_saved_plan"].get()
        if apply_plan:
            cmd = self._script_command("cbz_library_maintenance.py")
            cmd.append("apply-plan")
            plan_file = LOG_DIR / "plan.json"
            cmd.append(str(plan_file))
            run_mode = opts.get("run_mode")
            if run_mode and run_mode.get() == "Dry run":
                cmd.append("--dry-run")
            return cmd

        cmd = self._script_command(tool["script"])
        if tool.get("subcommand"):
            cmd.append(tool["subcommand"])
        cmd.extend(tool.get("fixed_flags", []))

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

        # Handle run_mode toggle (Dry run / Normal run)
        run_mode = opts.get("run_mode")
        if run_mode:
            mode = run_mode.get()
            if mode == "Dry run":
                cmd.append("--dry-run")
            # "Normal run" means don't add --dry-run flag

        if opts.get("restart") and opts["restart"].get():
            cmd.append("--restart")
        if opts.get("full_rescan") and opts["full_rescan"].get():
            cmd.append("--full")

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

        stages_var = opts.get("stages")

        if stages_var and isinstance(stages_var, dict):

            selected = [stage for stage, value in stages_var.items() if value.get()]

            if selected:

                cmd.append(f"--stages={','.join(selected)}")

        workers_var = opts.get("workers")
        if workers_var:
            cmd.extend(["--workers", workers_var.get()])

        filter_var = opts.get("filter")
        if filter_var and filter_var.get().strip():
            cmd.extend(["--filter", filter_var.get().strip()])

        # metadata_dedupe defaults ON in the CLI, so only emit a flag to disable it.
        if "metadata_dedupe" in opts and not opts["metadata_dedupe"].get():
            cmd.append("--no-metadata-dedupe")

        if opts.get("uncensored_check") and opts["uncensored_check"].get():
            cmd.append("--uncensored-check")

        has_possible_series = (
            (opts.get("possible_series_check") and opts["possible_series_check"].get())
            or "--possible-series-check" in tool.get("fixed_flags", [])
        )
        if has_possible_series:
            if "--possible-series-check" not in tool.get("fixed_flags", []):
                cmd.append("--possible-series-check")

            common_words_var = opts.get("series_common_words")
            if common_words_var:
                cmd.extend(["--series-common-words", common_words_var.get()])

            min_group_size_var = opts.get("series_min_group_size")
            if min_group_size_var:
                cmd.extend(["--series-min-group-size", min_group_size_var.get()])

        # ── Propose / apply / plan tool flags ───────────────────────────────
        if opts.get("out") and opts["out"].get().strip():
            cmd.append(f"--out={opts['out'].get().strip()}")
        # Positional file arguments for apply-plan / apply-series.
        if opts.get("plan_file") and opts["plan_file"].get().strip():
            cmd.append(opts["plan_file"].get().strip())
        if opts.get("decisions_file") and opts["decisions_file"].get().strip():
            cmd.append(opts["decisions_file"].get().strip())
        # Series proposal/workflow commands accept grouping thresholds directly.
        if tool.get("subcommand") in {"propose-series", "series"}:
            cw = opts.get("series_common_words")
            if cw:
                cmd.extend(["--series-common-words", cw.get()])
            mg = opts.get("series_min_group_size")
            if mg:
                cmd.extend(["--series-min-group-size", mg.get()])

        names_only_var = opts.get("names_only")
        if names_only_var and names_only_var.get():
            cmd.append("--names-only")

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
        self._launch_command(cmd)

    def _launch_command(self, cmd):
        """Run *cmd* as a subprocess, streaming stdout into the log pane.

        Shared by the tool Run button and the Series Review window's Apply.
        """
        if self._running:
            self._log_line("Busy — wait for the current run to finish.", "warn")
            return
        self._log_line("\u2500" * 60, "muted")
        self._log_line(f"Running: {' '.join(cmd)}", "muted")
        self._log_line("\u2500" * 60, "muted")

        self._running = True
        self._run_btn.configure(state="disabled", bg="#555", fg=MUTED)
        self._stop_btn.configure(state="normal", bg=ERROR, fg="white")
        self._status_lbl.configure(text="Running\u2026", fg=WARNING)
        if self._active_tool:
            self._start_progress(self._active_tool)

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
                    cwd=str(REPO_ROOT),
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

    # ── Custom option actions ───────────────────────────────────────────────
    def _invoke_option_action(self, action):
        if action == "open_series_review":
            self._open_series_review()
        else:
            self._log_line(f"Unknown action: {action}", "warn")

    def _open_series_review(self):
        """Open the scrollable Yes/No review window for a generated proposal file."""
        out_var = self._option_vars.get("out")
        proposal = (out_var.get().strip() if out_var else "")
        if not proposal:
            messagebox.showwarning("Series Review", "Set the proposal file path first.")
            return
        path = Path(proposal)
        if not path.exists():
            messagebox.showwarning(
                "Series Review",
                "Proposal file not found.\n\nClick Run first to scan the library and "
                "generate the proposal, then open the review.",
            )
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            messagebox.showerror("Series Review", f"Could not read proposal:\n{exc}")
            return
        groups = data.get("groups", [])
        if not groups:
            messagebox.showinfo("Series Review", "No candidate groups were found to review.")
            return
        SeriesReviewWindow(self, path, data, self._apply_series_decisions)

    def _apply_series_decisions(self, decisions_path, dry_run):
        """Launch apply-series on a written decisions file (streams into the log)."""
        script = SCRIPT_DIR / "cbz_library_maintenance.py"
        cmd = [sys.executable, str(script), "apply-series", str(decisions_path)]
        if dry_run:
            cmd.append("--dry-run")
        self._launch_command(cmd)


class SeriesReviewWindow(tk.Toplevel):
    """Scrollable review of candidate same-series groups.

    Each group shows its member folders and offers Yes / No / Skip. Choosing Yes
    enables a target-folder-name field (prefilled with the suggested name). On
    Apply, a decisions JSON is written next to the proposal and apply-series is
    launched via the parent app.
    """

    def __init__(self, app, proposal_path, data, on_apply):
        super().__init__(app)
        self._app = app
        self._proposal_path = Path(proposal_path)
        self._on_apply = on_apply
        self._groups = data.get("groups", [])
        self._rows = []  # list of dicts: {group, verdict_var, target_var, target_entry}

        self.title("Series Review")
        self.configure(bg=BG)
        self.geometry("900x680")
        self.minsize(680, 480)
        self.transient(app)

        header = tk.Frame(self, bg=PANEL)
        header.pack(fill="x")
        tk.Label(header, text="Review candidate series matches", font=("Segoe UI", 15, "bold"),
                 bg=PANEL, fg=TEXT).pack(anchor="w", padx=PAD_X, pady=(14, 2))
        tk.Label(header,
                 text=(f"{len(self._groups)} group(s) from {self._proposal_path.name}.  "
                       "Mark Yes to merge into one folder, No to never match them again."),
                 font=FONT_BODY, bg=PANEL, fg=MUTED, wraplength=840, justify="left").pack(
                     anchor="w", padx=PAD_X, pady=(0, 12))

        # Scrollable body
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        self._inner = tk.Frame(canvas, bg=BG)
        self._inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=self._inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)
        self._canvas = canvas

        for group in self._groups:
            self._build_group_card(group)

        # Footer controls
        footer = tk.Frame(self, bg=PANEL)
        footer.pack(fill="x", side="bottom")
        self._dry_var = tk.BooleanVar(value=True)
        tk.Checkbutton(footer, text="Dry run (preview only)", variable=self._dry_var,
                       bg=PANEL, fg=TEXT, selectcolor=FIELD_BG, activebackground=PANEL,
                       activeforeground=TEXT, font=FONT_BODY).pack(side="left", padx=PAD_X, pady=12)
        tk.Button(footer, text="Cancel", font=FONT_BODY, bg=CARD, fg=TEXT, relief="flat",
                  cursor="hand2", padx=16, pady=6, command=self._close).pack(side="right", padx=(6, PAD_X), pady=12)
        tk.Button(footer, text="Apply decisions", font=("Segoe UI", 10, "bold"), bg=ACCENT,
                  fg="white", relief="flat", cursor="hand2", padx=16, pady=6,
                  command=self._apply).pack(side="right", pady=12)

        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_group_card(self, group):
        card = tk.Frame(self._inner, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=PAD_X, pady=6)

        title = group.get("suggested_name", "Possible Series")
        kind = group.get("kind", "")
        score = group.get("score")
        meta = kind + (f"  ·  score {score}" if score is not None else "")
        tk.Label(card, text=title, font=("Segoe UI", 12, "bold"), bg=CARD, fg=TEXT,
                 anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(card, text=meta, font=("Segoe UI", 8), bg=CARD, fg=MUTED,
                 anchor="w").pack(fill="x", padx=12)

        for member in group.get("members", []):
            tk.Label(card, text=f"   • {member['name']}   ({member.get('file_count', 0)} file(s))",
                     font=FONT_BODY, bg=CARD, fg="#cfd8ff", anchor="w").pack(fill="x", padx=12)

        controls = tk.Frame(card, bg=CARD)
        controls.pack(fill="x", padx=12, pady=(8, 10))

        verdict_var = tk.StringVar(value="skip")
        target_var = tk.StringVar(value=title)

        def _toggle_target(*_):
            state = "normal" if verdict_var.get() == "yes" else "disabled"
            target_entry.configure(state=state)

        for value, text in (("skip", "Skip"), ("yes", "Yes — same series"), ("no", "No — not a match")):
            tk.Radiobutton(controls, text=text, value=value, variable=verdict_var,
                           bg=CARD, fg=TEXT, selectcolor=FIELD_BG, activebackground=CARD,
                           activeforeground=TEXT, font=FONT_BODY,
                           command=_toggle_target).pack(side="left", padx=(0, 12))

        tk.Label(controls, text="Folder:", font=FONT_BODY, bg=CARD, fg=MUTED).pack(side="left", padx=(8, 4))
        target_entry = tk.Entry(controls, textvariable=target_var, font=FONT_BODY, bg=FIELD_BG,
                                fg=TEXT, insertbackground=TEXT, relief="flat",
                                highlightbackground="#3a3a6a", highlightthickness=1, width=32)
        target_entry.pack(side="left", fill="x", expand=True)
        target_entry.configure(state="disabled")

        self._rows.append({
            "group": group,
            "verdict": verdict_var,
            "target": target_var,
        })

    def _apply(self):
        decisions = []
        for row in self._rows:
            verdict = row["verdict"].get()
            if verdict == "skip":
                continue
            group = row["group"]
            entry = {
                "id": group.get("id"),
                "verdict": verdict,
                "parent": group.get("parent"),
                "members": [m["path"] for m in group.get("members", [])],
            }
            if verdict == "yes":
                target = row["target"].get().strip()
                if not target:
                    messagebox.showwarning("Series Review",
                                           f"Enter a folder name for '{group.get('suggested_name')}'.")
                    return
                entry["target_name"] = target
            decisions.append(entry)

        if not decisions:
            messagebox.showinfo("Series Review", "Nothing marked Yes or No — nothing to apply.")
            return

        dec_path = self._proposal_path.with_name("series_decisions.json")
        payload = {"version": 1, "proposal": str(self._proposal_path), "decisions": decisions}
        try:
            dec_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Series Review", f"Could not write decisions:\n{exc}")
            return

        self._on_apply(dec_path, self._dry_var.get())
        self._close()

    def _close(self):
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        self.destroy()


def main():
    app = CBZLauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
