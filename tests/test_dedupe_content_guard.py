"""Regression tests for the content guard on metadata-proposed duplicate groups.

Background, because these tests exist to stop a specific production incident
recurring: on 2026-08-17 a maintenance run deleted 1,922 archives whose
ComicInfo.xml resolved to the same Series/Volume/Number. Only 68 of those were
genuine duplicates -- upstream metadata routinely assigns distinct chapters the
same triple, so entire runs collapsed onto one keeper. Two examples from that
run: a 145-page "Omega Chapter 28" deleted in favour of a 38-page file, and
"Dusk Chapter 1" deleted in favour of a different series entirely.

`dedupe_archives_in_dir` therefore treats metadata as a *proposal* and requires
identical page content before deleting. These tests pin both halves of that:
identical content is still deduplicated, differing content is not.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.cbz_library_maintenance import (
    _archive_content_fingerprint,
    _split_group_by_content,
    dedupe_archives_in_dir,
)


def _make_cbz(path: Path, pages: dict[str, bytes], number: str = "1") -> None:
    """Write a CBZ whose ComicInfo advertises *number* and whose pages are *pages*."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Series>Test Series</Series>"
            f"<Volume>1</Volume><Number>{number}</Number></ComicInfo>",
        )
        for name, payload in pages.items():
            zf.writestr(name, payload)


def test_metadata_collision_with_different_content_is_not_deleted(tmp_path):
    """The production incident, in miniature: same metadata, different pages.

    Both files claim Series/Volume/Number "Test Series|v1|n1" but hold different
    page bytes, so neither may be deleted. Filenames are deliberately unrelated
    so pass 1 (filename key) cannot group them -- only the metadata pass can,
    which is exactly the path under test.
    """
    _make_cbz(tmp_path / "Omega Chapter 28.cbz", {"001.jpg": b"a" * 100})
    _make_cbz(tmp_path / "Manhwa18 2 Chapter 28.cbz", {"001.jpg": b"b" * 400})

    stats = dedupe_archives_in_dir(tmp_path, dry_run=False)

    assert stats.deleted == 0
    assert (tmp_path / "Omega Chapter 28.cbz").exists()
    assert (tmp_path / "Manhwa18 2 Chapter 28.cbz").exists()


def test_metadata_collision_with_identical_content_is_deduplicated(tmp_path):
    """The feature still works: same metadata AND same pages collapses to one.

    Guards against "fix" by simply disabling the metadata pass -- that would
    make this test fail, because nothing would be deleted.
    """
    pages = {"001.jpg": b"identical page bytes", "002.jpg": b"second page"}
    _make_cbz(tmp_path / "Chapter 1 [GroupA].cbz", pages)
    _make_cbz(tmp_path / "c001 GroupB scan.cbz", pages)

    stats = dedupe_archives_in_dir(tmp_path, dry_run=False)

    assert stats.deleted == 1
    survivors = sorted(p.name for p in tmp_path.glob("*.cbz"))
    assert len(survivors) == 1


def test_comicinfo_differences_do_not_defeat_content_matching(tmp_path):
    """Identical pages must still match when only ComicInfo differs.

    update_comicinfo_xml derives Title from the filename, so two copies of one
    chapter always carry different ComicInfo. If the fingerprint counted that
    file, no real duplicate would ever be detected.

    An earlier version of this test used a stray .txt entry as the "difference"
    and asserted the fingerprints must diverge. That encoded the hand-rolled
    implementation's behaviour of hashing every non-ComicInfo entry. The
    canonical algorithm counts only image extensions, and is right to: a readme
    or a thumbnail database is not page content. The difference asserted below
    is therefore a real page difference.
    """
    pages = {"001.jpg": b"page one", "002.jpg": b"page two"}
    first = tmp_path / "a.cbz"
    second = tmp_path / "b.cbz"
    _make_cbz(first, pages, number="1")
    _make_cbz(second, pages, number="7")  # different ComicInfo, same pages

    assert _archive_content_fingerprint(first) == _archive_content_fingerprint(second)

    third = tmp_path / "c.cbz"
    _make_cbz(third, {**pages, "003.jpg": b"an extra page"})
    assert _archive_content_fingerprint(first) != _archive_content_fingerprint(third)


