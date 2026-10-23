"""Field resolution and version-family evidence (design §3, §4.2; step 4).

`comic_automation.archive.field_resolution` decides series, volume, primary
number, year and version for each archive, or leaves them unresolved with
their candidates. These tests pin the §3.1 precedence ladder, the §3.2
volume/version split, the §4.2 family requirements, and the read-only
contract. The §7 named tests for this step are here under their design
names; tests whose named counterpart belongs to another step say so.

Most tests build `FolderFacts` by hand, because the resolver is pure and a
hand-built input states exactly which evidence exists. The template tests
build their XML from the repository's real templates and the real watcher
fill path, and the read-only tests use real CBZ files and a real migrated
database in ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from comic_automation.archive import field_resolution as fr
from comic_automation.archive.field_resolution import (
    ArchiveFacts,
    ComicInfoFacts,
    FamilyMember,
    FieldResolution,
    FolderFacts,
    HumanDecision,
    PageEvidence,
    StaticHumanDecisions,
    comicinfo_facts,
    establish_family,
    folder_facts_from_directory,
    load_page_evidence,
    normalize_number,
    parse_name_tokens,
    resolve_folder,
)
from comic_automation.archive.page_hashing import (
    CONTENT_SIGNATURE_ALGORITHM,
    CONTENT_SIGNATURE_VERSION,
    ArchivePageHashRepository,
    calculate_page_hashes,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.database.read_guards import (
    DatabaseChangedError,
    quick_check,
)


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


# --- builders --------------------------------------------------------------


def _pages(*digests: str, version: str = CONTENT_SIGNATURE_VERSION,
           order_resolved: bool = True) -> PageEvidence:
    return PageEvidence(
        algorithm=CONTENT_SIGNATURE_ALGORITHM,
        algorithm_version=version,
        order_resolved=order_resolved,
        page_digests=tuple(digests),
    )


def _info(**fields: str) -> ComicInfoFacts:
    """Usable ComicInfo with the given fields (already vetted)."""
    return ComicInfoFacts(status=fr.COMICINFO_USABLE, fields=dict(fields))


def _archive(filename: str, *, info: ComicInfoFacts | None = None,
             pages: PageEvidence | None = None) -> ArchiveFacts:
    return ArchiveFacts(
        archive_key=filename,
        filename=filename,
        comicinfo=info or ComicInfoFacts(status=fr.COMICINFO_ABSENT),
        pages=pages,
    )


def _resolve(*archives: ArchiveFacts, series_folder: str | None = None,
             decisions=fr.NO_HUMAN_DECISIONS):
    return resolve_folder(
        FolderFacts(series_folder=series_folder, archives=tuple(archives)),
        decisions,
    ).by_key()


def _member(key: str, *, series="Series", number="1", editions=(),
            marked=True, pages=None, volume: str | None = None,
            family_decision: str | None = None) -> FamilyMember:
    def resolved(field, value):
        return FieldResolution(field, value, fr.SOURCE_COMICINFO, (), "resolved")

    return FamilyMember(
        archive_key=key,
        series=resolved("series", series),
        number=resolved("number", number),
        editions=frozenset(editions),
        version_marked=marked,
        pages=pages if pages is not None else _pages(f"{key}-p1"),
        family_decision=family_decision,
        volume=None if volume is None else resolved("volume", volume),
    )


def _v(result, value: str):
    return next(t for t in result.v_tokens if t.value == value)


# --- §3.1 precedence -------------------------------------------------------


def test_conflicting_comicinfo_and_folder_evidence_is_unresolved():
    """§7 named. ComicInfo says one thing, folder context another: no pick.

    The series folder is "Superman" but the ComicInfo says "Batman", and the
    consistently numbered siblings put this archive at #2 while its
    ComicInfo says #7. Neither field may resolve, and both carry both
    candidates for review.
    """
    result = _resolve(
        _archive("Superman #001.cbz"),
        _archive("Superman #002.cbz", info=_info(Series="Batman", Number="7")),
        series_folder="Superman",
    )["Superman #002.cbz"]

    for name, values in (("series", {"Batman", "Superman"}), ("number", {"7", "2"})):
        field = result.fields[name]
        assert not field.resolved
        assert field.reason == "conflict"
        assert {e.value for e in field.evidence if e.source != fr.SOURCE_FILENAME} == values
    assert not result.renamable


def test_a_human_decision_overrides_conflicting_lower_evidence():
    decisions = StaticHumanDecisions({
        "Superman #002.cbz": [
            HumanDecision("series", "Superman", "rec-1"),
            HumanDecision("number", "2", "rec-2"),
        ],
    })
    result = _resolve(
        _archive("Superman #001.cbz"),
        _archive("Superman #002.cbz", info=_info(Series="Batman", Number="7")),
        series_folder="Superman",
        decisions=decisions,
    )["Superman #002.cbz"]

    assert result.fields["series"].value == "Superman"
    assert result.fields["series"].source == fr.SOURCE_HUMAN
    assert result.fields["number"].value == "2"
    assert result.fields["number"].source == fr.SOURCE_HUMAN
    assert result.renamable


def test_conflicting_human_decisions_are_unresolved_never_picked():
    decisions = StaticHumanDecisions({
        "A ch. 1.cbz": [
            HumanDecision("number", "1", "rec-1"),
            HumanDecision("number", "2", "rec-2"),
        ],
    })
    result = _resolve(
        _archive("A ch. 1.cbz", info=_info(Number="1")),
        decisions=decisions,
    )["A ch. 1.cbz"]
    assert not result.fields["number"].resolved
    assert result.fields["number"].reason == "human_conflict"


def test_comicinfo_decides_a_field_the_folder_is_silent_on():
    result = _resolve(
        _archive("whatever.cbz", info=_info(Series="Berserk", Number="12", Year="1990")),
    )["whatever.cbz"]
    assert result.fields["series"].value == "Berserk"
    assert result.fields["series"].source == fr.SOURCE_COMICINFO
    assert result.fields["number"].value == "12"
    assert result.fields["year"].value == "1990"
    assert result.renamable


def test_folder_context_decides_when_comicinfo_is_absent():
    result = _resolve(
        _archive("Berserk ch. 1.cbz"),
        _archive("Berserk ch. 2.cbz"),
        series_folder="Berserk",
    )["Berserk ch. 2.cbz"]
    assert result.fields["series"].source == fr.SOURCE_FOLDER
    assert result.fields["number"].value == "2"
    assert result.fields["number"].source == fr.SOURCE_FOLDER
    assert result.renamable


def test_agreeing_levels_report_the_higher_source_and_keep_both():
    """Agreement resolves; the reported source is the higher level.

    The series comparison goes through `series_key`, so underscore and
    space spellings of one title agree.
    """
    result = _resolve(
        _archive("Attack on Titan ch. 1.cbz", info=_info(Series="Attack_on_Titan", Number="1")),
        _archive("Attack on Titan ch. 2.cbz"),
        series_folder="Attack on Titan",
    )["Attack on Titan ch. 1.cbz"]
    series = result.fields["series"]
    assert series.resolved and series.source == fr.SOURCE_COMICINFO
    assert series.value == "Attack_on_Titan"
    assert [e.source for e in series.evidence] == [
        fr.SOURCE_COMICINFO, fr.SOURCE_FOLDER, fr.SOURCE_FILENAME,
    ]
    assert result.fields["number"].source == fr.SOURCE_COMICINFO


def test_the_filename_alone_never_resolves_a_field():
    result = _resolve(_archive("Berserk ch. 5 (1990).cbz"))["Berserk ch. 5 (1990).cbz"]
    for name in ("series", "number", "year"):
        field = result.fields[name]
        assert not field.resolved
        assert field.reason == "filename_candidate_only"
        assert field.evidence and all(
            e.source == fr.SOURCE_FILENAME for e in field.evidence
        )
    assert not result.renamable


def test_a_filename_that_disagrees_with_better_evidence_does_not_block():
    """A wrong current name is what a rename fixes, so it is no conflict."""
    result = _resolve(
        _archive("Bersrek ch. 9.cbz", info=_info(Series="Berserk", Number="5")),
    )["Bersrek ch. 9.cbz"]
    assert result.fields["series"].value == "Berserk"
    assert result.fields["number"].value == "5"
    assert result.renamable


def test_the_default_decision_source_is_empty():
    assert tuple(fr.NO_HUMAN_DECISIONS.decisions_for("anything")) == ()


# --- ComicInfo: template and consistency -----------------------------------


def _watcher_filled_template() -> str:
    """The watcher's real template, filled by the real watcher fill path.

    `process_comicinfo` injects COMICINFO_TEMPLATE and runs it through
    `update_comicinfo_xml` with the filename parse. The translation probe is
    patched out so the test needs no network; Latin text is never
    translated anyway.
    """
    from scripts.cbz_core import ParsedComicName, update_comicinfo_xml
    from scripts.cbz_watcher import COMICINFO_TEMPLATE

    parsed = ParsedComicName(
        original_path=Path("Berserk v03 ch. 7.cbz"),
        filename="Berserk v03 ch. 7.cbz",
        stem="Berserk v03 ch. 7",
        series="Berserk",
        chapter="7",
        volume="3",
    )
    with patch("scripts.cbz_core._run_googletrans", return_value=(None, None)):
        xml, changed = update_comicinfo_xml(COMICINFO_TEMPLATE, parsed)
    assert changed
    return xml


def test_the_watcher_injected_template_is_ignored():
    xml = _watcher_filled_template()
    facts = comicinfo_facts([xml])
    assert facts.status == fr.COMICINFO_TEMPLATE
    assert facts.fields["Volume"] == "3"          # it does restate the name

    result = _resolve(_archive("Berserk v03 ch. 7.cbz", info=facts))[
        "Berserk v03 ch. 7.cbz"
    ]
    assert not result.fields["series"].resolved
    assert not result.fields["number"].resolved
    # The template's Volume must not classify v03 as a volume either.
    assert _v(result, "3").classification == fr.V_UNRESOLVED
    assert result.notes and "template" in result.notes[0]


def test_the_sanitizer_injected_template_is_ignored():
    from scripts.cbz_sanitizer import COMICINFO_TEMPLATE

    xml = (
        COMICINFO_TEMPLATE.replace("<Series></Series>", "<Series>Berserk</Series>\n  <Volume>3</Volume>")
        .replace("<Number></Number>", "<Number>7</Number>")
        .replace("<Title></Title>", "<Title>Berserk v03 ch. 7</Title>")
    )
    assert comicinfo_facts([xml]).status == fr.COMICINFO_TEMPLATE


def test_the_template_fingerprint_matches_both_real_templates():
    """The constants are pinned to the templates the injectors really write."""
    from xml.etree import ElementTree

    from scripts.cbz_sanitizer import COMICINFO_TEMPLATE as SANITIZER
    from scripts.cbz_watcher import COMICINFO_TEMPLATE as WATCHER

    for template in (WATCHER, SANITIZER):
        root = ElementTree.fromstring(template)
        names = {child.tag.rsplit("}", 1)[-1] for child in root}
        assert names == fr.TEMPLATE_ELEMENTS
        assert fr.is_injected_template(root)


def _with_text(xml: str, local_name: str, text: str) -> str:
    """``xml`` with the element named ``local_name`` set to ``text``."""
    from xml.etree import ElementTree

    root = ElementTree.fromstring(xml)
    element = next(c for c in root if c.tag.rsplit("}", 1)[-1] == local_name)
    element.text = text
    return ElementTree.tostring(root, encoding="unicode")


def _with_element(xml: str, local_name: str, text: str) -> str:
    """``xml`` with a new ``local_name`` element appended."""
    from xml.etree import ElementTree

    root = ElementTree.fromstring(xml)
    ElementTree.SubElement(root, local_name).text = text
    return ElementTree.tostring(root, encoding="unicode")


def _edited_writer() -> str:
    return _with_text(_watcher_filled_template(), "Writer", "A credited writer")


def _edited_tags() -> str:
    return _with_element(_watcher_filled_template(), "Tags", "action")


@pytest.mark.parametrize(
    "edit", [_edited_writer, _edited_tags], ids=["writer", "tags"]
)
def test_an_unrelated_edit_does_not_launder_injected_fields(edit):
    """PR #119 review, finding 1 (Codex probe ``edited_template``).

    The real watcher template, filled by the real watcher path for
    ``Berserk v03 ch. 7.cbz``, then edited in a field that says nothing
    about Series/Number/Volume. Those three still restate the filename, so
    they must stay filename-grade: nothing resolves from ComicInfo, ``v3``
    is not a volume, and the archive is not renamable on ComicInfo alone.
    """
    facts = comicinfo_facts([edit()])
    assert facts.status == fr.COMICINFO_EDITED_TEMPLATE
    assert facts.uncertain_fields == {"Series", "Number", "Volume"}

    result = _resolve(_archive("Berserk v03 ch. 7.cbz", info=facts))[
        "Berserk v03 ch. 7.cbz"
    ]
    for name in ("series", "number", "volume"):
        field = result.fields[name]
        assert not field.resolved, name
        assert all(e.source != fr.SOURCE_COMICINFO for e in field.evidence)
        assert any("injected template" in e.detail for e in field.evidence)
    assert _v(result, "3").classification == fr.V_UNRESOLVED
    assert not result.renamable


def test_an_edited_template_still_counts_for_fields_the_injectors_never_write():
    """Control: a Year added to an injected template is real metadata."""
    xml = _with_element(_edited_writer(), "Year", "1990")
    facts = comicinfo_facts([xml])
    assert facts.authoritative("Year")
    result = _resolve(_archive("Berserk v03 ch. 7.cbz", info=facts))[
        "Berserk v03 ch. 7.cbz"
    ]
    assert result.fields["year"].value == "1990"
    assert result.fields["year"].source == fr.SOURCE_COMICINFO


def test_injected_fields_still_resolve_from_independent_evidence():
    """Control: folder context or a field-specific human decision resolves
    an injected field in the ordinary way; the template neither blocks nor
    decides."""
    facts = comicinfo_facts([_edited_writer()])
    results = _resolve(
        _archive("Berserk ch. 6.cbz"),
        _archive("Berserk v03 ch. 7.cbz", info=facts),
        series_folder="Berserk",
        decisions=StaticHumanDecisions({
            "Berserk v03 ch. 7.cbz": [HumanDecision("volume", "3", "rec-v")],
        }),
    )
    result = results["Berserk v03 ch. 7.cbz"]
    assert result.fields["series"].source == fr.SOURCE_FOLDER
    assert result.fields["volume"].source == fr.SOURCE_HUMAN
    assert _v(result, "3").classification == fr.V_VOLUME


def test_a_non_komga_mihon_export_is_not_the_template():
    xml = _with_text(_watcher_filled_template(), "SourceMihon", "MangaDex")
    assert comicinfo_facts([xml]).status == fr.COMICINFO_USABLE


@pytest.mark.parametrize(
    "documents",
    [
        ["<ComicInfo><Number>1</Number><Number>2</Number></ComicInfo>"],
        ["<ComicInfo><Series>A</Series><Series>B</Series></ComicInfo>"],
        ["<ComicInfo><Number>9</Number><Count>3</Count></ComicInfo>"],
        ["<ComicInfo><Year>199</Year></ComicInfo>"],
        ["<ComicInfo><Month>13</Month></ComicInfo>"],
        ["<ComicInfo><Number>1</Number>", ],
        ["<NotComicInfo><Number>1</Number></NotComicInfo>"],
        [
            "<ComicInfo><Number>1</Number></ComicInfo>",
            "<ComicInfo><Number>2</Number></ComicInfo>",
        ],
    ],
    ids=[
        "duplicate-number", "duplicate-series", "number-over-count",
        "bad-year", "bad-month", "unparseable", "wrong-root",
        "two-differing-members",
    ],
)
def test_inconsistent_comicinfo_is_ignored(documents):
    facts = comicinfo_facts(documents)
    assert facts.status == fr.COMICINFO_INCONSISTENT and facts.reasons

    result = _resolve(_archive("x.cbz", info=facts))["x.cbz"]
    assert all(
        e.source != fr.SOURCE_COMICINFO
        for field in result.fields.values()
        for e in field.evidence
    )


@pytest.mark.parametrize(
    "year, plausible",
    [("1899", False), ("1066", False), ("1900", True),
     ("2100", True), ("2101", False)],
)
def test_year_bounds_are_enforced_at_both_ends(year, plausible):
    assert (fr.normalize_year(year) is not None) is plausible
    status = comicinfo_facts([f"<ComicInfo><Year>{year}</Year></ComicInfo>"]).status
    assert status == (fr.COMICINFO_USABLE if plausible else fr.COMICINFO_INCONSISTENT)


def test_identical_duplicate_comicinfo_members_are_consistent():
    doc = "<ComicInfo><Series>A</Series><Number>1</Number></ComicInfo>"
    assert comicinfo_facts([doc, doc]).status == fr.COMICINFO_USABLE


# --- sibling runs ----------------------------------------------------------


@pytest.mark.parametrize(
    "names",
    [
        ("S ch. 1.cbz", "S ch. 1 (copy).cbz"),          # duplicate numbering
        ("S ch. 1.cbz", "S #2.cbz"),                      # chapter and issue mixed
        ("S ch. 1.cbz", "T ch. 2.cbz"),                   # different series prefix
        ("S ch. 1.cbz", "S extra.cbz"),                   # a sibling with no number
        ("S ch. 1.cbz",),                                 # no siblings at all
    ],
    ids=["duplicate", "mixed-kind", "prefix", "unnumbered", "alone"],
)
def test_inconsistently_numbered_siblings_do_not_count(names):
    results = _resolve(*(_archive(n) for n in names), series_folder="S")
    first = results["S ch. 1.cbz"]
    assert not any(e.source == fr.SOURCE_FOLDER for e in first.fields["number"].evidence)
    assert not first.fields["number"].resolved
    assert not first.renamable


def test_numbers_compare_in_normal_form():
    assert normalize_number("003") == "3"
    assert normalize_number("12.50") == "12.5"
    assert normalize_number("1 - 03") == "1-3"
    assert normalize_number("Special") is None
    result = _resolve(
        _archive("S #003.cbz", info=_info(Number="3")),
        _archive("S #004.cbz"),
        series_folder="S",
    )["S #003.cbz"]
    assert result.fields["number"].value == "3"
    assert result.fields["number"].reason == "resolved"


def test_a_non_numeric_comicinfo_number_is_kept_as_evidence():
    """`Number` "Special" is not dropped: dropping it would let the folder
    decide a field ComicInfo has spoken to."""
    result = _resolve(
        _archive("S ch. 1.cbz", info=_info(Number="Special")),
        _archive("S ch. 2.cbz"),
        series_folder="S",
    )["S ch. 1.cbz"]
    assert not result.fields["number"].resolved
    assert result.fields["number"].reason == "conflict"


# --- §3.2 volume versus version --------------------------------------------


def test_name_evidence_alone_never_classifies_version_or_volume():
    """§7 named. A lone ``v2``, a ``Vol.`` and a ``[v3]`` stay unresolved.

    No ComicInfo, no siblings, no family: whatever the name says, the token
    is neither a volume nor a version, and the archive is not renamable.
    """
    results = _resolve(
        _archive("Series v2.cbz", pages=_pages("a")),
        _archive("Other ch. 1 [v3].cbz", pages=_pages("b")),
    )
    bare = results["Series v2.cbz"]
    assert _v(bare, "2").classification == fr.V_UNRESOLVED
    assert not bare.fields["volume"].resolved
    assert not bare.fields["version"].resolved
    bracketed = results["Other ch. 1 [v3].cbz"]
    assert _v(bracketed, "3").classification == fr.V_UNRESOLVED
    assert not bracketed.fields["version"].resolved
    assert not bare.renamable and not bracketed.renamable


def test_version_marker_can_never_parse_as_a_volume():
    """§7 named (the parsing half; the formatter half is step 3).

    ``[v3]`` is the canonical version token. Even with ComicInfo Volume 3,
    a volume run, and a human volume decision of 3, it never becomes a
    volume and never feeds the volume field.
    """
    tokens = parse_name_tokens("Series ch. 1 [v3].cbz")
    assert tokens.version_markers == ("3",)
    assert tokens.v_tokens == () and tokens.volume_tokens == ()

    decisions = StaticHumanDecisions({
        "Series ch. 1 [v3].cbz": [HumanDecision("volume", "3", "rec-1")],
    })
    result = _resolve(
        _archive("Series ch. 1 [v3].cbz", info=_info(Volume="3", Number="1"), pages=_pages("a")),
        decisions=decisions,
    )["Series ch. 1 [v3].cbz"]
    assert _v(result, "3").classification != fr.V_VOLUME
    assert all(
        e.detail != "filename [vN] marker" for e in result.fields["volume"].evidence
    )


def test_comicinfo_volume_agreement_classifies_a_bare_token_as_volume():
    result = _resolve(
        _archive("Berserk v03.cbz", info=_info(Series="Berserk", Volume="3"), pages=_pages("a")),
    )["Berserk v03.cbz"]
    assert _v(result, "3").classification == fr.V_VOLUME
    assert result.fields["volume"].value == "3"
    assert result.renamable          # primary number is the volume here


def test_a_volume_run_of_different_books_classifies_volume():
    results = _resolve(
        _archive("Berserk v01.cbz", pages=_pages("a1", "a2")),
        _archive("Berserk v02.cbz", pages=_pages("b1", "b2")),
        _archive("Berserk v03.cbz", pages=_pages("c1")),
        series_folder="Berserk",
    )
    for volume in ("1", "2", "3"):
        result = results[f"Berserk v0{volume}.cbz"]
        assert _v(result, volume).classification == fr.V_VOLUME
        assert result.fields["volume"].value == volume
        assert result.fields["volume"].source == fr.SOURCE_FOLDER
        assert result.renamable


def test_a_run_whose_pages_overlap_does_not_show_different_books():
    """v1 is a prefix of v2: a WIP version pair, not a volume run."""
    results = _resolve(
        _archive("Series v1.cbz", pages=_pages("p1", "p2")),
        _archive("Series v2.cbz", pages=_pages("p1", "p2", "p3")),
        series_folder="Series",
    )
    for result in results.values():
        assert result.v_tokens[0].classification == fr.V_UNRESOLVED
        assert not result.renamable


def test_a_legacy_signature_never_shows_different_books():
    results = _resolve(
        _archive("Berserk v01.cbz", pages=_pages("a1")),
        _archive("Berserk v02.cbz", pages=_pages("b1", version="1")),
        series_folder="Berserk",
    )
    assert all(r.v_tokens[0].classification == fr.V_UNRESOLVED for r in results.values())


def _wip_folder(**overrides):
    """Two WIP versions of chapter 1 plus chapter 2, all comparable."""
    pages = {
        "Series ch. 1 v1.cbz": _pages("p1", "p2"),
        "Series ch. 1 v2.cbz": _pages("p1", "p2", "p3"),
        "Series ch. 2.cbz": _pages("q1"),
    }
    pages.update(overrides)
    return [_archive(name, pages=evidence) for name, evidence in pages.items()]


def test_an_established_family_classifies_version():
    results = _resolve(*_wip_folder(), series_folder="Series")
    for version in ("1", "2"):
        result = results[f"Series ch. 1 v{version}.cbz"]
        assert result.family is not None and result.family.established
        assert _v(result, version).classification == fr.V_VERSION
        assert result.fields["version"].value == version
        assert result.fields["version"].source == fr.SOURCE_FAMILY
        assert not result.fields["volume"].resolved
        assert result.renamable
    assert results["Series ch. 2.cbz"].renamable


def test_legacy_signatures_never_form_a_family():
    """A v1 (legacy ordering) signature is not comparable: no family, no version.

    The metadata alone would establish this family. One member's signature
    is under the old ordering contract, so §4.5's "incomparable inventories
    are never a version" stops it, and the tokens stay unresolved.
    """
    results = _resolve(
        *_wip_folder(**{"Series ch. 1 v1.cbz": _pages("p1", "p2", version="1")}),
        series_folder="Series",
    )
    for version in ("1", "2"):
        result = results[f"Series ch. 1 v{version}.cbz"]
        assert result.family is not None
        assert result.family.status == fr.FAMILY_INCOMPARABLE
        assert any("legacy ordering" in r for r in result.family.reasons)
        assert _v(result, version).classification == fr.V_UNRESOLVED
        assert not result.fields["version"].resolved
        assert not result.renamable


@pytest.mark.parametrize(
    "pages",
    [
        None,
        _pages("p1", order_resolved=False),
        _pages(),
        PageEvidence("other-algorithm", CONTENT_SIGNATURE_VERSION, True, ("p1",)),
    ],
    ids=["missing", "unresolved-order", "empty", "unknown-algorithm"],
)
def test_any_incomparable_member_keeps_the_family_incomparable(pages):
    results = _resolve(
        *_wip_folder(**{"Series ch. 1 v1.cbz": pages}), series_folder="Series"
    )
    assert results["Series ch. 1 v2.cbz"].family.status == fr.FAMILY_INCOMPARABLE
    assert not results["Series ch. 1 v2.cbz"].fields["version"].resolved


def test_a_lone_version_marked_archive_is_not_a_family():
    result = _resolve(
        _archive("Series ch. 1 [v3].cbz", info=_info(Series="Series", Number="1"), pages=_pages("a")),
    )["Series ch. 1 [v3].cbz"]
    assert result.family is not None and not result.family.established
    assert _v(result, "3").classification == fr.V_UNRESOLVED
    assert not result.renamable


def test_volume_and_version_evidence_together_is_unresolved():
    """Restarted chapter numbering in two real volumes.

    Same series, same chapter 1, version-marked by ``v01``/``v02``: the
    metadata makes a family. The pages are disjoint and the siblings form a
    volume run, so there is volume evidence too. Both kinds present means
    unresolved, never a pick.
    """
    results = _resolve(
        _archive("S v01 ch. 1.cbz", pages=_pages("a")),
        _archive("S v02 ch. 1.cbz", pages=_pages("b")),
        series_folder="S",
    )
    for volume in ("1", "2"):
        token = _v(results[f"S v0{volume} ch. 1.cbz"], volume)
        assert token.classification == fr.V_UNRESOLVED
        assert token.reason == "evidence for both volume and version"


def test_a_human_decision_classifies_a_token():
    decisions = StaticHumanDecisions({
        "Series v2.cbz": [
            HumanDecision("series", "Series", "r1"),
            HumanDecision("volume", "2", "r2"),
        ],
    })
    result = _resolve(_archive("Series v2.cbz"), decisions=decisions)["Series v2.cbz"]
    assert _v(result, "2").classification == fr.V_VOLUME
    assert result.fields["volume"].source == fr.SOURCE_HUMAN
    assert result.renamable


# --- §4.2 family evidence --------------------------------------------------


def test_an_omnibus_containing_an_issue_is_not_its_version():
    """§7 named. Every page of #1 is inside the omnibus; that proves nothing.

    The omnibus has a different number (a range) and no version marker, so
    there is no family, whatever the pages share.
    """
    issue = _pages("p1", "p2")
    omnibus = _pages("p1", "p2", "p3", "p4")
    verdict = establish_family([
        _member("S #1.cbz", number="1", marked=False, pages=issue),
        _member("S #1-2.cbz", number="1-2", marked=False, pages=omnibus),
    ])
    assert verdict.status == fr.FAMILY_REFUSED
    assert "members have different numbers" in verdict.reasons

    results = _resolve(
        _archive("S #1.cbz", info=_info(Series="S", Number="1"), pages=issue),
        _archive("S #1-2 v2.cbz", info=_info(Series="S", Number="1-2"), pages=omnibus),
        series_folder="S",
    )
    assert results["S #1-2 v2.cbz"].v_tokens[0].classification != fr.V_VERSION
    assert results["S #1.cbz"].family is None or not results["S #1.cbz"].family.established


def test_different_editions_or_languages_are_not_one_family():
    """§7 named. Colored vs plain, and English vs absent, are refused."""
    colored = establish_family([
        _member("a", editions=()),
        _member("b", editions=("Colored",)),
    ])
    assert colored.status == fr.FAMILY_REFUSED
    assert any("different editions" in r for r in colored.reasons)

    language = establish_family([
        _member("a", editions=("English",)),
        _member("b", editions=()),
    ])
    assert language.status == fr.FAMILY_REFUSED

    # Through the resolver: the colored copy refuses the plain family.
    results = _resolve(
        _archive("S ch. 1 v1.cbz", pages=_pages("p1")),
        _archive("S ch. 1 v2 (Colored).cbz", pages=_pages("p1", "p2")),
        _archive("S ch. 2.cbz", pages=_pages("q1")),
        series_folder="S",
    )
    for name in ("S ch. 1 v1.cbz", "S ch. 1 v2 (Colored).cbz"):
        assert not results[name].family.established
        assert results[name].v_tokens[0].classification == fr.V_UNRESOLVED


def _ci(series="Series", number="1", **extra: str):
    """Real ComicInfo XML through `comicinfo_facts`, not hand-built facts."""
    body = "".join(f"<{k}>{v}</{k}>" for k, v in extra.items())
    return comicinfo_facts([
        f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>"
        f"{body}</ComicInfo>"
    ])


def test_different_comicinfo_languages_refuse_the_family():
    """PR #119 review, finding 3 (Codex probe ``language``): en vs fr."""
    results = _resolve(
        _archive("Series ch. 1 v1.cbz", info=_ci(LanguageISO="en"), pages=_pages("p1")),
        _archive("Series ch. 1 v2.cbz", info=_ci(LanguageISO="fr"), pages=_pages("p1")),
    )
    for result in results.values():
        assert result.family.status == fr.FAMILY_REFUSED
        assert any("different languages" in r for r in result.family.reasons)
        assert not result.fields["version"].resolved
        assert not result.renamable


