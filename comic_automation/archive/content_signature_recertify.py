"""Re-certify legacy ordered content signatures that the new order cannot move.

Steps 2, 3 and 5 of the re-certification plan in
``docs/engineering_decisions.md`` ("Page order breaks natural-key ties on the
raw name; duplicate names are unresolved"). Step 4, the re-inventory of the
archives this tool refuses, is a separate operator action and is not here.

``CONTENT_SIGNATURE_VERSION`` moved from ``"1"`` (natural key, ties left in
physical member order) to ``"2"`` (ties broken on the raw name). Every
version-``"1"`` row is therefore excluded from comparison. For an archive
whose stored image names have *no* natural-key tie and *no* repeat, the two
contracts order the names identically, so the stored digest already *is* the
version-``"2"`` digest; relabelling it loses nothing and recomputes nothing.
This tool finds those rows and relabels only them.

Three modes
-----------

``plan``
    Read-only. One deferred read transaction bracketed by
    ``PRAGMA data_version`` (`read_guards.read_consistent_snapshot`, which
    also opens ``mode=ro`` + ``query_only``). Classifies every
    ``ordered-page-sha256`` / version-``"1"`` signature into exactly one of
    ``stale``, ``unresolved``, ``tied``, ``inconsistent`` or ``order_stable``
    (precedence in that order) and writes a JSON plan carrying a digest over
    its canonical bytes.

``apply``
    Requires the plan file, ``--expect-digest``, ``--expect-count`` and a
    verified ``backup_cli`` report for the same database. The plan is
    refused *entirely* if its recomputed digest, the expected digest, or the
    expected count disagree. Otherwise one ``BEGIN IMMEDIATE`` transaction
    (`dal.transaction`) re-classifies each planned archive from the rows as
    they are *inside that transaction* and relabels it only if the result is
    still ``order_stable`` with exactly the planned basis. A row that moved
    is skipped and reported, never relabelled. A row already at version
    ``"2"`` with the planned basis is reported ``already_recertified``, so a
    re-run is a no-op. Only ``archive_content_signatures.algorithm_version``
    and ``updated_at`` change; page rows, page hashes and perceptual rows are
    not written.

``postflight``
    Read-only, same snapshot guard. Reports `signature_evidence_counts`
    (population: every signature row, any location state), the
    current-location-scoped classification of the legacy rows that remain,
    and the planned order-stable archives still at version ``"1"``.

Provenance -- what a relabel certifies, and what it does not
------------------------------------------------------------

**Recorded-metadata provenance only. No archive file is opened.** A row is
"bound to current bytes" in exactly the sense `enqueue_missing` already uses:
``archive_content_signatures.source_file_size`` equals
``file_locations.file_size`` and ``archive_content_signatures
.source_modified_time_ns`` equals ``file_locations.modified_time_ns`` for the
archive's single ``is_current = 1`` location. Those are recorded values;
equality is a diagnostic, not proof that the stored page rows describe the
bytes on disk today. The relabel certifies that **the recorded order** of
the stored inventory is the order version ``"2"`` would assign to the stored
names, and that the stored digest equals the version-``"2"`` digest
recomputed from the stored page sha256 rows. It does **not** satisfy design
#112's full-file / member validation for authoritative supersession, and it
says nothing about live bytes. Perceptual rows are retained because their
binding -- ``page_hashes.page_id`` to an ``archive_pages`` row whose
``(page_index, entry_name)`` is unchanged -- is untouched by a relabel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Iterable, Sequence

from comic_automation.archive import output_guards
from comic_automation.archive.page_hashing import (
    CONTENT_SIGNATURE_ALGORITHM,
    CONTENT_SIGNATURE_VERSION,
    PAGE_HASH_ALGORITHM,
    PAGE_HASH_ALGORITHM_VERSION,
    _natural_key,
    page_order_key,
    signature_evidence_counts,
)
from comic_automation.database.dal import open_connection, transaction
from comic_automation.database.read_guards import (
    quick_check,
    read_consistent_snapshot,
)


PLAN_FORMAT = "content-signature-recertify/1"
LEGACY_VERSION = "1"
# The version this tool relabels to. Pinned to the module constant so a
# future bump cannot silently turn this into a "1" -> "3" relabel: the order
# argument below is about "1" versus "2" specifically.
TARGET_VERSION = CONTENT_SIGNATURE_VERSION

STALE = "stale"
UNRESOLVED = "unresolved"
TIED = "tied"
INCONSISTENT = "inconsistent"
ORDER_STABLE = "order_stable"
# Precedence: the first class that applies wins, so every row is in one.
CLASSES = (STALE, UNRESOLVED, TIED, INCONSISTENT, ORDER_STABLE)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_INCOMPLETE = 3

PROVENANCE = (
    "Recorded-metadata provenance only; no archive file was opened. A row is "
    "treated as bound to current bytes when archive_content_signatures."
    "source_file_size = file_locations.file_size and "
    "archive_content_signatures.source_modified_time_ns = "
    "file_locations.modified_time_ns for the archive's single is_current = 1 "
    "location. That equality is a diagnostic, not proof that the stored page "
    "rows describe the live bytes. Re-certification certifies the recorded "
    "order only: the stored entry names sort identically under ordering "
    "contracts 1 and 2, and the stored digest equals the version-2 digest "
    "recomputed from the stored page sha256 rows. It does not satisfy "
    "full-file/member validation for authoritative supersession."
)

POPULATION_REQUIREMENT = (
    "signature_evidence_counts counts every archive_content_signatures row, "
    "whatever its location state. The postflight condition is scoped: no "
    "order_stable legacy row may remain for a current location, and no "
    "planned order_stable archive may remain at version 1, including one "
    "apply skipped because it drifted into another class. Rows classified "
    "stale, unresolved, tied or "
    "inconsistent stay legacy (excluded from comparison) until step 4's "
    "operator-approved re-inventory rewrites them."
)


class PlanRefusedError(RuntimeError):
    """The plan, its digest or its count disagree; nothing was written."""


@dataclass(frozen=True)
class Classification:
    """One signature's class, why, and -- if order-stable -- its basis.

    ``basis`` is everything apply re-derives and compares before it writes:
    any field that differs means the stored state is not the one planned.
    """

    archive_id: int
    algorithm_version: str
    cls: str
    reason: str
    basis: dict | None
    detail: tuple[str, ...] = ()


def _names_digest(names: Sequence[str]) -> str:
    """Digest of the stored entry names in page_index order.

    Lets apply detect a renamed, added, removed or reordered page row without
    the plan carrying every name of every archive.
    """
    payload = json.dumps(list(names), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ordered_signature_digest(page_digests: Iterable[str]) -> str:
    """`calculate_page_hashes`'s signature over already-ordered page digests.

    The 8-byte big-endian page count, then each page sha256's raw bytes, in
    order. Restated here rather than imported because `calculate_page_hashes`
    computes it inline from a live archive; a test pins it to that function's
    output for the same pages.
    """
    digests = list(page_digests)
    signature = hashlib.sha256(len(digests).to_bytes(8, "big"))
    for digest in digests:
        signature.update(bytes.fromhex(digest))
    return signature.hexdigest()


def classify(
    signature: sqlite3.Row | dict,
    locations: Sequence[sqlite3.Row | dict],
    pages: Sequence[sqlite3.Row | dict],
) -> Classification:
    """Classify one signature from stored rows alone.

    ``signature`` is its ``archive_content_signatures`` row; ``locations``
    its ``is_current = 1`` ``file_locations`` rows; ``pages`` its
    ``archive_pages`` rows ordered by ``page_index``, each carrying the
    exact sha256 ``digest`` (None when that page has no sha256 row).

    The version label is *not* consulted: callers decide which versions they
    accept, so apply can recognise a row it already relabelled.
    """
    archive_id = int(signature["archive_id"])
    version = str(signature["algorithm_version"])

    def result(cls: str, reason: str, detail=(), basis=None) -> Classification:
        return Classification(
            archive_id, version, cls, reason, basis, tuple(detail)
        )

    # Stale first: a row not bound to the current location's recorded
    # metadata goes to the ordinary rehash path whatever its names say.
    if not locations:
        return result(STALE, "no_current_location")
    if len(locations) > 1:
        # Two current locations means the binding is ambiguous; choosing one
        # would be a guess about which file the signature describes.
        return result(STALE, "multiple_current_locations")
    location = locations[0]
    if (
        location["file_size"] is None
        or location["modified_time_ns"] is None
        or int(signature["source_file_size"]) != int(location["file_size"])
        or int(signature["source_modified_time_ns"])
        != int(location["modified_time_ns"])
    ):
        return result(STALE, "source_metadata_differs")

    names = [str(page["entry_name"]) for page in pages]

    # A repeated raw name has no order under any contract (SQLite's BINARY
    # comparison and Python's == are the same raw equality).
    repeated = sorted(name for name, n in Counter(names).items() if n > 1)
    if repeated:
        return result(UNRESOLVED, "repeated_entry_name", repeated)

    # Distinct names that tie on the natural key are exactly where "1" left
    # physical order in charge and "2" sorts by raw name.
    by_key: dict = {}
    for name in names:
        by_key.setdefault(_natural_key(name), []).append(name)
    tied = sorted(
        name for group in by_key.values() if len(group) > 1 for name in group
    )
    if tied:
        return result(TIED, "natural_key_tie", tied)

    # The stored inventory must be the basis the stored digest claims, or a
    # relabel would certify a digest nobody can reproduce from the rows.
    if len(pages) != int(signature["page_count"]):
        return result(INCONSISTENT, "page_count_differs")
    if [int(page["page_index"]) for page in pages] != list(range(len(pages))):
        return result(INCONSISTENT, "page_index_not_contiguous")
    if any(page["digest"] is None for page in pages):
        return result(INCONSISTENT, "missing_page_sha256")
    if sorted(names, key=page_order_key) != names:
        # No tie means "1" and "2" agree, so a stored order that is not the
        # "2" order was not written by either contract.
        return result(INCONSISTENT, "stored_order_not_page_order")
    by_name = {str(p["entry_name"]): str(p["digest"]) for p in pages}
    recomputed = ordered_signature_digest(
        by_name[name] for name in sorted(names, key=page_order_key)
    )
    if recomputed != str(signature["digest"]):
        return result(INCONSISTENT, "digest_not_reproducible")

    return result(
        ORDER_STABLE,
        "no_tie_no_repeat",
        basis={
            "archive_id": archive_id,
            "signature_id": int(signature["id"]),
            "digest": str(signature["digest"]),
            "page_count": int(signature["page_count"]),
            "source_file_size": int(signature["source_file_size"]),
            "source_modified_time_ns": int(
                signature["source_modified_time_ns"]
            ),
            "current_location_id": int(location["id"]),
            "entry_names_sha256": _names_digest(names),
        },
    )


_SIGNATURE_COLUMNS = """
    acs.id, acs.archive_id, acs.algorithm, acs.algorithm_version,
    acs.digest, acs.page_count, acs.source_file_size,
    acs.source_modified_time_ns
