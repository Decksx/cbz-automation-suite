"""Properties of the golden corpus, and of the pipeline over it.

Two layers, and the first is not optional.

*Corpus integrity* asserts the fixtures are what they claim: byte-identical
across rebuilds, distinct where they must differ, identical where they must
match. A corpus that silently drifts turns every test built on it into a
test of nothing, and the drift is invisible because the tests still pass.

*Pipeline properties* run the real inspection path over those fixtures and
pin behaviour the structural work is about to depend on: that identity is
keyed on content rather than order, that a metadata-only edit leaves pages
alone, that corruption is classified by kind rather than lumped together.

No production database, no live library, no network. Every fixture is
synthetic and built inside `tmp_path`.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from comic_automation.archive import (
    ArchiveInspectionError,
    UnsupportedArchiveFormatError,
    inspect_archive,
)
from tests import golden_corpus as gc


# --- corpus integrity ----------------------------------------------------


@pytest.mark.parametrize("case", gc.CASES, ids=lambda c: c.name)
def test_every_case_is_byte_reproducible(case, tmp_path: Path) -> None:
    """The same case built twice must produce identical bytes.

    This is the property that makes everything else here meaningful. ZIP
    members carry a timestamp and `writestr` defaults it to *now*, so the
    naive builder produces a different file every second while every logical
    property still holds.
    """
    first = case.build(tmp_path / "a" / f"{case.name}.cbz")
    second = case.build(tmp_path / "b" / f"{case.name}.cbz")

    assert gc.sha256_file(first) == gc.sha256_file(second)
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("case", gc.CASES, ids=lambda c: c.name)
def test_every_member_timestamp_is_pinned(case, tmp_path: Path) -> None:
    """The member timestamp must be the pinned constant, not "now".

    Asserted directly rather than inferred from two builds matching:
    consecutive builds share a wall-clock second, so an unpinned builder
    still produces identical bytes when the reproducibility test runs and
    only diverges once the tests straddle a second boundary. That is the
    worst kind of failure -- intermittent, environment-dependent, and
    arriving long after the change that caused it.
    """
    path = case.build(tmp_path / f"{case.name}.cbz")

    if not zipfile.is_zipfile(path):
        pytest.skip("case is deliberately not a readable archive")

    with zipfile.ZipFile(path) as archive:
        stamps = {info.date_time for info in archive.infolist()}

    assert stamps <= {gc.FIXED_DATE_TIME}


def test_page_images_are_deterministic_and_seed_distinct() -> None:
    """Same seed, same bytes; different seeds, different bytes.

    Both halves matter. Without the second, a "one page difference" fixture
    could encode identically to the page it replaced and the difference test
    would pass while testing nothing.
    """
    assert gc.page_image(7) == gc.page_image(7)

    rendered = {gc.page_image(seed) for seed in range(1, 30)}
    assert len(rendered) == 29


def test_the_corpus_covers_every_required_shape() -> None:
    """The roadmap's Step 8 list, asserted rather than assumed.

    Named explicitly so that deleting a fixture fails here instead of
    quietly reducing coverage.
    """
    required = {
        "ordinary",
        "corrupt_zip",
        "truncated_image",
        "unidentified_image",
        "duplicate_pages",
        "reordered_pages",
        "one_page_difference",
        "comicinfo_only_change",
        "unicode_paths",
        "unsafe_members",
        "many_entries",
    }

    assert required <= set(gc.CASES_BY_NAME)


def test_case_names_are_unique() -> None:
    names = [case.name for case in gc.CASES]
    assert len(names) == len(set(names))


# --- identity: what must and must not change a page ----------------------


def _page_digests(path: Path) -> dict[str, str]:
    """SHA-256 per image member, keyed by name."""
    payloads = gc.member_payloads(path)
    return {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in payloads.items()
        if name.casefold().endswith(".png")
    }


def test_a_comicinfo_only_change_leaves_every_page_identical(
    tmp_path: Path,
) -> None:
    """The distinction between an archive digest and a page inventory.

    A whole-file hash says these two archives are unrelated. A page-level
    inventory says they hold the same pages and differ only in metadata,
    which is the fact revision semantics will have to preserve.
    """
    ordinary = gc.build_case("ordinary", tmp_path)
    changed = gc.build_case("comicinfo_only_change", tmp_path)

    assert _page_digests(ordinary) == _page_digests(changed)
    assert gc.sha256_file(ordinary) != gc.sha256_file(changed)


def test_reordering_pages_preserves_the_page_set(tmp_path: Path) -> None:
    """Physical order changes; the set of pages does not.

    Ordered-page identity must therefore be derived from a defined order
    rather than from `infolist()` order, which reordering changes.
    """
    ordinary = gc.build_case("ordinary", tmp_path)
    reordered = gc.build_case("reordered_pages", tmp_path)

    assert _page_digests(ordinary) == _page_digests(reordered)
    assert gc.member_names(ordinary) != gc.member_names(reordered)
    assert gc.sha256_file(ordinary) != gc.sha256_file(reordered)


def test_sorted_member_order_is_stable_under_reordering(
    tmp_path: Path,
) -> None:
    """Sorting by name recovers a deterministic order from either archive."""
    ordinary = gc.build_case("ordinary", tmp_path)
    reordered = gc.build_case("reordered_pages", tmp_path)

    assert sorted(gc.member_names(ordinary)) == sorted(
        gc.member_names(reordered)
    )


def test_one_page_difference_changes_exactly_one_page(
    tmp_path: Path,
) -> None:
    ordinary = _page_digests(gc.build_case("ordinary", tmp_path))
    differing = _page_digests(
        gc.build_case("one_page_difference", tmp_path)
    )

    assert set(ordinary) == set(differing)
    changed = [
        name for name in ordinary if ordinary[name] != differing[name]
    ]
    assert changed == ["003.png"]


def test_one_page_fewer_drops_exactly_one_page(tmp_path: Path) -> None:
    ordinary = _page_digests(gc.build_case("ordinary", tmp_path))
    fewer = _page_digests(gc.build_case("one_page_fewer", tmp_path))

    assert set(ordinary) - set(fewer) == {"003.png"}


def test_duplicate_pages_share_one_digest(tmp_path: Path) -> None:
    """Identical content under two names is one page, twice.

    Deduplication has to see this without treating the second name as a
    defect: the archive is legitimate.
    """
    digests = _page_digests(gc.build_case("duplicate_pages", tmp_path))

    assert digests["001.png"] == digests["002.png"]
    assert digests["003.png"] != digests["001.png"]
    assert len(set(digests.values())) == 2


def test_moving_a_file_does_not_change_its_content(tmp_path: Path) -> None:
    """Relocation must not look like a content change.

    Archive identity is keyed on path today, so a move mints a new identity
    while the bytes are provably the same. Pinning the bytes-side of that
    is what makes the identity-side a decision rather than an accident.
    """
    original = gc.build_case("ordinary", tmp_path / "before")
    digest = gc.sha256_file(original)

    moved = tmp_path / "after" / "relocated.cbz"
    moved.parent.mkdir(parents=True)
    original.rename(moved)

    assert gc.sha256_file(moved) == digest
    assert _page_digests(moved)


# --- in-place replacement ------------------------------------------------


def test_replacement_in_place_keeps_the_path_and_changes_the_bytes(
    tmp_path: Path,
) -> None:
    """The shape that makes path-keyed identity ambiguous."""
    target = gc.build_case("ordinary", tmp_path)
    before = gc.sha256_file(target)

    gc.replace_in_place(target, "one_page_difference", tmp_path / "src")

    assert target.exists()
    assert gc.sha256_file(target) != before


def test_same_size_replacement_is_genuinely_the_same_size(
    tmp_path: Path,
) -> None:
    """The case a size/mtime staleness check cannot see.

    `same_size_replacement` raises if it cannot hit the exact length, so
    this asserts the fixture is honest rather than approximately right --
    a replacement one byte off would be caught by the very guard it exists
    to defeat.
    """
    target = gc.build_case("ordinary", tmp_path)
    size_before = target.stat().st_size
    replacement = gc.same_size_replacement(target)

    assert len(replacement) == size_before

    target.write_bytes(replacement)

    assert target.stat().st_size == size_before
    assert gc.sha256_file(target) != gc.sha256_file(
        gc.build_case("ordinary", tmp_path / "fresh")
    )


# --- inspection over the corpus ------------------------------------------


def test_ordinary_archive_inspects_cleanly(tmp_path: Path) -> None:
    result = inspect_archive(gc.build_case("ordinary", tmp_path))

    assert result.page_count == 3
    assert result.comic_info_valid is True


def test_corrupt_zip_raises_inspection_error(tmp_path: Path) -> None:
    with pytest.raises(ArchiveInspectionError):
        inspect_archive(gc.build_case("corrupt_zip", tmp_path))


def test_a_truncated_image_does_not_make_the_archive_corrupt(
    tmp_path: Path,
) -> None:
    """Corrupt page and corrupt archive are different findings.

    The terminal-failure classification separates 40 corrupt archives from
    92 corrupt page images. That distinction only survives if a structurally
    sound archive containing one bad image still inspects.
    """
    result = inspect_archive(gc.build_case("truncated_image", tmp_path))

    assert result.page_count == 2


def test_malformed_comicinfo_does_not_fail_the_archive(
    tmp_path: Path,
) -> None:
    """Invalid metadata is reported as invalid, not fatal."""
    result = inspect_archive(gc.build_case("malformed_comicinfo", tmp_path))

    assert result.page_count == 2
    assert result.comic_info_valid is False


def test_an_archive_with_no_images_is_distinguishable_from_an_empty_one(
    tmp_path: Path,
) -> None:
    """Both hold zero pages; only one holds zero entries.

    1,256 identities in production hold no pages, and the sub-reason for
    each is what separates "never inspected" from "genuinely contains no
    images". Collapsing these two shapes would erase that distinction at
    the source.
    """
    no_images = inspect_archive(gc.build_case("no_images", tmp_path))
    empty = inspect_archive(gc.build_case("empty_archive", tmp_path))

    assert no_images.page_count == 0
    assert empty.page_count == 0
    assert no_images.entry_count > empty.entry_count
    assert empty.entry_count == 0


def test_unicode_member_names_round_trip(tmp_path: Path) -> None:
    """Non-ASCII names survive inspection unchanged."""
    path = gc.build_case("unicode_paths", tmp_path)
    result = inspect_archive(path)

    assert result.page_count == 3

    names = gc.member_names(path)
    assert any("é" in name for name in names)
    assert any("：" in name for name in names)
    assert any("日本語" in name for name in names)


def test_unicode_paths_are_reproducible_across_rebuilds(
    tmp_path: Path,
) -> None:
    """Encoding of non-ASCII member names must be stable, not just valid.

    A builder that emitted a different ZIP name-encoding flag between runs
    would still inspect correctly while breaking every digest built on it.
    """
    first = gc.build_case("unicode_paths", tmp_path / "a")
    second = gc.build_case("unicode_paths", tmp_path / "b")

    assert gc.member_names(first) == gc.member_names(second)
    assert gc.sha256_file(first) == gc.sha256_file(second)


def test_many_entries_inspects_without_special_casing(
    tmp_path: Path,
) -> None:
    result = inspect_archive(gc.build_case("many_entries", tmp_path))

    assert result.page_count == 120


def test_non_cbz_suffix_is_rejected_before_reading(tmp_path: Path) -> None:
    path = gc.build_case("ordinary", tmp_path)
    renamed = path.with_suffix(".zip")
    path.rename(renamed)

    with pytest.raises(UnsupportedArchiveFormatError):
        inspect_archive(renamed)


# --- unsafe members: current behaviour, pinned ---------------------------


def test_unsafe_member_names_are_preserved_not_rejected(
    tmp_path: Path,
) -> None:
    """Pins an accepted limitation rather than asserting a fix.

    Nothing in this project extracts archives to disk, so traversing member
    names are not an active vulnerability -- `docs/archive_io_resource_audit.md`
    records this deliberately under "Confirmed risks in current code": the
    rewrite paths carry such names through verbatim instead of rejecting
    them, and a downstream tool doing naive extraction would inherit them.

    This test asserts what the code does *today*. If member validation is
    later added, this test should fail and be replaced by one asserting
    rejection -- that is the intended signal, not a regression.
    """
    path = gc.build_case("unsafe_members", tmp_path)
    names = gc.member_names(path)

    assert "../escape.png" in names
    assert "nested/../../escape2.png" in names
    assert "/absolute.png" in names

    # It inspects rather than raising: the names are data, not paths.
    result = inspect_archive(path)
    assert result.page_count == 4


def test_inspecting_unsafe_members_writes_nothing_outside_the_archive(
    tmp_path: Path,
) -> None:
    """The reason the above is safe: inspection never extracts.

    Asserted by observation rather than by reading the implementation --
    if inspection ever started extracting, the traversing names in this
    fixture would land outside the archive's directory and this fails.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = gc.build_case("unsafe_members", workspace)

    before = {p for p in tmp_path.rglob("*")}
    inspect_archive(path)
    after = {p for p in tmp_path.rglob("*")}

    assert before == after


# --- the corpus as a whole -----------------------------------------------


@pytest.mark.parametrize("case", gc.CASES, ids=lambda c: c.name)
def test_every_case_behaves_as_its_metadata_claims(
    case, tmp_path: Path
) -> None:
    """`expects_valid_zip` / `expects_images` must describe reality.

    These flags let other tests iterate the corpus and know what to expect.
    A flag that drifts out of step with its fixture would send those tests
    down the wrong branch silently.
    """
    path = case.build(tmp_path / f"{case.name}.cbz")

    assert zipfile.is_zipfile(path) == case.expects_valid_zip

    if not case.expects_valid_zip:
        return

    result = inspect_archive(path)
    assert (result.page_count > 0) == case.expects_images


def test_all_cases_build_into_one_directory_without_collision(
    tmp_path: Path,
) -> None:
    built = gc.build_all(tmp_path)

    assert len(built) == len(gc.CASES)
    assert len({path.name for path in built.values()}) == len(gc.CASES)