def test_a_language_against_an_absent_language_refuses_the_family():
    results = _resolve(
        _archive("Series ch. 1 v1.cbz", info=_ci(LanguageISO="en"), pages=_pages("p1")),
        _archive("Series ch. 1 v2.cbz", info=_ci(), pages=_pages("p1")),
    )
    assert results["Series ch. 1 v2.cbz"].family.status == fr.FAMILY_REFUSED


def test_the_same_language_is_a_family_control():
    results = _resolve(
        _archive("Series ch. 1 v1.cbz", info=_ci(LanguageISO="en"), pages=_pages("p1")),
        _archive("Series ch. 1 v2.cbz", info=_ci(LanguageISO="EN"), pages=_pages("p1")),
    )
    for result in results.values():
        assert result.family.established
        assert result.renamable


def test_a_supported_edition_on_one_member_refuses_the_family():
    """PR #119 review, finding 3 (Codex probe ``supported_edition_ignored``)."""
    results = _resolve(
        _archive("Series ch. 1 (Alternate Cover) [v1].cbz", info=_ci(), pages=_pages("p1")),
        _archive("Series ch. 1 [v2].cbz", info=_ci(), pages=_pages("p1")),
    )
    assert parse_name_tokens("Series ch. 1 (Alternate Cover) [v1].cbz").editions == {
        "Alternate Cover"
    }
    for result in results.values():
        assert result.family.status == fr.FAMILY_REFUSED
        assert any("different editions" in r for r in result.family.reasons)
        assert not result.renamable