"""

_PAGES_SQL = """
    SELECT ap.archive_id, ap.page_index, ap.entry_name, ph.digest
    FROM archive_pages AS ap
    LEFT JOIN page_hashes AS ph
      ON ph.page_id = ap.id
     AND ph.algorithm = ?
     AND ph.algorithm_version = ?
    WHERE {where}
    ORDER BY ap.archive_id, ap.page_index
"""


def classify_legacy(connection: sqlite3.Connection) -> list[Classification]:
    """Classify every legacy signature, ascending by archive id.

    Three set-based reads, merged in Python, so the cost is one pass over the
    legacy archives' page rows rather than a query per archive.
    """
    params = (CONTENT_SIGNATURE_ALGORITHM, LEGACY_VERSION)
    legacy = (
        "SELECT archive_id FROM archive_content_signatures "
        "WHERE algorithm = ? AND algorithm_version = ?"
    )
    signatures = connection.execute(
        f"SELECT {_SIGNATURE_COLUMNS} FROM archive_content_signatures AS acs "
        "WHERE acs.algorithm = ? AND acs.algorithm_version = ? "
        "ORDER BY acs.archive_id",
        params,
    ).fetchall()
    locations: dict[int, list] = {}
    for row in connection.execute(
        "SELECT id, archive_id, file_size, modified_time_ns "
        f"FROM file_locations WHERE is_current = 1 AND archive_id IN ({legacy}) "
        "ORDER BY archive_id, id",
        params,
    ):
        locations.setdefault(int(row["archive_id"]), []).append(row)
    pages: dict[int, list] = {}
    cursor = connection.execute(
        _PAGES_SQL.format(where=f"ap.archive_id IN ({legacy})"),
        (PAGE_HASH_ALGORITHM, PAGE_HASH_ALGORITHM_VERSION, *params),
    )
    for archive_id, rows in groupby(cursor, key=lambda r: int(r["archive_id"])):
        pages[archive_id] = list(rows)

    return [
        classify(
            signature,
            locations.get(int(signature["archive_id"]), []),
            pages.get(int(signature["archive_id"]), []),
        )
        for signature in signatures
    ]


def classify_archive(
    connection: sqlite3.Connection, archive_id: int
) -> Classification | None:
    """Classify one archive's signature as stored now, whatever its version.

    None when the archive has no signature of this algorithm. Used by apply
    inside its write transaction, so what it sees is what the UPDATE acts on.
    """
    signature = connection.execute(
        f"SELECT {_SIGNATURE_COLUMNS} FROM archive_content_signatures AS acs "
        "WHERE acs.archive_id = ? AND acs.algorithm = ?",
        (archive_id, CONTENT_SIGNATURE_ALGORITHM),
    ).fetchone()
    if signature is None:
        return None
    locations = connection.execute(
        "SELECT id, archive_id, file_size, modified_time_ns "
        "FROM file_locations WHERE is_current = 1 AND archive_id = ? "
        "ORDER BY id",
        (archive_id,),
    ).fetchall()
    pages = connection.execute(
        _PAGES_SQL.format(where="ap.archive_id = ?"),
        (PAGE_HASH_ALGORITHM, PAGE_HASH_ALGORITHM_VERSION, archive_id),
    ).fetchall()
    return classify(signature, locations, pages)


# --- plan -----------------------------------------------------------------


def canonical_bytes(value: Any) -> bytes:
    """The one rendering the plan digest is taken over."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def plan_digest(body: dict) -> str:
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def build_plan_body(classifications: Sequence[Classification]) -> dict:
    """The digested part of a plan: classes, lists and counts, nothing else.

    Snapshot readings (data_version, the database path) are kept out of the
    body, so two plans over the same rows have the same digest.
    """
    by_class: dict[str, list[Classification]] = {cls: [] for cls in CLASSES}
    for item in classifications:
        by_class[item.cls].append(item)

    def listing(cls: str) -> list[dict]:
        return [
            {
                "archive_id": item.archive_id,
                "reason": item.reason,
                "names": list(item.detail),
            }
            for item in by_class[cls]
        ]

    reinventory = sorted(
        item.archive_id
        for cls in (TIED, UNRESOLVED, INCONSISTENT)
        for item in by_class[cls]
    )
    return {
        "plan_format": PLAN_FORMAT,
        "algorithm": CONTENT_SIGNATURE_ALGORITHM,
        "from_version": LEGACY_VERSION,
        "to_version": TARGET_VERSION,
        "provenance": PROVENANCE,
        "population_requirement": POPULATION_REQUIREMENT,
        "counts": {
            "legacy_total": len(classifications),
            **{cls: len(by_class[cls]) for cls in CLASSES},
            "reinventory_cost": len(reinventory),
        },
        "order_stable": [item.basis for item in by_class[ORDER_STABLE]],
        "tied": listing(TIED),
        "unresolved": listing(UNRESOLVED),
        "inconsistent": listing(INCONSISTENT),
        "stale": listing(STALE),
        # Step 4's population: archives whose digest must be recomputed from
        # bytes. Stale rows are not in it; they take the ordinary rehash path.
        "reinventory_archive_ids": reinventory,
    }


