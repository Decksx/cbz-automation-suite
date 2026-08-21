"""Perceptual-coverage accounting, on top of the shared contract.

Three kinds of test here, and the distinction matters.

*Arithmetic* tests seed a small library whose numbers can be worked out by
hand, then assert the three measurements report exactly those numbers.

*Neutrality proofs* are the reason this audit was rebuilt. Each one takes a
measurement, changes one thing about the library, and asserts the measurement
did or did not move. Historical coverage must survive a retirement; operational
coverage must survive a file being deleted from disk. These are the properties
the old single-population report silently violated, so they are asserted
directly rather than inferred from a total.

*Bypass proofs* seed a state the audit claims cannot exist -- a half-paired
page, a completed job over unhashed pages, unexplained residue -- and assert
the audit fails rather than reports. A guard exercised only through the path
that respects it has not been demonstrated.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from comic_automation.archive import perceptual_coverage_audit as audit
from comic_automation.archive.perceptual_coverage_audit import (
    DatabaseChangedError,
    DatabaseIntegrityError,
    DatabaseMutatedError,
    EXIT_INVARIANT_VIOLATION,
    EXIT_OK,
    MAX_PRINTED_ARCHIVE_IDS,
    OutputPathCollisionError,
    build_accountability,
    check_invariants,
    fingerprint_database,
    main,
    measure_historical,
    measure_operational,
    page_census,
    readonly_database_connection,
    run_audit,
    validate_output_paths,
)
from comic_automation.archive import classification as C
from comic_automation.archive import disposition
from comic_automation.archive.perceptual_hashing import (
    DHASH_ALGORITHM,
    DHASH_ALGORITHM_VERSION,
    PHASH_ALGORITHM,
    PHASH_ALGORITHM_VERSION,
)
from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

JOB_TYPE = "hash_archive_pages_perceptual"
INSPECT_JOB_TYPE = "inspect_archive"


# --- fixture-building helpers -------------------------------------------


def add_archive(conn: sqlite3.Connection, archive_id: int) -> int:
    conn.execute(
        "INSERT INTO archive_files (id, file_size) VALUES (?, ?)",
        (archive_id, 4096),
    )
    return archive_id


def add_location(
    conn: sqlite3.Connection,
    archive_id: int,
    path: Path | str,
    *,
    is_current: bool = True,
    file_size: int = 4096,
    mtime: int = 111,
) -> None:
    conn.execute(
        """
        INSERT INTO file_locations
            (archive_id, path, is_current, file_size, modified_time_ns)
        VALUES (?, ?, ?, ?, ?)
        """,
        (archive_id, str(path), 1 if is_current else 0, file_size, mtime),
    )


def add_signature(
    conn: sqlite3.Connection,
    archive_id: int,
    *,
    page_count: int = 2,
    file_size: int = 4096,
    mtime: int = 111,
) -> None:
    conn.execute(
        """
        INSERT INTO archive_content_signatures (
            archive_id, algorithm, algorithm_version, digest, page_count,
            image_bytes, source_file_size, source_modified_time_ns
        )
        VALUES (?, 'ordered-page', '1', ?, ?, 1, ?, ?)
        """,
        (archive_id, f"digest-{archive_id}", page_count, file_size, mtime),
    )


def add_pages(
    conn: sqlite3.Connection,
    archive_id: int,
    count: int,
    *,
    hashed: int = 0,
    dhash_only: int = 0,
) -> list[int]:
    """Add `count` pages, the first `hashed` fully covered.

    `dhash_only` seeds pages carrying a dHash and no pHash -- a state the
    hashing worker cannot produce, used by the pairing bypass proof.
    """
    page_ids: list[int] = []

    for index in range(count):
        covered = index < hashed
        cursor = conn.execute(
            """
            INSERT INTO archive_pages (
                archive_id, page_index, entry_name, entry_size,
                compressed_size, crc32, width, height
            )
            VALUES (?, ?, ?, 10, 10, 0, ?, ?)
            """,
            (
                archive_id,
                index,
                f"{index}.jpg",
                100 if covered else None,
                100 if covered else None,
            ),
        )
        page_id = int(cursor.lastrowid)
        page_ids.append(page_id)

        if covered:
            for algorithm, version in (
                (DHASH_ALGORITHM, DHASH_ALGORITHM_VERSION),
                (PHASH_ALGORITHM, PHASH_ALGORITHM_VERSION),
            ):
                add_page_hash(conn, page_id, algorithm, version)

    for page_id in page_ids[hashed : hashed + dhash_only]:
        conn.execute(
            "UPDATE archive_pages SET width = 100, height = 100 "
            "WHERE id = ?",
            (page_id,),
        )
        add_page_hash(
            conn, page_id, DHASH_ALGORITHM, DHASH_ALGORITHM_VERSION
        )

    return page_ids


def add_page_hash(
    conn: sqlite3.Connection, page_id: int, algorithm: str, version: str
) -> None:
    conn.execute(
        """
        INSERT INTO page_hashes
            (page_id, algorithm, algorithm_version, digest, bytes_read)
        VALUES (?, ?, ?, ?, 1)
        """,
        (page_id, algorithm, version, f"h{page_id}{algorithm}"),
    )


def add_job(
    conn: sqlite3.Connection,
    archive_id: int,
    job_type: str,
    status: str,
) -> int:
    column = {
        "completed": "completed_at",
        "failed": "completed_at",
        "cancelled": "cancelled_at",
    }.get(status)

    if column is not None:
        cursor = conn.execute(
            f"INSERT INTO jobs (job_type, archive_id, status, {column}) "
            "VALUES (?, ?, ?, '2026-08-01T00:00:00Z')",
            (job_type, archive_id, status),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO jobs (job_type, archive_id, status) "
            "VALUES (?, ?, ?)",
            (job_type, archive_id, status),
        )

    return int(cursor.lastrowid)


def write_archive_file(path: Path, size: int = 4096) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


def build_library(database: Path, root: Path) -> dict[str, int]:
    """A small library whose every figure can be checked by hand.

    ==  =====  =======  ==================================================
    id  pages  covered  shape
    ==  =====  =======  ==================================================
     1      3        3  present and matching, fully covered
     2      4        2  present and matching, half covered
     3      0        0  zero pages -- in accountability, in neither ratio
     4      5        0  present, will be retired by the neutrality proofs
     5      2        2  file absent from disk: covered, and still ours
    ==  =====  =======  ==================================================

    Totals: 5 identities, 14 pages, 7 covered, 7 outstanding.
    """
    root.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database)

    try:
        apply_migrations(connection, MIGRATIONS)

        for archive_id, pages, hashed, on_disk in (
            (1, 3, 3, True),
            (2, 4, 2, True),
            (3, 0, 0, True),
            (4, 5, 0, True),
            (5, 2, 2, False),
        ):
            add_archive(connection, archive_id)
            path = root / f"{archive_id}.cbz"
            add_location(connection, archive_id, path)
            add_signature(connection, archive_id, page_count=max(pages, 1))
            add_pages(connection, archive_id, pages, hashed=hashed)

            if on_disk:
                write_archive_file(path)

        connection.commit()
    finally:
        connection.close()

    return {
        "identities": 5,
        "pages": 14,
        "covered": 7,
        "outstanding": 7,
    }


def classify_library(database: Path, root: Path | None):
    scope = [str(root)] if root is not None else None

    with readonly_database_connection(database) as connection:
        return C.classify(connection, scope=scope)


@pytest.fixture()
def library(tmp_path: Path):
    """A seeded database plus its declared root."""
    database = tmp_path / "audit.db"
    root = tmp_path / "lib"
    facts = build_library(database, root)
    return database, root, facts


# --- the three measurements ---------------------------------------------


def test_every_archive_appears_exactly_once(library) -> None:
    database, root, facts = library
    output = run_audit(database=database, scope=[str(root)])

    ids = [row["archive_id"] for row in output["archives"]]

    assert len(ids) == facts["identities"]
    assert len(set(ids)) == facts["identities"]


def test_historical_denominator_is_every_inventoried_page(library) -> None:
    database, root, facts = library
    output = run_audit(database=database, scope=[str(root)])
    historical = output["measurements"]["historical"]

    assert historical["total_pages"] == facts["pages"]
    assert historical["covered_pages"] == facts["covered"]
    assert historical["outstanding_pages"] == facts["outstanding"]
    assert historical["excluded_pages"] == 0
    assert historical["excluded_archives"] == 0


def test_operational_equals_historical_without_any_disposition(
    library,
) -> None:
    """With nothing retired, the two measurements must agree exactly.

    They answer different questions, but on a library where nobody has
    decided anything the answers coincide -- and any divergence here
    would mean an observation had leaked into the operational
    denominator.
    """
    database, root, _ = library
    output = run_audit(database=database, scope=[str(root)])
    measurements = output["measurements"]

    assert (
        measurements["operational"]["total_pages"]
        == measurements["historical"]["total_pages"]
    )
    assert (
        measurements["operational"]["covered_pages"]
        == measurements["historical"]["covered_pages"]
    )
    assert measurements["operational"]["excluded_archives"] == 0


def test_zero_page_identities_remain_in_accountability(library) -> None:
    """Archive 3 has no pages, so neither ratio can describe it.

    Accountability exists precisely so that an identity invisible to
    every page-denominator measurement is still named.
    """
    database, root, facts = library
    output = run_audit(database=database, scope=[str(root)])
    accountability = output["measurements"]["accountability"]

    assert accountability["archive_identities"] == facts["identities"]
    assert accountability["zero_page_identity_count"] == 1
    assert accountability["zero_page_archive_ids"] == [3]
    assert sum(accountability["zero_page_subreasons"].values()) == 1


def test_every_axis_sums_to_the_identity_total(library) -> None:
    database, root, facts = library
    output = run_audit(database=database, scope=[str(root)])
    totals = output["measurements"]["accountability"]["axis_totals"]

    for axis in C.AXES:
        assert sum(totals[axis].values()) == facts["identities"], axis


def test_outstanding_pages_reconcile_by_archive_and_by_axis(
    library,
) -> None:
    database, root, facts = library
    output = run_audit(database=database, scope=[str(root)])
    accountability = output["measurements"]["accountability"]

    by_archive = sum(row["outstanding_pages"] for row in output["archives"])
    assert by_archive == facts["outstanding"]

    for axis in C.AXES:
        pages = accountability["outstanding_pages_by_axis"][axis]
        assert sum(pages.values()) == facts["outstanding"], axis


# --- neutrality proofs ---------------------------------------------------


def test_historical_coverage_is_unchanged_by_a_seeded_retirement(
    library,
) -> None:
    """The load-bearing property of the historical measurement.

    Retiring archive 4 removes five pages from operational scope. It must
    not remove them from history: the backfill either hashed those pages
    or it did not, and a decision taken today cannot change what happened.
    """
    database, root, _ = library
    before = run_audit(database=database, scope=[str(root)])["measurements"]

    connection = connect_database(database)
    try:
        disposition.retire(
            connection, 4, reason="out of scope", evidence="operator"
        )
        connection.commit()
    finally:
        connection.close()

    after = run_audit(database=database, scope=[str(root)])["measurements"]

    assert after["historical"] == before["historical"]


def test_historical_coverage_is_unchanged_by_a_seeded_supersession(
    library,
) -> None:
    database, root, _ = library
    before = run_audit(database=database, scope=[str(root)])["measurements"]

    connection = connect_database(database)
    try:
        disposition.supersede(
            connection, 4, 1, reason="replaced", evidence="sha256"
        )
        connection.commit()
    finally:
        connection.close()

    after = run_audit(database=database, scope=[str(root)])["measurements"]

    assert after["historical"] == before["historical"]


def test_operational_coverage_moves_only_by_the_dispositioned_pages(
    library,
) -> None:
    """Retiring archive 4 must move operational by exactly its 5 pages."""
    database, root, facts = library
    before = run_audit(database=database, scope=[str(root)])["measurements"]

    connection = connect_database(database)
    try:
        disposition.retire(
            connection, 4, reason="out of scope", evidence="operator"
        )
        connection.commit()
    finally:
        connection.close()

    after = run_audit(database=database, scope=[str(root)])["measurements"]
    operational = after["operational"]

    assert operational["excluded_archives"] == 1
    assert operational["excluded_pages"] == 5
    assert operational["total_pages"] == facts["pages"] - 5
    # Archive 4 had no covered pages, so the numerator cannot move.
    assert (
        operational["covered_pages"]
        == before["operational"]["covered_pages"]
    )


def _disposition_movement(database: Path, root: Path, apply) -> tuple:
    """Measurements either side of a disposition, and the deltas."""
    before = run_audit(database=database, scope=[str(root)])["measurements"]

    connection = connect_database(database)
    try:
        apply(connection)
        connection.commit()
    finally:
        connection.close()

    after = run_audit(database=database, scope=[str(root)])["measurements"]
    return before, after


@pytest.mark.parametrize(
    "name, apply",
    [
        (
            "retirement",
            lambda conn: disposition.retire(
                conn, 2, reason="out of scope", evidence="operator"
            ),
        ),
        (
            "supersession",
            lambda conn: disposition.supersede(
                conn, 2, 1, reason="replaced", evidence="sha256"
            ),
        ),
    ],
)
def test_a_disposition_moves_numerator_denominator_and_outstanding(
    library, name, apply
) -> None:
    """Archive 2 is *partially* covered: 4 pages, 2 of them hashed.

    That is the whole point of using it. The retirement this library
    actually holds in production (archive 45217) has zero covered pages,
    so an implementation that shrank the denominator while keeping the
    full historical numerator would produce the right answer there and
    be wrong everywhere else. A partially covered archive forces all
    three numbers to move by known, different amounts.
    """
    database, root, facts = library
    before, after = _disposition_movement(database, root, apply)

    operational = after["operational"]

    # Historical is untouched by either kind of decision.
    assert after["historical"] == before["historical"]

    assert operational["excluded_archives"] == 1
    assert operational["excluded_pages"] == 4
    assert operational["excluded_covered_pages"] == 2
    assert operational["excluded_outstanding_pages"] == 2

    assert operational["total_pages"] == facts["pages"] - 4
    assert operational["covered_pages"] == facts["covered"] - 2
    assert operational["outstanding_pages"] == facts["outstanding"] - 2

    # And the numerator really did move -- not merely the denominator.
    assert (
        operational["covered_pages"]
        < before["operational"]["covered_pages"]
    )


def test_operational_coverage_is_unmoved_by_a_file_going_missing(
    library,
) -> None:
    """The 2026-07-28 lesson, as arithmetic.

    Deleting archive 1's file makes it `missing` on the availability
    axis. Availability is an observation, and an observation must not
    retire anything, so neither coverage measurement may move.
    """
    database, root, _ = library
    before = run_audit(database=database, scope=[str(root)])["measurements"]

    (root / "1.cbz").unlink()

    after = run_audit(database=database, scope=[str(root)])["measurements"]

    assert after["historical"] == before["historical"]
    assert after["operational"] == before["operational"]

    # ...and the observation really did register.
    output = run_audit(database=database, scope=[str(root)])
    availability = output["measurements"]["accountability"]["axis_totals"][
        "availability"
    ]
    assert availability[C.AVAILABILITY_MISSING] >= 1


def test_operational_coverage_is_unmoved_by_an_unavailable_declared_root(
    library,
) -> None:
    """A declared root that is not mounted, not an unrelated sibling.

    The distinction is the entire point of the availability axis and it
    is easy to fake by accident: declaring some *other* absent directory
    while the archives live elsewhere puts them outside every declared
    root, which is `undeclared_scope` -- a different value, reached by a
    different code path, proving nothing about unavailability.

    Here the archives stay beneath the declared root and the root itself
    is removed, so the run genuinely cannot look. The emitted value is
    asserted explicitly before any coverage claim, so this test cannot
    silently drift back into testing the wrong thing.
    """
    database, root, facts = library
    before = run_audit(database=database, scope=[str(root)])["measurements"]

    shutil.rmtree(root)
    assert not root.exists()

    output = run_audit(database=database, scope=[str(root)])
    availability = output["measurements"]["accountability"]["axis_totals"][
        "availability"
    ]

    # Every identity is beneath the declared root, so every one of them
    # must be unobservable -- and none may be called missing.
    assert (
        availability[C.AVAILABILITY_UNAVAILABLE_SCOPE]
        == facts["identities"]
    )
    assert availability[C.AVAILABILITY_MISSING] == 0
    assert availability[C.AVAILABILITY_UNDECLARED_SCOPE] == 0

    after = output["measurements"]
    assert after["historical"] == before["historical"]
    assert after["operational"] == before["operational"]


def test_a_path_outside_every_declared_root_is_undeclared_not_missing(
    library, tmp_path: Path
) -> None:
    """The case the old unavailable-root test was accidentally exercising.

    Kept deliberately, and named for what it actually proves: declaring
    an unrelated root leaves the archives outside every declared root,
    which is `undeclared_scope`. It must also move neither measurement.
    """
    database, root, facts = library
    before = run_audit(database=database, scope=[str(root)])["measurements"]

    unrelated = tmp_path / "somewhere-else"
    unrelated.mkdir()

    output = run_audit(database=database, scope=[str(unrelated)])
    availability = output["measurements"]["accountability"]["axis_totals"][
        "availability"
    ]

    assert (
        availability[C.AVAILABILITY_UNDECLARED_SCOPE] == facts["identities"]
    )
    assert availability[C.AVAILABILITY_MISSING] == 0

    after = output["measurements"]
    assert after["historical"] == before["historical"]
    assert after["operational"] == before["operational"]


@pytest.mark.parametrize("availability", C.AVAILABILITIES)
def test_no_availability_value_moves_either_coverage(availability) -> None:
    """Every value on the axis, pinned at the measurement level.

    The database-level tests above cover the values a seeded library can
    actually reach. This covers all ten -- including `unreadable`,
    `non_regular` and `multiple_current_locations`, which need a
    filesystem or a schema violation to reproduce -- so a future value
    cannot quietly start moving a coverage number.
    """
    baseline = _one_archive_result(
        availability=C.AVAILABILITY_PRESENT_MATCHING
    )
    observed = _one_archive_result(availability=availability)

    assert measure_historical(observed.archives) == measure_historical(
        baseline.archives
    )
    assert measure_operational(observed.archives) == measure_operational(
        baseline.archives
    )


def test_a_run_with_no_scope_still_reports_the_same_coverage(
    library,
) -> None:
    """Not looking at the filesystem is an honest answer, not a smaller one."""
    database, root, _ = library
    scoped = run_audit(database=database, scope=[str(root)])
    unscoped = run_audit(database=database)

    assert (
        unscoped["measurements"]["historical"]
        == scoped["measurements"]["historical"]
    )
    assert (
        unscoped["measurements"]["operational"]
        == scoped["measurements"]["operational"]
    )
    assert unscoped["filesystem_consulted"] is False


# --- scope identity ------------------------------------------------------


def test_scope_digest_is_reported_and_order_independent(
    library, tmp_path: Path
) -> None:
    database, root, _ = library
    other = tmp_path / "second"
    other.mkdir()

    one = run_audit(database=database, scope=[str(root), str(other)])
    two = run_audit(database=database, scope=[str(other), str(root)])

    assert one["scope_digest"] == two["scope_digest"]
    assert one["declared_scope"]["digest"] == one["scope_digest"]

    narrower = run_audit(database=database, scope=[str(root)])
    assert narrower["scope_digest"] != one["scope_digest"]


def test_console_prints_the_scope_before_any_number(
    library, capsys
) -> None:
    database, root, _ = library
    output = run_audit(database=database, scope=[str(root)])
    audit.print_summary(output)

    printed = capsys.readouterr().out

    assert "DECLARED SCOPE" in printed
    assert output["scope_digest"] in printed
    # Matched as whole section headings: "COVERAGE" on its own also
    # occurs inside the banner, which would make this pass for the
    # wrong reason.
    assert printed.index("\nDECLARED SCOPE\n") < printed.index(
        "\nCOVERAGE\n"
    )


# --- bypass proofs -------------------------------------------------------


def test_a_half_paired_page_fails_the_audit(tmp_path: Path) -> None:
    """A dHash with no matching pHash is a state no worker can produce.

    The hashing job writes both or neither, so a page carrying one is
    evidence the pairing broke somewhere. The audit must refuse rather
    than publish a coverage number over it.
    """
    database = tmp_path / "half.db"
    root = tmp_path / "lib"
    root.mkdir()
    connection = connect_database(database)

    try:
        apply_migrations(connection, MIGRATIONS)
        add_archive(connection, 1)
        add_location(connection, 1, root / "1.cbz")
        add_signature(connection, 1, page_count=2)
        add_pages(connection, 1, 2, hashed=0, dhash_only=1)
        connection.commit()
    finally:
        connection.close()

    write_archive_file(root / "1.cbz")

    output = run_audit(database=database, scope=[str(root)])

    assert output["page_census"]["half_paired_pages"] == 1
    assert output["page_census"]["dhash_pages"] == 1
    assert output["page_census"]["phash_pages"] == 0
    assert output["invariants_passed"] is False
    assert "no_half_paired_pages" in output["failed_invariants"]
    assert (
        "dhash_and_phash_counts_are_equal" in output["failed_invariants"]
    )


def test_a_completed_job_over_unhashed_pages_fails_the_audit(
    tmp_path: Path,
) -> None:
    """A worker reporting success over work it did not do.

    If this were tolerated every coverage number in the report would
    overstate what actually ran, so it fails the audit rather than
    appearing as a footnote.
    """
    database = tmp_path / "partial.db"
    root = tmp_path / "lib"
    root.mkdir()
    connection = connect_database(database)

    try:
        apply_migrations(connection, MIGRATIONS)
        add_archive(connection, 1)
        add_location(connection, 1, root / "1.cbz")
        add_signature(connection, 1, page_count=4)
        add_pages(connection, 1, 4, hashed=1)
        add_job(connection, 1, JOB_TYPE, "completed")
        connection.commit()
    finally:
        connection.close()

    write_archive_file(root / "1.cbz")

    output = run_audit(database=database, scope=[str(root)])

    assert output["invariants_passed"] is False
    assert (
        "no_completed_job_left_partial_coverage"
        in output["failed_invariants"]
    )


def test_unexplained_residue_fails_the_audit() -> None:
    """Residue is a defect in the classifier, and is treated as one.

    `unexplained` has no predicate, so it cannot be seeded through the
    database -- the contract only emits it when its own eligible and
    excluded sets fail to partition the library. The invariant is
    therefore proven against a classification carrying that residue
    directly, which is the only way this state can be constructed.
    """
    residue = C.ArchiveClassification(
        archive_id=99,
        disposition=C.DISPOSITION_ACTIVE,
        availability=C.AVAILABILITY_PRESENT_MATCHING,
        inventory=C.INVENTORY_COVERED,
        perceptual_work=C.WORK_COMPLETED,
        selection=C.SELECTION_UNEXPLAINED,
        total_pages=2,
        outstanding_pages=0,
    )
    result = C.ClassificationResult(
        archives=(residue,),
        scope=C.DeclaredScope.declare(None),
        filesystem_consulted=False,
    )
    census = {
        "pages": 2,
        "dhash_pages": 2,
        "phash_pages": 2,
        "half_paired_pages": 0,
        "pages_missing_dimensions": 0,
        "covered_pages": 2,
    }

    historical = measure_historical(result.archives)
    operational = measure_operational(result.archives)
    invariants = check_invariants(result, census, historical, operational)
    failed = [
        invariant.name for invariant in invariants if not invariant.passed
    ]

    assert "no_unexplained_residue" in failed

    accountability = build_accountability(result)
    assert accountability["unexplained_count"] == 1
    assert accountability["unexplained_archive_ids"] == [99]


def _one_archive_result(**overrides):
    """A single-archive classification, for invariant-level proofs.

    Some states the invariants guard against cannot be reached through
    the database, because the contract will not emit them. Constructing
    the classification directly is the only way to demonstrate that the
    check would catch one.
    """
    return C.ClassificationResult(
        archives=(C.ArchiveClassification(**_baseline_fields(**overrides)),),
        scope=C.DeclaredScope.declare(None),
        filesystem_consulted=False,
    )


def _baseline_fields(**overrides) -> dict:
    """A legitimate single-archive tuple, before any deliberate defect."""
    fields = {
        "archive_id": 1,
        "disposition": C.DISPOSITION_ACTIVE,
        "availability": C.AVAILABILITY_PRESENT_MATCHING,
        # A terminal failure, not a completed job: this archive has an
        # outstanding page, and a *completed* job over an outstanding
        # page is itself an invariant violation. The baseline has to be
        # a state the audit considers legitimate, or every proof built
        # on it would fail for the wrong reason.
        "inventory": C.INVENTORY_INCOMPLETE,
        "perceptual_work": C.WORK_FAILED,
        "selection": C.SELECTION_EXCLUDED,
        "total_pages": 4,
        "outstanding_pages": 1,
    }
    fields.update(overrides)
    return fields


def _census(**overrides) -> dict[str, int]:
    census = {
        "pages": 4,
        "dhash_pages": 3,
        "phash_pages": 3,
        "half_paired_pages": 0,
        "pages_missing_dimensions": 0,
        "covered_pages": 3,
    }
    census.update(overrides)
    return census


def _failed_invariants(result, census, identities=None) -> list[str]:
    historical = measure_historical(result.archives)
    operational = measure_operational(result.archives)

    return [
        invariant.name
        for invariant in check_invariants(
            result, census, historical, operational, identities
        )
        if not invariant.passed
    ]


def test_the_baseline_invariant_fixture_passes_cleanly() -> None:
    """The control for the constructed-state proofs below.

    Without this, a test asserting some invariant fails could be passing
    because the fixture was broken in some entirely different way.
    """
    assert _failed_invariants(_one_archive_result(), _census()) == []


def test_a_dropped_zero_page_identity_is_caught_by_the_census(
    library,
) -> None:
    """The omission the page census structurally cannot see.

    Archive 3 holds no pages, so removing it from the classification
    leaves every page number in the report reconciling perfectly -- the
    denominator, the numerator, the outstanding count and the whole
    census all still agree. Only comparing against `archive_files`
    notices that the library got smaller.
    """
    database, root, _ = library
    result = classify_library(database, root)

    with readonly_database_connection(database) as connection:
        census = page_census(connection)
        expected_ids = audit.identity_census(connection)

    assert 3 in expected_ids

    pruned = C.ClassificationResult(
        archives=tuple(
            archive for archive in result.archives if archive.archive_id != 3
        ),
        scope=result.scope,
        filesystem_consulted=result.filesystem_consulted,
    )

    # The page-based reconciliations still pass: this is exactly why the
    # identity census had to be a separate measurement.
    page_invariants = _failed_invariants(
        pruned,
        census,
        audit.reconcile_identities(
            pruned, [a.archive_id for a in pruned.archives]
        ),
    )
    assert "historical_denominator_matches_the_page_census" not in (
        page_invariants
    )
    assert "outstanding_pages_reconcile_by_archive" not in page_invariants

    # Against the real identity set, the omission is caught.
    identities = audit.reconcile_identities(pruned, expected_ids)
    assert identities["missing_count"] == 1
    assert identities["missing_archive_ids"] == [3]

    assert (
        "every_archive_has_exactly_one_complete_tuple"
        in _failed_invariants(pruned, census, identities)
    )


def test_an_invented_identity_is_caught_by_the_census(library) -> None:
    database, root, _ = library
    result = classify_library(database, root)

    with readonly_database_connection(database) as connection:
        expected_ids = audit.identity_census(connection)

    invented = C.ClassificationResult(
        archives=result.archives
        + (C.ArchiveClassification(**_baseline_fields(archive_id=9999)),),
        scope=result.scope,
        filesystem_consulted=result.filesystem_consulted,
    )
    identities = audit.reconcile_identities(invented, expected_ids)

    assert identities["extra_count"] == 1
    assert identities["extra_archive_ids"] == [9999]


def test_a_duplicated_classification_row_is_caught(library) -> None:
    database, root, _ = library
    result = classify_library(database, root)

    with readonly_database_connection(database) as connection:
        expected_ids = audit.identity_census(connection)

    doubled = C.ClassificationResult(
        archives=result.archives + (result.archives[0],),
        scope=result.scope,
        filesystem_consulted=result.filesystem_consulted,
    )
    identities = audit.reconcile_identities(doubled, expected_ids)

    assert identities["duplicate_count"] == 1
    assert identities["duplicate_archive_ids"] == [
        result.archives[0].archive_id
    ]


def test_an_incomplete_tuple_is_caught(library) -> None:
    database, root, _ = library
    result = classify_library(database, root)

    with readonly_database_connection(database) as connection:
        expected_ids = audit.identity_census(connection)

    blanked = C.ClassificationResult(
        archives=(
            C.ArchiveClassification(
                **_baseline_fields(archive_id=1, availability="")
            ),
        )
        + result.archives[1:],
        scope=result.scope,
        filesystem_consulted=result.filesystem_consulted,
    )
    identities = audit.reconcile_identities(blanked, expected_ids)

    assert identities["incomplete_tuple_count"] == 1
    assert identities["incomplete_tuple_archive_ids"] == [1]


def test_run_audit_catches_an_identity_the_classifier_dropped(
    library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof that run_audit uses the *independent* census.

    The direct reconcile_identities tests above would still pass if
    run_audit reconciled the classified rows against themselves, because
    they never go through run_audit. This drops a zero-page identity
    inside the snapshot and asserts the assembled report notices -- which
    it can only do by having read archive_files separately.
    """
    database, root, facts = library
    real_collect = audit.collect

    def collect_dropping_archive_three(connection, *, scope):
        result, census, expected_ids = real_collect(
            connection, scope=scope
        )
        pruned = C.ClassificationResult(
            archives=tuple(
                archive
                for archive in result.archives
                if archive.archive_id != 3
            ),
            scope=result.scope,
            filesystem_consulted=result.filesystem_consulted,
        )
        return pruned, census, expected_ids

    monkeypatch.setattr(audit, "collect", collect_dropping_archive_three)

    output = run_audit(database=database, scope=[str(root)])

    assert output["invariants_passed"] is False
    assert (
        "every_archive_has_exactly_one_complete_tuple"
        in output["failed_invariants"]
    )
    assert output["identity_census"]["missing_archive_ids"] == [3]
    assert output["identity_census"]["expected_identities"] == (
        facts["identities"]
    )

    # The page-based reconciliations are untouched, which is exactly why
    # the identity census had to exist: archive 3 has no pages.
    for name in (
        "historical_denominator_matches_the_page_census",
        "covered_pages_match_the_page_census",
        "outstanding_pages_reconcile_by_archive",
    ):
        assert name not in output["failed_invariants"]


