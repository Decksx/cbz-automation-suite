"""Only canonical ordered evidence is grouped or aligned.

Design ``docs/canonical_naming_and_version_supersession.md`` §1.2 and §4.1.
A content signature is a digest over pages *in order*, so it is evidence
only under one ordering contract and only when the order exists:

* **Version.** ``CONTENT_SIGNATURE_VERSION`` "1" ordered tied names by
  physical position; "2" breaks ties on the raw name. A "1" row and a "2"
  row can collide for different archives (the counterexample below), so
  readers accept only the current version.
* **Resolved order.** An archive with duplicated raw image names has no
  page order, so it is never grouped and never aligned.

Each reader that groups or aligns on ordered content is exercised at its
real entry point -- ``ArchivePageHashRepository.duplicate_content_groups``,
``content_duplicate_audit.collect_duplicate_groups`` / ``run_audit``, and
``NearDuplicateRepository.load_fingerprints`` / ``generate_candidates`` --
each with a compatible-current positive control, so an exclusion that
simply hid everything would fail too. Synthetic ``tmp_path`` data only.
"""

from __future__ import annotations

import sqlite3
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from comic_automation.archive.content_duplicate_audit import (
    collect_duplicate_groups,
    run_audit,
)
from comic_automation.archive.near_duplicate import NearDuplicateRepository
from comic_automation.archive.page_hashing import (
    CONTENT_SIGNATURE_VERSION,
    ArchivePageHashRepository,
    calculate_page_hashes,
    signature_evidence_counts,
)
from comic_automation.archive.perceptual_hashing import (
    ArchivePerceptualHashRepository,
    calculate_perceptual_hashes,
)
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations
from tests import golden_corpus as gc
from tests.test_page_order_tie_break import _old_order_result


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

LEGACY_VERSION = "1"


def test_the_ordering_contract_version_was_bumped() -> None:
    """The tie-break changed the order, so the version had to move with it."""
    assert CONTENT_SIGNATURE_VERSION == "2"