def test_colored_and_colorized_are_not_merged():
    """Their equivalence is undecided, so they are different editions."""
    results = _resolve(
        _archive("Series ch. 1 (Colored) v1.cbz", info=_ci(), pages=_pages("p1")),
        _archive("Series ch. 1 (Colorized) v2.cbz", info=_ci(), pages=_pages("p1")),
    )
    for result in results.values():
        assert result.family.status == fr.FAMILY_REFUSED


def test_the_edition_vocabulary_is_the_formatters():
    from scripts.cbz_canonical_name import EDITION_WORDS

    assert fr.EDITION_VOCABULARY == EDITION_WORDS
    for word in EDITION_WORDS:
        assert parse_name_tokens(f"S ch. 1 ({word.upper()}).cbz").editions == {word}


def test_an_unrecognised_variant_tag_refuses_rather_than_reading_as_absent():
    """A tag outside the vocabulary may be an unsupported variant."""
    results = _resolve(
        _archive("Series ch. 1 (Coloured) v1.cbz", info=_ci(), pages=_pages("p1")),
        _archive("Series ch. 1 v2.cbz", info=_ci(), pages=_pages("p1")),
    )
    for result in results.values():
        assert result.family.status == fr.FAMILY_REFUSED
        assert any("unrecognised tags" in r for r in result.family.reasons)

    # Control: the same tag on every member is no difference.
    results = _resolve(
        _archive("Series ch. 1 [Judas] v1.cbz", info=_ci(), pages=_pages("p1")),
        _archive("Series ch. 1 [Judas] v2.cbz", info=_ci(), pages=_pages("p1")),
    )
    assert all(r.family.established for r in results.values())


