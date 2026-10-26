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


def comicvine_series_year(volume) -> int | None:
    """A four-digit Volume in 1900-2100 is ComicVine's series start year.

    ComicTagger writes ``<Series>Batman</Series><Volume>2016</Volume>`` for
    the 2016 run; such a value is never a collected-volume number.
    """
    text = "" if volume is None else str(volume).strip()
    return int(text) if re.fullmatch(r"[0-9]{4}", text) and 1900 <= int(text) <= 2100 else None


def filename_series_year(filename: str, *titles: str | None) -> int | None:
    """Return a ``(YYYY)`` that directly follows a decided series title.

    ``Batman (2016) #003`` carries 2016 in series position; ``Batman #003
    (2016)`` does not. Only an unbracketed prefix equal to a title counts, and
    numbering must follow: in an unnumbered ``Series (2019)`` it is the issue year.
    """
    stem = re.sub(r"\.(?:cbz|zip)$", "", filename, flags=re.I)
    match = re.match(r"([^()\[\]]*?)\s*\(([0-9]{4})\)", stem)
    if match is None or not display_key(match[1]):
        return None
    if not re.search(r"[0-9]", re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem[match.end():])):
        return None
    keys = {display_key(split_series_year(title)[0]) for title in titles if title}
    return int(match[2]) if display_key(match[1]) in keys else None


def unconsumed_years(filename: str, *kept) -> None:
    """Refuse to format away any parenthesised filename year (shared guard).

    Each ``kept`` year (the decided series year, the issue year) consumes one
    occurrence. Any other ``(YYYY)``, including one after a title the folder
    replaced as an alias or typo, keeps the arrival name / holds the plan.
    """
    stem = re.sub(r"\.(?:cbz|zip)$", "", filename, flags=re.I)
    years = [int(y) for y in re.findall(r"\(\s*([0-9]{4})\s*\)", stem)]
    for year in kept:
        if year is not None and int(year) in years:
            years.remove(int(year))
    if years:
        raise KeepArrivalName(f"unconsumed filename year {years}; never formatted away")


def decide_series_year(*, series: str, folder: str | None, filename: str,
                       ci_series: str | None = None, ci_volume=None) -> tuple[int | None, int | None]:
    """Decide the reboot series year; return ``(series_year, filename_year)``.

    A year-qualified chosen series folder decides, and any other offered
    series year must agree with it. Without a folder year, ComicInfo
    ``<Series>Batman (2016)</Series>`` or a ComicVine ``<Volume>2016`` supplies
    it (David #3003), and a filename series year must agree. A filename series
    year alone is unconfirmed: it is never dropped, so the arrival name is kept.
    """
    base, chosen_year = split_series_year(series_display(series, folder))
    fn_year = filename_series_year(filename, base, series, ci_series)
    ci_base, ci_year = split_series_year(ci_series) if ci_series else (None, None)
    if ci_year is not None and display_key(ci_base) != display_key(base):
        ci_year = None  # A different title is an alias; the chosen folder decides identity.
    vol_year = comicvine_series_year(ci_volume)
    offered = {year for year in (fn_year, ci_year, vol_year) if year is not None}
    if chosen_year is not None:
        if offered - {chosen_year}:
            raise CanonicalNameError("series year conflicts with the chosen series folder")
        return chosen_year, fn_year
    if len(offered) > 1:
        raise CanonicalNameError(f"conflicting series years {sorted(offered)}")
    if fn_year is not None and ci_year is None and vol_year is None:
        raise KeepArrivalName("unconfirmed filename series year; retaining legacy arrival name")
    return (next(iter(offered)) if offered else None), fn_year


