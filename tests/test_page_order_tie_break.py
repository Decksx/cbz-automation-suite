"""Page order tie-break, duplicate-name refusal, re-association by name.

Design ``docs/canonical_naming_and_version_supersession.md`` §1.2 and §6
step 2. Three behaviours, each pinned here against synthetic ``tmp_path``
archives only:

1. **Tie-break.** Names whose natural keys tie (``a.png``/``A.png``,
   ``01.png``/``1.png``, ``p1.png``/``p01.png``) are ordered by their raw
   name, so for archives whose image names are unique the page order -- and
   therefore the content signature -- no longer depends on physical member
   order. Every reader of page order (exact page hashing, perceptual
   hashing, the source-drift inventory) uses the same ordering helper, and
   the tests below check each of them, because two readers that disagree
   about order would report drift on every tied archive.
2. **Duplicate names are unresolved.** Identical raw names cannot be ordered
   by name. The ordering helper reports them, the in-memory result carries
   the flag, stored rows expose it (duplicated ``archive_pages.entry_name``),
   and the two consumers gated on it so far (``duplicate_content_groups``
   and the maintenance deletion guard) refuse to compare such an archive.
   ``content_duplicate_audit`` and ``near_duplicate`` are not gated yet;
   see ``docs/engineering_decisions.md``.
3. **Re-association by entry name.** A re-inventory under the new order
   moves members to different ``page_index`` values. Each digest must stay
   with the member it was computed from, never with the index it used to
   occupy.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import warnings
import zipfile
from pathlib import Path

import pytest

from comic_automation.archive import source_drift_recovery
from comic_automation.archive.page_hashing import (
    PAGE_HASH_ALGORITHM,
    ArchivePageHashes,
    ArchivePageHashRepository,
    PageContentHash,
    _natural_key,
    calculate_page_hashes,
    order_page_entries,
    page_order_key,
)
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    ArchivePerceptualHashRepository,
    calculate_perceptual_hashes,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from scripts.cbz_library_maintenance import _archive_content_fingerprint
from tests import golden_corpus as gc


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

# Every pair here ties on the natural key: case only, zero padding only,
# and zero padding after a prefix. Mixed into one archive so a single
# permutation exercises all three tie shapes at once.
TIED_NAMES = ("a.png", "A.png", "1.png", "01.png", "001.png", "p1.png",
              "p01.png", "P1.png")

# The order the tie-break must produce for TIED_NAMES, written out rather
# than computed with `page_order_key` so the test is an oracle and not a
# restatement. Natural key first: a leading text part sorts before a
# leading number part, so the letter names come first; within a tie the
# raw names compare by code point, where "A" (65) < "a" (97),
# "0" < "1", and "P" < "p".
EXPECTED_TIED_ORDER = [
    "A.png", "a.png",
    "P1.png", "p01.png", "p1.png",
    "001.png", "01.png", "1.png",
]


def _build_with_duplicates(path: Path, entries: list[tuple[str, bytes]]):
    """`gc.build_cbz`, silencing zipfile's "Duplicate name" UserWarning.

    zipfile writes a second record with the same name and only warns, which
    is exactly how a real archive with duplicate member names comes to exist.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return gc.build_cbz(path, entries)