def make_plan(database: str | Path) -> dict:
    """Plan from one WAL-aware read-only snapshot. Writes nothing."""
    snapshot = read_consistent_snapshot(
        database,
        classify_legacy,
        context="content signature re-certification plan",
        integrity_check=quick_check,
    )
    body = build_plan_body(snapshot.result)
    return {
        "plan": body,
        "plan_digest": plan_digest(body),
        "snapshot": {
            "database": str(snapshot.database),
            **snapshot.report_fields(),
        },
    }


# --- apply ----------------------------------------------------------------


def _check_backup_report(database: Path, backup_report: Path) -> None:
    """Refuse unless a verified backup_cli report names this database.

    The backup is the operator's (``python -m
    comic_automation.database.backup_cli``); this only refuses an apply that
    cannot point at one, so "backup before apply" is checked, not trusted.
    """
    try:
        report = json.loads(Path(backup_report).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlanRefusedError(f"Unreadable backup report: {error}") from error
    if report.get("verified") is not True:
        raise PlanRefusedError(
            f"Backup report {backup_report} is not verified."
        )
    source = report.get("source_database")
    if not source or not output_guards.same_file(source, database):
        raise PlanRefusedError(
            f"Backup report {backup_report} is for {source!r}, not {database}."
        )


def load_plan(
    plan_path: str | Path, *, expect_digest: str, expect_count: int
) -> dict:
    """Read a plan back and refuse it unless every number agrees.

    The digest is recomputed over the body as read, never taken from the
    file, so an edited list cannot ride under its original digest.
    """
    try:
        document = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        body = document["plan"]
        recorded = document["plan_digest"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PlanRefusedError(f"Unreadable plan {plan_path}: {error}") from error

    recomputed = plan_digest(body)
    if recomputed != recorded:
        raise PlanRefusedError(
            f"Plan digest mismatch: file records {recorded}, body hashes to "
            f"{recomputed}. The plan was edited after it was written."
        )
    if recomputed != expect_digest:
        raise PlanRefusedError(
            f"--expect-digest {expect_digest} does not match the plan "
            f"({recomputed})."
        )
    if (
        body.get("plan_format") != PLAN_FORMAT
        or body.get("algorithm") != CONTENT_SIGNATURE_ALGORITHM
        or body.get("from_version") != LEGACY_VERSION
        or body.get("to_version") != TARGET_VERSION
    ):
        raise PlanRefusedError(
            "Plan was written for another format, algorithm or version pair."
        )
    planned = body["order_stable"]
    if len(planned) != expect_count:
        raise PlanRefusedError(
            f"--expect-count {expect_count} does not match the plan's "
            f"{len(planned)} order-stable row(s)."
        )
    return body


def apply_plan(
    database: str | Path,
    plan_path: str | Path,
    *,
    expect_digest: str,
    expect_count: int,
    backup_report: str | Path,
) -> dict:
    """Relabel the planned order-stable rows that are still exactly as planned.

    Refusals (`PlanRefusedError`) happen before the database is opened for
    writing. Per-row drift is not a refusal: that row is skipped and named,
    the others proceed, all inside one transaction.
    """
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")
    body = load_plan(
        plan_path, expect_digest=expect_digest, expect_count=expect_count
    )
    _check_backup_report(database, Path(backup_report))

    relabelled: list[int] = []
    already: list[int] = []
    skipped: list[dict] = []

    connection = open_connection(database)
    try:
        with transaction(connection):
            for planned in body["order_stable"]:
                archive_id = int(planned["archive_id"])
                # Re-derived inside the write lock: the plan is a claim about
                # an earlier snapshot, and only this read is what the UPDATE
                # will act on.
                now = classify_archive(connection, archive_id)
                if now is None:
                    skipped.append(
                        {"archive_id": archive_id, "reason": "signature_missing"}
                    )
                    continue
                if now.cls != ORDER_STABLE:
                    skipped.append(
                        {
                            "archive_id": archive_id,
                            "reason": f"now_{now.cls}:{now.reason}",
                        }
                    )
                    continue
                if now.basis != planned:
                    skipped.append(
                        {"archive_id": archive_id, "reason": "basis_changed"}
                    )
                    continue
                if now.algorithm_version == TARGET_VERSION:
                    already.append(archive_id)
                    continue
                if now.algorithm_version != LEGACY_VERSION:
                    skipped.append(
                        {
                            "archive_id": archive_id,
                            "reason": f"version_{now.algorithm_version}",
                        }
                    )
                    continue
                cursor = connection.execute(
                    """
                    UPDATE archive_content_signatures
                    SET algorithm_version = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND archive_id = ?
                      AND algorithm = ? AND algorithm_version = ?
                    """,
                    (
                        TARGET_VERSION,
                        int(planned["signature_id"]),
                        archive_id,
                        CONTENT_SIGNATURE_ALGORITHM,
                        LEGACY_VERSION,
                    ),
                )
                if cursor.rowcount != 1:
                    # Unreachable while the re-check above holds the lock;
                    # if it ever fires, the whole apply rolls back.
                    raise RuntimeError(
                        f"Relabel of archive {archive_id} touched "
                        f"{cursor.rowcount} rows."
                    )
                relabelled.append(archive_id)
    finally:
        connection.close()

    return {
        "plan_digest": expect_digest,
        "planned": expect_count,
        "relabelled": relabelled,
        "already_recertified": already,
        "skipped": skipped,
        "counts": {
            "relabelled": len(relabelled),
            "already_recertified": len(already),
            "skipped": len(skipped),
        },
        "provenance": PROVENANCE,
        "postflight": postflight(database, body),
    }


# --- postflight -----------------------------------------------------------


def postflight(database: str | Path, body: dict | None = None) -> dict:
    """Read-only evidence counts after apply, with their populations named.

    ``passed`` needs both scoped conditions: no current-location
    order-stable legacy row remains, and no planned order-stable archive is
    still at version "1". Remaining stale/unresolved/tied/inconsistent rows
    do not fail it; they are step 4's population and are counted.
    """
    planned = [] if body is None else [
        int(item["archive_id"]) for item in body["order_stable"]
    ]

    def read(connection: sqlite3.Connection) -> dict:
        remaining = classify_legacy(connection)
        still_legacy = []
        for archive_id in planned:
            row = connection.execute(
                "SELECT algorithm_version FROM archive_content_signatures "
                "WHERE archive_id = ? AND algorithm = ?",
                (archive_id, CONTENT_SIGNATURE_ALGORITHM),
            ).fetchone()
            if row is not None and str(row[0]) == LEGACY_VERSION:
                still_legacy.append(archive_id)
        return {
            "counts": signature_evidence_counts(connection),
            "remaining": remaining,
            "still_legacy": still_legacy,
        }

    snapshot = read_consistent_snapshot(
        database,
        read,
        context="content signature re-certification postflight",
        integrity_check=quick_check,
    )
    remaining = snapshot.result["remaining"]
    by_class = Counter(item.cls for item in remaining)
    scoped = {cls: by_class.get(cls, 0) for cls in CLASSES}
    still_legacy = snapshot.result["still_legacy"]
    return {
        "signature_evidence_counts": snapshot.result["counts"],
        "signature_evidence_counts_population": (
            "every archive_content_signatures row, any location state"
        ),
        "legacy_rows_by_class": scoped,
        "legacy_rows_by_class_population": (
            "version-1 rows; every class but stale is bound to its current "
            "location's recorded size/mtime"
        ),
        "planned_order_stable_still_legacy": still_legacy,
        "passed": scoped[ORDER_STABLE] == 0 and not still_legacy,
        "population_requirement": POPULATION_REQUIREMENT,
        **snapshot.report_fields(),
    }


# --- CLI ------------------------------------------------------------------


def _refuse_output(path: Path | None, database: Path,
                   extra: Sequence[tuple[str, str]] = ()) -> None:
    """Refuse an output path that exists or names an input.

    Checked before the database is opened, so a refusal costs nothing. The
    read-only guarantee covers the connection, not a report writer handed
    the database (or its WAL) as a destination.
    """
    if path is None:
        return
    for target, description in output_guards.protected_database_paths(
        str(database), extra=list(extra)
    ):
        if output_guards.same_file(path, target):
            raise PlanRefusedError(f"--json-out {path} is {description}.")
    if Path(path).exists():
        raise PlanRefusedError(f"--json-out {path} already exists.")


def _write_new_json(path: Path, payload: dict) -> Path:
    """Create `path` (never overwrite) holding `payload` as sorted JSON."""
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m comic_automation.archive.content_signature_recertify",
        description=(
            "Re-certify legacy (version 1) ordered-page-sha256 content "
            "signatures whose stored order is unchanged under version 2. "
            "Recorded-metadata provenance only: no archive file is read."
        ),
    )
    modes = parser.add_subparsers(dest="mode", required=True)

    plan = modes.add_parser("plan", help="Read-only classification and plan.")
    plan.add_argument("--database", type=Path, required=True)
    plan.add_argument("--json-out", type=Path, required=True,
                      help="Plan file to create; must not exist.")

    apply = modes.add_parser(
        "apply", help="Relabel the planned order-stable rows."
    )
    apply.add_argument("--database", type=Path, required=True)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--expect-digest", required=True)
    apply.add_argument("--expect-count", type=int, required=True)
    apply.add_argument(
        "--backup-report", type=Path, required=True,
        help="Verified backup_cli JSON report for this database.",
    )
    apply.add_argument("--json-out", type=Path, default=None)

    post = modes.add_parser("postflight", help="Read-only postflight counts.")
    post.add_argument("--database", type=Path, required=True)
    post.add_argument("--plan", type=Path, default=None)
    post.add_argument("--json-out", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        extra = []
        if getattr(args, "plan", None) is not None:
            extra.append((str(args.plan), "the plan"))
        _refuse_output(args.json_out, args.database, extra)

        if args.mode == "plan":
            output = make_plan(args.database)
            _write_new_json(args.json_out, output)
            counts = output["plan"]["counts"]
            print(json.dumps(counts, indent=2, sort_keys=True))
            print(f"plan_digest: {output['plan_digest']}")
            print(f"expect_count: {counts[ORDER_STABLE]}")
            return EXIT_OK

        if args.mode == "apply":
            output = apply_plan(
                args.database,
                args.plan,
                expect_digest=args.expect_digest,
                expect_count=args.expect_count,
                backup_report=args.backup_report,
            )
            if args.json_out is not None:
                _write_new_json(args.json_out, output)
            print(json.dumps(
                {"counts": output["counts"], "skipped": output["skipped"],
                 "postflight_passed": output["postflight"]["passed"]},
                indent=2, sort_keys=True,
            ))
            if output["skipped"] or not output["postflight"]["passed"]:
                return EXIT_INCOMPLETE
            return EXIT_OK

        body = None
        if args.plan is not None:
            document = json.loads(args.plan.read_text(encoding="utf-8"))
            body = document["plan"]
            if plan_digest(body) != document["plan_digest"]:
                raise PlanRefusedError("Plan digest mismatch.")
        output = postflight(args.database, body)
        if args.json_out is not None:
            _write_new_json(args.json_out, output)
        print(json.dumps(output, indent=2, sort_keys=True))
        return EXIT_OK if output["passed"] else EXIT_INCOMPLETE
    except PlanRefusedError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as error:  # reported, never swallowed silently
        print(f"FAILED: {error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