def test_an_operational_numerator_that_was_not_subtracted_is_caught(
    library,
) -> None:
    """The invariant, not the measurement, is what this proves.

    `measure_operational` is asserted directly elsewhere. This builds a
    Coverage whose denominator was correctly shrunk while the numerator
    kept every historical covered page -- the precise bug that archive
    45217 cannot reveal on production data -- and asserts the runtime
    check refuses it.
    """
    result = _one_archive_result(
        disposition=C.DISPOSITION_RETIRED,
        total_pages=4,
        outstanding_pages=1,
    )
    historical = measure_historical(result.archives)
    assert (historical.total_pages, historical.covered_pages) == (4, 3)

    unsubtracted = audit.Coverage(
        name="operational",
        covered_pages=historical.covered_pages,
        total_pages=0,
        excluded_pages=4,
        excluded_archives=1,
        excluded_covered_pages=3,
        excluded_outstanding_pages=1,
        basis="deliberately wrong numerator",
    )
    identities = audit.reconcile_identities(result, [1])
    failed = [
        invariant.name
        for invariant in check_invariants(
            result,
            _census(pages=4, dhash_pages=3, phash_pages=3, covered_pages=3),
            historical,
            unsubtracted,
            identities,
        )
        if not invariant.passed
    ]

    assert (
        "operational_numerator_is_historical_minus_dispositioned" in failed
    )
    assert (
        "operational_outstanding_is_historical_minus_dispositioned"
        in failed
    )