def _build(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # "Duplicate name"
        return gc.build_cbz(path, entries)


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


def _save(connection, path: Path, *, legacy: bool = False,
          perceptual: bool = False) -> int:
    """Inventory `path` as the current code does, or as the old code did.

    ``legacy=True`` stores the pre-tie-break inventory and labels its
    signature with the old version, which is exactly the row a database
    written before the bump holds.
    """
    archive_id, location_id = _seed(connection, path)
    result = _old_order_result(path) if legacy else calculate_page_hashes(path)
    ArchivePageHashRepository(connection).save(
        archive_id=archive_id, location_id=location_id, result=result
    )
    if perceptual:
        ArchivePerceptualHashRepository(connection).save(
            archive_id=archive_id, result=calculate_perceptual_hashes(path)
        )
    if legacy:
        connection.execute(
            "UPDATE archive_content_signatures SET algorithm_version = ? "
            "WHERE archive_id = ?",
            (LEGACY_VERSION, archive_id),
        )
        connection.commit()
    return archive_id


def _signature(connection, archive_id: int) -> tuple[str, str]:
    row = connection.execute(
        "SELECT digest, algorithm_version FROM archive_content_signatures "
        "WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()
    return str(row[0]), str(row[1])


# --- version gate ---------------------------------------------------------


def test_a_legacy_and_a_current_signature_never_form_one_group(
    tmp_path: Path,
) -> None:
    """Codex's counterexample: different archives, one digest across versions.

    A packs a.png=X, A.png=Y; the old rule keeps physical order for the
    tie and stores X,Y. B packs a.png=Y, A.png=X; the new rule puts A.png
    first and also stores X,Y. The digests collide although the archives
    hold different pages under each name. Neither the repository nor the
    audit may group them, and the audit must not call them actionable.
    """
    x, y = b"page X", b"page Y"
    archive_a = _build(tmp_path / "A.cbz", [("a.png", x), ("A.png", y)])
    archive_b = _build(tmp_path / "B.cbz", [("a.png", y), ("A.png", x)])

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        id_a = _save(connection, archive_a, legacy=True)
        id_b = _save(connection, archive_b)

        digest_a, version_a = _signature(connection, id_a)
        digest_b, version_b = _signature(connection, id_b)
        # Precondition: the collision is real, so a reader that ignored the
        # version would group them.
        assert digest_a == digest_b
        assert (version_a, version_b) == (LEGACY_VERSION, "2")

        repository_groups = ArchivePageHashRepository(
            connection
        ).duplicate_content_groups()
        audit_groups = collect_duplicate_groups(connection)
        counts = signature_evidence_counts(connection)

    assert repository_groups == []
    assert audit_groups == []
    assert counts == {
        "comparable": 1,
        "legacy_ordering_version": 1,
        "unknown_algorithm": 0,
        "unresolved_page_order": 0,
    }


def test_legacy_signatures_are_not_evidence_even_among_themselves(
    tmp_path: Path,
) -> None:
    """Two legacy rows with one digest are diagnostics, not a group.

    Keeping versions apart (grouping by version) is not enough: two
    legacy rows can collide the same way a legacy and a current one can.
    """
    entries = [("1.png", b"one"), ("2.png", b"two")]
    paths = [_build(tmp_path / f"{n}.cbz", entries) for n in range(2)]

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        for path in paths:
            _save(connection, path, legacy=True)

        assert ArchivePageHashRepository(
            connection
        ).duplicate_content_groups() == []
        assert collect_duplicate_groups(connection) == []
        assert signature_evidence_counts(connection)[
            "legacy_ordering_version"
        ] == 2


def test_current_signatures_still_group_and_are_actionable(
    tmp_path: Path,
) -> None:
    """Positive control: compatible current rows group exactly as before."""
    entries = [("a.png", b"page X"), ("A.png", b"page Y")]
    paths = [_build(tmp_path / f"{n}.cbz", entries) for n in range(2)]

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        for path in paths:
            _save(connection, path)

        repository_groups = ArchivePageHashRepository(
            connection
        ).duplicate_content_groups()
        audit_groups = collect_duplicate_groups(connection)

    assert len(repository_groups) == 1
    assert repository_groups[0]["algorithm_version"] == "2"
    assert len(audit_groups) == 1
    assert audit_groups[0]["member_count"] == 2
    assert audit_groups[0]["actionable"] is True


def test_an_unknown_signature_algorithm_is_not_evidence(
    tmp_path: Path,
) -> None:
    """A row from some other algorithm is counted, never grouped."""
    entries = [("1.png", b"one")]
    paths = [_build(tmp_path / f"{n}.cbz", entries) for n in range(2)]

    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)
        for path in paths:
            _save(connection, path)
        connection.execute(
            "UPDATE archive_content_signatures SET algorithm = 'other'"
        )
        connection.commit()

        assert ArchivePageHashRepository(
            connection
        ).duplicate_content_groups() == []
        assert collect_duplicate_groups(connection) == []
        assert signature_evidence_counts(connection)[
            "unknown_algorithm"
        ] == 2


# --- unresolved order at the audit entry point ----------------------------


def test_the_audit_never_reports_an_unresolved_order_as_a_group(
    tmp_path: Path,
) -> None:
    """Three duplicate-named archives: no group, counted as unresolved.

    Run through `run_audit`, the operator entry point, against a closed
    database file. A resolved control pair in the same database must still
    come back as one actionable group.
    """
    duplicated = [("1.png", b"a"), ("1.png", b"b"), ("2.png", b"c")]
    resolved = [("1.png", b"a"), ("2.png", b"b"), ("3.png", b"c")]
    database = tmp_path / "pages.db"

    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        for n in range(3):
            _save(connection, _build(tmp_path / f"d{n}.cbz", duplicated))
        control_ids = [
            _save(connection, _build(tmp_path / f"r{n}.cbz", resolved))
            for n in range(2)
        ]

    report = run_audit(database=database)

    assert [
        sorted(m["archive_id"] for m in group["members"])
        for group in report["groups"]
    ] == [control_ids]
    assert report["groups"][0]["actionable"] is True
    assert report["summary"]["actionable_groups"] == 1
    assert report["excluded_signatures"]["unresolved_page_order"] == 3
    assert report["excluded_signatures"]["comparable"] == 2


# --- near-duplicate loader and generator ----------------------------------


def _tagged_png(seed: int, tag: str) -> bytes:
    """`gc.page_image(seed)` re-encoded with a text chunk.

    Same pixels, so identical dhash and phash, but different bytes, so each
    archive gets its own content signature -- the near-duplicate comparison
    skips pairs whose signatures are equal.
    """
    info = PngInfo()
    info.add_text("archive", tag)
    buffer = BytesIO()
    with Image.open(BytesIO(gc.page_image(seed))) as image:
        image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def test_near_duplicate_loader_excludes_unresolved_and_legacy_archives(
    tmp_path: Path,
) -> None:
    """Only canonical archives are aligned and scored.

    Three duplicate-named archives and one legacy-version archive all
    carry the same page images as two resolved current controls, so
    without the gate every pair among them would score
    page_match_ratio == 1.0. Only the controls may load, and the only
    candidate is the control pair.
    """
    with database_connection(tmp_path / "pages.db") as connection:
        apply_migrations(connection, MIGRATIONS)

        def build(name: str, names: list[str]) -> Path:
            return _build(
                tmp_path / f"{name}.cbz",
                [(entry, _tagged_png(seed, name))
                 for seed, entry in enumerate(names, start=1)],
            )

        for n in range(3):
            _save(connection, build(f"dup{n}", ["1.png", "1.png", "2.png"]),
                  perceptual=True)
        _save(connection, build("legacy", ["1.png", "2.png", "3.png"]),
              legacy=True, perceptual=True)
        controls = [
            _save(connection, build(f"ok{n}", ["1.png", "2.png", "3.png"]),
                  perceptual=True)
            for n in range(2)
        ]

        repository = NearDuplicateRepository(connection)
        loaded = sorted(f.archive_id for f in repository.load_fingerprints())
        candidates = repository.generate_candidates(limit=50)

    assert loaded == controls
    assert [(c.archive_a_id, c.archive_b_id) for c in candidates] == [
        tuple(controls)
    ]
    assert candidates[0].page_match_ratio == 1.0
