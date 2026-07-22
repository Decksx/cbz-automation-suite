#!/usr/bin/env python3
"""
organize_plex.py — Sanitize and organize media files into Plex-ready folder structures.

Processes Movies, TV Shows, Anime, Kids Shows, Stand-Up Comedy, and custom libraries.
Cleans filenames (strips tech tags, release groups, CRC hashes, etc.), infers episode
numbers, handles Extras/Specials, and moves files to Plex-standard destinations.

Usage:
    python tools\\plex\\organize_plex.py --dry-run
    python tools\\plex\\organize_plex.py
    python tools\\plex\\organize_plex.py --skip "Movies" "Kids Shows"
    python tools\\plex\\organize_plex.py --log C:\\logs\\plex.log
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# LIBRARY DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Library:
    name: str
    sources: list[Path]
    destination: Path
    kind: str                           # 'movie' | 'tvshow' | 'anime'
    exclude_subfolders: list[str] = field(default_factory=list)

LIBRARIES: list[Library] = [
    Library(
        name        = "Anime",
        sources     = [Path(r"Z:\David Anime")],
        destination = Path(r"Z:\Plex\Anime"),
        kind        = "anime",
    ),
    Library(
        name        = "Movies",
        sources     = [Path(r"Z:\Movies")],
        destination = Path(r"Z:\Plex\Movies"),
        kind        = "movie",
    ),
    Library(
        name        = "David Movies",
        sources     = [Path(r"Z:\david movies")],
        destination = Path(r"Z:\Plex\David Movies"),
        kind        = "movie",
    ),
    Library(
        name        = "TV Shows",
        sources     = [Path(r"Z:\TV Shows")],
        destination = Path(r"Z:\Plex\TV Shows"),
        kind        = "tvshow",
    ),
    Library(
        name               = "David Shows",
        sources            = [Path(r"Z:\Davids Shows")],
        destination        = Path(r"Z:\Plex\David Shows"),
        kind               = "tvshow",
        exclude_subfolders = ["Kids Shows", "Stand Up", "more stand up", "Magique"],
    ),
    Library(
        name        = "Kids Shows",
        sources     = [Path(r"Z:\Davids Shows\Kids Shows")],
        destination = Path(r"Z:\Plex\Kids Shows"),
        kind        = "tvshow",
    ),
    Library(
        name        = "Stand Up",
        sources     = [Path(r"Z:\Davids Shows\Stand Up"), Path(r"Z:\Davids Shows\more stand up")],
        destination = Path(r"Z:\Plex\Stand Up"),
        kind        = "movie",   # Plex treats stand-up specials as movies
    ),
    Library(
        name        = "Magique",
        sources     = [Path(r"Z:\Davids Shows\Magique")],
        destination = Path(r"Z:\Plex\Magique"),
        kind        = "tvshow",
    ),
]

VALID_LIBRARY_NAMES = [lib.name for lib in LIBRARIES]

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".iso", ".flv"}

EXTRAS_KEYWORDS = {
    "extras", "special", "specials", "ova", "ovas", "bonus",
    "trailer", "featurette", "behind the scenes", "deleted scene",
    "interview", "short", "scene",
}

# ─────────────────────────────────────────────────────────────────────────────
# REGEX PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

# Leading release group tag: [MiniDEX], [Cleo], etc.
RE_LEADING_GROUP = re.compile(r'^\s*\[[^\]]{1,30}\]\s*')

# CRC hash in brackets: [2D0F2E06], [ABC123] — 5-8 hex chars
RE_CRC = re.compile(r'\[[0-9A-Fa-f]{5,8}\]')

# Dots between word characters (not file extensions)
RE_INTER_WORD_DOT = re.compile(r'(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])')

# Tech tags to strip — broken into named groups for readability
RE_TECH_TAGS = re.compile(
    r'[\(\[]?'
    r'(?:'
        # Resolution
        r'\b(?:2160p?|1080p?|720p?|480p?|4K|UHD|HD|SD)\b'
        r'|'
        # Source / platform
        r'\b(?:BluRay|Blu-Ray|BDRip|BDRemux|BD|WEB-?DL|WEBRip|WEB'
        r'|HMAX|DSNP|AMZN|HULU|NF|CR|HDTV|DVDRip|DVD|HDCAM|CAM|REMUX)\b'
        r'|'
        # Video codec
        r'\b(?:x264|x265|H\.?264|H\.?265|HEVC|AVC|AV1|XviD|MPEG2|VP9|VC-1)\b'
        r'|'
        # Audio codec
        r'\b(?:AAC2?\.0|AAC5?\.1|AAC|AC3|DDP2?\.0|DDP5?\.1|DDP'
        r'|DTS-HD|DTS-MA|DTS|FLAC2?\.0|FLAC|MA5?\.1|Opus|TrueHD|Atmos|EAC3)\b'
        r'|'
        # Bit depth / HDR
        r'\b(?:10-?[Bb]it|10bit|8bit|12bit|HDR10(?:Plus)?|HDR|HLG|DV|Dolby-?Vision)\b'
        r'|'
        # Audio channels
        r'\b(?:Dual-?Audio|Multi-?Audio|Dual|5\.1|2\.0|7\.1)\b'
        r'|'
        # Subtitles / language
        r'\b(?:Eng(?:lish)?-?Subs?|Multi-?Subs?|Subs?|Dubbed|Dub|English-?Dub|Hindi|Esub)\b'
        r'|'
        # Release flags
        r'\b(?:REPACK|PROPER|REAL|EXTENDED|THEATRICAL|UNRATED|UNCENSORED'
        r'|COMPLETE|FULL|RETAIL|LIMITED|INTERNAL|iNTERNAL|DC)\b'
        r'|'
        # Misc
        r'\b(?:IMAX|HFR|60FPS|24FPS)\b'
        r'|'
        # Trailing release group after hyphen at end: -NTb, -FLUX, -YumYum
        r'(?<=-)[A-Za-z0-9]{2,20}$'
    r')'
    r'[\)\]]?',
    re.IGNORECASE,
)

# Empty bracket/paren pairs left after stripping
RE_EMPTY_BRACKETS = re.compile(r'[\(\[\{]\s*[\)\]\}]')

# Whole bracketed/parenthesised blocks that contain tech keywords — strip as a unit.
# e.g. "(Dual Audio 10bit BD1080p x265)", "[US.BD][AV1][10bit][1080p][OPUS]"
_TECH_BLOCK_HINT = (
    r'(?:2160|1080|720|480|4K|UHD|BluRay|BDRip|BD|WEB|HEVC|AVC|AV1|x26[45]|H\.?26[45]'
    r'|AAC|DDP|DTS|FLAC|Opus|TrueHD|Atmos|10.?bit|8bit|HDR|Dual.?Audio|Multi.?Audio'
    r'|Dubbed|Subbed|REMUX|WEBRip|DVDRip|HDTV|Blu.Ray)'
)
RE_BRACKET_TECH_BLOCK = re.compile(
    r'[\(\[][^\(\)\[\]]{0,120}' + _TECH_BLOCK_HINT + r'[^\(\)\[\]]{0,120}[\)\]]',
    re.IGNORECASE,
)

# Dotted codec/audio tokens to strip BEFORE dot→space conversion:
# DD5.1, DDP5.1, DDP2.0, AAC2.0, FLAC2.0, H.264, H.265, VC-1, DTS-HD, etc.
RE_DOTTED_CODECS = re.compile(
    r'\b(?:DDP?|AAC|FLAC|MA)\d?\.\d\b'     # DD5.1 / DDP5.1 / AAC2.0 / FLAC2.0
    r'|'
    r'\bH\.26[45]\b'                         # H.264 / H.265
    r'|'
    r'\bVC-1\b'                              # VC-1
    r'|'
    r'\bDTS(?:-HD|-MA)?\b',                  # DTS / DTS-HD / DTS-MA
    re.IGNORECASE,
)

# Standard SxxExx pattern: S01E01, S01E01-E03, S01E01E02
RE_EP_SXXEXX = re.compile(r'(S\d{1,2})(E\d{2}(?:-?E\d{2})*)', re.IGNORECASE)

# Bare anime episode number: " - 01", "_05_", " 12 "
RE_EP_BARE = re.compile(r'(?:[\s_\-]+)(\d{2,3})(?=[\s_\-\.\[]|$)')

# Year: (2021) or surrounded by non-digits
RE_YEAR = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────────

# ANSI colours for console (Windows 10+ supports these natively)
COLOURS = {
    "INFO":  "\033[97m",   # white
    "DRY":   "\033[96m",   # cyan
    "WARN":  "\033[93m",   # yellow
    "ERROR": "\033[91m",   # red
    "SKIP":  "\033[90m",   # dark grey
    "RESET": "\033[0m",
}


def setup_logging(log_path: Optional[Path], dry_run: bool) -> logging.Logger:
    logger = logging.getLogger("plex_organizer")
    logger.setLevel(logging.DEBUG)

    fmt = "[%(asctime)s] [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Console handler with colour
    class ColourFormatter(logging.Formatter):
        LEVEL_MAP = {
            "INFO":    "INFO",
            "WARNING": "WARN",
            "ERROR":   "ERROR",
            "DEBUG":   "DRY",
        }

        def format(self, record: logging.LogRecord) -> str:
            label = self.LEVEL_MAP.get(record.levelname, record.levelname)
            colour = COLOURS.get(label, "")
            reset  = COLOURS["RESET"]
            ts     = datetime.now().strftime(datefmt)
            return f"{colour}[{ts}] [{label}] {record.getMessage()}{reset}"

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColourFormatter())
    logger.addHandler(console)

    # File handler (skipped in dry-run)
    if not dry_run and log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        logger.addHandler(fh)

    return logger


# Module-level logger; replaced in main()
log = logging.getLogger("plex_organizer")

# ─────────────────────────────────────────────────────────────────────────────
# SANITIZATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_name(raw: str) -> str:
    """Sanitize a folder or file stem, stripping tech tags and normalizing spacing."""
    name = raw

    # 1. Strip leading release group [Tag]
    name = RE_LEADING_GROUP.sub("", name)

    # 2. Strip CRC hashes
    name = RE_CRC.sub("", name)

    # 3. Strip entire bracketed/parenthesised tech blocks BEFORE splitting dots/underscores.
    #    e.g. "(Dual Audio 10bit BD1080p x265)", "[US.BD][AV1][10bit][1080p][OPUS]"
    #    Match any bracket group that contains at least one known tech keyword.
    name = RE_BRACKET_TECH_BLOCK.sub(" ", name)

    # 4. Strip dotted codec/audio tokens while dots are still intact:
    #    DD5.1, DDP5.1, AAC2.0, FLAC2.0, H.264, H.265, VC-1, etc.
    name = RE_DOTTED_CODECS.sub(" ", name)

    # 5. Underscores → spaces; inter-word dots → spaces
    name = name.replace("_", " ")
    name = RE_INTER_WORD_DOT.sub(" ", name)

    # 6. Strip remaining individual tech tags
    name = RE_TECH_TAGS.sub(" ", name)

    # 7. Strip stray leading/trailing hyphens
    name = re.sub(r'\s*-\s*$', '', name)
    name = re.sub(r'^\s*-\s*', '', name)

    # 8. Collapse multiple spaces
    name = re.sub(r' {2,}', ' ', name).strip()

    # 9. Remove empty bracket pairs
    name = RE_EMPTY_BRACKETS.sub("", name).strip()

    # 10. Final trim of trailing punctuation
    name = name.rstrip(".-_ ")

    return name


def extract_year(raw: str) -> Optional[str]:
    """Return the first 4-digit year found in the string, or None."""
    m = RE_YEAR.search(raw)
    return m.group(1) if m else None


def build_folder_name(clean: str, raw: str) -> str:
    """
    Return a Plex-friendly folder name, appending (year) if a year is found
    in the raw name but not already present in the cleaned name.
    """
    year = extract_year(raw)
    if year and year not in clean:
        return f"{clean} ({year})"
    return clean


def is_extra(name: str) -> bool:
    """Return True if the filename looks like an extra/special/OVA."""
    lower = name.lower()
    return any(re.search(rf'\b{re.escape(kw)}\b', lower) for kw in EXTRAS_KEYWORDS)


@dataclass
class EpisodeInfo:
    season:   str   # e.g. "S01"
    episodes: str   # e.g. "E01" or "E01-E03"
    clean:    str   # basename with episode tag removed


def parse_episode(basename: str) -> Optional[EpisodeInfo]:
    """
    Try to extract season/episode info from a file's stem.
    Prefers SxxExx; falls back to bare anime episode numbers.
    """
    # 1. Standard SxxExx
    m = RE_EP_SXXEXX.search(basename)
    if m:
        season   = m.group(1).upper()
        episodes = m.group(2).upper()
        remainder = (basename[:m.start()] + basename[m.end():]).strip(" -")
        return EpisodeInfo(season=season, episodes=episodes, clean=remainder)

    # 2. Bare anime number: " - 04", "_12 "
    m2 = RE_EP_BARE.search(basename)
    if m2:
        ep_num   = m2.group(1).zfill(2)
        remainder = (basename[:m2.start()] + basename[m2.end():]).strip(" -")
        return EpisodeInfo(season="S01", episodes=f"E{ep_num}", clean=remainder)

    return None


def unique_path(path: Path) -> Path:
    """
    If path already exists, append _2, _3, … to the stem until we find
    a free name. Works for both files and directories.
    """
    if not path.exists():
        return path

    parent = path.parent
    suffix = path.suffix
    stem   = path.stem

    i = 2
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1

# ─────────────────────────────────────────────────────────────────────────────
# FILE MOVER
# ─────────────────────────────────────────────────────────────────────────────

def move_file(src: Path, dst: Path, dry_run: bool) -> None:
    """Move src to dst, creating parent directories as needed."""
    dst = unique_path(dst)

    if dry_run:
        log.debug(f'DRY-RUN  MOVE: "{src}"  →  "{dst}"')
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
        log.info(f'MOVED: "{src}"  →  "{dst}"')
    except OSError as exc:
        log.error(f'ERROR moving "{src}": {exc}')

# ─────────────────────────────────────────────────────────────────────────────
# PROCESSORS
# ─────────────────────────────────────────────────────────────────────────────

def process_movie(file: Path, destination: Path, dry_run: bool) -> None:
    """
    Clean and move a movie file.
    Plex structure: Movies/Movie Title (Year)/Movie Title (Year).ext
    """
    stem = file.stem
    ext  = file.suffix

    cleaned     = clean_name(stem)
    folder_name = build_folder_name(cleaned, stem)
    dest_file   = destination / folder_name / f"{folder_name}{ext}"

    move_file(file, dest_file, dry_run)


def process_tv_episode(file: Path, show_name: str, destination: Path, dry_run: bool) -> None:
    """
    Clean and move a single TV/anime episode file.
    Plex structure: Show/Season XX/Show - SxxExx - Title.ext
    """
    stem = file.stem
    ext  = file.suffix

    # Extras go to Season 00 / Specials
    if is_extra(stem):
        dest = destination / show_name / "Specials (Season 00)" / file.name
        move_file(file, dest, dry_run)
        return

    ep = parse_episode(stem)

    if ep:
        season_num    = int(ep.season[1:])
        season_folder = f"Season {season_num:02d}"
        ep_title      = clean_name(ep.clean).strip(" -")

        if ep_title:
            filename = f"{show_name} - {ep.season}{ep.episodes} - {ep_title}{ext}"
        else:
            filename = f"{show_name} - {ep.season}{ep.episodes}{ext}"

        dest = destination / show_name / season_folder / filename
    else:
        log.warning(f'Could not detect episode number for "{file}" — placing in _NeedsReview')
        dest = destination / show_name / "_NeedsReview" / file.name

    move_file(file, dest, dry_run)


def process_show_folder(show_dir: Path, destination: Path, dry_run: bool) -> None:
    """
    Process all video files inside a single show's source folder.
    Cleans the show folder name and routes each episode appropriately.
    """
    raw_name   = show_dir.name
    cleaned    = clean_name(raw_name)
    show_name  = build_folder_name(cleaned, raw_name)

    log.info(f'Processing show: "{raw_name}"  →  "{show_name}"')

    videos = [
        f for f in show_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    ]

    for video in videos:
        process_tv_episode(video, show_name, destination, dry_run)


def process_library(lib: Library, skip: set[str], dry_run: bool) -> None:
    """Process all sources in a library definition."""
    if lib.name in skip:
        log.info(f"Skipping library: {lib.name}")   # logged as SKIP colour via level
        return

    log.info("━" * 51)
    log.info(f"Library: {lib.name}  (Type: {lib.kind})")
    log.info("━" * 51)

    for src_root in lib.sources:
        if not src_root.exists():
            log.warning(f'Source not found, skipping: "{src_root}"')
            continue

        if lib.kind == "movie":
            # Each subfolder = one movie
            for subfolder in sorted(src_root.iterdir()):
                if subfolder.is_dir():
                    for video in subfolder.rglob("*"):
                        if video.is_file() and video.suffix.lower() in VIDEO_EXTENSIONS:
                            process_movie(video, lib.destination, dry_run)
            # Loose files at root
            for video in src_root.iterdir():
                if video.is_file() and video.suffix.lower() in VIDEO_EXTENSIONS:
                    process_movie(video, lib.destination, dry_run)

        elif lib.kind in ("tvshow", "anime"):
            for show_dir in sorted(src_root.iterdir()):
                if show_dir.is_dir() and show_dir.name not in lib.exclude_subfolders:
                    process_show_folder(show_dir, lib.destination, dry_run)

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools\\plex\\organize_plex.py",
        description="Sanitize and organize media files into Plex-ready folder structures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Valid library names:
  {chr(10).join('  ' + n for n in VALID_LIBRARY_NAMES)}

Examples:
  python tools\\plex\\organize_plex.py --dry-run
  python tools\\plex\\organize_plex.py --skip "Movies" "Kids Shows"
  python tools\\plex\\organize_plex.py --log D:\\logs\\plex_run.log
        """,
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview all moves without touching any files.",
    )
    parser.add_argument(
        "--skip", "-s",
        nargs="+",
        metavar="LIBRARY",
        default=[],
        choices=VALID_LIBRARY_NAMES,
        help="One or more library names to skip.",
    )
    parser.add_argument(
        "--log", "-l",
        metavar="PATH",
        default=None,
        help=r"Log file path. Defaults to Z:\Plex\Logs\organize_<timestamp>.log",
    )
    return parser.parse_args()


def main() -> None:
    global log

    args = parse_args()

    # Resolve log path
    if args.log:
        log_path = Path(args.log)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path  = Path(r"Z:\Plex\Logs") / f"organize_{timestamp}.log"

    log = setup_logging(log_path if not args.dry_run else None, args.dry_run)

    skip_set = set(args.skip)

    if args.dry_run:
        log.info("═" * 51)
        log.info("DRY-RUN MODE — No files will be moved or renamed.")
        log.info("═" * 51)
    else:
        log.info("═" * 51)
        log.info("organize_plex.py — Starting run")
        log.info(f"Log: {log_path}")
        log.info("═" * 51)

    for lib in LIBRARIES:
        process_library(lib, skip_set, args.dry_run)

    log.info("═" * 51)
    log.info("Done.")
    if not args.dry_run:
        log.info(f"Full log saved to: {log_path}")


if __name__ == "__main__":
    main()