def _infos(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as archive:
        return archive.infolist()


# --- 1. tie-break --------------------------------------------------------


def test_page_order_key_breaks_natural_key_ties_on_the_raw_name() -> None:
    """The key is (natural key, raw name), so tied names still differ."""
    for first, second in [("a.png", "A.png"), ("01.png", "1.png"),
                          ("p1.png", "p01.png")]:
        assert page_order_key(first)[0] == page_order_key(second)[0]
        assert page_order_key(first) != page_order_key(second)


@pytest.mark.parametrize("seed", range(6))
def test_page_order_is_independent_of_physical_member_order_for_unique_names(
    seed: int, tmp_path: Path
) -> None:
    """Every physical packing of tied-but-unique names gives one order.

    Each trial re-packs the same members in a shuffled order and checks the
    exact page order, the indices and the content signature, plus that the
    result reports itself resolved. Before the tie-break each tied pair kept
    whichever member came first physically, so the signature varied with
    the packing.
    """
    rng = random.Random(seed)
    pages = [(name, f"payload {name}".encode()) for name in TIED_NAMES]
    extras = [(gc.COMIC_INFO, gc.comic_info_xml()), ("notes.txt", b"x")]
    signatures = set()

    for variant in range(4):
        entries = pages + extras
        rng.shuffle(entries)
        result = calculate_page_hashes(
            gc.build_cbz(tmp_path / f"{variant}.cbz", entries)
        )

        assert [p.entry_name for p in result.pages] == EXPECTED_TIED_ORDER
        assert [p.page_index for p in result.pages] == list(
            range(len(TIED_NAMES))
        )
        assert result.order_resolved is True
        assert result.duplicate_entry_names == ()
        signatures.add(result.content_digest)

    assert len(signatures) == 1


def test_perceptual_and_source_drift_inventories_use_the_same_tie_break(
    tmp_path: Path,
) -> None:
    """All three readers of page order agree on tied names.

    The perceptual save refuses unless its result matches the stored exact
    inventory index for index, and the source-drift tool compares its own
    live inventory against the same rows. If either reader ordered tied
    names differently from `calculate_page_hashes`, every tied archive
    would look drifted forever. Packed in reverse of the expected order so
    a reader that falls back to physical order cannot agree by accident.
    """
    entries = [
        (name, gc.page_image(index + 1))
        for index, name in enumerate(EXPECTED_TIED_ORDER)
    ][::-1]
    path = gc.build_cbz(tmp_path / "tied.cbz", entries)

    exact = [p.entry_name for p in calculate_page_hashes(path).pages]
    perceptual = [
        p.entry_name for p in calculate_perceptual_hashes(path).pages
    ]
    _, live = source_drift_recovery._live_inventory(path)

    assert exact == EXPECTED_TIED_ORDER
    assert perceptual == EXPECTED_TIED_ORDER
    assert [row["entry_name"] for row in live] == EXPECTED_TIED_ORDER


# --- 2. duplicate names are unresolved -----------------------------------


@pytest.mark.parametrize(
    "second_payload", [b"first", b"second"], ids=["same-bytes", "diff-bytes"]
)
def test_duplicate_member_names_make_the_archive_unresolved(
    second_payload: bytes, tmp_path: Path
) -> None:
    """Two members named `1.png` cannot be ordered by name.

    Whether or not their bytes differ, the archive is reported unresolved,
    by the ordering helper and on the page-hash result, rather than being
    silently ordered by physical position as if that were an answer.
    """
    path = _build_with_duplicates(
        tmp_path / "dupes.cbz",
        [("2.png", b"two"), ("1.png", b"first"), ("1.png", second_payload)],
    )

    order = order_page_entries(_infos(path))
    result = calculate_page_hashes(path)

    assert order.resolved is False
    assert order.duplicate_names == ("1.png",)
    assert result.order_resolved is False
    assert result.duplicate_entry_names == ("1.png",)
    # Every member is still inventoried: unresolved withholds the order's
    # authority, not the pages.
    assert result.page_count == 3


def test_duplicate_name_detection_is_scoped_to_image_members(
    tmp_path: Path,
) -> None:
    """Duplicated non-image names do not make the *page order* unresolved.

    Only image members are ordered, so only their names can make the order
    ambiguous. Whether duplicated non-image members block a supersession
    comparison is §4.1/§4.4 work for a later step, not this one.
    """
    path = _build_with_duplicates(
        tmp_path / "notes.cbz",
        [("1.png", b"one"), ("notes.txt", b"a"), ("notes.txt", b"b")],
    )

    assert order_page_entries(_infos(path)).resolved is True
    assert calculate_page_hashes(path).order_resolved is True


def test_maintenance_fingerprint_refuses_an_archive_with_duplicate_page_names(
    tmp_path: Path,
) -> None:
    """The deletion guard must not group an archive whose order is unresolved.

    Two packings of the same duplicate-named members produce different
    signatures, and two archives could share one by physical accident. The
    guard returns None, which isolates the archive as a singleton.
    """
    path = _build_with_duplicates(
        tmp_path / "dupes.cbz", [("1.png", b"a"), ("1.png", b"b")]
    )
    control = gc.build_cbz(tmp_path / "ok.cbz", [("1.png", b"a")])

    assert _archive_content_fingerprint(path) is None
    assert _archive_content_fingerprint(control) is not None


# --- database fixtures ----------------------------------------------------


def _seed(connection: sqlite3.Connection, path: Path) -> tuple[int, int]:
    stat = path.stat()
    archive_id = int(
        connection.execute(
            "INSERT INTO archive_files (file_size) VALUES (?)",
            (stat.st_size,),
        ).lastrowid
    )
    location_id = int(
        connection.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns
            )
            VALUES (?, ?, ?, ?)
            """,
            (archive_id, str(path.resolve()), stat.st_size,
             stat.st_mtime_ns),
        ).lastrowid
    )
    connection.commit()
    return archive_id, location_id


def _stored_pages(connection, archive_id: int) -> list[tuple[int, str, str]]:
    """(page_index, entry_name, sha256 digest) as stored, in index order."""
    return [
        (int(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT ap.page_index, ap.entry_name, ph.digest
            FROM archive_pages AS ap
            JOIN page_hashes AS ph
              ON ph.page_id = ap.id
             AND ph.algorithm = ?
            WHERE ap.archive_id = ?
            ORDER BY ap.page_index
            """,
            (PAGE_HASH_ALGORITHM, archive_id),
        )
    ]