def test_the_identity_census_is_reported_in_the_output(library) -> None:
    database, root, facts = library
    output = run_audit(database=database, scope=[str(root)])
    identities = output["identity_census"]

    assert identities["expected_identities"] == facts["identities"]
    assert identities["classified_identities"] == facts["identities"]
    assert identities["missing_count"] == 0
    assert identities["extra_count"] == 0
    assert identities["duplicate_count"] == 0
    assert identities["incomplete_tuple_count"] == 0


def test_an_axis_value_outside_the_vocabulary_fails_to_sum() -> None:
    """Axis totals are built from a fixed vocabulary of known values.

    An archive carrying a value nobody has a definition for is dropped
    from that axis's counts, so the axis stops summing to the identity
    total. That is the failure mode the sum check exists to catch.
    """
    result = _one_archive_result(availability="invented_state")

    assert (
        "every_axis_sums_to_the_identity_total"
        in _failed_invariants(result, _census())
    )


def test_page_totals_that_disagree_with_the_census_are_caught() -> None:
    """The reconciliation is a real comparison, not a restatement.

    The classification says four pages with one outstanding; the census
    is told the library holds ten. Two independent measurements of the
    same library disagreeing is exactly what must not pass silently.
    """
    failed = _failed_invariants(
        _one_archive_result(), _census(pages=10, covered_pages=3)
    )

    assert "historical_denominator_matches_the_page_census" in failed
    assert "outstanding_pages_reconcile_by_archive" in failed
    assert "outstanding_pages_reconcile_by_every_axis" in failed