def with_comicinfo_year(xml_text: str, year) -> str | None:
    """Record an issue year that a series-year filename omits; None if present.

    A different existing Year refuses rather than replacing metadata.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CanonicalNameError(f"cannot record ComicInfo Year: {exc}") from exc
    element = root.find("Year")
    current = (element.text or "").strip() if element is not None else ""
    if current:
        if fr.normalize_year(current) != fr.normalize_year(str(year)):
            raise CanonicalNameError(f"ComicInfo Year {current} conflicts with arrival year {year}")
        return None
    if element is None:
        element = ET.SubElement(root, "Year")
    element.text = str(year)
    return ET.tostring(root, encoding="unicode")


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
                 full_default: bool = False, ci_series: str | None = None,
                 ci_volume=None) -> CanonicalFields:
    """Map resolved fields to issue/chapter/collected slots without guessing.

    Explicit TPB/Full/Vol labels may express a collected volume. A conflicting
    number, a primary issue/chapter inside that collection, refuses; an unnumbered
    Full defaults to volume 1 under the operator-approved policy. Manga volume labels remain ordinary volumes without TPB.
    ``ci_series``/``ci_volume`` are authoritative ComicInfo values only; they
    can confirm a reboot series year (see :func:`decide_series_year`).
    """
    if style not in ("issue", "chapter"):
        raise CanonicalNameError("number style must be resolved before formatting")
    series_year, _ = decide_series_year(series=series, folder=folder, filename=tokens.filename,
                                        ci_series=ci_series, ci_volume=ci_volume)
    vol_year = comicvine_series_year(ci_volume)
    if vol_year is not None and volume is not None and fr.normalize_number(str(volume)) == str(vol_year):
        volume = None  # ComicVine's Volume is the series start year, never a collected volume.
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
    unconsumed_years(tokens.filename, series_year, year)
    chosen_series = split_series_year(series_display(series, folder))[0]
    return CanonicalFields(
        series=chosen_series, series_year=series_year, volume=volume,
        issue=number if style == "issue" else None,
        chapter=number if style == "chapter" else None,
        year=year, edition=edition, special=special, version=version,
        collection=collection,
    )


def watcher_fields(parsed, fact: fr.ArchiveFacts, *, series: str, style: str) -> CanonicalFields:
    """Format an arrival; see :func:`watcher_naming` (which also returns its XML year)."""
    return watcher_naming(parsed, fact, series=series, style=style)[0]


def watcher_naming(parsed, fact: fr.ArchiveFacts, *, series: str, style: str) -> tuple[CanonicalFields, str | None]:
    """Format an arrival from its pre-rewrite metadata and decided folder.

    The watcher already permits its parsed arrival numbers as display facts;
    this grants no batch-planner or version-family authority. For Number and Volume, authoritative
    ComicInfo takes precedence, with explicit label conflicts held. Injected
    template fields never become new metadata authority on a retry.

    Returns the fields and the issue year the caller must record in ComicInfo
    ``<Year>`` because a reboot series-year filename omits it (else None).
    A parenthesised filename year is never discarded: it is the decided
    series year, the issue year (filename or XML), or the arrival name is kept.
    """
    from scripts.cbz_core import display_tokens_for

    ci = fact.comicinfo
    if ci.status == fr.COMICINFO_INCONSISTENT:
        raise CanonicalNameError("inconsistent arrival ComicInfo")
    tokens = fr.parse_name_tokens(fact.filename)
    display = display_tokens_for(series, fact.filename)
    if len(tokens.primary_numbers) > 1 or len(tokens.volume_tokens) > 1:
        raise CanonicalNameError("conflicting arrival number tokens")
    resolved_series = ci.fields["Series"] if ci.authoritative("Series") else series
    series_display(resolved_series, series)  # Keeping a name cannot bypass title conflict.
    ci_series = ci.fields["Series"] if ci.authoritative("Series") else None
    ci_volume = ci.fields["Volume"] if ci.authoritative("Volume") else None
    series_year, fn_year = decide_series_year(series=resolved_series, folder=series,
        filename=fact.filename, ci_series=ci_series, ci_volume=ci_volume)
    # The filename's series-position year is the decided series year, so only
    # a second year is an issue year (checked against ComicInfo Year below).
    display_years = [int(y) for y in display.years]
    series_token = fn_year
    if fn_year is not None and fn_year in display_years:
        display_years.remove(fn_year)
    elif series_year is not None and len(display_years) == 2 and display_years[0] == series_year:
        # Folder year first, issue year second (e.g. a retry after the legacy
        # display reordered the name): the first is the decided series year.
        series_token = display_years.pop(0)
    if len(display_years) > 1 or len(display.editions) > 1 or len(display.versions) > 1:
        raise CanonicalNameError("conflicting arrival display tokens")
    number = parsed.chapter
    if number is None and tokens.primary_numbers:
        number = tokens.primary_spellings[0]
    volume = parsed.volume
    if volume is None and tokens.volume_words:
        volume = tokens.volume_word_spellings[0]
    for field, current in (("Number", number), ("Volume", volume)):
        if field == "Volume" and comicvine_series_year(ci_volume) is not None:
            continue  # ComicVine series start year, decided above; not a volume.
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
    key = display_key(body)
    titles = sorted({display_key(split_series_year(t)[0]) for t in (resolved_series, series)}, key=len, reverse=True)
    title = next((t for t in titles if t and key.startswith(t)), "")
    remaining = key[len(title):]
    consumed = list(tokens.primary_spellings) + list(tokens.volume_spellings)
    if not tokens.primary_numbers and parsed.chapter is not None:
        consumed.append(parsed.chapter)
    if not tokens.volume_tokens and parsed.volume is not None:
        consumed.append(parsed.volume)
    for spelling in consumed:
        remaining = remaining.replace(display_key(str(spelling)), "", 1)
    runs = re.findall(r"[0-9]+", remaining)
    if (not consumed and ci.authoritative("Number") and len(runs) == 1
            and fr.normalize_number(runs[0]) == fr.normalize_number(ci.fields["Number"])):
        # A lone bare number equal to authoritative ComicInfo Number is that
        # Number (planner parity); an unconfirmed bare number is never guessed.
        remaining = remaining.replace(runs[0], "", 1)
    if any(c.isdigit() for c in remaining):
        raise KeepArrivalName("unconsumed filename number; retaining legacy arrival name")
    issue_years = [str(y) for y in (tokens.years if tokens.canonical else display_years)]
    year = issue_years[0] if issue_years else None
    if ci.authoritative("Year"):
        year = fr.normalize_year(ci.fields["Year"])
        if year is None or (issue_years and str(year) != str(issue_years[0])):
            raise CanonicalNameError("arrival year conflicts or is invalid")
    paren_years = [int(y) for y in re.findall(r"\(\s*([0-9]{4})\s*\)", stem)]
    for kept in (series_token, int(issue_years[0]) if issue_years else None):
        if kept is not None and kept in paren_years:
            paren_years.remove(kept)
    if paren_years:
        raise KeepArrivalName(f"unconsumed filename year {paren_years}; retaining legacy arrival name")
    markers = fr.effective_markers(tokens, resolved_series)
    if markers.ambiguity or len(markers.specials) > 1:
        raise CanonicalNameError(markers.ambiguity or "conflicting arrival specials")
    fields = media_fields(series=resolved_series, folder=series, style=style,
        tokens=tokens, number=number, volume=volume, year=year,
        edition=display.editions[0] if display.editions else None,
        special=next(iter(markers.specials), None),
        version=display.versions[0] if display.versions else None,
        ci_series=ci_series, ci_volume=ci_volume)
    xml_year = None
    if fields.series_year is not None and year is not None and not ci.authoritative("Year"):
        xml_year = str(year)
    return fields, xml_year
