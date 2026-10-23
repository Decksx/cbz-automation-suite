"""Re-derivation and supersession evaluator (design §4.1, §4.3-§4.6; step 5).

`comic_automation.archive.supersession` binds every archive to the sha256
of the bytes it evaluated, re-derives the page sequence from those bytes,
refuses stored rows that disagree, and within step 4's *established*
families classifies relations, chains and the keeper's label.

Almost every test here goes through the entry point, `evaluate_folder`,
with real CBZ files written to ``tmp_path`` and a real migrated database
whose rows are stored by the production page-hash repository. The §7 named
tests for this step carry their design names; tests whose named
counterpart is shared with step 4 say so.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from comic_automation.archive import archive_limits
from comic_automation.archive import supersession as sv
from comic_automation.archive.field_resolution import (
    HumanDecision,
    PageEvidence,
    StaticHumanDecisions,
)
from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
    hash_ordered_pages,
)
from comic_automation.archive.supersession import (
    InconsistentRelationsError,
    Relation,
    derive_archive,
    evaluate_folder,
    greedy_index_map,
    resolve_chain,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from comic_automation.database.read_guards import DatabaseChangedError, quick_check


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


# --- builders ----------------------------------------------------------------


def _ci(series: str = "Series", number: str = "1", notes: str = "") -> bytes:
    note = f"<Notes>{notes}</Notes>" if notes else ""
    return (
        f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>"
        f"{note}</ComicInfo>"
    ).encode()


def _book(
    folder: Path,
    name: str,
    pages: list[bytes],
    extra: dict[str, bytes] | None = None,
) -> Path:
    """A real CBZ: pages as 001.png, 002.png, ... plus any extra members."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, data in enumerate(pages, start=1):
            archive.writestr(f"{index:03d}.png", data)
        for member, data in (extra or {}).items():
            archive.writestr(member, data)
    return path


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "test.db"
    path.parent.mkdir()
    with database_connection(path) as connection:
        apply_migrations(connection, MIGRATIONS)
    return path


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "library" / "Series"
    path.mkdir(parents=True)
    return path


def _seed(database: Path, archive: Path) -> int:
    """Store real page rows for ``archive`` the way the page-hash job does."""
    result = calculate_page_hashes(archive)
    with database_connection(database) as connection:
        archive_id = int(connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)",
            (archive.stat().st_size,),
        ).lastrowid)
        ArchivePageHashRepository(connection).save(
            archive_id=archive_id, location_id=None, result=result
        )
    return archive_id


def _seed_all(database: Path, folder: Path) -> dict[str, int]:
    return {path.name: _seed(database, path) for path in sorted(folder.glob("*.cbz"))}


def _mutate(database: Path, sql: str, params: tuple = ()) -> None:
    with database_connection(database) as connection:
        connection.execute(sql, params)
        connection.commit()


def _evaluate(folder: Path, database: Path, ids: dict[str, int] | None = None, **kw):
    if ids is None:
        ids = _seed_all(database, folder)
    kw.setdefault("series_folder", "Series")
    return evaluate_folder(folder, database=database, archive_ids=ids, **kw)


def _key(folder: Path, name: str) -> str:
    return str(folder / name)


def _family(result, folder: Path, name: str) -> sv.FamilySupersession:
    families = result.families_of(_key(folder, name))
    assert len(families) == 1, families
    return families[0]


def _archive(result, folder: Path, name: str) -> sv.ArchiveEvaluation:
    return next(a for a in result.archives if a.archive_key == _key(folder, name))


def _pointers(family) -> dict[str, tuple[str, tuple[int, ...] | None]]:
    return {
        Path(p.older).name: (Path(p.newer).name, p.index_map) for p in family.pointers
    }


P1, P2, P3, P4 = b"page-one", b"page-two", b"page-three", b"page-four"


# --- §4.4 strict prefix --------------------------------------------------------


def test_strict_prefix_is_superseded_by_the_longer_version(folder, database):
    """§7 named. v1's pages are a strict prefix of v2's: v1 is superseded.

    Also pins the strong identity: each member's sha256 is the sha256 of the
    file's bytes, and its sequence is exactly what the production page
    hasher derives from the same file.
    """
    v1 = _book(folder, "Series ch. 1 v1.cbz", [P1, P2])
    v2 = _book(folder, "Series ch. 1 v2.cbz", [P1, P2, P3])

    result = _evaluate(folder, database)
    family = _family(result, folder, v1.name)

    assert family.status == sv.FAMILY_SUPERSESSION
    assert family.reasons == ()
    assert family.keeper == str(v2)
    assert _pointers(family) == {v1.name: (v2.name, (0, 1))}
    (relation,) = family.relations
    assert (relation.relation, relation.outcome) == (sv.REL_PREFIX, sv.OUTCOME_SUPERSEDED)
    for path in (v1, v2):
        member = _archive(result, folder, path.name)
        assert member.comparable and member.stored_rows == sv.STORED_AGREE
        assert member.derived.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert member.derived.page_evidence() == PageEvidence.from_page_hashes(
            calculate_page_hashes(path)
        )
    assert family.label.status == sv.LABEL_RESOLVED
    assert family.label.value == "2"