def test_a_covered_count_that_disagrees_with_the_census_is_caught() -> None:
    failed = _failed_invariants(_one_archive_result(), _census(covered_pages=1))

    assert "covered_pages_match_the_page_census" in failed


def test_unexplained_is_never_produced_by_a_predicate(library) -> None:
    """On a valid library the residue bucket is empty.

    Paired with the test above: one proves the audit fails on residue,
    this proves the residue is not manufactured by ordinary data.
    """
    database, root, _ = library
    output = run_audit(database=database, scope=[str(root)])

    assert output["measurements"]["accountability"]["unexplained_count"] == 0
    assert output["invariants_passed"] is True


def test_the_removed_backlog_flag_appears_nowhere_in_the_report(
    library, tmp_path: Path, capsys
) -> None:
    """`never_enqueued_backlog` is gone, not renamed.

    Asserted across every surface it used to occupy -- the JSON payload,
    the CSV header and the console -- because a positive predicate that
    survives in any one of them is still a positive predicate.
    """
    database, root, _ = library
    json_output = tmp_path / "report.json"
    csv_output = tmp_path / "report.csv"

    output = run_audit(
        database=database,
        scope=[str(root)],
        json_output=json_output,
        csv_output=csv_output,
    )
    audit.print_summary(output)
    printed = capsys.readouterr().out

    assert "never_enqueued_backlog" not in json.dumps(output)
    assert "never_enqueued_backlog" not in json_output.read_text(
        encoding="utf-8"
    )
    assert "never_enqueued_backlog" not in csv_output.read_text(
        encoding="utf-8-sig"
    )
    assert "never_enqueued_backlog" not in printed

    # And not merely renamed: no key anywhere in the payload, and no
    # CSV column, may carry the concept under a different spelling.
    # (The console is excluded from this sweep because the temporary
    # database path pytest generates contains this test's own name.)
    assert not [
        key for key in _all_keys(output) if "backlog" in key.lower()
    ]
    assert "backlog" not in csv_output.read_text(
        encoding="utf-8-sig"
    ).splitlines()[0].lower()