def test_different_group_tags_refuse_the_family_for_review():
    """Codex probe ``different_group_tags``: the confirmed conservative rule.

    ``[A]`` vs ``[B]`` may be scanlator tags rather than variants; refusing
    is an accepted false negative, and the tags stay in the reason.
    """
    names = ("Series ch. 1 [A] v1.cbz", "Series ch. 1 [B] v2.cbz")
    results = _resolve(*(_archive(n, info=_ci(), pages=_pages("p1")) for n in names))
    for result in results.values():
        assert result.family.status == fr.FAMILY_REFUSED
        assert any("['a'], ['b']" in r for r in result.family.reasons)


@pytest.mark.parametrize("marker", ["Omnibus", "Compilation", "Special", "One-shot"])
def test_a_special_marker_on_one_member_refuses_the_family(marker):
    """PR #119 round 2, finding 1 (Codex probe ``known_special``).

    Same ComicInfo Series and Number on both, comparable pages, version
    markers: only the formatter's special slot tells an Omnibus #1 from an
    ordinary #1. The marker must survive ComicInfo resolving the series to
    plain "Series", and refuse the family.
    """
    names = (f"Series {marker} #001 [v1].cbz", "Series #001 [v2].cbz")
    tokens = parse_name_tokens(names[0])
    assert tokens.specials == {marker}
    assert tokens.series == "Series" and tokens.other_tags == set()

    results = _resolve(*(_archive(n, info=_ci(), pages=_pages("p1")) for n in names))
    for result in results.values():
        assert result.family.status == fr.FAMILY_REFUSED
        assert any("special/collection markers" in r for r in result.family.reasons)
        assert not result.fields["version"].resolved
        assert not result.renamable


