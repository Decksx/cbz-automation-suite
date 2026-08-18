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
    chapter always carry different ComicInfo. If the fingerprint included that
    file, no real duplicate would ever be detected.
    """
    pages = {"001.jpg": b"page one", "002.jpg": b"page two"}
    first = tmp_path / "a.cbz"
    second = tmp_path / "b.cbz"
    _make_cbz(first, pages, number="1")
    _make_cbz(second, pages, number="1")
    # Rewrite one ComicInfo so the two differ in metadata but not in pages.
    with zipfile.ZipFile(second, "a") as zf:
        zf.writestr("extra_ComicInfo_marker.txt", b"")

    assert _archive_content_fingerprint(first) == _archive_content_fingerprint(
        tmp_path / "a.cbz"
    )
    # The marker file is a real content difference, so these must NOT match.
    assert _archive_content_fingerprint(first) != _archive_content_fingerprint(second)


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


def test_filename_pass_is_unaffected_by_the_content_guard(tmp_path):
    """Pass 1 (filename key) keeps its existing behaviour.

    The guard was scoped to the metadata pass because the filename key is a far
    tighter grouping: the same production run made 1,922 metadata deletions but
    only 11 by filename.
    """
    _make_cbz(tmp_path / "Chapter 1.cbz", {"001.jpg": b"small"})
    _make_cbz(tmp_path / "Chapter  1.cbz", {"001.jpg": b"considerably larger page"})

    stats = dedupe_archives_in_dir(tmp_path, dry_run=False, use_metadata=False)

    # Normalised filename keys collide, so one is removed even though the
    # content differs -- unchanged pre-existing behaviour.
    assert stats.deleted == 1
