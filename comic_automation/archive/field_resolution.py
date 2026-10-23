"""Field resolution and version-family evidence for canonical naming (read-only).

Step 4 of ``docs/canonical_naming_and_version_supersession.md`` (§3, §4.2).
This module decides, for each archive, what its series, volume, primary
number (issue or chapter), year and version *are*, and with what evidence.
It does not format a name (step 3), compare page sequences for
supersession (step 5), or rename anything (step 7). Its output is what the
formatter and the plan consume.

Nothing here writes. The filesystem helpers open archives with
``zipfile.ZipFile(mode="r")`` and list directories; the one database
helper goes through `read_guards.read_consistent_snapshot`, the
repository's WAL-aware read-only path (``mode=ro`` + ``query_only`` +
``data_version`` bracketing), and opens no connection of its own.

The model
---------

Every field resolves independently to a `FieldResolution`: either a value
with its deciding source and every piece of evidence considered, or
**unresolved** with the candidates and the reason. Conflicting evidence is
never averaged and never picked.

Sources, in the §3.1 precedence order:

1. ``human_decision`` -- an injected `HumanDecisionSource`. The decision
   manifest (``docs/series_identity_decision_manifest.md``) is not built,
   so the default is `NO_HUMAN_DECISIONS`. A human decision overrides every
   lower source, including one that conflicts with it. Two human decisions
   that disagree are a persistent conflict (`_human_conflicts`): the field
   is unresolved, the archive is not renamable, its family is refused, and
   the conflicting values never classify a ``v<N>`` token.
2. ``comicinfo`` -- ``Series``/``Volume``/``Number``/``Year``, counted only
   when the document is internally consistent and the field is not one the
   watcher's injected template restated from the filename
   (`ComicInfoFacts.authoritative`). Editing an unrelated field of an
   injected template does not promote its injected fields.
3. ``folder_context`` (and, for the version field, ``version_family``) --
   the resolved series folder, sibling runs that are numbered consistently,
   and established version families.
4. ``filename`` -- a candidate only. It is recorded as evidence and shown
   in review, and never decides a field on its own.

**Levels 2 and 3 must agree.** §3.1 orders the sources but also says a
field with conflicting evidence is unresolved, and §7 names
``test_conflicting_comicinfo_and_folder_evidence_is_unresolved``. So when
ComicInfo and folder context both speak to a field and disagree, the field
is unresolved; when only one speaks, it decides; when both agree, the
higher one is reported as the source. Only a human decision overrides a
disagreement. The filename never conflicts with anything, because a name
that disagrees with better evidence is exactly what a rename fixes.

``v<N>`` tokens (§3.2)
----------------------

A bare ``v<N>`` in a current filename may be a volume or a version.
`VTokenClassification` records which, and name evidence alone never
decides:

* **volume** when authoritative ComicInfo ``Volume`` equals N, or when the folder
  forms a volume run whose page content shows different books
  (`_volume_run`);
* **version** only when the archive is a member of an established version
  family (`establish_family`);
* **unresolved** when neither holds, or when both do.

A bracketed ``[vN]`` is the canonical version token (§2.1) and is never a
volume, but it is still only a *version marker*: it becomes a version only
through an established family.

Version families (§4.2)
-----------------------

A family needs affirmative evidence, independent of page overlap: the same
decided series and the same resolved primary number (or a human decision
declaring the family), the same edition set and the same ComicInfo
``LanguageISO`` (absent counts as a value for both), the same
special/collection marker from the formatter's ``SPECIAL_WORDS`` (an
Omnibus #1 is not a version of issue #1), the same unrecognised
name tags (a tag the vocabulary does not know may be an unsupported
variant), no member with conflicting human decisions, and at least one
version-marked member. Page content is never family evidence.
It is, however, a gate: §4.5 says incomparable inventories are never a
version, so a family whose members do not all carry a comparable ordered
signature under the current ordering contract (``CONTENT_SIGNATURE_VERSION``
with a resolved order, at least one page, a non-empty digest for every
page, and a stored inventory whose row count and indexes match its
signature) is reported ``incomparable``
and classifies nothing. A legacy signature can therefore never put an
archive into a family.

Establishing a family is not supersession. Whether one member's pages are
a prefix of another's, bound to the current bytes, is step 5.

Renamability
------------

An archive is renamable only when its series and its primary number are
resolved (§3.1), where the primary number is ``number`` when any source
offers one and otherwise ``volume``. Any conflicting human decision also
blocks it. This module also refuses an archive
whose name carries an unresolved ``v<N>`` token: that token is a volume or a
version, both of which the canonical name must keep, and renaming without
it would discard the only record of it. That last rule is this module's
conservative reading, not a sentence in the design.
"""

from __future__ import annotations

import re
import sqlite3
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree

from comic_automation.archive.archive_limits import validate_archive_members
from comic_automation.archive.inspection import (
    COMIC_INFO_FILENAME,
    MAX_COMIC_INFO_BYTES,
)
from comic_automation.archive.page_hashing import (
    CONTENT_SIGNATURE_ALGORITHM,
    CONTENT_SIGNATURE_VERSION,
    PAGE_HASH_ALGORITHM,
    PAGE_HASH_ALGORITHM_VERSION,
    ArchivePageHashes,
)
from comic_automation.database.read_guards import (
    ConsistentSnapshot,
    read_consistent_snapshot,
)
from scripts.cbz_canonical_name import EDITION_WORDS, SPECIAL_WORDS
from scripts.cbz_routing import series_key


FIELD_SERIES = "series"
FIELD_VOLUME = "volume"
FIELD_NUMBER = "number"
FIELD_YEAR = "year"
FIELD_VERSION = "version"
FIELDS = (FIELD_SERIES, FIELD_VOLUME, FIELD_NUMBER, FIELD_YEAR, FIELD_VERSION)

# A human decision may also declare a version family (§4.2, last bullet).
# It is not a name field, so it is not in FIELDS.
DECISION_FAMILY = "family"

SOURCE_HUMAN = "human_decision"
SOURCE_COMICINFO = "comicinfo"
SOURCE_FOLDER = "folder_context"
SOURCE_FAMILY = "version_family"
SOURCE_FILENAME = "filename"

# Precedence for reporting. SOURCE_FAMILY sits at the folder-context level:
# a family is a relation between siblings, established from their metadata.
PRECEDENCE = (
    SOURCE_HUMAN,
    SOURCE_COMICINFO,
    SOURCE_FOLDER,
    SOURCE_FAMILY,
    SOURCE_FILENAME,
)

V_VOLUME = "volume"
V_VERSION = "version"
V_UNRESOLVED = "unresolved"

FAMILY_ESTABLISHED = "established"
FAMILY_REFUSED = "refused"
FAMILY_INCOMPARABLE = "incomparable"

# The edition vocabulary is the step-3 formatter's closed list
# (`scripts.cbz_canonical_name.EDITION_WORDS`, provisional per design §8
# question 4), so resolution and formatting can never disagree about what
# an edition is. Matching here is case-insensitive and yields the canonical
# spelling. No aliases are added: "Colored" and "Colorized" stay distinct
# because their equivalence is undecided, and a spelling outside the list
# ("Coloured") is not silently folded into one inside it -- it is left as
# an unrecognised tag, which refuses a family rather than vanishing.
EDITION_VOCABULARY: tuple[str, ...] = EDITION_WORDS
# The formatter's special/collection slot (`SPECIAL_WORDS`), also
# provisional under §8 question 4. A special marker is work identity: an
# Omnibus #1 is not a version of issue #1, whatever Series and Number say.
SPECIAL_VOCABULARY: tuple[str, ...] = SPECIAL_WORDS
# The decensor markers. Members differing only by these are a decensor
# pair, which §4.5 says is never a version.
DECENSOR_EDITIONS = frozenset({"Uncensored", "Decensored"})

YEAR_MIN = 1900
YEAR_MAX = 2100