def _all_keys(payload) -> list[str]:
    """Every mapping key in a nested JSON-shaped structure."""
    if isinstance(payload, dict):
        keys: list[str] = []

        for key, value in payload.items():
            keys.append(key)
            keys.extend(_all_keys(value))

        return keys

    if isinstance(payload, list):
        return [key for item in payload for key in _all_keys(item)]

    return []


# --- read-only guarantees ------------------------------------------------


def test_read_only_connection_rejects_writes(library) -> None:
    database, _, _ = library

    with readonly_database_connection(database) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE archive_files SET file_size = 1 WHERE id = 1"
            )


def test_run_audit_leaves_the_database_byte_identical(library) -> None:
    database, root, _ = library

    before = fingerprint_database(database)
    before_bytes = database.read_bytes()

    run_audit(database=database, scope=[str(root)])

    assert fingerprint_database(database) == before
    assert database.read_bytes() == before_bytes


def test_run_audit_reports_the_snapshot_boundary(library) -> None:
    database, root, _ = library
    output = run_audit(database=database, scope=[str(root)])

    assert output["quick_check"] == "ok"
    assert output["data_version_before"] == output["data_version_after"]
    assert output["data_version_unchanged"] is True
    assert output["concurrent_commit_detected"] is False
    assert output["database_file_unchanged"] is True


