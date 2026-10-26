"""Shared media naming decisions after identity and numbering are resolved.

This layer selects display and number slots; it does not make a filename into
series authority or authorize a version family. Both callers use these facts.
"""

from __future__ import annotations

import re
import unicodedata

from comic_automation.archive import field_resolution as fr
from scripts.cbz_canonical_name import CanonicalFields, CanonicalNameError


class KeepArrivalName(CanonicalNameError):
    """Media formatting would lose unclassified numbering; keep legacy display.

    This does not authorize a number or a planner rename. The watcher can
    continue its existing tag-sensitive landing with its intact arrival name.
    """


def display_key(text: str) -> str:
    """Fold only case, spacing and punctuation, retaining every letter/mark.

    The broader routing series key removes meaningful words such as
    Uncensored and cannot authorize a display substitution here.
    """
    text = unicodedata.normalize("NFC", text).casefold()
    return "".join(c for c in text if unicodedata.category(c)[0] in "LNM")


def series_display(resolved: str, folder: str | None) -> str:
    """Use the operator-confirmed series folder, preserving metadata separately.

    The caller only passes a series folder, never an intake/library category.
    This changes display after identity resolution, never its evidence rank.
    """
    if folder is None:
        return resolved
    return folder


def split_series_year(display: str) -> tuple[str, int | None]:
    """Separate a reboot year from its display without treating it as issue year."""
    match = re.fullmatch(r"(.+?)\s+\(([0-9]{4})\)", display)
    return (match[1], int(match[2])) if match else (display, None)


def unnumbered_full(tokens: fr.NameTokens, *titles: str | None) -> bool:
    """Recognize an explicit Full marker against decided/original series names."""
    stem = re.sub(r"\.(?:cbz|zip)$", "", tokens.filename, flags=re.I)
    full = re.search(r"(?<!\w)Full(?!\w)", stem, re.I)
    if full is None or tokens.volume_tokens:
        return False
    tail = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem[full.end():]).strip(" []()._-")
    if tail:
        return False
    prefix = display_key(stem[:full.start()])
    for title in filter(None, titles):
        if display_key(stem) == display_key(title):
            continue  # A title literally ending in Full keeps its word.
        if not prefix or prefix in {display_key(title), display_key(split_series_year(title)[0])}:
            return True
    return False


def media_fields(*, series: str, number: str | None, volume: str | None,
                 style: str, tokens: fr.NameTokens, year=None, edition=None,
                 special=None, version=None, folder: str | None = None,
                 full_default: bool = False) -> CanonicalFields:
    """Map resolved fields to issue/chapter/collected slots without guessing.

    Explicit TPB/Full/Vol labels may express a collected volume. A conflicting
    number, a primary issue/chapter inside that collection, refuses; an unnumbered
    Full defaults to volume 1 under the operator-approved policy. Manga volume labels remain ordinary volumes without TPB.
    """
    if style not in ("issue", "chapter"):
        raise CanonicalNameError("number style must be resolved before formatting")
    # A year/edition after Full or brackets around it cannot hide the explicit
    # operator collection decision. Compare the prefix against the decided title so a
    # genuine title ending in Full does not lose one of its words.
    full = full_default or unnumbered_full(tokens, series, folder)
    labelled_volume = bool(tokens.collections or tokens.volume_words or (
        volume is not None and not tokens.primary_numbers))
    collection = None
    if full:
        if style != "issue":
            raise CanonicalNameError("Full collection conflicts with chapter style")
        if volume is not None and number is not None and fr.normalize_number(volume) != fr.normalize_number(number):
            raise CanonicalNameError("Full volume conflicts with resolved Number")
        volume, number, collection = volume or number or "1", None, "TPB"
    elif labelled_volume:
        if len(tokens.collections) > 1 or len(tokens.volume_tokens) != 1 or volume is None:
            raise CanonicalNameError("collected volume is unresolved or conflicting")
        if fr.normalize_number(volume) != tokens.volume_tokens[0]:
            raise CanonicalNameError("collection label conflicts with resolved volume")
        if tokens.collections and tokens.primary_numbers:
            raise CanonicalNameError("collected edition also claims an issue/chapter")
        if not tokens.primary_numbers:
            if number is not None and fr.normalize_number(number) != fr.normalize_number(volume):
                raise CanonicalNameError("collection volume conflicts with primary Number")
            number = None
            collection = "TPB" if style == "issue" else None
    # A canonical TPB is Western-only; an explicit conflicting manga style
    # must not silently erase an already-present collected-edition marker.
    if tokens.collections and style != "issue":
        raise CanonicalNameError("Western collection marker conflicts with chapter style")
    chosen_series, series_year = split_series_year(series_display(series, folder))
    return CanonicalFields(
        series=chosen_series, series_year=series_year, volume=volume,
        issue=number if style == "issue" else None,
        chapter=number if style == "chapter" else None,
        year=year, edition=edition, special=special, version=version,
        collection=collection,
    )