def test_chain_removal_candidates_point_at_the_final_keeper(folder, database):
    """§7 named. v1 < v2 < v3: both v1 and v2 point at v3, never v1 at v2."""
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    _book(folder, "Series ch. 1 v3.cbz", [P1, P2, P3])

    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")

    assert family.status == sv.FAMILY_SUPERSESSION
    assert Path(family.keeper).name == "Series ch. 1 v3.cbz"
    assert _pointers(family) == {
        "Series ch. 1 v1.cbz": ("Series ch. 1 v3.cbz", (0,)),
        "Series ch. 1 v2.cbz": ("Series ch. 1 v3.cbz", (0, 1)),
    }
    assert family.label.value == "3"


def test_a_forked_version_family_goes_to_review(folder, database):
    """§7 named. v2 and v3 both extend v1 differently: two maximal members."""
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    _book(folder, "Series ch. 1 v3.cbz", [P1, P3])

    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")

    assert family.status == sv.FAMILY_REVIEW
    assert family.keeper is None and family.pointers == () and family.label is None
    assert any("no unique keeper" in r for r in family.reasons)
    assert any("v2.cbz', '" in r and "v3.cbz'" in r for r in family.reasons)


# --- §4.4 inserted pages, reorder, change -------------------------------------


def test_inserted_pages_are_always_held_for_review(folder, database):
    """An ordered subsequence that is not a prefix: proposed, never actionable.

    David's decision on §8 question 1: "save it for me to look at". The
    keeper and pointer are shown, with the index map, but the family is
    review, not supersession.
    """
    _book(folder, "Series ch. 1 v1.cbz", [P1, P2])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P3, P2])

    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")

    assert family.status == sv.FAMILY_REVIEW
    (relation,) = family.relations
    assert relation.relation == sv.REL_SUBSEQUENCE
    assert relation.outcome == sv.OUTCOME_INSERTED_PAGES
    assert relation.index_map == (0, 2)
    assert Path(family.keeper).name == "Series ch. 1 v2.cbz"
    assert any("inserted pages" in r for r in family.reasons)


def test_ordered_subsequence_preserves_duplicate_page_multiplicity(tmp_path, database):
    """§7 named. A page repeated in A must be repeated in B as often.

    ``[P1, P1, P2]`` is inside ``[P1, P3, P1, P2]`` (map 0, 2, 3), but not
    inside ``[P1, P3, P2]``, which holds P1 only once: a set comparison
    would call both subsequences.
    """
    inside = tmp_path / "inside" / "Series"
    _book(inside, "Series ch. 1 v1.cbz", [P1, P1, P2])
    _book(inside, "Series ch. 1 v2.cbz", [P1, P3, P1, P2])
    family = _family(_evaluate(inside, database), inside, "Series ch. 1 v1.cbz")
    (relation,) = family.relations
    assert relation.outcome == sv.OUTCOME_INSERTED_PAGES
    assert relation.index_map == (0, 2, 3)

    missing = tmp_path / "missing" / "Series"
    _book(missing, "Series ch. 1 v1.cbz", [P1, P1, P2])
    _book(missing, "Series ch. 1 v2.cbz", [P1, P3, P2, P4])
    family = _family(_evaluate(missing, database), missing, "Series ch. 1 v1.cbz")
    (relation,) = family.relations
    assert (relation.relation, relation.outcome) == (sv.REL_OTHER, sv.OUTCOME_REVIEW)
    assert family.status == sv.FAMILY_REVIEW and family.keeper is None


@pytest.mark.parametrize(
    "older, newer",
    [
        ([P1, P2], [P2, P1, P3]),        # reordered, longer
        ([P1, P2], [P1, P4, P3]),        # changed page
        ([P1, P2], [P2, P1]),            # reordered, same length
        ([P1, P2, P3], [P1, P4]),        # pages removed and changed
    ],
    ids=["reordered", "changed", "reordered-same-length", "removed-and-changed"],
)
def test_reordered_or_changed_pages_go_to_review(folder, database, older, newer):
    _book(folder, "Series ch. 1 v1.cbz", older)
    _book(folder, "Series ch. 1 v2.cbz", newer)

    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")

    (relation,) = family.relations
    assert (relation.relation, relation.outcome) == (sv.REL_OTHER, sv.OUTCOME_REVIEW)
    assert relation.index_map is None
    assert family.status == sv.FAMILY_REVIEW
    assert family.keeper is None and family.pointers == ()


# --- §4.4 equal pages and duplicates ------------------------------------------


def test_equal_pages_with_different_non_image_members_go_to_review(folder, database):
    """§7 named. Same pages, different credits: review, listing both members."""
    _book(folder, "Series ch. 1 v1.cbz", [P1, P2], {"credits.txt": b"old credits"})
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2], {"credits.txt": b"new credits"})

    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")

    (relation,) = family.relations
    assert (relation.relation, relation.outcome) == (sv.REL_EQUAL_PAGES, sv.OUTCOME_REVIEW)
    differing = {(Path(d["archive"]).name, d["name"], d["sha256"])
                 for d in relation.differing_non_image}
    assert differing == {
        ("Series ch. 1 v1.cbz", "credits.txt", hashlib.sha256(b"old credits").hexdigest()),
        ("Series ch. 1 v2.cbz", "credits.txt", hashlib.sha256(b"new credits").hexdigest()),
    }
    assert family.status == sv.FAMILY_REVIEW and family.keeper is None