def test_page_rename_alone_still_counts_as_identical(tmp_path):
    """Entry names are excluded from the fingerprint, so renamed pages match.

    Different scanlation releases of one chapter frequently differ only in page
    filenames. Those are genuine duplicates and must still be collapsed.
    """
    first = tmp_path / "release-a.cbz"
    second = tmp_path / "release-b.cbz"
    _make_cbz(first, {"001.jpg": b"page one", "002.jpg": b"page two"})
    _make_cbz(second, {"page_01.jpg": b"page one", "page_02.jpg": b"page two"})

    assert _archive_content_fingerprint(first) == _archive_content_fingerprint(second)


def test_unreadable_archive_is_never_deleted_as_a_duplicate(tmp_path):
    """A corrupt archive becomes its own singleton rather than somebody's dupe.

    Returning None from the fingerprint must not be treated as "matches
    everything else that also failed", which would delete corrupt files in
    pairs.
    """
    good = tmp_path / "good.cbz"
    _make_cbz(good, {"001.jpg": b"x"})
    broken_one = tmp_path / "broken1.cbz"
    broken_two = tmp_path / "broken2.cbz"
    broken_one.write_bytes(b"not a zip at all")
    broken_two.write_bytes(b"also not a zip")

    assert _archive_content_fingerprint(broken_one) is None

    subgroups = _split_group_by_content([good, broken_one, broken_two])

    # Every unreadable file is isolated, so no subgroup can delete one.
    assert sorted(len(s) for s in subgroups) == [1, 1, 1]


def test_filename_pass_also_requires_content_proof(tmp_path):
    """Pass 1 (filename key) must not delete differing content either.

    An earlier revision left this pass unguarded on the grounds that the
    filename key is "tighter" and produced only 11 deletions during the
    incident. Neither is a safety property: two files whose names normalise
    alike can hold different chapters, and deletion is irreversible. This test
    replaces one that asserted the unguarded behaviour.
    """
    _make_cbz(tmp_path / "Chapter 1.cbz", {"001.jpg": b"small"})
    _make_cbz(tmp_path / "Chapter  1.cbz", {"001.jpg": b"considerably larger page"})

    stats = dedupe_archives_in_dir(tmp_path, dry_run=False, use_metadata=False)

    assert stats.deleted == 0
    assert (tmp_path / "Chapter 1.cbz").exists()
    assert (tmp_path / "Chapter  1.cbz").exists()


def test_filename_pass_still_collapses_identical_content(tmp_path):
    """Guarding pass 1 must not disable it."""
    pages = {"001.jpg": b"same page bytes"}
    _make_cbz(tmp_path / "Chapter 1.cbz", pages)
    _make_cbz(tmp_path / "Chapter  1.cbz", pages)

    stats = dedupe_archives_in_dir(tmp_path, dry_run=False, use_metadata=False)

    assert stats.deleted == 1


def test_page_order_changes_the_fingerprint(tmp_path):
    """Reordered pages are a different comic, not a duplicate.

    The previous fingerprint sorted its entries before hashing, so an archive
    whose pages were bound in a different order compared equal and could be
    deleted. Page order is derived from entry name, so renaming pages such that
    their order changes must change the fingerprint.
    """
    forward = tmp_path / "forward.cbz"
    reversed_pages = tmp_path / "reversed.cbz"
    _make_cbz(forward, {"001.jpg": b"page A", "002.jpg": b"page B"})
    # Same two page payloads, opposite reading order.
    _make_cbz(reversed_pages, {"001.jpg": b"page B", "002.jpg": b"page A"})

    assert _archive_content_fingerprint(forward) != _archive_content_fingerprint(
        reversed_pages
    )