# --- the watcher's injected ComicInfo template ------------------------------
#
# `scripts/cbz_watcher.py` and `scripts/cbz_sanitizer.py` inject the same
# COMICINFO_TEMPLATE into an archive that has no ComicInfo.xml, then fill
# Title/Series/Number (and Volume, Notes, AlternateSeries) *from the
# filename parse*. Such a document restates the filename; counting it as
# level-2 evidence would let the filename decide a field through the back
# door. It is recognized by the template's fingerprint, not by its text,
# because filling it changes the text.
#
# A document is **template-derived** when it carries the Komga provenance
# stamp (SourceMihon == "Komga", which the template writes and the routing
# config already treats as circular provenance) and both Tachiyomi elements
# the template carries. It is a **pristine** template when, in addition,
# every template field the watcher does not fill is still empty and no
# element lies outside the template plus the fields the injectors write.
#
# Editing a template-derived document does not launder it. A Writer filled
# in, or a Tags element added, says nothing about whether Series, Number or
# Volume were ever corrected: those still restate the filename parse. So a
# template-derived document's injector-written fields keep their injected
# origin (`ComicInfoFacts.uncertain_fields`) and are filename-grade
# candidates, never deciding evidence. Only fields the injectors never write
# (Year, LanguageISO, ...) can count from an edited template. A field-specific
# human decision, or independent folder evidence, still resolves the
# uncertain ones in the ordinary way.
#
# The cost, accepted on review: a genuine Mihon export from a Komga source
# carries the same stamp and is treated as template-derived too.
# `tests/test_field_resolution.py` pins these sets against the real
# templates, so a template change that drifts from them fails a test.
TEMPLATE_ELEMENTS = frozenset({
    "Title",
    "Series",
    "Number",
    "Summary",
    "Writer",
    "Penciller",
    "Genre",
    "Web",
    "PublishingStatusTachiyomi",
    "Categories",
    "SourceMihon",
})
TEMPLATE_INJECTOR_WRITES = frozenset({
    "Title",
    "Series",
    "Number",
    "Volume",
    "Notes",
    "AlternateSeries",
})
TEMPLATE_EMPTY_ELEMENTS = frozenset({
    "Summary",
    "Writer",
    "Penciller",
    "Genre",
    "Web",
    "PublishingStatusTachiyomi",
    "Categories",
})
TEMPLATE_PROVENANCE_ELEMENT = "SourceMihon"
TEMPLATE_PROVENANCE_VALUE = "komga"
TEMPLATE_MARKER_ELEMENTS = frozenset({"PublishingStatusTachiyomi", "Categories"})

# ComicInfo fields read here. Count and Month are read only for the
# internal-consistency checks; LanguageISO is family identity (§4.2).
_COMICINFO_FIELDS = (
    "Series", "Volume", "Number", "Year", "Count", "Month", "LanguageISO",
)


# --- numbers and years -----------------------------------------------------

_NUMBER_TEXT = r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?"
_NUMBER_FULL_RE = re.compile(
    r"\s*(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*"
)


def _normalize_one(text: str) -> str:
    whole, _, fraction = text.partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def normalize_number(text: str | None) -> str | None:
    """Canonical text of a §2.2 number, or None if it is not one.

    Plain, fractional and range numbers are accepted (``003`` -> ``3``,
    ``12.50`` -> ``12.5``, ``1 - 03`` -> ``1-3``). Padding is the
    formatter's job; this is the comparison form, so ``#003`` in a name and
    ``<Number>3</Number>`` agree. Anything else (``Special``, ``1a``) is not
    a number and returns None: §2.2 sends specials outside the closed list
    to review, and this module does not hold that list.
    """
    if text is None:
        return None
    match = _NUMBER_FULL_RE.fullmatch(text)
    if match is None:
        return None
    first, second = match.groups()
    if second is None:
        return _normalize_one(first)
    return f"{_normalize_one(first)}-{_normalize_one(second)}"


def normalize_year(text: str | None) -> str | None:
    """A four-digit year in [YEAR_MIN, YEAR_MAX], or None."""
    if text is None:
        return None
    stripped = text.strip()
    if not re.fullmatch(r"\d{4}", stripped):
        return None
    return stripped if YEAR_MIN <= int(stripped) <= YEAR_MAX else None