def test_equal_full_files_are_a_whole_archive_duplicate(folder, database):
    """Byte-identical files are a duplicate, never a supersession.

    At the top of a family they leave two maximal members, so there is no
    keeper (review, naming the duplicates). Below a keeper they are both
    superseded by it.
    """
    first = _book(folder, "Series ch. 1 v1.cbz", [P1, P2])
    (folder / "Series ch. 1 [v1].cbz").write_bytes(first.read_bytes())

    family = _family(_evaluate(folder, database), folder, first.name)
    (relation,) = family.relations
    assert (relation.relation, relation.outcome) == (sv.REL_DUPLICATE, sv.OUTCOME_DUPLICATE)
    assert family.status == sv.FAMILY_REVIEW and family.keeper is None
    assert any("whole-archive duplicates" in r for r in family.reasons)


def test_duplicates_below_the_keeper_both_point_at_it(tmp_path, database):
    folder = tmp_path / "dups" / "Series"
    first = _book(folder, "Series ch. 1 v1.cbz", [P1, P2])
    (folder / "Series ch. 1 [v1].cbz").write_bytes(first.read_bytes())
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2, P3])

    family = _family(_evaluate(folder, database), folder, first.name)

    assert family.status == sv.FAMILY_SUPERSESSION
    assert _pointers(family) == {
        "Series ch. 1 v1.cbz": ("Series ch. 1 v2.cbz", (0, 1)),
        "Series ch. 1 [v1].cbz": ("Series ch. 1 v2.cbz", (0, 1)),
    }


# --- §4.4 non-image payload ---------------------------------------------------


def test_a_unique_non_image_member_blocks_supersession(tmp_path, database):
    """§7 named. A prefix that would drop A's notes.txt is downgraded to review.

    Missing from B, and present in B with different bytes, both block. The
    control carries the identical member and supersedes.
    """
    cases = {
        "missing": {},
        "different": {"notes.txt": b"rewritten notes"},
        "control": {"notes.txt": b"translator notes"},
    }
    for case, newer_extra in cases.items():
        folder = tmp_path / case / "Series"
        _book(folder, "Series ch. 1 v1.cbz", [P1], {"notes.txt": b"translator notes"})
        _book(folder, "Series ch. 1 v2.cbz", [P1, P2], newer_extra)
        family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")
        (relation,) = family.relations
        if case == "control":
            assert relation.outcome == sv.OUTCOME_SUPERSEDED
            assert family.status == sv.FAMILY_SUPERSESSION
            continue
        assert relation.relation == sv.REL_PREFIX
        assert relation.outcome == sv.OUTCOME_REVIEW, case
        assert [m.name for m in relation.unique_non_image] == ["notes.txt"]
        assert family.status == sv.FAMILY_REVIEW and family.keeper is None


def test_dropping_comicinfo_needs_the_operator_flag(tmp_path, database):
    """A's own ComicInfo.xml blocks by default; accepted, it is listed, and
    only when the newer archive carries its own ComicInfo.xml.

    The flag covers ComicInfo.xml only: a unique notes.txt still blocks with
    the flag on.
    """
    def build(case: str, extra_older: dict[str, bytes]) -> Path:
        folder = tmp_path / case / "Series"
        _book(folder, "Series ch. 1 v1.cbz", [P1],
              {"ComicInfo.xml": _ci(notes="first scan"), **extra_older})
        _book(folder, "Series ch. 1 v2.cbz", [P1, P2],
              {"ComicInfo.xml": _ci(notes="second scan")})
        return folder

    folder = build("default", {})
    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")
    assert family.status == sv.FAMILY_REVIEW
    assert [m.name for m in family.relations[0].unique_non_image] == ["ComicInfo.xml"]

    folder = build("accepted", {})
    family = _family(
        _evaluate(folder, database, accept_comicinfo_drop=True), folder,
        "Series ch. 1 v1.cbz",
    )
    assert family.status == sv.FAMILY_SUPERSESSION
    (relation,) = family.relations
    assert [m.name for m in relation.accepted_comicinfo_drops] == ["ComicInfo.xml"]
    assert relation.unique_non_image == ()

    # §8 question 6: the flag drops A's ComicInfo only when B has its own.
    folder = tmp_path / "keeper-without-comicinfo" / "Series"
    _book(folder, "Series ch. 1 v1.cbz", [P1], {"ComicInfo.xml": _ci(notes="first scan")})
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    family = _family(
        _evaluate(folder, database, accept_comicinfo_drop=True), folder,
        "Series ch. 1 v1.cbz",
    )
    assert family.status == sv.FAMILY_REVIEW
    (relation,) = family.relations
    assert [m.name for m in relation.unique_non_image] == ["ComicInfo.xml"]
    assert relation.accepted_comicinfo_drops == ()

    folder = build("accepted-plus-notes", {"notes.txt": b"n"})
    family = _family(
        _evaluate(folder, database, accept_comicinfo_drop=True), folder,
        "Series ch. 1 v1.cbz",
    )
    assert family.status == sv.FAMILY_REVIEW
    assert [m.name for m in family.relations[0].unique_non_image] == ["notes.txt"]


# --- §4.5 cycles ---------------------------------------------------------------


def _edge(older: str, newer: str) -> Relation:
    return Relation(older, newer, sv.REL_PREFIX, sv.OUTCOME_SUPERSEDED, "test", (0,))