def test_natural_page_order_matches_the_canonical_algorithm(tmp_path):
    """Page order must follow natural sort, not lexicographic sort.

    Lexicographically "10.jpg" precedes "2.jpg"; naturally it does not. The
    rest of the system (page_hashing._natural_key) uses natural order, so a
    guard sorting lexicographically could call two archives identical whose
    canonical page order differs -- reachable by re-zero-padding page names and
    redistributing content between them.

    Here both archives hold the same three payloads but assign them to page
    numbers such that only one ordering agrees. They must not compare equal.
    """
    natural = tmp_path / "natural.cbz"
    padded = tmp_path / "padded.cbz"
    _make_cbz(natural, {"1.jpg": b"A", "2.jpg": b"B", "10.jpg": b"C"})
    # Same payload set; zero-padding makes lexicographic order match natural
    # order here, but the page CONTENT is assigned differently.
    _make_cbz(padded, {"01.jpg": b"A", "02.jpg": b"C", "10.jpg": b"B"})

    assert _archive_content_fingerprint(natural) != _archive_content_fingerprint(
        padded
    )


def test_zero_padding_alone_does_not_change_the_fingerprint(tmp_path):
    """Renaming pages while preserving natural order keeps archives equal.

    "1.jpg" and "01.jpg" occupy the same position under natural ordering, so a
    release that only re-pads its page names is still a duplicate.
    """
    plain = tmp_path / "plain.cbz"
    padded = tmp_path / "padded.cbz"
    _make_cbz(plain, {"1.jpg": b"A", "2.jpg": b"B", "10.jpg": b"C"})
    _make_cbz(padded, {"01.jpg": b"A", "02.jpg": b"B", "010.jpg": b"C"})

    assert _archive_content_fingerprint(plain) == _archive_content_fingerprint(padded)


def test_non_image_entries_are_excluded_like_the_canonical_algorithm(tmp_path):
    """Only image extensions count as pages.

    The canonical implementation filters on IMAGE_EXTENSIONS. A guard that
    hashed every non-ComicInfo entry would treat a stray readme or thumbs
    database as page content and call two otherwise-identical archives
    different.
    """
    bare = tmp_path / "bare.cbz"
    littered = tmp_path / "littered.cbz"
    _make_cbz(bare, {"001.jpg": b"page one", "002.jpg": b"page two"})
    _make_cbz(
        littered,
        {"001.jpg": b"page one", "002.jpg": b"page two", "readme.txt": b"junk"},
    )

    assert _archive_content_fingerprint(bare) == _archive_content_fingerprint(littered)


def test_maintenance_fingerprint_equals_the_stored_signature_algorithm(tmp_path):
    """The guard and archive_content_signatures must agree by construction.

    This is the property that makes the guard meaningful: it is the same
    function the database's content signature comes from, not a second opinion
    that could drift from it.
    """
    from comic_automation.archive.page_hashing import calculate_page_hashes

    archive = tmp_path / "a.cbz"
    _make_cbz(archive, {"1.jpg": b"A", "2.jpg": b"B", "10.jpg": b"C"})

    assert (
        _archive_content_fingerprint(archive)
        == calculate_page_hashes(archive).content_digest
    )


def test_fingerprint_is_cryptographic_not_a_checksum(tmp_path):
    """The digest must be a SHA-256 chain, not a CRC.

    CRC32 is a 32-bit accidental-corruption check whose collisions are cheap to
    construct. A collision would equate different bytes and authorise deleting
    one of them, so the fingerprint's width is a safety property worth pinning.
    """
    archive = tmp_path / "a.cbz"
    _make_cbz(archive, {"001.jpg": b"page"})

    fingerprint = _archive_content_fingerprint(archive)

    assert fingerprint is not None
    assert len(fingerprint) == 64  # SHA-256 hex
    int(fingerprint, 16)  # and it is hex


def test_identical_pages_in_differently_named_files_still_match(tmp_path):
    """Renaming pages without changing their order keeps archives equal.

    This is the property that lets genuinely duplicate releases collapse, and
    it must survive the move to an ordered digest.
    """
    first = tmp_path / "a.cbz"
    second = tmp_path / "b.cbz"
    _make_cbz(first, {"001.jpg": b"page one", "002.jpg": b"page two"})
    _make_cbz(second, {"aaa.jpg": b"page one", "bbb.jpg": b"page two"})

    assert _archive_content_fingerprint(first) == _archive_content_fingerprint(second)
