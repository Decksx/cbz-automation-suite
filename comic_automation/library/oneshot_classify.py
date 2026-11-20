"""Pure classification of one top-level Comix folder for the oneshots planner (#214).

This module does no I/O. The planner's scanner observes a folder and hands its
facts to :func:`classify_folder`, which applies section 4 of
``docs/comix_oneshots_contract.md`` and returns one class with machine-readable
review reasons. Keeping the rules pure lets every R1 decision be tested
exhaustively without a library.

The classifier is deliberately conservative: anything it cannot prove safe goes
to ``review``, which never moves. In particular a number it cannot tell apart from
part of a title (R1) is ``ambiguous_number``, never ``oneshot``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata


ONESHOTS_DIRECTORY = "_oneshots"
# Contract section 4. Any other file in the folder is an extra file (review).
BOOK_SUFFIXES = frozenset({".cbz", ".cbr", ".zip", ".pdf", ".epub"})

ONESHOT = "oneshot"
NUMBERED_SINGLE = "numbered_single"
SERIES = "series"
REVIEW = "review"

_RESERVED_DEVICES = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$", re.I)

# R1 token families. Each must be followed by digits. "v" needs the digits to
# be attached ("v2", "v 2" is too ambiguous with a title word), so it has its own
# pattern; the longer words allow a separator. Matching is on NFKC-folded text so
# full-width digits count too.
_NUMBERED = [
    ("part", re.compile(r"(?<![a-z])(?:part|pt)\.?\s*[-_#]?\s*(\d+)", re.I)),
    ("chapter", re.compile(r"(?<![a-z])(?:chapter|chap|ch)\.?\s*[-_#]?\s*(\d+)", re.I)),
    ("volume", re.compile(r"(?<![a-z])(?:volume|vol)\.?\s*[-_#]?\s*(\d+)", re.I)),
    ("volume", re.compile(r"(?<![a-z])v(\d+)(?![a-z])", re.I)),
    ("issue", re.compile(r"(?<![a-z])issue\.?\s*[-_#]?\s*(\d+)", re.I)),
    ("issue", re.compile(r"#\s*(\d+)")),
    ("episode", re.compile(r"(?<![a-z])(?:episode|ep)\.?\s*[-_#]?\s*(\d+)", re.I)),
    ("act", re.compile(r"(?<![a-z])act\s+(\d+)", re.I)),
    ("chapter", re.compile(r"第\s*(\d+)\s*話")),
    ("volume", re.compile(r"第\s*(\d+)\s*巻")),
    ("volume", re.compile(r"(\d+)\s*巻")),
]
# A bare number at the end of the stem ("Mob Psycho 100", "1984", "Title 02")
# may be a title, a year or a chapter. R1 can't decide, so it is review.
_BARE_TRAILING = re.compile(r"(?:^|[\s_\-])(\d+)\s*$")


@dataclass(frozen=True)
class NumberEvidence:
    """Why a name counts as numbered (R1), or why it is ambiguous."""

    kind: str          # part | chapter | volume | issue | episode | act | bare
    value: int
    source: str        # file | folder | comicinfo
    text: str


@dataclass(frozen=True)
class FolderFacts:
    """What the scanner observed about one top-level folder. No I/O happens here.

    ``files`` and ``subdirectories`` are names directly inside the folder.
    ``comicinfo`` holds the book's ``Number`` and ``Volume`` text when the single
    book was readable; ``None`` when there is no single book or it wasn't read.
    """

    folder: str
    files: tuple[str, ...]
    subdirectories: tuple[str, ...] = ()
    reparse_point: bool = False
    unreadable: bool = False
    comicinfo: dict[str, str] | None = None
    comicinfo_unreadable: bool = False


@dataclass(frozen=True)
class Classification:
    folder: str
    kind: str
    reasons: tuple[str, ...] = ()
    book: str | None = None
    target: str | None = None
    renamed: bool = False
    number_evidence: NumberEvidence | None = None
    ambiguous: tuple[NumberEvidence, ...] = field(default=())


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _stem_and_suffix(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0:
        return (name if dot < 0 else "", name[dot:].lower() if dot >= 0 else "")
    return name[:dot], name[dot:].lower()


def number_evidence(text: str, source: str) -> tuple[NumberEvidence | None, NumberEvidence | None]:
    """Return ``(numbered, ambiguous)`` evidence for one name.

    An explicit token family wins: ``Title Vol 1`` is numbered. Only when there
    is no explicit token does a bare trailing number count, and then only as
    ambiguous, because ``1984`` and ``Mob Psycho 100`` are titles.
    """
    folded = _fold(text)
    for kind, pattern in _NUMBERED:
        match = pattern.search(folded)
        if match:
            return NumberEvidence(kind, int(match.group(1)), source, match.group(0)), None
    bare = _BARE_TRAILING.search(folded)
    if bare:
        return None, NumberEvidence("bare", int(bare.group(1)), source, bare.group(1))
    return None, None


def _comicinfo_evidence(info: dict[str, str]) -> tuple[NumberEvidence | None, NumberEvidence | None]:
    """Read ComicInfo ``Number``/``Volume``. The caller decides what the number means:
    alone it is only ambiguous, and it is a conflict when it disagrees with the name."""
    for field_name, kind in (("Number", "issue"), ("Volume", "volume")):
        value = (info.get(field_name) or "").strip()
        if not value:
            continue
        digits = re.search(r"\d+", _fold(value))
        if digits:
            return NumberEvidence(kind, int(digits.group(0)), "comicinfo", value), None
        # A non-numeric Number ("Special", "½") can't be classified safely.
        return None, NumberEvidence("bare", -1, "comicinfo", value)
    return None, None


def unsafe_component(name: str) -> bool:
    """True when ``name`` is not one safe Windows path component (contract section 4)."""
    return (
        not name.strip()
        or name in {".", ".."}
        or any(ord(c) < 32 for c in name)
        or any(c in name for c in '<>:"/\\|?*')
        or name.endswith((" ", "."))
        or _RESERVED_DEVICES.fullmatch(name.split(".", 1)[0].rstrip(" .")) is not None
    )


def _descriptive(stem: str) -> bool:
    """A stem that says nothing about the book can't be its _oneshots file name."""
    stripped = re.sub(r"[\s_\-.]+", "", _fold(stem))
    return bool(stripped) and not stripped.isdigit()