def test_a_cycle_in_the_relations_stops_the_run():
    """§4.5: a cycle is stale or corrupt inventory and raises, never reports."""
    with pytest.raises(InconsistentRelationsError, match="cycle"):
        resolve_chain(["a", "b", "c"], [_edge("a", "b"), _edge("b", "c"), _edge("c", "a")])
    # Control: the same members without the back edge have keeper c.
    chain = resolve_chain(["a", "b", "c"], [_edge("a", "b"), _edge("b", "c"), _edge("a", "c")])
    assert chain.keeper == "c"


def test_a_member_without_a_direct_relation_to_the_keeper_stops_the_run():
    with pytest.raises(InconsistentRelationsError, match="no direct"):
        resolve_chain(["a", "b", "c"], [_edge("a", "b"), _edge("b", "c")])


def test_a_cycle_from_corrupt_relations_stops_the_folder(folder, database, monkeypatch):
    """Entry point: a cyclic relation set raises out of `evaluate_folder`.

    Relations derived from bytes cannot cycle (every edge lengthens the
    sequence), so the corrupt inventory is simulated by replacing the pair
    classifier with one that reports a -> b -> c -> a.
    """
    for name in ("Series ch. 1 v1.cbz", "Series ch. 1 v2.cbz", "Series ch. 1 v3.cbz"):
        _book(folder, name, [P1, name.encode()])
    ids = _seed_all(database, folder)
    order = {"v1": "v2", "v2": "v3", "v3": "v1"}

    def cyclic(first_key, first, second_key, second, **_):
        a, b = Path(first_key).stem[-2:], Path(second_key).stem[-2:]
        older, newer = (first_key, second_key) if order[a] == b else (second_key, first_key)
        return _edge(older, newer)

    monkeypatch.setattr(sv, "compare_pair", cyclic)
    with pytest.raises(InconsistentRelationsError, match="cycle"):
        _evaluate(folder, database, ids)


def test_an_archive_in_two_established_families_stops_the_run(
    folder, database, monkeypatch
):
    """Step 4 never establishes two families sharing a member; if it did, one
    archive could be superseded twice, so the evaluator refuses to go on.
    Simulated by duplicating step 4's established verdict.
    """
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    ids = _seed_all(database, folder)
    real = sv.resolve_folder

    def doubled(*args, **kwargs):
        resolution = real(*args, **kwargs)
        return replace(resolution, families=resolution.families * 2)

    monkeypatch.setattr(sv, "resolve_folder", doubled)
    with pytest.raises(InconsistentRelationsError, match="two established families"):
        _evaluate(folder, database, ids)


# --- §4.3 provenance -----------------------------------------------------------


def test_rows_from_an_older_revision_are_stale_and_refused(folder, database):
    """§7 named. Rows stored for old bytes, then the file was replaced.

    The family would be a clean prefix supersession from the current bytes;
    the stale rows make v1 unresolved, so nothing is compared.
    """
    v1 = _book(folder, "Series ch. 1 v1.cbz", [P1, P4])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2, P3])
    ids = _seed_all(database, folder)
    _book(folder, v1.name, [P1, P2])          # replaced after inventory

    result = _evaluate(folder, database, ids)
    member = _archive(result, folder, v1.name)
    assert not member.comparable
    assert member.stored_rows.startswith("stored rows are stale")
    family = _family(result, folder, v1.name)
    assert family.status == sv.FAMILY_UNRESOLVED
    assert family.relations == () and family.keeper is None
    assert any("stored rows are stale" in r for r in family.reasons)

    # Control: re-inventoried rows agree, and the same files supersede.
    fresh = _evaluate(folder, database)
    assert _family(fresh, folder, v1.name).status == sv.FAMILY_SUPERSESSION


def test_legacy_or_incomplete_stored_rows_are_refused(tmp_path, database):
    """Stored rows that are not comparable evidence cannot agree with anything."""
    folder = tmp_path / "legacy" / "Series"
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    ids = _seed_all(database, folder)
    _mutate(database, "UPDATE archive_content_signatures SET algorithm_version = '1' "
            "WHERE archive_id = ?", (ids["Series ch. 1 v1.cbz"],))
    result = _evaluate(folder, database, ids)
    reasons = _archive(result, folder, "Series ch. 1 v1.cbz").unresolved_reasons
    assert any("legacy ordering contract" in r for r in reasons)
    assert _family(result, folder, "Series ch. 1 v1.cbz").status == sv.FAMILY_UNRESOLVED


def test_an_incomplete_inventory_is_unresolved(tmp_path, database):
    """§7 named. A missing stored page row, or an unreadable member.

    The first is a stored inventory whose row count disagrees with its
    signature. The second is a current file whose second page fails its
    CRC: the archive is unresolved, never treated as the one-page sequence
    that could be read.
    """
    folder = tmp_path / "rows" / "Series"
    _book(folder, "Series ch. 1 v1.cbz", [P1, P2])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2, P3])
    ids = _seed_all(database, folder)
    _mutate(database, "DELETE FROM archive_pages WHERE archive_id = ? AND page_index = 1",
            (ids["Series ch. 1 v1.cbz"],))
    result = _evaluate(folder, database, ids)
    assert any("incomplete inventory" in r for r in
               _archive(result, folder, "Series ch. 1 v1.cbz").unresolved_reasons)
    assert _family(result, folder, "Series ch. 1 v1.cbz").status == sv.FAMILY_UNRESOLVED

    folder = tmp_path / "crc" / "Series"
    corrupt = _book(folder, "Series ch. 1 v1.cbz", [P1, b"SECOND-PAGE-BYTES"])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2, P3])
    ids = _seed_all(database, folder)
    data = bytearray(corrupt.read_bytes())
    offset = bytes(data).index(b"SECOND-PAGE-BYTES")
    data[offset] ^= 0xFF
    corrupt.write_bytes(bytes(data))
    result = _evaluate(folder, database, ids)
    member = _archive(result, folder, corrupt.name)
    assert member.derived.problem.startswith("corrupt or unreadable archive")
    assert member.derived.pages == ()
    assert _family(result, folder, corrupt.name).status == sv.FAMILY_UNRESOLVED