@pytest.mark.parametrize("marker", ["Omnibus", "One-shot"])
def test_matching_special_markers_are_a_family_control(marker):
    names = (f"Series {marker} #001 [v1].cbz", f"Series {marker.upper()} #001 [v2].cbz")
    results = _resolve(*(_archive(n, info=_ci(), pages=_pages("p1")) for n in names))
    for result in results.values():
        assert result.family.established
        assert result.renamable


def test_two_special_markers_on_one_member_are_unresolved():
    """The tokenizer has one slot, so only a caller-built member can carry
    two markers; `establish_family` must still refuse it."""
    def with_specials(member, specials):
        return FamilyMember(**{**member.__dict__, "specials": frozenset(specials)})

    verdict = establish_family([
        with_specials(_member("a"), {"Omnibus", "Special"}),
        with_specials(_member("b"), {"Omnibus", "Special"}),
    ])
    assert verdict.status == fr.FAMILY_REFUSED
    assert any("more than one special" in r for r in verdict.reasons)


@pytest.mark.parametrize(
    "filename, series",
    [
        ("Extra Life ch. 1 [v1].cbz", "Extra Life"),
        ("The Special Ones #003.cbz", "The Special Ones"),
        ("Bonus Track ch. 2.cbz", "Bonus Track"),
        ("Complete Idiot Guide v02.cbz", "Complete Idiot Guide"),
        ("Extra ch. 1.cbz", "Extra"),
    ],
)
def test_a_special_word_outside_the_slot_is_series_text(filename, series):
    """Lead probe: a special word is a marker only in #118's slot -- the
    last word of the pre-number prefix with series text before it."""
    tokens = parse_name_tokens(filename)
    assert tokens.series == series
    assert tokens.specials == frozenset()


def test_the_slot_still_recognises_a_trailing_special():
    tokens = parse_name_tokens("Series Omnibus Special #001.cbz")
    assert (tokens.series, tokens.specials) == ("Series Omnibus", {"Special"})
    assert parse_name_tokens("Series One-shot.cbz").specials == {"One-shot"}