def watcher_fields(parsed, fact: fr.ArchiveFacts, *, series: str, style: str) -> CanonicalFields:
    """Format an arrival from its pre-rewrite metadata and decided folder.

    The watcher already permits its parsed arrival numbers as display facts;
    this grants no batch-planner or version-family authority. For Number and Volume, authoritative
    ComicInfo takes precedence, with explicit label conflicts held. Injected
    template fields never become new metadata authority on a retry.
    """
    from scripts.cbz_core import display_tokens_for

    ci = fact.comicinfo
    if ci.status == fr.COMICINFO_INCONSISTENT:
        raise CanonicalNameError("inconsistent arrival ComicInfo")
    tokens = fr.parse_name_tokens(fact.filename)
    display = display_tokens_for(series, fact.filename)
    if len(tokens.primary_numbers) > 1 or len(tokens.volume_tokens) > 1:
        raise CanonicalNameError("conflicting arrival number tokens")
    if len(display.years) > 1 or len(display.editions) > 1 or len(display.versions) > 1:
        raise CanonicalNameError("conflicting arrival display tokens")
    resolved_series = ci.fields["Series"] if ci.authoritative("Series") else series
    series_display(resolved_series, series)  # Keeping a name cannot bypass title conflict.
    number = parsed.chapter
    if number is None and tokens.primary_numbers:
        number = tokens.primary_spellings[0]
    volume = parsed.volume
    if volume is None and tokens.volume_words:
        volume = tokens.volume_word_spellings[0]
    for field, current in (("Number", number), ("Volume", volume)):
        if ci.authoritative(field):
            offered = ci.fields[field]
            if fr.normalize_number(offered) is None:
                raise CanonicalNameError(f"invalid ComicInfo {field}")
            if current is not None and fr.normalize_number(current) != fr.normalize_number(offered):
                raise CanonicalNameError(f"arrival label conflicts with ComicInfo {field}")
            if field == "Number":
                number = offered
            else:
                volume = offered
    # Number-like text is never disposable merely because it has no parsed
    # field. Remove only the decided title, bracket display/source tokens,
    # and number occurrences actually consumed by the arrival parsers.
    stem = re.sub(r"\.(?:cbz|zip)$", "", fact.filename, flags=re.I)
    body = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)
    key, title = display_key(body), display_key(resolved_series)
    remaining = key[len(title):] if title and key.startswith(title) else key
    consumed = list(tokens.primary_spellings) + list(tokens.volume_spellings)
    if not tokens.primary_numbers and parsed.chapter is not None:
        consumed.append(parsed.chapter)
    if not tokens.volume_tokens and parsed.volume is not None:
        consumed.append(parsed.volume)
    for spelling in consumed:
        remaining = remaining.replace(display_key(str(spelling)), "", 1)
    if any(c.isdigit() for c in remaining):
        raise KeepArrivalName("unconsumed filename number; retaining legacy arrival name")
    issue_years = tokens.years if tokens.canonical else display.years
    year = issue_years[0] if issue_years else None
    if ci.authoritative("Year"):
        year = fr.normalize_year(ci.fields["Year"])
        if year is None or (issue_years and str(year) != str(issue_years[0])):
            raise CanonicalNameError("arrival year conflicts or is invalid")
    markers = fr.effective_markers(tokens, resolved_series)
    if markers.ambiguity or len(markers.specials) > 1:
        raise CanonicalNameError(markers.ambiguity or "conflicting arrival specials")
    return media_fields(series=resolved_series, folder=series, style=style,
        tokens=tokens, number=number, volume=volume, year=year,
        edition=display.editions[0] if display.editions else None,
        special=next(iter(markers.specials), None),
        version=display.versions[0] if display.versions else None)