# --- evidence and results --------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One source's statement about one field.

    ``value`` is the comparison form (normalized number, raw series text).
    ``display`` is the spelling as the source wrote it (``12.50``, ``003``),
    so the formatter receives the source's spelling rather than the
    comparison key: `normalize_number` drops trailing fractional zeros,
    which the step-3 formatter deliberately preserves. ``detail`` says where
    it came from, for the review section.
    """

    source: str
    value: str
    detail: str
    display: str | None = None

    @property
    def display_value(self) -> str:
        return self.value if self.display is None else self.display

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "value": self.value,
            "display": self.display_value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FieldResolution:
    """A field's outcome: resolved to one value, or unresolved.

    ``evidence`` holds everything considered, deciding or not, in
    precedence order, so an unresolved field carries its candidates and a
    resolved one carries its corroboration. ``reason`` is a short code:
    ``resolved``, ``human_conflict``, ``conflict``, ``filename_candidate_only``
    or ``no_evidence``.

    ``display_value`` is the deciding evidence's own spelling of ``value``.
    When several deciding sources agree on the value but spell it
    differently (``12.50`` and ``12.5``), the highest-precedence source's
    spelling is reported, and every spelling stays in ``evidence``.
    """

    field: str
    value: str | None
    source: str | None
    evidence: tuple[Evidence, ...]
    reason: str
    display_value: str | None = None

    @property
    def resolved(self) -> bool:
        return self.value is not None

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "resolved": self.resolved,
            "value": self.value,
            "display_value": self.display_value,
            "source": self.source,
            "reason": self.reason,
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class HumanDecision:
    """One recorded human decision about one field of one archive.

    ``field`` is one of `FIELDS`, or `DECISION_FAMILY` with a family id as
    the value. ``record_id`` is the decision record it came from, carried
    into the evidence so the plan can cite it.
    """

    field: str
    value: str
    record_id: str


class HumanDecisionSource(Protocol):
    """Where recorded human decisions come from.

    The decision manifest that will implement this does not exist yet. The
    resolver only reads through this interface, so the manifest can be
    plugged in without touching the precedence logic.
    """

    def decisions_for(self, archive_key: str) -> Iterable[HumanDecision]:
        ...


class StaticHumanDecisions:
    """A fixed mapping of archive key to decisions. Empty by default."""

    def __init__(
        self, decisions: Mapping[str, Iterable[HumanDecision]] | None = None
    ) -> None:
        self._decisions = {
            key: tuple(values) for key, values in (decisions or {}).items()
        }

    def decisions_for(self, archive_key: str) -> Iterable[HumanDecision]:
        return self._decisions.get(archive_key, ())


NO_HUMAN_DECISIONS: HumanDecisionSource = StaticHumanDecisions()


# --- ComicInfo -------------------------------------------------------------

COMICINFO_ABSENT = "absent"
COMICINFO_USABLE = "usable"
COMICINFO_TEMPLATE = "injected_template"
COMICINFO_EDITED_TEMPLATE = "edited_template"
COMICINFO_INCONSISTENT = "inconsistent"


@dataclass(frozen=True)
class ComicInfoFacts:
    """What an archive's ComicInfo.xml may contribute, and why.

    ``status`` is ``absent``, ``usable``, ``injected_template`` (pristine),
    ``edited_template`` or ``inconsistent``. ``fields`` holds the raw
    stripped text of the read fields, whatever the status.

    ``uncertain_fields`` are fields whose origin is the injector's filename
    parse: every read field of a pristine template, and the
    injector-written ones (`TEMPLATE_INJECTOR_WRITES`) of an edited one.
    `authoritative` is the one test of whether a field is level-2 evidence.
    """

    status: str
    fields: Mapping[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    uncertain_fields: frozenset[str] = frozenset()

    @property
    def usable(self) -> bool:
        return self.status == COMICINFO_USABLE

    def authoritative(self, name: str) -> bool:
        """True when field ``name`` counts as ComicInfo evidence."""
        return (
            self.status in (COMICINFO_USABLE, COMICINFO_EDITED_TEMPLATE)
            and name in self.fields
            and name not in self.uncertain_fields
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(root: ElementTree.Element) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for child in root:
        children.setdefault(_local_name(child.tag), []).append(
            (child.text or "").strip()
        )
    return children


def is_template_derived(root: ElementTree.Element) -> bool:
    """True when ``root`` started life as the injected template.

    The Komga provenance stamp plus both Tachiyomi marker elements, whatever
    else has been edited since. See the comment above `TEMPLATE_ELEMENTS`.
    """
    children = _children(root)
    provenance = children.get(TEMPLATE_PROVENANCE_ELEMENT)
    if (
        provenance is None
        or len(provenance) != 1
        or provenance[0].casefold() != TEMPLATE_PROVENANCE_VALUE
    ):
        return False
    return TEMPLATE_MARKER_ELEMENTS <= set(children)


def is_injected_template(root: ElementTree.Element) -> bool:
    """True when ``root`` is the watcher's template, filled but unedited.

    See the comment above `TEMPLATE_ELEMENTS` for the fingerprint and why
    a filled template is still the template.
    """
    if not is_template_derived(root):
        return False
    children = _children(root)
    if not set(children) <= TEMPLATE_ELEMENTS | TEMPLATE_INJECTOR_WRITES:
        return False
    for name in TEMPLATE_EMPTY_ELEMENTS:
        values = children.get(name)
        if values is None or any(values):
            return False
    return True


def comicinfo_facts(documents: Sequence[str | bytes]) -> ComicInfoFacts:
    """Classify an archive's ComicInfo.xml member(s).

    ``documents`` is every member whose basename is ``ComicInfo.xml``
    (case-insensitive). The design requires "internally consistent" without
    defining it; the checks here are this module's reading:

    * more than one ComicInfo member with differing bytes;
    * a document that does not parse, or whose root is not ``ComicInfo``;
    * a read field present twice with different values;
    * a ``Year`` that is not a four-digit year in range, or a ``Month``
      outside 1-12;
    * a numeric ``Number`` greater than a numeric ``Count``.

    The template check runs on a consistent document, so the reported
    reason for a template is always "template", never a side effect. A
    pristine template contributes nothing; an edited one contributes only
    the fields the injectors never write (``uncertain_fields``).
    """
    if not documents:
        return ComicInfoFacts(status=COMICINFO_ABSENT)

    raw = [
        item if isinstance(item, bytes) else item.encode("utf-8")
        for item in documents
    ]
    if len(set(raw)) > 1:
        return ComicInfoFacts(
            status=COMICINFO_INCONSISTENT,
            reasons=("multiple ComicInfo.xml members with differing content",),
        )

    try:
        root = ElementTree.fromstring(raw[0])
    except ElementTree.ParseError as exc:
        return ComicInfoFacts(
            status=COMICINFO_INCONSISTENT,
            reasons=(f"ComicInfo.xml does not parse: {exc}",),
        )
    if _local_name(root.tag) != "ComicInfo":
        return ComicInfoFacts(
            status=COMICINFO_INCONSISTENT,
            reasons=(f"root element is {_local_name(root.tag)!r}",),
        )

    values: dict[str, set[str]] = {}
    for child in root:
        name = _local_name(child.tag)
        if name in _COMICINFO_FIELDS:
            values.setdefault(name, set()).add((child.text or "").strip())

    reasons: list[str] = []
    fields: dict[str, str] = {}
    for name, seen in sorted(values.items()):
        nonempty = {value for value in seen if value}
        if len(nonempty) > 1:
            reasons.append(
                f"{name} appears more than once with different values "
                f"{sorted(nonempty)}"
            )
        elif nonempty:
            fields[name] = next(iter(nonempty))

    if "Year" in fields and normalize_year(fields["Year"]) is None:
        reasons.append(f"Year {fields['Year']!r} is not a plausible year")
    if "Month" in fields and not (
        fields["Month"].isdigit() and 1 <= int(fields["Month"]) <= 12
    ):
        reasons.append(f"Month {fields['Month']!r} is not 1-12")
    number = normalize_number(fields.get("Number"))
    count = fields.get("Count", "")
    if number is not None and "-" not in number and count.isdigit():
        if int(count) > 0 and float(number) > int(count):
            reasons.append(f"Number {number} exceeds Count {count}")

    if reasons:
        return ComicInfoFacts(
            status=COMICINFO_INCONSISTENT,
            fields=fields,
            reasons=tuple(reasons),
        )
    if is_injected_template(root):
        return ComicInfoFacts(
            status=COMICINFO_TEMPLATE,
            fields=fields,
            reasons=(
                "watcher-injected ComicInfo template: its values restate "
                "the filename parse",
            ),
            uncertain_fields=frozenset(fields),
        )
    if is_template_derived(root):
        uncertain = frozenset(fields) & TEMPLATE_INJECTOR_WRITES
        return ComicInfoFacts(
            status=COMICINFO_EDITED_TEMPLATE,
            fields=fields,
            reasons=(
                "edited copy of the injected ComicInfo template: "
                f"{', '.join(sorted(uncertain)) or 'no read field'} still "
                "restate the filename parse",
            ),
            uncertain_fields=uncertain,
        )
    return ComicInfoFacts(status=COMICINFO_USABLE, fields=fields)


def read_comicinfo_documents(path: str | Path) -> tuple[bytes, ...]:
    """Every ComicInfo.xml member of a CBZ, read-only.

    Opened ``mode="r"``; the central directory goes through the shared
    archive validator first. A member larger than ``MAX_COMIC_INFO_BYTES``
    is returned as an empty document, which then fails to parse and makes
    the ComicInfo inconsistent rather than being read unbounded.
    """
    documents: list[bytes] = []
    with zipfile.ZipFile(path, mode="r") as archive:
        members = archive.infolist()
        validate_archive_members(members)
        for member in members:
            if member.is_dir():
                continue
            basename = member.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if basename.casefold() != COMIC_INFO_FILENAME:
                continue
            if member.file_size > MAX_COMIC_INFO_BYTES:
                documents.append(b"")
                continue
            documents.append(archive.read(member))
    return tuple(documents)


# --- filename tokens -------------------------------------------------------

_EXTENSION_RE = re.compile(r"\.(?:cbz|zip)$", re.IGNORECASE)
_VERSION_MARKER_RE = re.compile(r"\[\s*v(\d+)\s*\]", re.IGNORECASE)
_YEAR_TOKEN_RE = re.compile(r"\(\s*(\d{4})\s*\)")
_BRACKET_GROUP_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_CHAPTER_RE = re.compile(
    rf"(?<![A-Za-z])(?:chapter|chap|chp|ch)\.?\s*({_NUMBER_TEXT})",
    re.IGNORECASE,
)
_ISSUE_RE = re.compile(rf"#\s*({_NUMBER_TEXT})")
_VOLUME_WORD_RE = re.compile(
    rf"(?<![A-Za-z])vol(?:ume)?\.?\s*({_NUMBER_TEXT})", re.IGNORECASE
)
_V_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])v(\d+)(?![A-Za-z0-9.])", re.IGNORECASE)
def _word_alternation_re(words: Iterable[str]) -> re.Pattern[str]:
    """Whole-word, case-insensitive match of any of ``words``, longest first."""
    return re.compile(
        r"(?<![A-Za-z])("
        + "|".join(
            re.escape(word).replace(r"\ ", r"\s+")
            for word in sorted(words, key=len, reverse=True)
        )
        + r")(?![A-Za-z])",
        re.IGNORECASE,
    )


_EDITION_RE = _word_alternation_re(EDITION_VOCABULARY)
_EDITION_BY_FOLD = {word.casefold(): word for word in EDITION_VOCABULARY}
_SPECIAL_RE = _word_alternation_re(SPECIAL_VOCABULARY)
_SPECIAL_BY_FOLD = {word.casefold(): word for word in SPECIAL_VOCABULARY}
_TAG_YEAR_RE = re.compile(r"\d{4}")
_TAG_VERSION_RE = re.compile(r"v\d+", re.IGNORECASE)
_SERIES_TRIM = " \t-_.,~"


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _spelling(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class NameTokens:
    """What a current filename claims. Every value here is a candidate.

    ``v_tokens`` are bare ``v<N>`` tokens outside brackets, whose meaning
    (volume or version) is open. ``version_markers`` are bracketed ``[vN]``
    tokens, which are never volumes. ``volume_words`` are ``Vol.``/
    ``Volume`` tokens. ``chapters`` and ``issues`` are ``ch.``/``#``
    numbers. All numbers are in `normalize_number` form.

    Each number tuple has a parallel ``*_spellings`` tuple holding that
    occurrence's own text (``1.00``, ``012.50``). Spelling travels with the
    token, never with the value: a ``Vol. 1.00`` and a ``ch. 1.0`` in one
    name compare equal and keep their own spellings.

    ``editions`` are `EDITION_VOCABULARY` words and ``specials`` are
    `SPECIAL_VOCABULARY` words (the formatter's special/collection slot),
    both in canonical spelling. A special word is work identity, not series
    text: ``Series Omnibus #001`` proposes series ``Series`` with special
    ``Omnibus``, so a later ComicInfo ``Series`` cannot erase the marker.
    ``other_tags`` is everything else the name says that is not series,
    number, year, version, edition or special: bracket contents
    (``[Judas]``, ``(Digital)``) and leftover text after the numbering. It
    cannot tell a scanlator tag from an unsupported variant, so a version
    family whose members differ in it is refused rather than guessed (§4.2).
    """

    filename: str
    series: str | None
    chapters: tuple[str, ...]
    issues: tuple[str, ...]
    volume_words: tuple[str, ...]
    v_tokens: tuple[str, ...]
    version_markers: tuple[str, ...]
    years: tuple[str, ...]
    editions: frozenset[str]
    other_tags: frozenset[str] = frozenset()
    specials: frozenset[str] = frozenset()
    chapter_spellings: tuple[str, ...] = ()
    issue_spellings: tuple[str, ...] = ()
    volume_word_spellings: tuple[str, ...] = ()
    v_token_spellings: tuple[str, ...] = ()

    @property
    def primary_numbers(self) -> tuple[tuple[str, str], ...]:
        """(kind, number) for every chapter and issue token."""
        return tuple(("chapter", value) for value in self.chapters) + tuple(
            ("issue", value) for value in self.issues
        )

    @property
    def primary_spellings(self) -> tuple[str, ...]:
        """The spelling of each `primary_numbers` entry, in the same order."""
        return (
            (self.chapter_spellings or self.chapters)
            + (self.issue_spellings or self.issues)
        )

    @property
    def volume_tokens(self) -> tuple[str, ...]:
        return self.volume_words + self.v_tokens

    @property
    def volume_spellings(self) -> tuple[str, ...]:
        """The spelling of each `volume_tokens` entry, in the same order."""
        return (
            (self.volume_word_spellings or self.volume_words)
            + (self.v_token_spellings or self.v_tokens)
        )

    @property
    def version_marked(self) -> bool:
        """True when the name carries a possible version marker.

        A bare ``v<N>`` counts as well as ``[vN]``: §4.2 asks only that a
        member *carry* a marker, and whether a bare token is that marker is
        what the family decides. A bare token that is really a volume is
        caught later, when its classification sees volume evidence too.
        """
        return bool(self.v_tokens or self.version_markers)


def parse_name_tokens(filename: str) -> NameTokens:
    """Tokenize a current filename into candidate values.

    Deliberately independent of `scripts.cbz_core.parse_comic_name`. That
    parser treats every ``v<N>`` as a volume (`extract_volume_number`),
    which is the ambiguity §3.2 forbids, and it can call a translation
    service. Nothing here is decided by the name; it only proposes.
    """
    stem = _EXTENSION_RE.sub("", filename)
    version_markers = tuple(
        _normalize_one(match.group(1))
        for match in _VERSION_MARKER_RE.finditer(stem)
    )
    years = tuple(
        year
        for year in (
            normalize_year(match.group(1))
            for match in _YEAR_TOKEN_RE.finditer(stem)
        )
        if year is not None
    )
    editions = frozenset(
        _EDITION_BY_FOLD[_fold(match.group(1))]
        for match in _EDITION_RE.finditer(stem)
    )
    # Edition phrases are blanked first, so "Special Edition" is an edition
    # and never also the special word "Special".
    specials = frozenset(
        _SPECIAL_BY_FOLD[_fold(match.group(1))]
        for match in _SPECIAL_RE.finditer(_EDITION_RE.sub(" ", stem))
    )

    # Bracket contents that are not a year, a version marker, an edition or
    # a special word: scanlator tags, source tags, or a variant the
    # vocabulary does not know. Kept, so a family can refuse members that
    # differ in them.
    other_tags: set[str] = set()
    for match in _BRACKET_GROUP_RE.finditer(stem):
        content = _fold(match.group(0)[1:-1])
        if not content or _TAG_YEAR_RE.fullmatch(content):
            continue
        if _TAG_VERSION_RE.fullmatch(content) and match.group(0)[0] == "[":
            continue
        if content in _EDITION_BY_FOLD or content in _SPECIAL_BY_FOLD:
            continue
        other_tags.add(content)

    # Bracket groups are scanlator tags, years, editions and version
    # markers, all captured above. Numbers inside them are not this book's
    # numbering, so they are blanked before the number tokens are read.
    body = _BRACKET_GROUP_RE.sub(" ", stem)

    spans: list[tuple[int, int]] = []

    def numbers(regex: re.Pattern[str]) -> tuple[tuple[int, str, str], ...]:
        found = []
        for match in regex.finditer(body):
            spans.append(match.span())
            found.append(
                (
                    match.start(),
                    normalize_number(match.group(1)) or "",
                    _spelling(match.group(1)),
                )
            )
        return tuple(found)

    chapters = numbers(_CHAPTER_RE)
    issues = numbers(_ISSUE_RE)
    volume_words = numbers(_VOLUME_WORD_RE)
    v_tokens = numbers(_V_TOKEN_RE)
    starts = [
        start
        for start, _, _ in chapters + issues + volume_words + v_tokens
    ]
    prefix = body[: min(starts)] if starts else body
    # The special slot follows the series in the canonical grammar; it is
    # identity evidence of its own (`specials`), not part of the series.
    prefix = _SPECIAL_RE.sub(" ", _EDITION_RE.sub(" ", prefix))
    series = " ".join(prefix.split()).strip(_SERIES_TRIM) or None

    # Text after the numbering that is neither a number token nor an
    # edition or special word ("ch. 1 Colourised") could be a variant too.
    if starts:
        rest = list(body)
        for start, end in spans:
            rest[start:end] = " " * (end - start)
        leftover = _EDITION_RE.sub(" ", "".join(rest)[min(starts):])
        leftover = _SPECIAL_RE.sub(" ", leftover)
        leftover = " ".join(leftover.split()).strip(_SERIES_TRIM)
        if leftover:
            other_tags.add(_fold(leftover))

    return NameTokens(
        filename=filename,
        series=series,
        chapters=tuple(value for _, value, _ in chapters),
        issues=tuple(value for _, value, _ in issues),
        volume_words=tuple(value for _, value, _ in volume_words),
        v_tokens=tuple(value for _, value, _ in v_tokens),
        version_markers=version_markers,
        years=years,
        editions=editions,
        other_tags=frozenset(other_tags),
        specials=specials,
        chapter_spellings=tuple(raw for _, _, raw in chapters),
        issue_spellings=tuple(raw for _, _, raw in issues),
        volume_word_spellings=tuple(raw for _, _, raw in volume_words),
        v_token_spellings=tuple(raw for _, _, raw in v_tokens),
    )


# --- page evidence ---------------------------------------------------------


@dataclass(frozen=True)
class PageEvidence:
    """An archive's ordered exact page digests, with their provenance.

    ``algorithm``/``algorithm_version`` are the content-signature row's
    (``archive_content_signatures``). Only the current ordering contract is
    comparable: a ``"1"`` row ordered ties by physical member order, so its
    page sequence proves nothing (`page_hashing.CONTENT_SIGNATURE_VERSION`).

    Every page keeps its slot in ``page_digests``: a page with no digest is
    an empty string there, never dropped, so an incomplete inventory cannot
    pass as a shorter complete one. ``inventory_problem`` is set by the
    stored-row loader when the rows disagree with their signature (row
    count, or page indexes not contiguous from 0).

    This is nomination-grade evidence (§4.3): stored rows are not bound to
    the current bytes. Step 5 re-derives from the file before anything is
    compared for supersession.
    """

    algorithm: str
    algorithm_version: str
    order_resolved: bool
    page_digests: tuple[str, ...]
    inventory_problem: str | None = None

    @property
    def comparable(self) -> bool:
        return self.incomparable_reason is None

    @property
    def incomparable_reason(self) -> str | None:
        if self.algorithm != CONTENT_SIGNATURE_ALGORITHM:
            return f"unknown signature algorithm {self.algorithm!r}"
        if self.algorithm_version != CONTENT_SIGNATURE_VERSION:
            return (
                f"legacy ordering contract {self.algorithm_version!r} "
                f"(current {CONTENT_SIGNATURE_VERSION!r})"
            )
        if self.inventory_problem is not None:
            return f"incomplete inventory: {self.inventory_problem}"
        if not self.order_resolved:
            return "page order unresolved (duplicate member names)"
        if not self.page_digests:
            return "no image pages"
        if any(not digest for digest in self.page_digests):
            return "incomplete inventory: a page has an empty or missing digest"
        return None

    @classmethod
    def from_page_hashes(cls, result: ArchivePageHashes) -> "PageEvidence":
        """Evidence from a fresh `calculate_page_hashes` result."""
        return cls(
            algorithm=CONTENT_SIGNATURE_ALGORITHM,
            algorithm_version=CONTENT_SIGNATURE_VERSION,
            order_resolved=result.order_resolved,
            page_digests=tuple(page.digest for page in result.pages),
        )


def _read_page_evidence(
    connection: sqlite3.Connection, archive_ids: Sequence[int]
) -> dict[int, PageEvidence]:
    evidence: dict[int, PageEvidence] = {}
    for archive_id in archive_ids:
        signature = connection.execute(
            "SELECT algorithm, algorithm_version, page_count FROM "
            "archive_content_signatures WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()
        if signature is None:
            continue
        rows = connection.execute(
            """
            SELECT ap.page_index, ap.entry_name, ph.digest
            FROM archive_pages AS ap
            LEFT JOIN page_hashes AS ph
              ON ph.page_id = ap.id
             AND ph.algorithm = ?
             AND ph.algorithm_version = ?
            WHERE ap.archive_id = ?
            ORDER BY ap.page_index
            """,
            (PAGE_HASH_ALGORITHM, PAGE_HASH_ALGORITHM_VERSION, archive_id),
        ).fetchall()
        names = Counter(row[1] for row in rows)
        indexes = [int(row[0]) for row in rows]
        # The signature records how many pages it was computed over. A
        # missing page row would otherwise leave a shorter sequence that
        # looks complete, and a gap in page_index means the same thing.
        if len(rows) != int(signature[2]):
            problem: str | None = (
                f"{len(rows)} page rows but the signature records "
                f"{int(signature[2])} pages"
            )
        elif indexes != list(range(len(rows))):
            problem = "page indexes are not contiguous from 0"
        else:
            problem = None
        evidence[archive_id] = PageEvidence(
            algorithm=signature[0],
            algorithm_version=signature[1],
            # Raw-name duplicates are the stored form of an unresolved
            # order (page_hashing._STORED_DUPLICATE_ENTRY_NAMES_SQL).
            order_resolved=all(count == 1 for count in names.values()),
            # A missing (NULL) or empty digest keeps its slot as "" and
            # makes the evidence incomparable; it is never filtered out.
            page_digests=tuple(row[2] or "" for row in rows),
            inventory_problem=problem,
        )
    return evidence


def load_page_evidence(
    database: str | Path,
    archive_ids: Sequence[int],
    *,
    integrity_check: Callable[[sqlite3.Connection], str] | None = None,
) -> ConsistentSnapshot[dict[int, PageEvidence]]:
    """Read stored page evidence for ``archive_ids`` in one guarded snapshot.

    Goes through `read_consistent_snapshot`: a ``mode=ro``/``query_only``
    connection from the DAL, ``data_version`` sampled either side of one
    deferred read transaction, and `DatabaseChangedError` if another
    connection committed in between (§4.3, "Database snapshot"). Archives
    with no signature row are absent from the result. The returned
    snapshot carries the ``data_version`` pair for the plan.
    """
    kwargs = {} if integrity_check is None else {"integrity_check": integrity_check}
    return read_consistent_snapshot(
        database,
        lambda connection: _read_page_evidence(connection, archive_ids),
        context="field-resolution page evidence read",
        **kwargs,
    )


# --- archives, folders and families ----------------------------------------


@dataclass(frozen=True)
class ArchiveFacts:
    """Everything the resolver knows about one archive, gathered read-only.

    ``archive_key`` identifies the archive to the decision source and in
    the output: a path, or ``archive:<id>``. ``pages`` is None when no page
    evidence is available, which is treated as non-comparable.
    """

    archive_key: str
    filename: str
    comicinfo: ComicInfoFacts = field(
        default_factory=lambda: ComicInfoFacts(status=COMICINFO_ABSENT)
    )
    pages: PageEvidence | None = None


@dataclass(frozen=True)
class FolderFacts:
    """One folder: its resolved series folder name and every archive in it.

    ``series_folder`` is the folder's name only when the caller has
    resolved it as a series folder (the series index's decision, not this
    module's). None means the folder contributes no series evidence.
    ``archives`` are all the archives in the folder; siblings are judged
    among them.
    """

    series_folder: str | None
    archives: tuple[ArchiveFacts, ...]


@dataclass(frozen=True)
class FamilyMember:
    """One archive as a candidate member of a version family.

    ``volume`` is the volume as known *without* the name's ``v<N>`` tokens
    (human decision or ComicInfo only). Whether a token is a version is what
    the family decides, so a volume derived from that token cannot be
    evidence for or against the family.

    ``language`` is the authoritative ComicInfo ``LanguageISO`` (casefolded)
    or None. ``other_tags`` are `NameTokens.other_tags`. ``human_conflicts``
    names every decision field (`FIELDS` and `DECISION_FAMILY`) on which this
    archive's human decisions disagree; any of them refuses the family.
    ``specials`` are `NameTokens.specials`, the special/collection slot.
    """

    archive_key: str
    series: FieldResolution
    number: FieldResolution
    editions: frozenset[str]
    version_marked: bool
    pages: PageEvidence | None
    family_decision: str | None = None
    volume: FieldResolution | None = None
    language: str | None = None
    other_tags: frozenset[str] = frozenset()
    human_conflicts: frozenset[str] = frozenset()
    specials: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FamilyEvidence:
    """The §4.2 verdict for a set of candidate members.

    ``status`` is ``established``, ``refused`` (the metadata does not make
    them one family) or ``incomparable`` (it would, but a member's page
    inventory is not comparable, and §4.5 says such an archive is never a
    version). Only ``established`` classifies anything. ``reasons`` lists
    every failed requirement, not just the first.
    """

    status: str
    members: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence: tuple[str, ...] = ()

    @property
    def established(self) -> bool:
        return self.status == FAMILY_ESTABLISHED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "members": list(self.members),
            "reasons": list(self.reasons),
            "evidence": list(self.evidence),
        }


def establish_family(members: Sequence[FamilyMember]) -> FamilyEvidence:
    """Decide whether ``members`` form one version family (§4.2).

    Page overlap is not consulted. Pages are read only for the
    comparability gate, which can stop a family but never start one.

    Requirements, all of which must hold:

    * at least two members -- a family is a relation, and a lone archive
      has nothing to be a version *of*;
    * same work: either every member carries the same human family
      decision, or every member has a resolved series (equal under
      a non-empty `series_key`) and a resolved primary number (equal), and no two
      members have different known volumes;
    * same edition set, absent counting as a value; a difference only in
      the decensor markers is a decensor pair, never a version (§4.5);
    * same language, absent counting as a value;
    * the same special/collection marker (absent counting as a value), and
      no member with more than one -- an Omnibus #1 and an ordinary #1
      share series and number but are different works;
    * the same unrecognised tags: a tag the vocabulary does not know may be
      an unsupported variant, so a difference refuses rather than being
      read as absence;
    * no member with conflicting human decisions -- a conflict persists and
      refuses, it never falls back to automatic grouping;
    * at least one version-marked member, or the human family decision.
    """
    keys = tuple(member.archive_key for member in members)
    conflicts = [
        f"{member.archive_key} has conflicting human decisions on "
        f"{', '.join(sorted(member.human_conflicts))}"
        for member in members
        if member.human_conflicts
    ]
    if len(members) < 2:
        return FamilyEvidence(
            status=FAMILY_REFUSED,
            members=keys,
            reasons=("a version family needs at least two members", *conflicts),
        )

    reasons: list[str] = list(conflicts)
    evidence: list[str] = []

    decisions = {member.family_decision for member in members}
    human_family = len(decisions) == 1 and None not in decisions
    if human_family:
        evidence.append(f"human decision declares family {decisions.pop()!r}")
    else:
        if not all(member.series.resolved for member in members):
            reasons.append("a member's series is unresolved")
        elif len(
            series_keys := {series_key(m.series.value or "") for m in members}
        ) != 1:
            reasons.append("members have different series")
        elif series_keys == {""}:
            # A title made only of words series_key strips ("Uncensored")
            # keys to "", which SeriesIndex excludes as no identity at all.
            # Equal empty keys say nothing about being the same series.
            reasons.append("members' series key is empty (no identity)")
        else:
            evidence.append("same decided series")
        if not all(member.number.resolved for member in members):
            reasons.append("a member's primary number is unresolved")
        elif len({member.number.value for member in members}) != 1:
            reasons.append("members have different numbers")
        else:
            evidence.append(f"same number {members[0].number.value}")
        volumes = {
            member.volume.value
            for member in members
            if member.volume is not None and member.volume.resolved
        }
        if len(volumes) > 1:
            reasons.append(f"members have different volumes {sorted(volumes)}")

    edition_sets = {member.editions for member in members}
    if len(edition_sets) > 1:
        differing = frozenset.union(*edition_sets) - frozenset.intersection(
            *edition_sets
        )
        if differing <= DECENSOR_EDITIONS:
            reasons.append(
                "censored and uncensored members are a decensor pair, "
                "never versions"
            )
        else:
            reasons.append(
                f"members have different editions or languages "
                f"{sorted(sorted(s) for s in edition_sets)}"
            )
    else:
        evidence.append(f"same editions {sorted(next(iter(edition_sets)))}")

    languages = {member.language for member in members}
    if len(languages) > 1:
        reasons.append(
            "members have different languages "
            f"{sorted(language or 'absent' for language in languages)}"
        )
    special_sets = {member.specials for member in members}
    if any(len(specials) > 1 for specials in special_sets):
        reasons.append(
            "a member carries more than one special/collection marker "
            f"{sorted(sorted(s) for s in special_sets)} (unresolved)"
        )
    elif len(special_sets) > 1:
        reasons.append(
            "members have different special/collection markers "
            f"{sorted(sorted(s) or ['absent'] for s in special_sets)}"
        )
    tag_sets = {member.other_tags for member in members}
    if len(tag_sets) > 1:
        reasons.append(
            "members carry different unrecognised tags "
            f"{sorted(sorted(tags) for tags in tag_sets)} "
            "(possibly an unsupported variant)"
        )

    if not human_family:
        if any(member.version_marked for member in members):
            evidence.append("at least one member carries a version marker")
        else:
            reasons.append("no member carries a version marker")

    if reasons:
        return FamilyEvidence(
            status=FAMILY_REFUSED,
            members=keys,
            reasons=tuple(reasons),
            evidence=tuple(evidence),
        )

    incomparable = [
        f"{member.archive_key}: "
        + (
            "no page evidence"
            if member.pages is None
            else str(member.pages.incomparable_reason)
        )
        for member in members
        if member.pages is None or not member.pages.comparable
    ]
    if incomparable:
        return FamilyEvidence(
            status=FAMILY_INCOMPARABLE,
            members=keys,
            reasons=tuple(incomparable),
            evidence=tuple(evidence),
        )
    return FamilyEvidence(
        status=FAMILY_ESTABLISHED,
        members=keys,
        reasons=(),
        evidence=tuple(evidence),
    )


# --- sibling runs ----------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    """A folder-level sibling run: per-archive values, or why there is none."""

    values: Mapping[str, str]
    reason: str


def _number_run(
    archives: Sequence[ArchiveFacts], tokens: Mapping[str, NameTokens]
) -> _Run:
    """Folder evidence for the primary number, from consistently numbered siblings.

    "Siblings that are numbered consistently" (§3.1) is not defined by the
    design. Read here as: at least two archives; each with exactly one
    chapter-or-issue token, all of the same kind; all with the same series
    prefix (`series_key`); and no two archives with the same numbering
    (number, volume tokens, version markers, editions), which would mean
    duplicates or a mixed folder. A folder that fails any of these
    contributes no number evidence at all.
    """
    if len(archives) < 2:
        return _Run({}, "fewer than two archives in the folder")
    kinds: set[str] = set()
    prefixes: set[str] = set()
    identities: Counter = Counter()
    values: dict[str, str] = {}
    for archive in archives:
        token = tokens[archive.archive_key]
        primary = token.primary_numbers
        if len(primary) != 1 or not primary[0][1]:
            return _Run({}, f"{archive.filename}: not exactly one chapter/issue number")
        kind, number = primary[0]
        kinds.add(kind)
        prefixes.add(series_key(token.series or ""))
        identities[
            (number, token.volume_tokens, token.version_markers, token.editions)
        ] += 1
        values[archive.archive_key] = number
    if len(kinds) != 1:
        return _Run({}, "siblings mix chapter and issue numbering")
    if len(prefixes) != 1:
        return _Run({}, "siblings have different series prefixes")
    if any(count > 1 for count in identities.values()):
        return _Run({}, "two siblings carry the same numbering")
    return _Run(values, "consistent")


def _pages_disjoint(first: PageEvidence, second: PageEvidence) -> bool:
    return not set(first.page_digests) & set(second.page_digests)


def _volume_run(
    archives: Sequence[ArchiveFacts], tokens: Mapping[str, NameTokens]
) -> _Run:
    """Folder evidence for volume: a volume run whose pages are different books.

    §3.2: a ``v<N>`` is a volume when "siblings form a volume run, and their
    page content shows different books". Read here as: at least two
    archives; each with exactly one volume token (bare ``v<N>`` or
    ``Vol.``); the same series prefix; at least two distinct volumes; no two
    archives with the same (volume, chapter/issue) numbering; every archive
    with a comparable page inventory; and the archives of different
    volumes sharing **no** page digest at all.

    "Different books" as page-disjoint is the conservative reading. Two
    versions of one book share pages (a later WIP version usually extends
    an earlier one), so they never pass; genuinely different volumes that
    happen to share a page (a reused credits page) also fail, and go to
    review rather than being guessed. Legacy or unresolved page orders are
    not comparable, so they can never show different books either.
    """
    if len(archives) < 2:
        return _Run({}, "fewer than two archives in the folder")
    prefixes: set[str] = set()
    identities: Counter = Counter()
    values: dict[str, str] = {}
    for archive in archives:
        token = tokens[archive.archive_key]
        if len(token.volume_tokens) != 1:
            return _Run({}, f"{archive.filename}: not exactly one volume token")
        volume = token.volume_tokens[0]
        prefixes.add(series_key(token.series or ""))
        identities[(volume, token.primary_numbers)] += 1
        values[archive.archive_key] = volume
        if archive.pages is None or not archive.pages.comparable:
            reason = (
                "no page evidence"
                if archive.pages is None
                else archive.pages.incomparable_reason
            )
            return _Run({}, f"{archive.filename}: pages not comparable ({reason})")
    if len(prefixes) != 1:
        return _Run({}, "siblings have different series prefixes")
    if len(set(values.values())) < 2:
        return _Run({}, "fewer than two distinct volumes")
    if any(count > 1 for count in identities.values()):
        return _Run({}, "two siblings carry the same numbering")
    for index, first in enumerate(archives):
        for second in archives[index + 1:]:
            if values[first.archive_key] == values[second.archive_key]:
                continue
            assert first.pages is not None and second.pages is not None
            if not _pages_disjoint(first.pages, second.pages):
                return _Run(
                    {},
                    f"{first.filename} and {second.filename} share pages; "
                    "page content does not show different books",
                )
    return _Run(values, "consistent, page content shows different books")


# --- the precedence ladder -------------------------------------------------


def _resolve(
    field_name: str,
    evidence: Sequence[Evidence],
    *,
    same: Callable[[str], str] = lambda value: value,
) -> FieldResolution:
    """Apply §3.1 to one field's evidence.

    Human decisions decide alone (and conflict only among themselves).
    Otherwise every ComicInfo, folder and family statement must agree; the
    highest present is the reported source. Filename evidence is carried
    along but never decides.
    """
    ordered = tuple(
        sorted(evidence, key=lambda item: PRECEDENCE.index(item.source))
    )
    human = [item for item in ordered if item.source == SOURCE_HUMAN]
    if human:
        if len({same(item.value) for item in human}) == 1:
            return FieldResolution(
                field_name,
                human[0].value,
                SOURCE_HUMAN,
                ordered,
                "resolved",
                human[0].display_value,
            )
        return FieldResolution(field_name, None, None, ordered, "human_conflict")

    deciding = [
        item
        for item in ordered
        if item.source in (SOURCE_COMICINFO, SOURCE_FOLDER, SOURCE_FAMILY)
    ]
    if deciding:
        if len({same(item.value) for item in deciding}) == 1:
            return FieldResolution(
                field_name,
                deciding[0].value,
                deciding[0].source,
                ordered,
                "resolved",
                deciding[0].display_value,
            )
        return FieldResolution(field_name, None, None, ordered, "conflict")

    if ordered:
        return FieldResolution(
            field_name, None, None, ordered, "filename_candidate_only"
        )
    return FieldResolution(field_name, None, None, ordered, "no_evidence")


def _human_evidence(
    decisions: Sequence[HumanDecision], field_name: str
) -> list[Evidence]:
    found = []
    for decision in decisions:
        if decision.field != field_name:
            continue
        value = decision.value
        if field_name in (FIELD_NUMBER, FIELD_VOLUME, FIELD_VERSION):
            value = normalize_number(value) or value
        found.append(
            Evidence(
                SOURCE_HUMAN,
                value,
                f"decision {decision.record_id}",
                display=decision.value,
            )
        )
    return found


def _human_conflicts(decisions: Sequence[HumanDecision]) -> frozenset[str]:
    """Every decision field on which ``decisions`` disagree.

    Compared in each field's comparison form (`series_key` for series,
    `normalize_number` for numbers), so two receipts spelling one decision
    differently agree. A conflict is a state that persists: it blocks
    readiness, refuses the archive's family, and stops human evidence from
    classifying a ``v<N>`` token. It never decays into "no decision".
    """
    conflicted = set()
    for name in (*FIELDS, DECISION_FAMILY):
        values = {
            series_key(item.value) if name == FIELD_SERIES else item.value
            for item in _human_evidence(decisions, name)
        }
        if len(values) > 1:
            conflicted.add(name)
    return frozenset(conflicted)


# --- per-archive output ----------------------------------------------------


@dataclass(frozen=True)
class VTokenClassification:
    """How one ``v<N>`` token in the current name was classified (§3.2)."""

    value: str
    bracketed: bool
    classification: str
    evidence: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": f"[v{self.value}]" if self.bracketed else f"v{self.value}",
            "classification": self.classification,
            "evidence": list(self.evidence),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ArchiveResolution:
    """Every field of one archive, the ``v<N>`` verdicts, and renamability.

    ``renamable`` is False when the series or the primary number is
    unresolved, a ``v<N>`` token is unresolved, or any human decisions
    conflict; ``review`` then names
    each blocking reason, and the unresolved fields carry their candidates.
    ``notes`` are non-blocking diagnostics, such as why a ComicInfo was
    ignored.
    """

    archive_key: str
    filename: str
    fields: Mapping[str, FieldResolution]
    v_tokens: tuple[VTokenClassification, ...]
    family: FamilyEvidence | None
    comicinfo: ComicInfoFacts
    renamable: bool
    review: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "archive": self.archive_key,
            "filename": self.filename,
            "renamable": self.renamable,
            "review": list(self.review),
            "notes": list(self.notes),
            "comicinfo": {
                "status": self.comicinfo.status,
                "reasons": list(self.comicinfo.reasons),
            },
            "fields": {
                name: resolution.as_dict()
                for name, resolution in self.fields.items()
            },
            "v_tokens": [token.as_dict() for token in self.v_tokens],
            "family": None if self.family is None else self.family.as_dict(),
        }


@dataclass(frozen=True)
class FolderResolution:
    """The resolver's output for one folder."""

    archives: tuple[ArchiveResolution, ...]
    number_run: str
    volume_run: str
    families: tuple[FamilyEvidence, ...]

    def by_key(self) -> dict[str, ArchiveResolution]:
        return {archive.archive_key: archive for archive in self.archives}

    def as_dict(self) -> dict[str, Any]:
        return {
            "number_run": self.number_run,
            "volume_run": self.volume_run,
            "families": [family.as_dict() for family in self.families],
            "archives": [archive.as_dict() for archive in self.archives],
        }


def _comicinfo_evidence(
    comicinfo: ComicInfoFacts, name: str, normalize: Callable[[str], str | None]
) -> list[Evidence]:
    if name not in comicinfo.fields:
        return []
    raw = comicinfo.fields[name]
    value = normalize(raw)
    if not comicinfo.authoritative(name):
        if comicinfo.status in (COMICINFO_TEMPLATE, COMICINFO_EDITED_TEMPLATE):
            # Injector-written: it restates the filename parse, so it is a
            # filename-grade candidate, shown in review, never deciding.
            return [
                Evidence(
                    SOURCE_FILENAME,
                    value or raw,
                    f"ComicInfo <{name}>{raw} from the injected template "
                    "(filename-derived)",
                    display=raw,
                )
            ]
        return []
    # A present but unparseable value (Number "Special") is still evidence:
    # dropping it would let a lower source decide a field ComicInfo has
    # spoken to. Kept raw, it can only agree with an identical raw value.
    return [
        Evidence(
            SOURCE_COMICINFO, value or raw, f"ComicInfo <{name}>{raw}", display=raw
        )
    ]


def _classify_v_token(
    value: str,
    *,
    bracketed: bool,
    human_volumes: set[str],
    human_versions: set[str],
    comicinfo_volume: str | None,
    run_volume: str | None,
    in_family: bool,
    human_conflict: bool = False,
) -> VTokenClassification:
    """Classify one ``v<N>`` token under §3.2.

    A human decision naming the token's value as the volume or the version
    decides alone; one naming it both ways is a conflict. Otherwise volume
    evidence (ComicInfo ``Volume`` agrees, or a volume run of different
    books) and version evidence (an established family) are gathered, and
    exactly one kind must be present. The token's mere presence in the name
    contributes nothing to either side.

    A bracketed ``[vN]`` collects no volume evidence from any source, a
    human volume decision included: it is the canonical *version* token
    (§2.1), and a volume that happens to equal N is a coincidence of
    numbers, not a statement about the token.

    ``human_conflict`` is True when this archive's human volume or version
    decisions disagree. The token is then unresolved outright: matching it
    against one member of a conflicting set would pick a side.
    """
    if human_conflict:
        return VTokenClassification(
            value=value,
            bracketed=bracketed,
            classification=V_UNRESOLVED,
            evidence=(),
            reason="conflicting human volume/version decisions",
        )
    volume_why: list[str] = []
    version_why: list[str] = []
    if not bracketed and value in human_volumes:
        volume_why.append("human decision: volume")
    if value in human_versions:
        version_why.append("human decision: version")

    if not (volume_why or version_why):
        if not bracketed:
            if comicinfo_volume == value:
                volume_why.append("ComicInfo Volume agrees")
            if run_volume == value:
                volume_why.append("siblings form a volume run of different books")
        if in_family:
            version_why.append("member of an established version family")

    if volume_why and version_why:
        kind, reason = V_UNRESOLVED, "evidence for both volume and version"
    elif volume_why:
        kind, reason = V_VOLUME, "volume"
    elif version_why:
        kind, reason = V_VERSION, "version"
    elif bracketed:
        kind, reason = V_UNRESOLVED, "a [vN] marker is a version only through a family"
    else:
        kind, reason = V_UNRESOLVED, "name evidence alone never classifies"
    return VTokenClassification(
        value=value,
        bracketed=bracketed,
        classification=kind,
        evidence=tuple(volume_why + version_why),
        reason=reason,
    )


def resolve_folder(
    folder: FolderFacts,
    decisions: HumanDecisionSource = NO_HUMAN_DECISIONS,
) -> FolderResolution:
    """Resolve every field of every archive in ``folder``. Pure.

    Order matters, because the volume/version split depends on families
    and families depend on series and number:

    1. tokens, ComicInfo and human decisions per archive;
    2. series, number and year (no dependency on ``v<N>``);
    3. folder sibling runs (number, volume);
    4. version families, from resolved series and number;
    5. ``v<N>`` classification, then volume and version.
    """
    archives = folder.archives
    tokens = {a.archive_key: parse_name_tokens(a.filename) for a in archives}
    human = {
        a.archive_key: tuple(decisions.decisions_for(a.archive_key))
        for a in archives
    }
    number_run = _number_run(archives, tokens)
    volume_run = _volume_run(archives, tokens)

    series: dict[str, FieldResolution] = {}
    number: dict[str, FieldResolution] = {}
    year: dict[str, FieldResolution] = {}
    for archive in archives:
        key = archive.archive_key
        token = tokens[key]

        evidence = _human_evidence(human[key], FIELD_SERIES)
        evidence += _comicinfo_evidence(
            archive.comicinfo, "Series", lambda raw: raw or None
        )
        if folder.series_folder:
            evidence.append(
                Evidence(
                    SOURCE_FOLDER,
                    folder.series_folder,
                    "resolved series folder",
                )
            )
        if token.series:
            evidence.append(
                Evidence(SOURCE_FILENAME, token.series, "filename prefix")
            )
        series[key] = _resolve(FIELD_SERIES, evidence, same=series_key)

        evidence = _human_evidence(human[key], FIELD_NUMBER)
        evidence += _comicinfo_evidence(
            archive.comicinfo, "Number", normalize_number
        )
        if key in number_run.values:
            evidence.append(
                Evidence(
                    SOURCE_FOLDER,
                    number_run.values[key],
                    "consistently numbered siblings",
                    # The run requires exactly one primary token, so this
                    # is that token's own spelling.
                    display=token.primary_spellings[0],
                )
            )
        evidence += [
            Evidence(
                SOURCE_FILENAME,
                value,
                f"filename {kind}",
                display=spelling,
            )
            for (kind, value), spelling in zip(
                token.primary_numbers, token.primary_spellings
            )
        ]
        number[key] = _resolve(FIELD_NUMBER, evidence)

        evidence = _human_evidence(human[key], FIELD_YEAR)
        evidence += _comicinfo_evidence(archive.comicinfo, "Year", normalize_year)
        evidence += [
            Evidence(SOURCE_FILENAME, value, "filename (YYYY)")
            for value in token.years
        ]
        year[key] = _resolve(FIELD_YEAR, evidence)

    # Version-family candidates: same decided series and same number. They
    # are deliberately *not* partitioned by edition, language or tag: a
    # member differing in those is then a co-member that refuses the family
    # in `establish_family`, with the difference named for review, instead
    # of silently dropping out. A single agreed human family decision groups
    # by its family id. Conflicting family decisions are not "no decision":
    # the archive joins every family it was declared into *and* its work
    # group, and its conflict refuses each of them. An archive with some
    # other conflict and no group still gets a (refused) verdict of its own.
    groups: dict[tuple, list[ArchiveFacts]] = {}
    family_decisions: dict[str, str | None] = {}
    conflicts = {
        a.archive_key: _human_conflicts(human[a.archive_key]) for a in archives
    }
    for archive in archives:
        key = archive.archive_key
        declared = {d.value for d in human[key] if d.field == DECISION_FAMILY}
        family_decisions[key] = (
            next(iter(declared)) if len(declared) == 1 else None
        )
        group_keys: list[tuple] = []
        if family_decisions[key] is not None:
            group_keys.append(("human", family_decisions[key]))
        else:
            group_keys += [("human", value) for value in sorted(declared)]
            if series[key].resolved and number[key].resolved:
                group_keys.append(
                    (
                        "work",
                        series_key(series[key].value or ""),
                        number[key].value,
                    )
                )
        if not group_keys and conflicts[key]:
            group_keys.append(("conflicted", key))
        for group_key in group_keys:
            groups.setdefault(group_key, []).append(archive)

    # The volume known without the v<N> tokens, for the family's
    # different-volumes refusal (see FamilyMember).
    work_volume = {
        archive.archive_key: _resolve(
            FIELD_VOLUME,
            _human_evidence(human[archive.archive_key], FIELD_VOLUME)
            + _comicinfo_evidence(archive.comicinfo, "Volume", normalize_number),
        )
        for archive in archives
    }

    families: list[FamilyEvidence] = []
    family_of: dict[str, FamilyEvidence] = {}
    for group_key in sorted(groups, key=repr):
        members = groups[group_key]
        verdict = establish_family(
            [
                FamilyMember(
                    archive_key=m.archive_key,
                    series=series[m.archive_key],
                    number=number[m.archive_key],
                    editions=tokens[m.archive_key].editions,
                    version_marked=tokens[m.archive_key].version_marked,
                    pages=m.pages,
                    family_decision=family_decisions[m.archive_key],
                    volume=work_volume[m.archive_key],
                    language=(
                        m.comicinfo.fields["LanguageISO"].casefold()
                        if m.comicinfo.authoritative("LanguageISO")
                        else None
                    ),
                    other_tags=tokens[m.archive_key].other_tags,
                    human_conflicts=conflicts[m.archive_key],
                    specials=tokens[m.archive_key].specials,
                )
                for m in members
            ]
        )
        families.append(verdict)
        for member in members:
            # Only an archive with conflicting family decisions sits in
            # several groups, and its conflict refuses every one of them, so
            # whichever verdict lands last is a refusal.
            family_of[member.archive_key] = verdict

    results: list[ArchiveResolution] = []
    for archive in archives:
        key = archive.archive_key
        token = tokens[key]
        family = family_of.get(key)
        in_family = family is not None and family.established
        comicinfo_volume = (
            normalize_number(archive.comicinfo.fields.get("Volume"))
            if archive.comicinfo.authoritative("Volume")
            else None
        )
        token_conflict = bool(conflicts[key] & {FIELD_VOLUME, FIELD_VERSION})
        human_volumes = {e.value for e in _human_evidence(human[key], FIELD_VOLUME)}
        human_versions = {
            e.value for e in _human_evidence(human[key], FIELD_VERSION)
        }

        candidates = [(value, False) for value in token.v_tokens] + [
            (value, True) for value in token.version_markers
        ]
        classifications = [
            _classify_v_token(
                value,
                bracketed=bracketed,
                human_volumes=human_volumes,
                human_versions=human_versions,
                comicinfo_volume=comicinfo_volume,
                run_volume=volume_run.values.get(key),
                in_family=in_family,
                human_conflict=token_conflict,
            )
            for value, bracketed in candidates
        ]

        evidence = _human_evidence(human[key], FIELD_VOLUME)
        evidence += _comicinfo_evidence(
            archive.comicinfo, "Volume", normalize_number
        )
        if key in volume_run.values:
            evidence.append(
                Evidence(
                    SOURCE_FOLDER,
                    volume_run.values[key],
                    "volume run of different books",
                    # Exactly one volume token, so this is its spelling.
                    display=token.volume_spellings[0],
                )
            )
        evidence += [
            Evidence(SOURCE_FILENAME, value, "filename Vol.", display=spelling)
            for value, spelling in zip(
                token.volume_words, token.volume_word_spellings
            )
        ] + [
            Evidence(
                SOURCE_FILENAME,
                value,
                "filename v<N> (volume or version)",
                display=spelling,
            )
            for value, spelling in zip(token.v_tokens, token.v_token_spellings)
        ]
        volume = _resolve(FIELD_VOLUME, evidence)

        evidence = _human_evidence(human[key], FIELD_VERSION)
        evidence += [
            Evidence(SOURCE_FAMILY, item.value, "classified version via family")
            for item in classifications
            if item.classification == V_VERSION
            and not any(why.startswith("human") for why in item.evidence)
        ]
        evidence += [
            Evidence(
                SOURCE_FILENAME,
                value,
                "filename [vN] marker" if bracketed else "filename v<N>",
            )
            for value, bracketed in candidates
        ]
        version = _resolve(FIELD_VERSION, evidence)

        fields = {
            FIELD_SERIES: series[key],
            FIELD_VOLUME: volume,
            FIELD_NUMBER: number[key],
            FIELD_YEAR: year[key],
            FIELD_VERSION: version,
        }

        review: list[str] = []
        if not series[key].resolved:
            review.append(f"series unresolved ({series[key].reason})")
        if number[key].has_evidence:
            if not number[key].resolved:
                review.append(f"primary number unresolved ({number[key].reason})")
        elif volume.has_evidence:
            if not volume.resolved:
                review.append(
                    f"primary number (volume) unresolved ({volume.reason})"
                )
        else:
            review.append("no primary number from any source")
        for name in sorted(conflicts[key]):
            review.append(f"conflicting human decisions on {name}")
        for item in classifications:
            if item.classification == V_UNRESOLVED:
                review.append(
                    f"{'[v' if item.bracketed else 'v'}{item.value}"
                    f"{']' if item.bracketed else ''} unresolved ({item.reason})"
                )
        notes = (
            (f"ComicInfo ignored: {'; '.join(archive.comicinfo.reasons)}",)
            if archive.comicinfo.status
            in (COMICINFO_TEMPLATE, COMICINFO_EDITED_TEMPLATE, COMICINFO_INCONSISTENT)
            else ()
        )

        results.append(
            ArchiveResolution(
                archive_key=key,
                filename=archive.filename,
                fields=fields,
                v_tokens=tuple(classifications),
                family=family,
                comicinfo=archive.comicinfo,
                renamable=not review,
                review=tuple(review),
                notes=notes,
            )
        )

    return FolderResolution(
        archives=tuple(results),
        number_run=number_run.reason,
        volume_run=volume_run.reason,
        families=tuple(families),
    )


# --- gathering facts from disk, read-only ----------------------------------


def archive_facts_from_file(
    path: str | Path,
    *,
    archive_key: str | None = None,
    pages: PageEvidence | None = None,
) -> ArchiveFacts:
    """`ArchiveFacts` for one CBZ, reading only its ComicInfo member(s)."""
    archive_path = Path(path)
    return ArchiveFacts(
        archive_key=archive_key or str(archive_path),
        filename=archive_path.name,
        comicinfo=comicinfo_facts(read_comicinfo_documents(archive_path)),
        pages=pages,
    )


def folder_facts_from_directory(
    directory: str | Path,
    *,
    series_folder: str | None = None,
    pages: Mapping[str, PageEvidence] | None = None,
) -> FolderFacts:
    """`FolderFacts` for the ``*.cbz`` files directly in ``directory``.

    Lists and reads only. ``pages`` maps a filename to its page evidence
    (from `load_page_evidence` or a fresh `calculate_page_hashes`); the
    caller owns the archive-id-to-file mapping, because which row describes
    which file is exactly the provenance question step 5 settles.
    """
    root = Path(directory)
    files = sorted(
        (
            child
            for child in root.iterdir()
            if child.is_file() and child.suffix.casefold() == ".cbz"
        ),
        key=lambda child: child.name,
    )
    page_map = pages or {}
    return FolderFacts(
        series_folder=series_folder,
        archives=tuple(
            archive_facts_from_file(child, pages=page_map.get(child.name))
            for child in files
        ),
    )