def test_a_bare_edition_phrase_does_not_hide_the_special_slot():
    """The canonical order is series, special, numbering, then edition; a
    bare edition phrase before the number is blanked so it neither joins
    the series text nor takes the slot from a real special."""
    tokens = parse_name_tokens("Series Omnibus Special Edition #001.cbz")
    assert tokens.editions == {"Special Edition"}
    assert (tokens.series, tokens.specials) == ("Series", {"Omnibus"})
    plain = parse_name_tokens("Series Special Edition #001.cbz")
    assert (plain.series, plain.specials) == ("Series", frozenset())


def test_a_decided_series_ending_in_the_word_keeps_it_as_series_text():
    """Lead point 2: in the series "Super Special", ``Super Special #001``
    is issue #1, not a Special. Its version sibling here is named without
    the word (``Super #001 [v2]``), so the slot word would differ between
    the members; because the decided series ends with it, it is series
    text and the family is not refused over it."""
    names = ("Super Special #001 [v1].cbz", "Super #001 [v2].cbz")
    assert parse_name_tokens(names[0]).specials == {"Special"}
    results = _resolve(
        *(_archive(n, info=_ci(series="Super Special"), pages=_pages("p1")) for n in names),
    )
    for result in results.values():
        assert result.fields["series"].value == "Super Special"
        assert result.family.established
        assert result.renamable
    assert fr.effective_specials(frozenset({"Special"}), "Super Special") == frozenset()
    assert fr.effective_specials(frozenset({"Omnibus"}), "Series") == {"Omnibus"}
    assert fr.effective_specials(frozenset({"Omnibus"}), None) == {"Omnibus"}


def test_special_edition_is_an_edition_not_the_special_word():
    tokens = parse_name_tokens("Series #001 (Special Edition) [v1].cbz")
    assert tokens.editions == {"Special Edition"}
    assert tokens.specials == frozenset()


def test_the_special_vocabulary_is_the_formatters():
    from scripts.cbz_canonical_name import SPECIAL_WORDS

    assert fr.SPECIAL_VOCABULARY == SPECIAL_WORDS
    for word in SPECIAL_WORDS:
        assert parse_name_tokens(f"S {word.lower()} #1.cbz").specials == {word}


def test_decensor_pairs_are_never_versions():
    """§7 named test's family half (the "survive rename" half is step 7)."""
    for marker in ("Uncensored", "Decensored"):
        verdict = establish_family([
            _member("a", editions=()),
            _member("b", editions=(marker,)),
        ])
        assert verdict.status == fr.FAMILY_REFUSED
        assert any("decensor pair" in r for r in verdict.reasons)
    results = _resolve(
        _archive("Series ch. 1 v1.cbz", info=_ci(), pages=_pages("p1")),
        _archive("Series ch. 1 v2 (Uncensored).cbz", info=_ci(), pages=_pages("p1")),
    )
    assert any("decensor pair" in r for r in results["Series ch. 1 v1.cbz"].family.reasons)


# --- human conflicts persist (PR #119 review, finding 2) ---------------------

_PAIR = ("Series ch. 1 v1.cbz", "Series ch. 1 v2.cbz")


def _pair(decisions):
    return _resolve(
        *(_archive(n, info=_ci(), pages=_pages("p1")) for n in _PAIR),
        decisions=StaticHumanDecisions(decisions),
    )


def test_conflicting_human_family_decisions_refuse_the_family():
    """Codex probe ``conflicting_human_families``: A and B for one member.

    The metadata alone would establish the automatic family. The conflict
    must refuse it, not decay to "no human decision".
    """
    results = _pair({_PAIR[0]: [
        HumanDecision("family", "A", "receipt-A"),
        HumanDecision("family", "B", "receipt-B"),
    ]})
    for name in _PAIR:
        result = results[name]
        assert result.family.status == fr.FAMILY_REFUSED
        assert any("conflicting human decisions" in r for r in result.family.reasons)
        assert not result.fields["version"].resolved
        assert not result.renamable
    assert "conflicting human decisions on family" in results[_PAIR[0]].review


def test_a_conflicted_member_refuses_every_family_it_was_declared_into():
    """Family A has two agreeing members; a third is declared into A and B.

    Its series and number are unresolved, so it has no automatic work
    group to fall back into. Family A must still be refused because of it,
    not established without it.
    """
    names = ("Series ch. 1 v1.cbz", "Series ch. 1 v2.cbz", "loose.cbz")
    results = _resolve(
        *(_archive(n, info=_ci(), pages=_pages("p1")) for n in names[:2]),
        _archive(names[2], pages=_pages("p1")),
        decisions=StaticHumanDecisions({
            names[0]: [HumanDecision("family", "A", "r1")],
            names[1]: [HumanDecision("family", "A", "r2")],
            names[2]: [
                HumanDecision("family", "A", "r3"),
                HumanDecision("family", "B", "r4"),
            ],
        }),
    )
    for name in names:
        assert results[name].family is not None, name
        assert results[name].family.status == fr.FAMILY_REFUSED, name
        assert not results[name].renamable, name


def test_repeated_agreeing_family_decisions_still_declare_the_family():
    results = _pair({
        _PAIR[0]: [HumanDecision("family", "A", "r1"), HumanDecision("family", "A", "r2")],
        _PAIR[1]: [HumanDecision("family", "A", "r3")],
    })
    assert all(r.family.established and r.renamable for r in results.values())


def test_conflicting_human_volume_decisions_never_classify_a_token():
    """Codex probe ``human_volume_conflict``: volume 1 and 2 for v1."""
    results = _pair({_PAIR[0]: [
        HumanDecision("volume", "1", "rec1"),
        HumanDecision("volume", "2", "rec2"),
    ]})
    first = results[_PAIR[0]]
    assert first.fields["volume"].reason == "human_conflict"
    assert _v(first, "1").classification == fr.V_UNRESOLVED
    assert "conflicting human decisions on volume" in first.review
    assert not first.renamable
    assert first.family.status == fr.FAMILY_REFUSED
    assert not results[_PAIR[1]].family.established


def test_conflicting_human_version_decisions_never_classify_a_token():
    results = _pair({_PAIR[0]: [
        HumanDecision("version", "1", "rec1"),
        HumanDecision("version", "3", "rec2"),
    ]})
    first = results[_PAIR[0]]
    assert _v(first, "1").classification == fr.V_UNRESOLVED
    assert not first.renamable
    assert first.family.status == fr.FAMILY_REFUSED


def test_repeated_agreeing_volume_decisions_still_classify():
    results = _pair({_PAIR[0]: [
        HumanDecision("volume", "1", "rec1"),
        HumanDecision("volume", "01", "rec2"),
    ]})
    first = results[_PAIR[0]]
    assert _v(first, "1").classification == fr.V_VOLUME
    assert first.fields["volume"].source == fr.SOURCE_HUMAN
    assert first.renamable


def test_a_conflict_on_any_field_blocks_readiness():
    results = _resolve(
        _archive("Berserk ch. 1.cbz", info=_ci(series="Berserk", number="1")),
        decisions=StaticHumanDecisions({"Berserk ch. 1.cbz": [
            HumanDecision("year", "1990", "r1"),
            HumanDecision("year", "1991", "r2"),
        ]}),
    )
    result = results["Berserk ch. 1.cbz"]
    assert result.fields["series"].resolved and result.fields["number"].resolved
    assert "conflicting human decisions on year" in result.review
    assert not result.renamable