def test_duplicate_content_groups_exclude_archives_with_duplicate_entry_names(
    tmp_path: Path,
) -> None:
    """Stored rows expose "unresolved", and the duplicate report honours it.

    Two byte-identical archives with a duplicated page name share a content
    signature, but that signature is a function of physical order and
    proves nothing about page order. They must not form a duplicate group.
    A control pair with unique names still groups, so the exclusion is not
    simply hiding every group.
    """
    dupe_entries = [("1.png", b"a"), ("1.png", b"b"), ("2.png", b"c")]
    unique_entries = [("1.png", b"a"), ("2.png", b"c")]
    paths = [
        _build_with_duplicates(tmp_path / "d1.cbz", dupe_entries),
        _build_with_duplicates(tmp_path / "d2.cbz", dupe_entries),
        gc.build_cbz(tmp_path / "u1.cbz", unique_entries),
        gc.build_cbz(tmp_path / "u2.cbz", unique_entries),
    ]

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        repository = ArchivePageHashRepository(connection)
        ids = []

        for path in paths:
            archive_id, location_id = _seed(connection, path)
            repository.save(
                archive_id=archive_id,
                location_id=location_id,
                result=calculate_page_hashes(path),
            )
            ids.append(archive_id)

        groups = repository.duplicate_content_groups()
        unresolved = repository.unresolved_page_order_archive_ids()

    assert unresolved == ids[:2]
    assert len(groups) == 1
    assert sorted(groups[0]["paths"]) == sorted(
        str(path.resolve()) for path in paths[2:]
    )


# --- 3. re-association by entry name -------------------------------------


def _old_order_result(path: Path) -> ArchivePageHashes:
    """The inventory the pre-tie-break code would have stored for `path`.

    Reproduces the old sort exactly: a stable sort on the natural key alone,
    so tied names keep their physical order. Each digest is still the
    member's own -- the old code was wrong about order, never about which
    bytes a digest came from. Every member in these fixtures is an image.
    """
    pages = []

    with zipfile.ZipFile(path) as archive:
        old_order = sorted(
            archive.infolist(), key=lambda info: _natural_key(info.filename)
        )
        for index, info in enumerate(old_order):
            payload = archive.read(info)
            pages.append(
                PageContentHash(
                    page_index=index,
                    entry_name=info.filename,
                    entry_size=int(info.file_size),
                    compressed_size=int(info.compress_size),
                    crc32=int(info.CRC),
                    digest=hashlib.sha256(payload).hexdigest(),
                    bytes_read=len(payload),
                )
            )

    signature = hashlib.sha256(len(pages).to_bytes(8, "big"))
    for page in pages:
        signature.update(bytes.fromhex(page.digest))
    stat = path.stat()

    return ArchivePageHashes(
        pages=tuple(pages),
        content_digest=signature.hexdigest(),
        image_bytes=sum(page.bytes_read for page in pages),
        source_file_size=int(stat.st_size),
        source_modified_time_ns=int(stat.st_mtime_ns),
    )


