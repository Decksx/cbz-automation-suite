"""Canonical archive file names, as a pure fixpoint function.

Implements §6 step 3 part A of ``docs/canonical_naming_and_version_supersession.md``
(§2 grammar, numbers and path safety; §3.2 and §4.6 for what the volume and
version fields mean; §5.5 for the fixpoint requirement). Part B converged the
watcher and the ``cbz_core`` sanitizer onto it: ``cbz_core`` imports
:data:`EDITION_WORDS` for the display tokens it keeps, and the watcher's
fallback names come from :func:`format_canonical_name`.

The grammar (§2.1), with the special word placed as described below::

    <series> [<special>] [v<VV>] [#<III> | ch. <C>] [(<year>)] [(TPB)] [(<edition>)] [[v<N>]].cbz

This module **formats already-resolved fields**. It never resolves them: it
does not decide whether a ``v`` token is a volume or a version (§3.2), and it
does not verify a version label (§4.6). A caller passes ``version`` only once
that label has been verified for the keeper itself.

Three properties are load-bearing and pinned by ``tests/test_canonical_name.py``:

* **Fixpoint.** ``parse_canonical_name(format_canonical_name(f)) == f`` for
  every valid ``f``, and therefore ``format(parse(format(f))) == format(f)``.
  ``format_canonical_name`` checks this on every call and refuses a name that
  would read back as different fields, so an ambiguous series display name
  (``"Batman v01"`` with no volume) is refused rather than renamed into a
  name that later parses as a volume.
* **Idempotent sanitising.** ``sanitize_series_name(sanitize_series_name(x))
  == sanitize_series_name(x)``. ``cbz_core.sanitize`` was not
  (``"Batman].cC"`` -> ``"Batman.cC"`` -> ``""``, §5.5) until part B made it
  a fixpoint, but it also deletes brackets, group tags and URLs, which is
  not this function's job, so the series display name is still not routed
  through it.
* **Refuse, never drop.** Every invalid field raises a specific
  :class:`CanonicalNameError` subclass. Nothing is silently omitted,
  truncated or guessed: a too-long name raises instead of being cut, a
  reserved device name raises instead of being mangled, an edition outside
  the closed list raises instead of being left out.

The module is pure: no filesystem, database, clock or environment access.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, Hashable, Iterable, TypeVar

__all__ = [
    "CANONICAL_EXTENSION",
    "DEFAULT_MAX_NAME_LENGTH",
    "EDITION_WORDS",
    "SPECIAL_WORDS",
    "RESERVED_DEVICE_NAMES",
    "AmbiguousSeriesNameError",
    "CanonicalCollision",
    "CanonicalFields",
    "CanonicalNameError",
    "EmptySeriesNameError",
    "IssueAndChapterError",
    "MalformedNumberError",
    "MalformedVersionError",
    "MalformedYearError",
    "NameTooLongError",
    "NotCanonicalNameError",
    "ReservedNameError",
    "UnknownEditionError",
    "InvalidCollectionError",
    "UnknownSpecialWordError",
    "find_canonical_collisions",
    "format_canonical_name",
    "parse_canonical_name",
    "sanitize_series_name",
]

CANONICAL_EXTENSION = ".cbz"

# ---------------------------------------------------------------------------
# Closed lists.
#
# PROVISIONAL. Design §8 open question 4 leaves the final vocabularies open
# until a scan of real names; these are David's 2026-10-22 decisions as given
# for the step-3 formatter. Editing a list is the whole change: the parser
# and the ambiguity guard are both built from these tuples.
#
# Matching is exact-case. A field must carry the canonical spelling; a
# caller-side alias map (e.g. "colour" -> "Colored") belongs to resolution
# (§3, step 4), not here, so a misspelling is refused rather than corrected.
# ---------------------------------------------------------------------------

#: Edition or language variants, written ``(<edition>)``. One per name.
#: "Colored" and "Colorized" are both listed because both were named; whether
#: one should be folded into the other is undecided and is not decided here.
EDITION_WORDS: tuple[str, ...] = (
    "Uncensored",
    "Decensored",
    "Colored",
    "Colorized",
    "English",
    "Special Edition",
    "Limited Edition",
    "Alternate Cover",
)

#: Collection and special-issue words, written bare after the series name
#: (``Series Name Special (2019).cbz``). One per name.
SPECIAL_WORDS: tuple[str, ...] = (
    "Omnibus",
    "Compilation",
    "Complete",
    "Special",
    "Extra",
    "Bonus",
    "Omake",
    "One-shot",
)

#: Windows reserved device names. Reserved whether or not an extension
#: follows, and case-insensitively. The superscript-digit forms are listed by
#: Microsoft's naming documentation alongside the ASCII ones.
RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{dev}{d}" for dev in ("COM", "LPT") for d in "0123456789¹²³"}
)

#: Cap on the whole file name, extension included, in UTF-16 code units (the
#: unit NTFS counts; one astral character is two). NTFS allows 255 per path
#: component. 200 leaves room for the case-only rename's owned temporary name
#: ``<new>.<attempt>.tmp`` (§5.4), which must itself fit in 255. Whether the
#: *full path* fits the platform limit depends on the directory, which a pure
#: formatter does not know; callers that know it pass a tighter ``max_length``.
DEFAULT_MAX_NAME_LENGTH = 200

VOLUME_WIDTH = 2
ISSUE_WIDTH = 3


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CanonicalNameError(ValueError):
    """Base class: the fields cannot be given a canonical name.

    Callers route the archive to review on any subclass. Subclassing
    ``ValueError`` keeps a generic ``except ValueError`` working, but callers
    should catch this class so an unrelated ``ValueError`` is not mistaken
    for a refusal.
    """


class EmptySeriesNameError(CanonicalNameError):
    """The series display name is empty after sanitising."""


class AmbiguousSeriesNameError(CanonicalNameError):
    """The series display name ends in something that reads as a name token.

    ``"Batman v01"`` with no volume would format as ``Batman v01.cbz``, which
    any reader -- this parser included -- takes as series ``Batman`` volume 1.
    The field belongs in its own slot, so the name is refused.
    """


class IssueAndChapterError(CanonicalNameError):
    """Both an issue and a chapter were given; the grammar allows one."""


class MalformedNumberError(CanonicalNameError):
    """A volume, issue or chapter is not a plain, fractional or range number."""


class MalformedYearError(CanonicalNameError):
    """The year is not a four-digit year."""


class MalformedVersionError(CanonicalNameError):
    """The version is not a positive whole number."""


class UnknownEditionError(CanonicalNameError):
    """The edition is not in :data:`EDITION_WORDS`."""


class UnknownSpecialWordError(CanonicalNameError):
    """The special word is not in :data:`SPECIAL_WORDS`."""


class InvalidCollectionError(CanonicalNameError):
    """A collection must be TPB with a volume and no issue/chapter."""


class ReservedNameError(CanonicalNameError):
    """The name would be a Windows reserved device name."""


class NameTooLongError(CanonicalNameError):
    """The name exceeds the length cap; it is refused, never truncated."""


class NotCanonicalNameError(CanonicalNameError):
    """``parse_canonical_name`` was given a string the formatter cannot produce."""


# ---------------------------------------------------------------------------
# Series display name
# ---------------------------------------------------------------------------

# Characters Windows forbids in a path component, plus every C0 control and
# DEL. Replaced by a space, matching cbz_core._make_windows_safe, so
# "Batman: Year One" reads "Batman Year One" rather than "BatmanYear One".
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_series_name(name: str) -> str:
    """Return *name* made safe for a Windows file name, idempotently.

    Steps, in an order chosen so that no later step can re-create what an
    earlier one removed (which is how ``cbz_core.sanitize`` loses
    idempotence):

    1. NFC normalisation, so equal-looking names compare equal.
    2. Forbidden characters and control characters become spaces.
    3. Whitespace runs collapse to one space; leading/trailing whitespace goes.
    4. Trailing dots and spaces go, together, so ``"Foo . ."`` cannot leave a
       space that step 3 would have removed on a second pass.

    Unlike ``cbz_core.sanitize`` this never removes brackets, extensions or
    group tags: by the time a display name reaches the formatter those
    decisions have been made (§5.5), and removing text is not this function's
    job. It may return ``""``; :class:`CanonicalFields` refuses that.
    """
    if not isinstance(name, str):
        raise TypeError(f"series name must be str, got {type(name).__name__}")
    text = unicodedata.normalize("NFC", name)
    text = _FORBIDDEN_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.rstrip(" .")


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

# One endpoint: digits with an optional fractional part. "12." and ".5" are
# malformed; so are signs, exponents and spaces.
# ASCII digits only: Python's \d also matches fullwidth and other Unicode
# digits, which int() accepts but which are not the canonical spelling.
_ENDPOINT = r"[0-9]+(?:\.[0-9]+)?"
_NUMBER_RE = re.compile(rf"({_ENDPOINT})(?:-({_ENDPOINT}))?")


def _canonical_endpoint(text: str) -> str:
    """Strip integer-part leading zeros; keep the fraction exactly as given.

    The fraction is kept verbatim ("12.50" stays "12.50") because whether a
    trailing zero is significant is a resolution question, not a formatting
    one. Leading zeros are padding, which the formatter re-applies.
    """
    whole, dot, frac = text.partition(".")
    return (whole.lstrip("0") or "0") + dot + frac


def _canonical_number(value: object, field: str) -> str | None:
    """Validate and normalise a volume/issue/chapter value.

    Accepts a non-negative ``int`` or a string of the form ``N``, ``N.F`` or
    ``A-B`` (each endpoint optionally fractional). ``float`` is refused:
    ``12.5`` is exact but ``0.1 + 0.2`` is not, and a binary float cannot say
    whether ``12.50`` was meant. ``bool`` is refused although it is an int.
    A range must ascend strictly; ``3-1`` and ``2-2`` are malformed.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MalformedNumberError(f"{field}: expected int or str, got {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise MalformedNumberError(f"{field}: negative number {value!r}")
        return str(value)
    match = _NUMBER_RE.fullmatch(value)
    if match is None:
        raise MalformedNumberError(f"{field}: malformed number {value!r}")
    low = _canonical_endpoint(match.group(1))
    if match.group(2) is None:
        return low
    high = _canonical_endpoint(match.group(2))
    if Decimal(low) >= Decimal(high):
        raise MalformedNumberError(f"{field}: range {value!r} does not ascend")
    return f"{low}-{high}"


def _pad(number: str, width: int) -> str:
    """Zero-pad the integer part of each endpoint to *width*; keep fractions."""
    padded = []
    for endpoint in number.split("-"):
        whole, dot, frac = endpoint.partition(".")
        padded.append(whole.zfill(width) + dot + frac)
    return "-".join(padded)


def _canonical_year(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MalformedYearError(f"year: {value!r}")
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9]{4}", value):
            raise MalformedYearError(f"year: {value!r} is not four digits")
        value = int(value)
    if not isinstance(value, int) or not 1000 <= value <= 9999:
        raise MalformedYearError(f"year: {value!r} is not a four-digit year")
    return value


def _canonical_version(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MalformedVersionError(f"version: {value!r}")
    if isinstance(value, str):
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise MalformedVersionError(f"version: {value!r} is not a positive integer")
        value = int(value)
    if not isinstance(value, int) or value < 1:
        raise MalformedVersionError(f"version: {value!r} is not a positive integer")
    return value


# ---------------------------------------------------------------------------
# Ambiguity guard
# ---------------------------------------------------------------------------


def _alternation(words: Iterable[str]) -> str:
    # Longest first, so a word that prefixes another cannot win by order.
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


# A series display name may not END in anything a reader would take as a
# grammar token. Deliberately looser than the parser: case-insensitive, any
# padding, with or without the space after "ch.". The parser alone would only
# catch exact canonical tokens, but "Batman v1" or "Foo Ch.3" is just as
# misleading to a human and to the name-based candidate extraction of step 4.
_TRAILING_TOKEN_RE = re.compile(
    r"(?:"
    r"(?:^|\s)v\d[\d.\-]*"            # volume-like: v1, v01, v01-02
    r"|#\d[\d.\-]*"                   # issue-like: #3, #003
    r"|(?:^|\s)ch\.?\s*\d[\d.\-]*"    # chapter-like: ch. 1, ch1, Ch.12.5
    r"|\(\d{4}\)"                     # year
    r"|\[v\d+\]"                      # version
    rf"|\((?:{_alternation(EDITION_WORDS)}|TPB)\)"
    rf"|(?:^|\s)(?:{_alternation(SPECIAL_WORDS)})"
    r")$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalFields:
    """Already-resolved fields of one canonical name.

    Construction validates and normalises, so an instance is always valid and
    equal instances always format identically:

    * ``series`` is passed through :func:`sanitize_series_name`.
    * ``volume``/``issue``/``chapter`` become canonical number strings without
      padding (``"003"`` -> ``"3"``, ``12`` -> ``"12"``, ``"01-02"`` ->
      ``"1-2"``); the formatter applies padding.
    * ``year`` and ``version`` become ``int``.

    ``issue`` and ``chapter`` are mutually exclusive. ``volume`` may combine
    with either: the §2.1 grammar admits ``v<VV> ch. <C>`` even though its
    prose describes chapters as the volume-less form.

    ``series_year`` is reboot identity before primary numbering. The issue
    year stays in ComicInfo and is omitted from this series-year filename.

    ``collection`` is a separate collected-edition marker (currently TPB),
    requiring a volume without an issue/chapter. It coexists with edition
    variants such as Colored, which must not disappear.

    ``version`` must only be set when §4.6 has verified it for this archive
    itself; the formatter cannot check that.
    """

    series: str
    volume: str | int | None = None
    issue: str | int | None = None
    chapter: str | int | None = None
    year: int | str | None = None
    edition: str | None = None
    special: str | None = None
    version: int | str | None = None
    collection: str | None = None
    series_year: int | str | None = None

    def __post_init__(self) -> None:
        series = sanitize_series_name(self.series)
        if not series:
            raise EmptySeriesNameError(f"series {self.series!r} is empty after sanitising")
        if _TRAILING_TOKEN_RE.search(series):
            raise AmbiguousSeriesNameError(
                f"series {series!r} ends in a name token; pass it as a field instead"
            )
        if self.issue is not None and self.chapter is not None:
            raise IssueAndChapterError(
                f"issue {self.issue!r} and chapter {self.chapter!r} are mutually exclusive"
            )
        if self.edition is not None and self.edition not in EDITION_WORDS:
            raise UnknownEditionError(f"edition {self.edition!r} is not in EDITION_WORDS")
        if self.special is not None and self.special not in SPECIAL_WORDS:
            raise UnknownSpecialWordError(f"special {self.special!r} is not in SPECIAL_WORDS")
        if self.collection is not None and (
            self.collection != "TPB" or self.volume is None
            or self.issue is not None or self.chapter is not None
        ):
            raise InvalidCollectionError("TPB requires a volume and no issue/chapter")
        set_ = object.__setattr__
        set_(self, "series", series)
        set_(self, "volume", _canonical_number(self.volume, "volume"))
        set_(self, "issue", _canonical_number(self.issue, "issue"))
        set_(self, "chapter", _canonical_number(self.chapter, "chapter"))
        set_(self, "year", _canonical_year(self.year))
        set_(self, "version", _canonical_version(self.version))
        set_(self, "series_year", _canonical_year(self.series_year))
        if self.series_year is not None:
            if all(v is None for v in (self.volume, self.issue, self.chapter)):
                raise CanonicalNameError("series year requires primary numbering to distinguish it from issue year")
            set_(self, "year", None)  # Reboot year is the only filename year; issue year stays in XML.


# ---------------------------------------------------------------------------
# Format and parse
# ---------------------------------------------------------------------------

_NUM = rf"{_ENDPOINT}(?:-{_ENDPOINT})?"
# Built from the closed lists so editing a list cannot desynchronise the
# parser from the formatter. The series group is non-greedy, so trailing
# tokens bind to their slots; the formatter's round-trip check is what makes
# that safe, by refusing any series the non-greedy split would cut short.
_CANONICAL_RE = re.compile(
    r"(?P<series>.+?)"
    r"(?: \((?P<series_year>[0-9]{4})\)"
    rf"(?= (?:(?:{_alternation(SPECIAL_WORDS)}) )?(?:v[0-9]|#[0-9]|ch\. [0-9])))?"
    rf"(?: (?P<special>{_alternation(SPECIAL_WORDS)}))?"
    rf"(?: v(?P<volume>{_NUM}))?"
    rf"(?: (?:#(?P<issue>{_NUM})|ch\. (?P<chapter>{_NUM})))?"
    r"(?: \((?P<year>[0-9]{4})\))?"
    r"(?: \((?P<collection>TPB)\))?"
    rf"(?: \((?P<edition>{_alternation(EDITION_WORDS)})\))?"
    r"(?: \[v(?P<version>[1-9][0-9]*)\])?"
    + re.escape(CANONICAL_EXTENSION)
)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _is_reserved(name: str) -> bool:
    # Windows ignores everything from the first dot, and trailing spaces
    # before it, when matching device names: "CON.cbz", "con .cbz" and
    # "Con. Smith.cbz" all open the console device.
    base = name.split(".", 1)[0].rstrip(" ")
    return base.upper() in RESERVED_DEVICE_NAMES


def _assemble(fields: CanonicalFields) -> str:
    parts = [fields.series + (f" ({fields.series_year})" if fields.series_year is not None else "")]
    if fields.special is not None:
        parts.append(fields.special)
    if fields.volume is not None:
        parts.append(f"v{_pad(fields.volume, VOLUME_WIDTH)}")
    if fields.issue is not None:
        parts.append(f"#{_pad(fields.issue, ISSUE_WIDTH)}")
    if fields.chapter is not None:
        parts.append(f"ch. {fields.chapter}")
    if fields.year is not None:
        parts.append(f"({fields.year})")
    if fields.collection is not None:
        parts.append(f"({fields.collection})")
    if fields.edition is not None:
        parts.append(f"({fields.edition})")
    if fields.version is not None:
        parts.append(f"[v{fields.version}]")
    return " ".join(parts) + CANONICAL_EXTENSION


def _parse_unchecked(name: str) -> CanonicalFields:
    match = _CANONICAL_RE.fullmatch(name)
    if match is None:
        raise NotCanonicalNameError(f"{name!r} does not match the canonical grammar")
    try:
        return CanonicalFields(**match.groupdict())
    except CanonicalNameError as exc:
        raise NotCanonicalNameError(f"{name!r} parses to invalid fields: {exc}") from exc


def format_canonical_name(
    fields: CanonicalFields, *, max_length: int = DEFAULT_MAX_NAME_LENGTH
) -> str:
    """Return the canonical file name for *fields*.

    Raises :class:`ReservedNameError` for a Windows device name,
    :class:`NameTooLongError` above *max_length* UTF-16 code units, and
    :class:`AmbiguousSeriesNameError` if the name would parse back as
    different fields. The last check is the fixpoint guarantee enforced at
    the source: no name leaves this function unless
    ``parse_canonical_name(name) == fields``.
    """
    if not isinstance(fields, CanonicalFields):
        raise TypeError(f"expected CanonicalFields, got {type(fields).__name__}")
    name = _assemble(fields)
    if _is_reserved(name):
        raise ReservedNameError(f"{name!r} is a Windows reserved device name")
    if _utf16_length(name) > max_length:
        raise NameTooLongError(
            f"{name!r} is {_utf16_length(name)} UTF-16 units, cap is {max_length}"
        )
    # Backstop behind _TRAILING_TOKEN_RE: if some series still splits
    # differently on the way back, refuse it here rather than emit a name
    # whose parsed fields disagree with the ones that produced it. With the
    # guard as written no known input reaches this branch (mutation testing
    # found removing it alone fails nothing); it is kept because the guard
    # regex and the parser regex are separate code that a closed-list edit
    # or a grammar change could desynchronise, and it is tested by disabling
    # the guard (test_round_trip_self_check_backstops_the_trailing_token_guard).
    try:
        reparsed = _parse_unchecked(name)
    except NotCanonicalNameError as exc:
        raise AmbiguousSeriesNameError(f"{name!r} does not parse back: {exc}") from exc
    if reparsed != fields:
        raise AmbiguousSeriesNameError(
            f"{name!r} parses back as {reparsed!r}, not {fields!r}"
        )
    return name


def parse_canonical_name(
    name: str, *, max_length: int = DEFAULT_MAX_NAME_LENGTH
) -> CanonicalFields:
    """Parse a name produced by :func:`format_canonical_name` back to fields.

    Accepts exactly the formatter's image: a string is accepted only if
    re-formatting its parsed fields reproduces it byte for byte. So
    ``Batman v1.cbz`` (unpadded), ``Batman #0003.cbz`` (over-padded) and
    ``Batman.CBZ`` are refused with :class:`NotCanonicalNameError`. This is a
    recogniser for already-canonical names, never a parser of arbitrary
    library file names: it classifies nothing from name evidence (§3.2).
    """
    fields = _parse_unchecked(name)
    try:
        again = format_canonical_name(fields, max_length=max_length)
    except CanonicalNameError as exc:
        raise NotCanonicalNameError(f"{name!r} is not a valid canonical name: {exc}") from exc
    if again != name:
        raise NotCanonicalNameError(f"{name!r} is not canonical; canonical form is {again!r}")
    return fields


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------

S = TypeVar("S", bound=Hashable)


@dataclass(frozen=True)
class CanonicalCollision(Generic[S]):
    """Two or more sources that would receive the same canonical name.

    ``key`` is the case-folded name; ``entries`` holds each ``(source,
    canonical_name)`` in input order. Every entry goes to review: §2.3
    refuses *all* of them, not all but one.
    """

    key: str
    entries: tuple[tuple[S, str], ...]


def find_canonical_collisions(
    items: Iterable[tuple[S, CanonicalFields]],
    *,
    max_length: int = DEFAULT_MAX_NAME_LENGTH,
) -> list[CanonicalCollision[S]]:
    """Report sources whose canonical names collide case-insensitively.

    The caller passes the archives of **one** target directory; a pure
    function cannot know which names share a directory. Comparison uses
    ``str.casefold``, which folds at least as much as NTFS's case-insensitive
    comparison, so it can over-report (sent to review) but not under-report.

    Formatting errors propagate: an item that has no canonical name is a
    review item in its own right, and silently skipping it here would hide
    that. Collisions are returned in order of each key's first appearance.
    """
    groups: dict[str, list[tuple[S, str]]] = {}
    for source, fields in items:
        name = format_canonical_name(fields, max_length=max_length)
        groups.setdefault(name.casefold(), []).append((source, name))
    return [
        CanonicalCollision(key=key, entries=tuple(entries))
        for key, entries in groups.items()
        if len(entries) > 1
    ]