# --- display spelling ----------------------------------------------------------


def test_the_display_spelling_travels_with_the_comparison_key():
    """``12.50`` compares equal to ``12.5`` but is displayed as written."""
    results = _resolve(
        _archive("S ch. 012.50.cbz", info=_ci(series="S", number="12.50")),
        _archive("S ch. 13.cbz"),
        series_folder="S",
    )
    number = results["S ch. 012.50.cbz"].fields["number"]
    assert number.value == "12.5"
    assert number.source == fr.SOURCE_COMICINFO
    assert number.display_value == "12.50"
    assert {e.display_value for e in number.evidence} == {"12.50", "012.50"}

    folder_only = _resolve(
        _archive("S ch. 012.50.cbz"), _archive("S ch. 13.cbz"), series_folder="S",
    )["S ch. 012.50.cbz"].fields["number"]
    assert folder_only.source == fr.SOURCE_FOLDER
    assert folder_only.display_value == "012.50"


def test_equal_valued_volume_and_chapter_keep_their_own_spellings():
    """PR #119 round 2, finding 2 (Codex probe ``same_numeric_value...``).

    ``Vol. 1.00`` and ``ch. 1.0`` compare equal; each field must still get
    its own token's spelling, in the evidence, in the serialised output,
    and in what #118's formatter emits.
    """
    from scripts.cbz_canonical_name import CanonicalFields, format_canonical_name

    names = ("S Vol. 1.00 ch. 1.0.cbz", "S Vol. 2 ch. 2.00.cbz")
    results = _resolve(
        *(_archive(n, pages=_pages(str(i))) for i, n in enumerate(names)),
        series_folder="S",
    )
    expected = {
        names[0]: ("1", "1.00", "1", "1.0", "S v01.00 ch. 1.0.cbz"),
        names[1]: ("2", "2", "2", "2.00", "S v02 ch. 2.00.cbz"),
    }
    for name, (vol, vol_raw, num, num_raw, formatted) in expected.items():
        result = results[name]
        assert result.renamable
        volume, number = result.fields["volume"], result.fields["number"]
        assert (volume.value, volume.display_value) == (vol, vol_raw)
        assert (number.value, number.display_value) == (num, num_raw)
        data = result.as_dict()["fields"]
        assert data["volume"]["display_value"] == vol_raw
        assert {e["display"] for e in data["volume"]["evidence"]} == {vol_raw}
        assert data["number"]["display_value"] == num_raw
        assert {e["display"] for e in data["number"]["evidence"]} == {num_raw}
        assert format_canonical_name(CanonicalFields(
            series=result.fields["series"].value,
            volume=volume.display_value,
            chapter=number.display_value,
        )) == formatted


def test_overlap_without_family_evidence_is_never_compared():
    """§7 named. Identical pages, no version marker: refused, not compared.

    The members are even page-identical. Without a version marker or a
    human decision there is no family, and so nothing for step 5 to
    compare.
    """
    same = _pages("p1", "p2")
    verdict = establish_family([
        _member("a", marked=False, pages=same),
        _member("b", marked=False, pages=same),
    ])
    assert verdict.status == fr.FAMILY_REFUSED
    assert "no member carries a version marker" in verdict.reasons


def test_family_requirements_are_each_load_bearing():
    assert establish_family([_member("a"), _member("b")]).established
    assert not establish_family([_member("a")]).established
    assert not establish_family([_member("a"), _member("b", series="Other")]).established
    assert not establish_family([_member("a"), _member("b", number="2")]).established
    assert not establish_family(
        [_member("a", volume="1"), _member("b", volume="2")]
    ).established
    unresolved = _member("b")
    unresolved = FamilyMember(
        **{**unresolved.__dict__,
           "number": FieldResolution("number", None, None, (), "conflict")}
    )
    assert not establish_family([_member("a"), unresolved]).established


def test_an_empty_series_key_is_never_the_same_decided_series():
    """"Uncensored" and "Decensored" both key to "": no identity, no family."""
    verdict = establish_family([
        _member("a", series="Uncensored", number="3"),
        _member("b", series="Decensored", number="3", marked=False),
    ])
    assert verdict.status == fr.FAMILY_REFUSED
    assert "members' series key is empty (no identity)" in verdict.reasons


def test_members_with_unresolved_numbers_are_refused():
    """All-None numbers must not pass as "same number None"."""
    def unresolved(member: FamilyMember) -> FamilyMember:
        return FamilyMember(**{
            **member.__dict__,
            "number": FieldResolution("number", None, None, (), "conflict"),
        })

    verdict = establish_family([unresolved(_member("a")), unresolved(_member("b"))])
    assert verdict.status == fr.FAMILY_REFUSED
    assert "a member's primary number is unresolved" in verdict.reasons


def test_a_human_decision_can_declare_a_family():
    verdict = establish_family([
        _member("a", marked=False, number="1", family_decision="fam-1"),
        _member("b", marked=False, number="9", family_decision="fam-1"),
    ])
    assert verdict.established


# --- renamability and review ------------------------------------------------


def test_unresolved_series_or_number_is_not_renamable_and_carries_evidence():
    result = _resolve(
        _archive("Berserk ch. 3.cbz", info=_info(Series="Berserk")),
    )["Berserk ch. 3.cbz"]
    assert result.fields["series"].resolved
    assert not result.fields["number"].resolved
    assert not result.renamable
    assert any("primary number unresolved" in r for r in result.review)
    data = result.as_dict()
    assert data["fields"]["number"]["evidence"] == [
        {"source": "filename", "value": "3", "display": "3",
         "detail": "filename chapter"}
    ]


def test_no_primary_number_from_any_source_is_not_renamable():
    result = _resolve(
        _archive("Oneshot.cbz", info=_info(Series="Oneshot")),
    )["Oneshot.cbz"]
    assert not result.renamable
    assert "no primary number from any source" in result.review


def test_bracketed_group_tags_do_not_disturb_resolution():
    """Resolver side of §5.5 (the named watcher test is step 3).

    A ``[Group]`` tag, even one with digits, is neither series nor number.
    """
    results = _resolve(
        _archive("Berserk [Judas 2021] ch. 1.cbz"),
        _archive("Berserk [Judas 2021] ch. 2.cbz"),
        series_folder="Berserk",
    )
    tokens = parse_name_tokens("Berserk [Judas 2021] ch. 1.cbz")
    assert tokens.series == "Berserk" and tokens.chapters == ("1",)
    assert results["Berserk [Judas 2021] ch. 1.cbz"].fields["number"].value == "1"
    assert results["Berserk [Judas 2021] ch. 1.cbz"].renamable


# --- read-only ---------------------------------------------------------------