def propose_target(book: str, canonical: str, taken: set[str]) -> tuple[str | None, bool, str | None]:
    """Return ``(target, renamed, review_reason)`` for one book (contract section 4).

    ``taken`` holds the NFC-casefolded target names already planned or present in
    ``_oneshots``; the caller adds the result. The original suffix is always kept,
    so ``.cbr`` stays ``.cbr``. Nothing is ever overwritten.
    """
    stem, suffix = _stem_and_suffix(book)
    if _descriptive(stem) and _key(book) not in taken:
        return f"{ONESHOTS_DIRECTORY}/{book}", False, None
    fallback = f"{canonical}{suffix}"
    if unsafe_component(fallback):
        return None, False, "unsafe_path"
    if _key(fallback) in taken:
        return None, False, "target_collision"
    return f"{ONESHOTS_DIRECTORY}/{fallback}", True, None


def classify_folder(facts: FolderFacts, taken: set[str]) -> Classification:
    """Apply contract section 4 to one folder. ``taken`` is updated for a planned oneshot.

    The order matters: structural facts (subfolders, more than one book) settle
    ``series`` before any R1 check, and R1 is checked before a folder can become a
    oneshot, so a numbered single book is never a move candidate.
    """
    folder = facts.folder
    if _key(folder) == ONESHOTS_DIRECTORY:
        return Classification(folder, REVIEW, ("reserved_name",))
    if unsafe_component(folder):
        return Classification(folder, REVIEW, ("unsafe_path",))
    if facts.reparse_point:
        return Classification(folder, REVIEW, ("reparse_point",))
    if facts.unreadable:
        return Classification(folder, REVIEW, ("unreadable",))

    books = [name for name in facts.files if _stem_and_suffix(name)[1] in BOOK_SUFFIXES]
    others = [name for name in facts.files if name not in books]
    if facts.subdirectories or len(books) > 1:
        return Classification(folder, SERIES)
    if not books:
        return Classification(folder, REVIEW, ("extra_files",) if others else ("empty",))

    book = books[0]
    reasons: list[str] = []
    if others:
        # v1: every sidecar, including series.json, keeps the folder in place (F5).
        reasons.append("extra_files")
    if unsafe_component(book):
        reasons.append("unsafe_path")

    numbered: NumberEvidence | None = None
    ambiguous: list[NumberEvidence] = []
    for text, source in ((_stem_and_suffix(book)[0], "file"), (folder, "folder")):
        hit, unclear = number_evidence(text, source)
        numbered = numbered or hit
        if unclear:
            ambiguous.append(unclear)
    if facts.comicinfo is not None:
        hit, unclear = _comicinfo_evidence(facts.comicinfo)
        if unclear:
            ambiguous.append(unclear)
        elif hit is not None:
            if numbered is None:
                # Contract section 4: a number only in ComicInfo is review, not R1.
                # Taggers often set Number=1 on true oneshots, so this alone
                # can't decide the class either way.
                ambiguous.append(hit)
            elif hit.value != numbered.value:
                reasons.append("metadata_conflict")
    elif facts.comicinfo_unreadable:
        reasons.append("unreadable")

    if numbered is not None and not reasons:
        # R1: numbered books stay in (or get) a series folder, including 1.
        return Classification(folder, NUMBERED_SINGLE, (), book, None, False, numbered, tuple(ambiguous))
    if numbered is None and ambiguous:
        reasons.append("ambiguous_number")
    if reasons:
        return Classification(folder, REVIEW, tuple(dict.fromkeys(reasons)), book, None, False,
                              numbered, tuple(ambiguous))

    target, renamed, problem = propose_target(book, folder, taken)
    if problem:
        return Classification(folder, REVIEW, (problem,), book)
    taken.add(_key(target.split("/", 1)[1]))
    return Classification(folder, ONESHOT, (), book, target, renamed)