def test_same_size_same_mtime_replacement_is_detected_by_strong_identity(
    folder, database
):
    """§7 named. Equal size and mtime survive the replacement; the sha256 does not."""
    v1 = _book(folder, "Series ch. 1 v1.cbz", [P1, b"AAAA"])
    _book(folder, "Series ch. 1 v2.cbz", [P1, b"AAAA", P3])
    ids = _seed_all(database, folder)
    before = _evaluate(folder, database, ids)
    stat = v1.stat()
    old_sha = _archive(before, folder, v1.name).derived.sha256

    _book(folder, v1.name, [P1, b"BBBB"])
    os.utime(v1, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert (v1.stat().st_size, v1.stat().st_mtime_ns) == (stat.st_size, stat.st_mtime_ns)

    after = _evaluate(folder, database, ids)
    member = _archive(after, folder, v1.name)
    assert member.derived.sha256 != old_sha
    assert member.derived.sha256 == hashlib.sha256(v1.read_bytes()).hexdigest()
    assert member.stored_rows.startswith("stored rows are stale")
    assert _family(after, folder, v1.name).status == sv.FAMILY_UNRESOLVED
    assert _family(before, folder, v1.name).status == sv.FAMILY_SUPERSESSION


def test_a_file_changed_during_evaluation_is_unresolved(folder, database, monkeypatch):
    """The file on disk changes after its bytes were read and hashed.

    Simulated by rewriting v1 (same size) from inside the page-hashing pass
    of v1's own derivation. The evaluated bytes are self-consistent, but
    they are no longer the file's, so v1 is unresolved.
    """
    v1 = _book(folder, "Series ch. 1 v1.cbz", [P1, b"AAAA"])
    _book(folder, "Series ch. 1 v2.cbz", [P1, b"AAAA", P3])
    ids = _seed_all(database, folder)
    real = sv.hash_ordered_pages
    calls = []

    def hash_then_rewrite(archive, **kwargs):
        result = real(archive, **kwargs)
        if not calls:
            _book(folder, v1.name, [P1, b"BBBB"])
        calls.append(1)
        return result

    monkeypatch.setattr(sv, "hash_ordered_pages", hash_then_rewrite)
    result = _evaluate(folder, database, ids)
    member = _archive(result, folder, v1.name)
    assert member.derived.problem.startswith("file changed during evaluation")
    assert member.derived.pages == ()
    family = _family(result, folder, v1.name)
    assert family.status == sv.FAMILY_UNRESOLVED and family.relations == ()
    # Unbound bytes are "no page evidence" to step 4, never misreported as
    # an archive with no image pages.
    assert any("no page evidence" in r for r in family.reasons)
    assert not any("no image pages" in r for r in family.reasons)


def _without_snapshot_or_mode(result) -> dict:
    data = result.as_dict()
    data.pop("snapshot")
    for archive in data["archives"]:
        archive.pop("read_mode")
    return data


def test_the_streaming_path_matches_the_buffered_path(folder, database):
    """An archive above ``max_held_bytes`` is streamed, with the same result.

    The cap boundary is inclusive: an archive of exactly the cap is held.
    """
    v1 = _book(folder, "Series ch. 1 v1.cbz", [P1, P2],
               {"ComicInfo.xml": _ci(), "notes.txt": b"n"})
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2, P3],
          {"ComicInfo.xml": _ci(), "notes.txt": b"n"})
    ids = _seed_all(database, folder)

    buffered = _evaluate(folder, database, ids)
    streamed = _evaluate(folder, database, ids, max_held_bytes=1)

    assert {a.derived.read_mode for a in buffered.archives} == {sv.READ_BUFFERED}
    assert {a.derived.read_mode for a in streamed.archives} == {sv.READ_STREAMED}
    assert _without_snapshot_or_mode(streamed) == _without_snapshot_or_mode(buffered)
    assert _family(streamed, folder, v1.name).status == sv.FAMILY_SUPERSESSION

    size = v1.stat().st_size
    assert derive_archive(v1, max_held_bytes=size).read_mode == sv.READ_BUFFERED
    assert derive_archive(v1, max_held_bytes=size - 1).read_mode == sv.READ_STREAMED
    assert derive_archive(v1, max_held_bytes=size - 1) == replace(
        derive_archive(v1, max_held_bytes=size), read_mode=sv.READ_STREAMED
    )