def _cbz(path: Path, pages: dict[str, bytes], comicinfo: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in pages.items():
            archive.writestr(name, data)
        if comicinfo is not None:
            archive.writestr("ComicInfo.xml", comicinfo)
    return path


def _tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_resolution_from_disk_writes_nothing(tmp_path: Path):
    """The whole disk path -- listing, ComicInfo reads, resolution -- is read-only.

    Includes an archive with no ComicInfo, which is exactly where the
    watcher would inject one; this path must not.
    """
    folder = tmp_path / "Berserk"
    _cbz(folder / "Berserk ch. 1.cbz", {"001.png": b"a"},
         "<ComicInfo><Series>Berserk</Series><Number>1</Number></ComicInfo>")
    _cbz(folder / "Berserk ch. 2.cbz", {"001.png": b"b"})
    before = _tree_state(tmp_path)
    listing_before = sorted(os.listdir(folder))

    facts = folder_facts_from_directory(folder, series_folder="Berserk")
    results = resolve_folder(facts).by_key()

    assert _tree_state(tmp_path) == before
    assert sorted(os.listdir(folder)) == listing_before
    first = results[str(folder / "Berserk ch. 1.cbz")]
    assert first.comicinfo.status == fr.COMICINFO_USABLE
    assert first.fields["number"].source == fr.SOURCE_COMICINFO
    assert results[str(folder / "Berserk ch. 2.cbz")].comicinfo.status == fr.COMICINFO_ABSENT


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    with database_connection(path) as connection:
        apply_migrations(connection, MIGRATIONS)
    return path


def _seed_pages(database: Path, archive: Path, *, legacy: bool = False) -> int:
    """Store real page hashes for ``archive`` the way the page-hash job does."""
    result = calculate_page_hashes(archive)
    with database_connection(database) as connection:
        archive_id = int(connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)",
            (archive.stat().st_size,),
        ).lastrowid)
        ArchivePageHashRepository(connection).save(
            archive_id=archive_id, location_id=None, result=result
        )
        if legacy:
            connection.execute(
                "UPDATE archive_content_signatures SET algorithm_version = '1' "
                "WHERE archive_id = ?",
                (archive_id,),
            )
            connection.commit()
    return archive_id


def test_load_page_evidence_reads_through_the_guarded_snapshot(database, tmp_path):
    current = _cbz(tmp_path / "a.cbz", {"002.png": b"two", "001.png": b"one"})
    legacy = _cbz(tmp_path / "b.cbz", {"001.png": b"one"})
    current_id = _seed_pages(database, current)
    legacy_id = _seed_pages(database, legacy, legacy=True)
    db_before = hashlib.sha256(database.read_bytes()).hexdigest()

    snapshot = load_page_evidence(database, [current_id, legacy_id, 999])

    assert hashlib.sha256(database.read_bytes()).hexdigest() == db_before
    assert snapshot.data_version_before == snapshot.data_version_after
    evidence = snapshot.result
    assert set(evidence) == {current_id, legacy_id}
    assert evidence[current_id].comparable
    assert evidence[current_id].page_digests == (
        hashlib.sha256(b"one").hexdigest(),
        hashlib.sha256(b"two").hexdigest(),
    )
    assert evidence[current_id] == PageEvidence.from_page_hashes(
        calculate_page_hashes(current)
    )
    assert not evidence[legacy_id].comparable
    assert "legacy ordering" in evidence[legacy_id].incomparable_reason


def test_stored_duplicate_names_or_missing_digests_are_not_comparable(
    database, tmp_path
):
    """The stored forms of an unresolved order and an incomplete inventory.

    Two image members with one raw name are what `comparable_ordered_signature_sql`
    excludes in SQL; a page row without its exact digest is an incomplete
    inventory (§4.1). Either must make the stored sequence non-comparable.
    """
    duplicate = tmp_path / "dup.cbz"
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("001.png", b"one")
            archive.writestr("001.png", b"other")
    duplicate_id = _seed_pages(database, duplicate)

    missing_id = _seed_pages(
        database, _cbz(tmp_path / "m.cbz", {"1.png": b"a", "2.png": b"b"})
    )
    with database_connection(database) as connection:
        connection.execute(
            "DELETE FROM page_hashes WHERE page_id = (SELECT id FROM "
            "archive_pages WHERE archive_id = ? AND page_index = 1)",
            (missing_id,),
        )
        connection.commit()

    evidence = load_page_evidence(database, [duplicate_id, missing_id]).result
    assert not evidence[duplicate_id].comparable
    assert not evidence[missing_id].comparable


def test_an_empty_digest_is_incomplete_not_a_shorter_sequence():
    """PR #119 review, finding 4 (Codex probe ``missing_digest``)."""
    evidence = _pages("", "p1")
    assert not evidence.comparable
    assert "empty or missing digest" in evidence.incomparable_reason
    verdict = establish_family([_member("a", pages=evidence), _member("b")])
    assert verdict.status == fr.FAMILY_INCOMPARABLE
    # Control: the same sequence with every digest present is comparable.
    assert _pages("p0", "p1").comparable
    assert establish_family([_member("a"), _member("b")]).established


def _mutate(database: Path, sql: str, params: tuple) -> None:
    with database_connection(database) as connection:
        connection.execute(sql, params)
        connection.commit()


def test_a_stored_empty_digest_keeps_its_slot_and_is_refused(database, tmp_path):
    """Codex probe ``stored_empty_digest``: two pages, one digest set to ''."""
    archive_id = _seed_pages(
        database, _cbz(tmp_path / "book.cbz", {"1.png": b"a", "2.png": b"b"})
    )
    control = load_page_evidence(database, [archive_id]).result[archive_id]
    assert control.comparable and len(control.page_digests) == 2

    _mutate(
        database,
        "UPDATE page_hashes SET digest = '' WHERE page_id = (SELECT id FROM "
        "archive_pages WHERE archive_id = ? AND page_index = 0)",
        (archive_id,),
    )
    loaded = load_page_evidence(database, [archive_id]).result[archive_id]
    assert len(loaded.page_digests) == 2          # never silently shortened
    assert loaded.page_digests[0] == ""
    assert not loaded.comparable


def test_a_missing_page_row_does_not_pass_as_complete(database, tmp_path):
    """The signature records the page count; a deleted row must show."""
    archive_id = _seed_pages(
        database,
        _cbz(tmp_path / "book.cbz", {"1.png": b"a", "2.png": b"b", "3.png": b"c"}),
    )
    _mutate(
        database,
        "DELETE FROM archive_pages WHERE archive_id = ? AND page_index = 2",
        (archive_id,),
    )
    loaded = load_page_evidence(database, [archive_id]).result[archive_id]
    assert loaded.inventory_problem == "2 page rows but the signature records 3 pages"
    assert not loaded.comparable


def test_non_contiguous_page_indexes_are_incomplete(database, tmp_path):
    archive_id = _seed_pages(
        database, _cbz(tmp_path / "book.cbz", {"1.png": b"a", "2.png": b"b"})
    )
    _mutate(
        database,
        "UPDATE archive_pages SET page_index = 5 WHERE archive_id = ? "
        "AND page_index = 1",
        (archive_id,),
    )
    loaded = load_page_evidence(database, [archive_id]).result[archive_id]
    assert loaded.inventory_problem == "page indexes are not contiguous from 0"
    assert not loaded.comparable


def test_load_page_evidence_rejects_a_concurrent_commit(database, tmp_path):
    """A commit from another connection inside the read window rejects it."""
    archive_id = _seed_pages(database, _cbz(tmp_path / "a.cbz", {"1.png": b"x"}))

    def commit_then_check(connection: sqlite3.Connection) -> str:
        with database_connection(database) as writer:
            writer.execute("INSERT INTO archive_files (file_size) VALUES (1)")
            writer.commit()
        return quick_check(connection)

    with pytest.raises(DatabaseChangedError):
        load_page_evidence(database, [archive_id], integrity_check=commit_then_check)


def test_the_resolver_opens_no_database_connection_of_its_own():
    """No sqlite3.connect in the module: its only DB path is the DAL's."""
    source = Path(fr.__file__).read_text(encoding="utf-8")
    assert "sqlite3.connect" not in source
    assert "open_connection" not in source