def test_a_commit_mid_read_invalidates_the_report(
    library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The data_version bracket, proven by bypassing it.

    A writer committing between the classification and the page census
    would otherwise produce a report whose two halves describe different
    states of the library while still looking clean -- under WAL the
    commit can land entirely in the -wal file, leaving the main
    database's size and mtime untouched.
    """
    database, root, _ = library
    real_collect = audit.collect
    fingerprints: dict[str, object] = {}

    def collect_then_external_commit(connection, *, scope):
        result = real_collect(connection, scope=scope)

        fingerprints["before"] = fingerprint_database(database)

        with database_connection(database) as other:
            add_job(other, 1, JOB_TYPE, "pending")

        fingerprints["after"] = fingerprint_database(database)

        return result

    monkeypatch.setattr(audit, "collect", collect_then_external_commit)

    with pytest.raises(DatabaseChangedError) as raised:
        run_audit(database=database, scope=[str(root)])

    # Specifically the data_version guard, not the fingerprint fallback:
    # DatabaseMutatedError is a subclass, so the exact type is what says
    # which detector fired.
    assert type(raised.value) is DatabaseChangedError
    assert "data_version" in str(raised.value)

    # And this is why the bracket is required: at the moment of the
    # commit the main file's size and mtime were unchanged, so the
    # fingerprint comparison could not have raised.
    assert fingerprints["before"] == fingerprints["after"]


def test_run_audit_raises_if_the_database_file_is_mutated(
    library, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, root, _ = library
    original = audit.fingerprint_database
    calls = {"count": 0}

    def mutating_fingerprint(path):
        calls["count"] += 1
        if calls["count"] == 2:
            database.write_bytes(database.read_bytes() + b"\x00")
        return original(path)

    monkeypatch.setattr(audit, "fingerprint_database", mutating_fingerprint)

    with pytest.raises(DatabaseMutatedError):
        run_audit(database=database, scope=[str(root)])


def test_quick_check_failure_raises_integrity_error(
    library, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, root, _ = library

    monkeypatch.setattr(
        audit, "quick_check", lambda connection: "malformed page 7"
    )

    with pytest.raises(DatabaseIntegrityError):
        run_audit(database=database, scope=[str(root)])


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_audit(database=tmp_path / "absent.db")


# --- output paths --------------------------------------------------------


def test_validate_output_paths_rejects_json_matching_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"

    with pytest.raises(OutputPathCollisionError):
        validate_output_paths(
            database, json_output=database, csv_output=None
        )


def test_validate_output_paths_rejects_csv_matching_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"

    with pytest.raises(OutputPathCollisionError):
        validate_output_paths(
            database, json_output=None, csv_output=database
        )


def test_validate_output_paths_rejects_json_matching_csv(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "report.out"

    with pytest.raises(OutputPathCollisionError):
        validate_output_paths(
            tmp_path / "audit.db", json_output=shared, csv_output=shared
        )


def test_run_audit_rejects_an_output_colliding_with_the_database(
    library,
) -> None:
    database, root, _ = library

    with pytest.raises(OutputPathCollisionError):
        run_audit(
            database=database, scope=[str(root)], json_output=database
        )


# --- artefacts and CLI ---------------------------------------------------


def test_json_and_csv_carry_the_full_classification_tuple(
    library, tmp_path: Path
) -> None:
    database, root, facts = library
    json_output = tmp_path / "report.json"
    csv_output = tmp_path / "report.csv"

    run_audit(
        database=database,
        scope=[str(root)],
        json_output=json_output,
        csv_output=csv_output,
    )

    payload = json.loads(json_output.read_text(encoding="utf-8"))
    assert payload["measurements"]["historical"]["total_pages"] == (
        facts["pages"]
    )
    assert payload["scope_digest"]

    lines = csv_output.read_text(encoding="utf-8-sig").splitlines()
    header = lines[0].split(",")

    for column in (
        "disposition",
        "availability",
        "inventory",
        "perceptual_work",
        "selection",
        "total_pages",
        "covered_pages",
        "outstanding_pages",
    ):
        assert column in header

    assert len(lines) == facts["identities"] + 1


def test_cli_main_succeeds_on_a_clean_library(
    library, tmp_path: Path, capsys
) -> None:
    database, root, _ = library
    json_output = tmp_path / "report.json"

    exit_code = main(
        [
            "--database",
            str(database),
            "--scope",
            str(root),
            "--json-output",
            str(json_output),
        ]
    )

    printed = capsys.readouterr().out

    assert exit_code == EXIT_OK
    assert json_output.is_file()
    assert "HISTORICAL" in printed
    assert "OPERATIONAL" in printed
    assert "ACCOUNTABILITY" in printed


def test_cli_main_exits_nonzero_on_a_failed_invariant(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "half.db"
    root = tmp_path / "lib"
    root.mkdir()
    connection = connect_database(database)

    try:
        apply_migrations(connection, MIGRATIONS)
        add_archive(connection, 1)
        add_location(connection, 1, root / "1.cbz")
        add_signature(connection, 1, page_count=2)
        add_pages(connection, 1, 2, hashed=0, dhash_only=1)
        connection.commit()
    finally:
        connection.close()

    write_archive_file(root / "1.cbz")

    exit_code = main(
        ["--database", str(database), "--scope", str(root)]
    )

    assert exit_code == EXIT_INVARIANT_VIOLATION
    assert "no_half_paired_pages" in capsys.readouterr().err


def test_cli_main_reports_a_missing_database_without_traceback(
    tmp_path: Path, capsys
) -> None:
    exit_code = main(["--database", str(tmp_path / "absent.db")])

    assert exit_code == 1
    assert "ERROR" in capsys.readouterr().err


def test_console_caps_printed_ids_and_states_the_omitted_count(
    capsys,
) -> None:
    audit.print_archive_id_sample(
        list(range(MAX_PRINTED_ARCHIVE_IDS + 5)),
        label="UNEXPLAINED",
    )

    printed = capsys.readouterr().out

    assert "and 5 more" in printed
    assert "full list in the JSON and CSV outputs" in printed


def test_short_id_lists_print_without_an_omitted_count(capsys) -> None:
    audit.print_archive_id_sample([1, 2, 3], label="UNEXPLAINED")

    printed = capsys.readouterr().out

    assert "1, 2, 3" in printed
    assert "more" not in printed


# --- the page census is a second opinion ---------------------------------


def test_the_census_is_computed_independently_of_the_contract(
    library,
) -> None:
    """The reconciliation would be vacuous if both sides shared a source.

    The census scans archive_pages directly; the coverage figures come
    from summing the contract's per-archive numbers. The invariants
    compare them, so this asserts they really are two measurements.
    """
    database, root, facts = library

    with readonly_database_connection(database) as connection:
        census = page_census(connection)

    result = classify_library(database, root)
    historical = measure_historical(result.archives)

    assert census["pages"] == facts["pages"]
    assert census["covered_pages"] == facts["covered"]
    assert historical.total_pages == census["pages"]
    assert historical.covered_pages == census["covered_pages"]