@pytest.mark.parametrize("change", ["rewritten-same-stat", "touched"])
def test_a_streamed_file_changed_during_derivation_is_unresolved(
    folder, database, monkeypatch, change
):
    """The streaming path re-hashes and re-stats after deriving the members.

    ``rewritten-same-stat`` changes the bytes but restores size and mtime
    (in place, same file identity): only the second hash can see it.
    ``touched`` changes only the mtime: only the stat comparison can see
    it, because the members were read from the path rather than from the
    hashed bytes.
    """
    v1 = _book(folder, "Series ch. 1 v1.cbz", [P1, b"AAAA"])
    _book(folder, "Series ch. 1 v2.cbz", [P1, b"AAAA", P3])
    ids = _seed_all(database, folder)
    stat = v1.stat()
    real = sv.hash_ordered_pages
    calls = []

    def hash_then_change(archive, **kwargs):
        result = real(archive, **kwargs)
        if not calls:
            if change == "rewritten-same-stat":
                _book(folder, v1.name, [P1, b"BBBB"])
                os.utime(v1, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                assert v1.stat().st_size == stat.st_size
            else:
                os.utime(v1, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        calls.append(1)
        return result

    monkeypatch.setattr(sv, "hash_ordered_pages", hash_then_change)
    result = _evaluate(folder, database, ids, max_held_bytes=1)
    member = _archive(result, folder, v1.name)
    assert member.derived.read_mode == sv.READ_STREAMED
    assert member.derived.problem.startswith("file changed during evaluation")
    if change == "touched":
        assert "mtime_ns" in member.derived.problem
    assert _family(result, folder, v1.name).status == sv.FAMILY_UNRESOLVED


def test_duplicate_member_names_make_the_archive_unresolved(folder, database):
    """§7 named (step-2 test of the same name pins the order; this is step 5)."""
    duplicate = folder / "Series ch. 1 v1.cbz"
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("001.png", P1)
            archive.writestr("001.png", P2)
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2, P3])

    result = _evaluate(folder, database)
    member = _archive(result, folder, duplicate.name)
    assert any("duplicate image member names ['001.png']" in r
               for r in member.unresolved_reasons)
    family = _family(result, folder, duplicate.name)
    assert family.status == sv.FAMILY_UNRESOLVED and family.relations == ()


def test_an_empty_inventory_is_never_a_prefix(folder, database):
    """§7 named. No image pages is a prefix of everything, so it is excluded."""
    _book(folder, "Series ch. 1 v1.cbz", [], {"readme.txt": b"coming soon"})
    _book(folder, "Series ch. 1 v2.cbz", [P1])

    result = _evaluate(folder, database)
    family = _family(result, folder, "Series ch. 1 v1.cbz")
    assert family.status == sv.FAMILY_UNRESOLVED
    assert family.relations == () and family.pointers == ()
    assert any("no image pages" in r for r in family.reasons)
    # The derivation refuses it on its own too, for callers of
    # `derive_archive` that have no stored rows or family gate behind them.
    # (At the entry point this guard is redundant with both; see the report.)
    empty = derive_archive(folder / "Series ch. 1 v1.cbz")
    assert empty.problem is None and empty.pages == ()
    assert empty.unresolved_reason.startswith("no image pages")


def test_admission_uses_the_shared_archive_limits(folder, database, monkeypatch):
    """`validate_archive_members` is the gate; its limit is read at call time."""
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    ids = _seed_all(database, folder)
    monkeypatch.setattr(archive_limits, "MAX_ARCHIVE_ENTRIES", 1)
    # Admission is checked on the central directory before any member is
    # opened: only v2 holds 002.png, so opening it means v2 was read first.
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def recording_open(self, name, *args, **kwargs):
        opened.append(getattr(name, "filename", name))
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", recording_open)

    result = _evaluate(folder, database, ids)
    member = _archive(result, folder, "Series ch. 1 v2.cbz")
    assert "archive_too_many_entries" in member.derived.problem
    assert "002.png" not in opened and "001.png" in opened
    assert _archive(result, folder, "Series ch. 1 v1.cbz").comparable
    assert _family(result, folder, "Series ch. 1 v1.cbz").status == sv.FAMILY_UNRESOLVED


def test_a_file_without_stored_rows_is_unresolved(folder, database):
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    ids = _seed_all(database, folder)
    del ids["Series ch. 1 v2.cbz"]
    result = _evaluate(folder, database, ids)
    assert _archive(result, folder, "Series ch. 1 v2.cbz").stored_rows.startswith(
        "no stored inventory"
    )
    assert _family(result, folder, "Series ch. 1 v1.cbz").status == sv.FAMILY_UNRESOLVED

    # An id the database has no inventory for is refused the same way.
    with database_connection(database) as connection:
        bare_id = int(connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (1)"
        ).lastrowid)
        connection.commit()
    result = _evaluate(folder, database, {**ids, "Series ch. 1 v2.cbz": bare_id})
    assert _archive(result, folder, "Series ch. 1 v2.cbz").stored_rows == (
        f"no stored inventory rows for archive id {bare_id}"
    )
    assert _family(result, folder, "Series ch. 1 v1.cbz").status == sv.FAMILY_UNRESOLVED

    with pytest.raises(ValueError, match="not in the folder"):
        _evaluate(folder, database, {**ids, "elsewhere.cbz": 1})


def test_a_concurrent_database_change_rejects_the_plan(folder, database):
    """§7 named. A commit inside the read window raises; nothing is reported."""
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    ids = _seed_all(database, folder)

    def commit_then_check(connection: sqlite3.Connection) -> str:
        with database_connection(database) as writer:
            writer.execute("INSERT INTO archive_files (file_size) VALUES (1)")
            writer.commit()
        return quick_check(connection)

    with pytest.raises(DatabaseChangedError):
        _evaluate(folder, database, ids, integrity_check=commit_then_check)