def test_reorder_reassociates_hashes_by_entry_name_not_index(
    tmp_path: Path,
) -> None:
    """Re-inventory under the new order keeps each digest with its member.

    Each tied pair is packed opposite to its tie-break order, stored as the
    old code would have stored it, then re-inventoried. Every member moves
    to a different index. Each stored sha256 must equal the sha256 of the
    bytes of the member *named* on that row, and the content signature must
    be the new order's, recomputed rather than carried over.
    """
    payloads = {name: f"bytes of {name}".encode() for name in
                ("a.png", "A.png", "1.png", "01.png")}
    physical = ["a.png", "1.png", "A.png", "01.png"]
    path = gc.build_cbz(
        tmp_path / "tied.cbz", [(name, payloads[name]) for name in physical]
    )
    # Old: natural key only, ties in physical order. New: ties by raw name.
    old_names = ["a.png", "A.png", "1.png", "01.png"]
    expected_names = ["A.png", "a.png", "01.png", "1.png"]

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        archive_id, location_id = _seed(connection, path)
        repository = ArchivePageHashRepository(connection)
        repository.save(
            archive_id=archive_id,
            location_id=location_id,
            result=_old_order_result(path),
        )
        assert [row[1] for row in _stored_pages(connection, archive_id)] == (
            old_names
        )

        fresh = calculate_page_hashes(path)
        repository.save(
            archive_id=archive_id, location_id=location_id, result=fresh
        )
        stored = _stored_pages(connection, archive_id)
        signature = connection.execute(
            "SELECT digest FROM archive_content_signatures "
            "WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()[0]

    assert [row[1] for row in stored] == expected_names
    for index, name, digest in stored:
        assert digest == hashlib.sha256(payloads[name]).hexdigest(), name
    # Every member really did move, so an index-keyed association could
    # not have passed by coincidence.
    assert all(old_names.index(name) != index for index, name, _ in stored)
    assert signature == fresh.content_digest
    assert signature != _old_order_result(path).content_digest


def test_perceptual_save_refuses_rows_stored_under_the_old_order(
    tmp_path: Path,
) -> None:
    """Perceptual digests are never attached to a row by index alone.

    Stored rows are in the old order (natural key only, ties physical); the
    fresh perceptual result is in the new one. The indices line up one for one, so an index-only
    match would write the dhash of `A.png` onto the row named `a.png`. The
    save must refuse instead (the existing source-drift error, which the
    recovery tool resolves by re-inventorying) and write nothing.
    """
    physical = ["a.png", "1.png", "A.png", "01.png"]
    path = gc.build_cbz(
        tmp_path / "tied.cbz",
        [(name, gc.page_image(index + 1))
         for index, name in enumerate(physical)],
    )

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        archive_id, location_id = _seed(connection, path)
        ArchivePageHashRepository(connection).save(
            archive_id=archive_id,
            location_id=location_id,
            result=_old_order_result(path),
        )

        with pytest.raises(OSError, match="Stored page inventory"):
            ArchivePerceptualHashRepository(connection).save(
                archive_id=archive_id,
                result=calculate_perceptual_hashes(path),
            )

        perceptual_rows = connection.execute(
            "SELECT COUNT(*) FROM page_hashes WHERE algorithm = ?",
            (DHASH_ALGORITHM,),
        ).fetchone()[0]

    assert perceptual_rows == 0


def test_perceptual_save_after_reinventory_binds_each_digest_to_its_member(
    tmp_path: Path,
) -> None:
    """After the re-inventory, perceptual rows land on the right members.

    The positive half of the refusal above: once the exact inventory has
    been re-saved under the new order, the perceptual save succeeds and each
    stored dhash equals the dhash computed for the member named on its row.
    """
    physical = ["a.png", "1.png", "A.png", "01.png"]
    path = gc.build_cbz(
        tmp_path / "tied.cbz",
        [(name, gc.page_image(index + 1))
         for index, name in enumerate(physical)],
    )
    perceptual = calculate_perceptual_hashes(path)
    by_name = {page.entry_name: page.dhash for page in perceptual.pages}
    assert len(set(by_name.values())) == len(by_name)

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        archive_id, location_id = _seed(connection, path)
        repository = ArchivePageHashRepository(connection)
        repository.save(
            archive_id=archive_id,
            location_id=location_id,
            result=_old_order_result(path),
        )
        repository.save(
            archive_id=archive_id,
            location_id=location_id,
            result=calculate_page_hashes(path),
        )
        ArchivePerceptualHashRepository(connection).save(
            archive_id=archive_id, result=perceptual
        )
        rows = connection.execute(
            """
            SELECT ap.entry_name, ph.digest
            FROM archive_pages AS ap
            JOIN page_hashes AS ph
              ON ph.page_id = ap.id AND ph.algorithm = ?
            WHERE ap.archive_id = ?
            """,
            (DHASH_ALGORITHM, archive_id),
        ).fetchall()

    assert {str(name): str(digest) for name, digest in rows} == by_name