# --- perceptual -----------------------------------------------------------------


def _png(level: int) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (32, 32))
    image.putdata([((x * 7) % 256, (y * 5) % 256, 90) for y in range(32) for x in range(32)])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=level)
    return buffer.getvalue()


def test_perceptual_basis_nominates_but_never_proves(folder, database):
    """§7 named. The same picture re-encoded is a different exact page.

    v2's first page has v1's pixels in different bytes. A perceptual basis
    would call v1 a prefix of v2; the exact basis calls it changed, and the
    perceptual basis is not consulted at all.
    """
    from PIL import Image

    fast, small = _png(1), _png(9)
    assert fast != small
    assert Image.open(io.BytesIO(fast)).tobytes() == Image.open(io.BytesIO(small)).tobytes()
    _book(folder, "Series ch. 1 v1.cbz", [fast])
    _book(folder, "Series ch. 1 v2.cbz", [small, P2])

    result = _evaluate(folder, database)
    family = _family(result, folder, "Series ch. 1 v1.cbz")
    assert family.relations[0].relation == sv.REL_OTHER
    assert family.status == sv.FAMILY_REVIEW and family.keeper is None
    assert result.as_dict()["perceptual"] == "not consulted"
    assert family.as_dict()["basis"] == "sha256"


def test_mixed_or_stale_perceptual_rows_only_nominate(folder, database):
    """§7 named. Perceptual rows, however wrong, change nothing.

    Every stored page gets a dhash row claiming all pages look alike; the
    evaluation is identical to the one without them (snapshot aside).
    """
    _book(folder, "Series ch. 1 v1.cbz", [P1, P2])
    _book(folder, "Series ch. 1 v2.cbz", [P3, P4, P1])
    ids = _seed_all(database, folder)
    without = _evaluate(folder, database, ids).as_dict()
    _mutate(database, "INSERT INTO page_hashes (page_id, algorithm, algorithm_version, "
            "digest, bytes_read) SELECT id, 'dhash', '1', '0000000000000000', 0 "
            "FROM archive_pages")
    with_rows = _evaluate(folder, database, ids).as_dict()
    without.pop("snapshot"), with_rows.pop("snapshot")
    assert with_rows == without
    assert without["families"][0]["status"] == sv.FAMILY_REVIEW


# --- §4.2 / §4.5 never versions -------------------------------------------------


def test_decensor_pairs_are_never_versions(folder, database):
    """§7 named test's family half, at step 5's entry point.

    The uncensored file's pages extend the censored one's, so a comparison
    would call it a supersession. Step 4 refuses the pair, and nothing is
    compared. (The "survive rename" half belongs to step 7.)
    """
    _book(folder, "Series ch. 1 v1.cbz", [P1], {"ComicInfo.xml": _ci()})
    _book(folder, "Series ch. 1 v2 (Uncensored).cbz", [P1, P2], {"ComicInfo.xml": _ci()})

    result = _evaluate(folder, database)
    family = _family(result, folder, "Series ch. 1 v1.cbz")
    assert family.status == sv.FAMILY_NOT_ESTABLISHED
    assert family.relations == () and family.keeper is None
    assert any("decensor pair" in r for r in family.reasons)


def test_an_omnibus_containing_an_issue_is_not_its_version(folder, database):
    """§7 named (step 4 pins the family half). Same series and #1, all pages inside."""
    _book(folder, "Series #001 [v1].cbz", [P1, P2], {"ComicInfo.xml": _ci()})
    _book(folder, "Series Omnibus #001 [v2].cbz", [P1, P2, P3, P4],
          {"ComicInfo.xml": _ci()})

    family = _family(_evaluate(folder, database), folder, "Series #001 [v1].cbz")
    assert family.status == sv.FAMILY_NOT_ESTABLISHED
    assert family.relations == ()
    assert any("special/collection" in r for r in family.reasons)


def test_overlap_without_family_evidence_is_never_compared(folder, database):
    """§7 named (step 4 pins the family half). Shared pages, no version marker."""
    _book(folder, "Series ch. 1.cbz", [P1], {"ComicInfo.xml": _ci()})
    _book(folder, "Series ch. 1 (Digital).cbz", [P1, P2], {"ComicInfo.xml": _ci()})

    result = _evaluate(folder, database)
    assert all(f.relations == () for f in result.families)
    assert all(f.status == sv.FAMILY_NOT_ESTABLISHED for f in result.families)


# --- §4.6 keeper label -----------------------------------------------------------


def test_keeper_keeps_its_own_verified_version_label(tmp_path, database):
    """§7 named. From the keeper's own name, or from a human decision."""
    folder = tmp_path / "name" / "Series"
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 [v4].cbz", [P1, P2])
    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v1.cbz")
    assert Path(family.keeper).name == "Series ch. 1 [v4].cbz"
    assert (family.label.status, family.label.value, family.label.source) == (
        sv.LABEL_RESOLVED, "4", "version_family",
    )

    folder = tmp_path / "human" / "Series"
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    keeper = _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    decisions = StaticHumanDecisions({str(keeper): [HumanDecision("version", "7", "r1")]})
    family = _family(
        _evaluate(folder, database, decisions=decisions), folder, keeper.name
    )
    assert (family.label.value, family.label.source) == ("7", "human_decision")


def test_other_members_version_numbers_never_relabel_the_keeper(tmp_path, database):
    """§7 named. The obsolete member is v8; the keeper is v2, or unnumbered.

    A v2 keeper stays v2. An unnumbered keeper is held for a decision, and
    v8 appears only as a note -- never as the keeper's label.
    """
    folder = tmp_path / "numbered" / "Series"
    _book(folder, "Series ch. 1 v8.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v8.cbz")
    assert Path(family.keeper).name == "Series ch. 1 v2.cbz"
    assert family.label.value == "2"
    assert any("['8']" in note and "never used" in note for note in family.label.notes)

    folder = tmp_path / "unnumbered" / "Series"
    _book(folder, "Series ch. 1 v8.cbz", [P1])
    _book(folder, "Series ch. 1.cbz", [P1, P2])
    family = _family(_evaluate(folder, database), folder, "Series ch. 1 v8.cbz")
    assert family.status == sv.FAMILY_SUPERSESSION
    assert Path(family.keeper).name == "Series ch. 1.cbz"
    assert family.label.status == sv.LABEL_HELD
    assert family.label.value is None and family.label.display is None
    assert "8" not in json.dumps(family.label.as_dict()["value"])


def test_an_unnumbered_chain_leaves_the_label_unresolved(folder, database):
    """§7 named. A human-declared family with no marker anywhere."""
    first = _book(folder, "Series ch. 1.cbz", [P1])
    second = _book(folder, "Series ch. 001.cbz", [P1, P2])
    decisions = StaticHumanDecisions({
        str(first): [HumanDecision("family", "f1", "r1")],
        str(second): [HumanDecision("family", "f1", "r2")],
    })
    family = _family(
        _evaluate(folder, database, decisions=decisions), folder, first.name
    )
    assert family.status == sv.FAMILY_SUPERSESSION
    assert family.keeper == str(second)
    assert family.label.status == sv.LABEL_HELD
    assert "held for a decision" in family.label.reason


# --- output and read-only ------------------------------------------------------


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_the_result_is_serialisable_and_never_carries_a_rank(folder, database):
    _book(folder, "Series ch. 1 v1.cbz", [P1])
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2])
    _book(folder, "Series ch. 1 v3.cbz", [P1, P2, P3])
    result = _evaluate(folder, database).as_dict()

    assert json.loads(json.dumps(result)) == result
    assert not any("rank" in key for key in _walk_keys(result))
    assert result["snapshot"]["data_version_before"] == result["snapshot"]["data_version_after"]
    (family,) = result["families"]
    assert [Path(s["archive"]).name for s in family["superseded"]] == [
        "Series ch. 1 v1.cbz", "Series ch. 1 v2.cbz",
    ]
    assert {Path(s["keeper"]).name for s in family["superseded"]} == {"Series ch. 1 v3.cbz"}
    assert all(a["sha256"] and a["pages"] for a in result["archives"])


def _tree(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_evaluation_writes_nothing(tmp_path, folder, database):
    """Tree bytes, mtimes and listing, and the database, are untouched.

    ``data_version`` is sampled on a connection held open across the
    evaluation: it moves when any *other* connection commits, so an
    unchanged value proves the evaluator committed nothing.
    """
    _book(folder, "Series ch. 1 v1.cbz", [P1], {"ComicInfo.xml": _ci()})
    _book(folder, "Series ch. 1 v2.cbz", [P1, P2], {"notes.txt": b"n"})
    _book(folder, "Series ch. 2.cbz", [P3])
    ids = _seed_all(database, folder)
    library = tmp_path / "library"
    tree_before = _tree(library)

    with database_connection(database) as watcher:
        version_before = watcher.execute("PRAGMA data_version").fetchone()[0]
        db_before = hashlib.sha256(database.read_bytes()).hexdigest()
        result = _evaluate(folder, database, ids)
        version_after = watcher.execute("PRAGMA data_version").fetchone()[0]
        db_after = hashlib.sha256(database.read_bytes()).hexdigest()

    assert result.families
    assert _tree(library) == tree_before
    assert version_after == version_before
    assert db_after == db_before


def test_the_evaluator_opens_no_database_connection_of_its_own():
    source = Path(sv.__file__).read_text(encoding="utf-8")
    assert "sqlite3.connect" not in source
    assert "open_connection" not in source
    assert "database_connection" not in source


# --- units ---------------------------------------------------------------------


def test_greedy_index_map_walks_left_to_right():
    assert greedy_index_map(["a", "a"], ["a", "b", "a"]) == (0, 2)
    assert greedy_index_map(["a", "b"], ["a", "b", "c"]) == (0, 1)
    assert greedy_index_map(["b", "a"], ["a", "b"]) is None
    assert greedy_index_map(["a", "a"], ["a", "b"]) is None


def test_derivation_reuses_the_production_page_hasher(tmp_path):
    """The held-buffer pass and `calculate_page_hashes` agree page for page."""
    path = _book(tmp_path / "x", "book.cbz", [P2, P1],
                 {"A.png": b"upper", "a.png": b"lower", "notes.txt": b"n"})
    derived = derive_archive(path)
    reference = calculate_page_hashes(path)
    assert derived.pages == reference.pages
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        pages, order = hash_ordered_pages(archive)
    assert pages == reference.pages and order.resolved
    assert [m.name for m in derived.non_image] == ["notes.txt"]
