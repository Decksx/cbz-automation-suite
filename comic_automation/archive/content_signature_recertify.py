"""Re-certify legacy ordered content signatures that the new order cannot move.

Steps 2, 3 and 5 of the re-certification plan in
``docs/engineering_decisions.md`` ("Page order breaks natural-key ties on the
raw name; duplicate names are unresolved"). Step 4, the re-inventory of the
archives this tool refuses, is a separate operator action and is not here;
the plan's ``reinventory_archive_ids`` is a *proposed* population for it,
never a launch.

``CONTENT_SIGNATURE_VERSION`` moved from ``"1"`` (natural key, ties left in
physical member order) to ``"2"`` (ties broken on the raw name). Every
version-``"1"`` row is therefore excluded from comparison. For an archive
whose stored image names have *no* natural-key tie and *no* repeat, the two
contracts order the names identically, so the stored digest already *is* the
version-``"2"`` digest; relabelling it loses nothing and recomputes nothing.
This tool finds those rows and relabels only them.

The supported transition is exactly ``"1" -> "2"``. Both ends are literals,
and every mode refuses to run if the writer's ``CONTENT_SIGNATURE_VERSION``
is not ``"2"``: the argument above is about those two orderings and no
others, so a later bump needs its own argument, not this tool.

Three modes
-----------

``plan``
    Read-only. One deferred read transaction bracketed by
    ``PRAGMA data_version`` (`read_guards.read_consistent_snapshot`, which
    also opens ``mode=ro`` + ``query_only``). Classifies every
    ``ordered-page-sha256`` / version-``"1"`` signature into exactly one of
    ``stale``, ``unresolved``, ``tied``, ``inconsistent`` or ``order_stable``
    (precedence in that order) and writes a JSON plan whose digest covers
    its canonical body, including the target binding and the plan time.

``apply``
    Requires the plan file, ``--expect-digest``, ``--expect-count`` and a
    ``backup_cli`` report. Refused *entirely*, before any write, unless the
    plan verifies (`read_plan`), is bound to this database
    (`verify_target`), and the reported backup is a real, distinct, intact
    preimage of the plan taken after it (`verify_backup`). Then one
    ``BEGIN IMMEDIATE`` transaction (`dal.transaction`) re-checks the target
    and the backup file under the lock, re-classifies each planned archive
    from the rows as they are *inside that transaction*, and relabels it
    only if the result is still ``order_stable`` with exactly the planned
    basis. A row that moved is skipped and reported, never relabelled. A row
    already at version ``"2"`` with the planned basis is reported
    ``already_recertified``, so a re-run is a no-op. Only
    ``archive_content_signatures.algorithm_version`` and ``updated_at``
    change; page rows, page hashes and perceptual rows are not written.

``postflight``
    Read-only, same snapshot guard. Reports `signature_evidence_counts`
    (population: every signature row, any location state) and the
    classification of the legacy rows that remain. Given a plan, it first
    verifies the plan and its target binding, then proves the outcome of
    *each* planned row: present, this algorithm, version ``"2"``, still
    order-stable with the approved basis. Anything else is named and fails.

Bindings
--------

**Target.** The plan body records the target database's identity: its
normalised real path and its file identity (``st_dev``, ``st_ino``; on
Windows the volume serial number and NTFS file index). Apply and planned
postflight recompute it from ``--database`` and refuse on any difference.
Alias policy: any spelling that resolves to the same real path *and* the same
file is accepted (relative paths, case, ``..``, symlinks and junctions all
resolve). A hard link (another real path) is refused, as is a copy, a SQLite
backup clone or any file at another path. Restore policy: restoring a backup
over the target replaces the file, which changes its file identity, so a
restored database is refused and must be re-planned. A filesystem that
reports no file identity (``st_ino == 0``) is refused rather than trusted on
path alone. ``PRAGMA data_version`` is per connection and is not used as an
identity.

**Backup.** Backup creation stays the operator's
(``python -m comic_automation.database.backup_cli``). Apply verifies the
artifact the report names, not the report's word for it: see
`verify_backup` for the checks and the freshness and rerun policy.

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

Location references are diagnostics, not provenance. A signature or page row
whose ``location_id`` names *another archive's* location is ``inconsistent``.
One that names a non-current location of the same archive (a relocation), or
none at all, is order-stable if everything else holds -- a move does not
change the recorded order -- and the difference is reported in
``location_diagnostics``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
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


PLAN_FORMAT = "content-signature-recertify/2"
# The one supported transition. Literals, not the writer's constant: a
# relabel to "2" is justified only by the "1"-versus-"2" ordering argument.
LEGACY_VERSION = "1"
TARGET_VERSION = "2"

STALE = "stale"
UNRESOLVED = "unresolved"
TIED = "tied"
INCONSISTENT = "inconsistent"
ORDER_STABLE = "order_stable"
# Precedence: the first class that applies wins, so every row is in one.
CLASSES = (STALE, UNRESOLVED, TIED, INCONSISTENT, ORDER_STABLE)
# The plan keys a backup must reproduce to be this plan's preimage.
CLASSIFICATION_KEYS = (
    "counts", "order_stable", "tied", "unresolved", "inconsistent", "stale",
    "reinventory_archive_ids", "location_diagnostics",
)

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
    "rows describe the live bytes. Location references are diagnostics too. "
    "Re-certification certifies the recorded order only: the stored entry "
    "names sort identically under ordering contracts 1 and 2, and the stored "
    "digest equals the version-2 digest recomputed from the stored page "
    "sha256 rows. It does not satisfy full-file/member validation for "
    "authoritative supersession."
)

POPULATION_REQUIREMENT = (
    "signature_evidence_counts counts every archive_content_signatures row, "
    "whatever its location state. The planned postflight condition is per "
    "row: every planned order_stable archive must hold an ordered-page-sha256 "
    "version-2 signature that is still order_stable with the approved basis; "
    "a missing row, another algorithm or version, a drifted basis, or a row "
    "still at version 1 (including one apply skipped) is named and fails. No "
    "order_stable legacy row may remain for a current location. Rows "
    "classified stale, unresolved, tied or inconsistent stay legacy "
    "(excluded from comparison) until step 4's separately authorised "
    "re-inventory rewrites them; reinventory_archive_ids is a proposal."
)


class PlanRefusedError(RuntimeError):
    """The plan, its target or its backup disagree; nothing was written."""


def require_supported_transition() -> None:
    """Refuse to run unless the writer's contract is the supported target.

    Checked by every mode, so a future ``CONTENT_SIGNATURE_VERSION`` bump
    turns this tool off instead of turning it into a "1" -> "3" relabel.
    """
    if CONTENT_SIGNATURE_VERSION != TARGET_VERSION:
        raise PlanRefusedError(
            f"This tool supports only {LEGACY_VERSION} -> {TARGET_VERSION}; "
            f"the writer's contract is now {CONTENT_SIGNATURE_VERSION!r}."
        )


# --- file identity --------------------------------------------------------

# Indirection so a test can simulate a filesystem without file identity.
_stat = os.stat


def file_identity(path: str | Path) -> dict:
    """Portable identity of a database file: real path plus file id.

    Raises `PlanRefusedError` when the file is missing or the filesystem
    reports no file identity (``st_ino == 0``): a path alone cannot tell a
    restored or replaced file from the one that was planned.
    """
    real = os.path.realpath(str(path))
    try:
        stat = _stat(real)
    except OSError as error:
        raise PlanRefusedError(f"Cannot stat {path}: {error}") from error
    if not int(stat.st_ino):
        raise PlanRefusedError(
            f"{path}: the filesystem reports no file identity (st_ino 0); "
            "refusing to bind on the path alone."
        )
    return {
        "path": os.path.normcase(real),
        "st_dev": int(stat.st_dev),
        "st_ino": int(stat.st_ino),
    }


def verify_target(body: dict, database: str | Path) -> dict:
    """Refuse unless ``database`` is the file the plan was made for."""
    actual = file_identity(database)
    if body.get("target") != actual:
        raise PlanRefusedError(
            f"Plan target {body.get('target')!r} is not {database} "
            f"({actual!r}). A copy, clone, hard link or restored file is a "
            "different database; re-plan against it."
        )
    return actual


# --- classification -------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """One signature's class, why, and -- if order-stable -- its basis.

    ``basis`` is everything apply re-derives and compares before it writes:
    any field that differs means the stored state is not the one planned.
    ``diagnostics`` are location differences that do not affect the class.
    """

    archive_id: int
    algorithm: str
    algorithm_version: str
    cls: str
    reason: str
    basis: dict | None
    detail: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


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

    ``signature`` is its ``archive_content_signatures`` row joined to the
    location it references (``signature_location_archive_id``,
    ``signature_location_is_current``); ``locations`` its ``is_current = 1``
    ``file_locations`` rows; ``pages`` its ``archive_pages`` rows ordered by
    ``page_index``, each with the exact sha256 ``digest`` (None when absent)
    and the archive owning its ``location_id`` (``location_archive_id``).

    Neither the algorithm nor the version label is consulted: callers decide
    which they accept, so apply and postflight can recognise a row that was
    already relabelled -- or one that changed algorithm since the plan.
    """
    archive_id = int(signature["archive_id"])
    algorithm = str(signature["algorithm"])
    version = str(signature["algorithm_version"])

    def result(cls, reason, detail=(), basis=None, diagnostics=()):
        return Classification(
            archive_id, algorithm, version, cls, reason, basis,
            tuple(detail), tuple(diagnostics),
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

    # A reference to another archive's location means these rows are not
    # what they claim to be about; no diagnostic can paper over that.
    if (
        signature["location_id"] is not None
        and signature["signature_location_archive_id"] is not None
        and int(signature["signature_location_archive_id"]) != archive_id
    ):
        return result(INCONSISTENT, "signature_location_foreign")
    if any(
        page["location_archive_id"] is not None
        and int(page["location_archive_id"]) != archive_id
        for page in pages
    ):
        return result(INCONSISTENT, "page_location_foreign")

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

    # Same-archive location differences: a relocation or a nulled reference.
    # Reported, never promoted to provenance, and not a change of order.
    diagnostics = []
    if signature["location_id"] is None:
        diagnostics.append("signature_location_null")
    elif signature["signature_location_archive_id"] is None:
        diagnostics.append("signature_location_dangling")
    elif int(signature["location_id"]) != int(location["id"]):
        diagnostics.append("signature_location_not_current")
    if any(
        page["location_id"] is None
        or int(page["location_id"]) != int(location["id"])
        for page in pages
    ):
        diagnostics.append("page_location_not_current")

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
            "signature_location_id": (
                None if signature["location_id"] is None
                else int(signature["location_id"])
            ),
            "entry_names_sha256": _names_digest(names),
            "location_diagnostics": diagnostics,
        },
        diagnostics=diagnostics,
    )


_SIGNATURE_SELECT = """
    SELECT acs.id, acs.archive_id, acs.location_id, acs.algorithm,
           acs.algorithm_version, acs.digest, acs.page_count,
           acs.source_file_size, acs.source_modified_time_ns,
           sl.archive_id AS signature_location_archive_id
    FROM archive_content_signatures AS acs
    LEFT JOIN file_locations AS sl ON sl.id = acs.location_id
"""

_PAGES_SQL = """
    SELECT ap.archive_id, ap.page_index, ap.entry_name, ap.location_id,
           pl.archive_id AS location_archive_id, ph.digest
    FROM archive_pages AS ap
    LEFT JOIN file_locations AS pl ON pl.id = ap.location_id
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
        _SIGNATURE_SELECT
        + "WHERE acs.algorithm = ? AND acs.algorithm_version = ? "
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
    """Classify one archive's signature as stored now, any algorithm/version.

    None when the archive has no signature row at all
    (``archive_content_signatures.archive_id`` is UNIQUE). Used by apply
    inside its write transaction, so what it sees is what the UPDATE acts on,
    and by planned postflight.
    """
    signature = connection.execute(
        _SIGNATURE_SELECT + "WHERE acs.archive_id = ?", (archive_id,)
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


def planned_outcome(found: Classification | None, planned: dict) -> str:
    """Name the state of one planned row relative to its approved basis.

    ``recertified`` is the success outcome; ``legacy_as_planned`` means it is
    still exactly the approved preimage. Everything else is drift, named.
    """
    if found is None:
        return "missing"
    if found.algorithm != CONTENT_SIGNATURE_ALGORITHM:
        return f"algorithm_{found.algorithm}"
    if found.cls != ORDER_STABLE:
        return f"now_{found.cls}:{found.reason}"
    if found.basis != planned:
        return "basis_changed"
    if found.algorithm_version == TARGET_VERSION:
        return "recertified"
    if found.algorithm_version == LEGACY_VERSION:
        return "legacy_as_planned"
    return f"version_{found.algorithm_version}"


# --- plan -----------------------------------------------------------------


def canonical_bytes(value: Any) -> bytes:
    """The one rendering the plan digest is taken over."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def plan_digest(body: dict) -> str:
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def build_plan_body(
    classifications: Sequence[Classification],
    *,
    target: dict | None = None,
    planned_at_ns: int | None = None,
) -> dict:
    """The digested part of a plan: binding, time, classes, lists, counts.

    ``target`` and ``planned_at_ns`` are inside the digest, so the approval
    covers *which* database and *when*; data_version readings stay out
    because they are per-connection numbers, not identities.
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
    diagnostics = Counter(
        d for item in by_class[ORDER_STABLE] for d in item.diagnostics
    )
    return {
        "plan_format": PLAN_FORMAT,
        "algorithm": CONTENT_SIGNATURE_ALGORITHM,
        "from_version": LEGACY_VERSION,
        "to_version": TARGET_VERSION,
        "target": target,
        "planned_at_ns": planned_at_ns,
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
        # Proposed population for step 4, which is separately authorised:
        # archives whose digest must be recomputed from bytes. Stale rows
        # are not in it; they take the ordinary rehash path.
        "reinventory_archive_ids": reinventory,
        "location_diagnostics": dict(sorted(diagnostics.items())),
    }


def make_plan(database: str | Path) -> dict:
    """Plan from one WAL-aware read-only snapshot. Writes nothing."""
    require_supported_transition()
    # Taken before the read, so a backup whose file is at least this new was
    # necessarily written after the snapshot the plan describes began.
    planned_at_ns = time.time_ns()
    target = file_identity(database)
    snapshot = read_consistent_snapshot(
        database,
        classify_legacy,
        context="content signature re-certification plan",
        integrity_check=quick_check,
    )
    body = build_plan_body(
        snapshot.result, target=target, planned_at_ns=planned_at_ns
    )
    return {
        "plan": body,
        "plan_digest": plan_digest(body),
        "snapshot": {
            "database": str(snapshot.database),
            **snapshot.report_fields(),
        },
    }


# --- reading a plan back ----------------------------------------------------


def read_plan(plan_path: str | Path) -> tuple[dict, str]:
    """Read a plan and refuse it unless it is intact and this tool's.

    The digest is recomputed over the body as read, never taken from the
    file, so an edited list cannot ride under its original digest. Shared by
    apply and planned postflight. Returns the body and its digest.
    """
    require_supported_transition()
    try:
        document = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        body = document["plan"]
        recorded = document["plan_digest"]
        recomputed = plan_digest(body)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PlanRefusedError(f"Unreadable plan {plan_path}: {error}") from error

    if recomputed != recorded:
        raise PlanRefusedError(
            f"Plan digest mismatch: file records {recorded}, body hashes to "
            f"{recomputed}. The plan was edited after it was written."
        )
    if (
        not isinstance(body, dict)
        or body.get("plan_format") != PLAN_FORMAT
        or body.get("algorithm") != CONTENT_SIGNATURE_ALGORITHM
        or body.get("from_version") != LEGACY_VERSION
        or body.get("to_version") != TARGET_VERSION
        or not isinstance(body.get("order_stable"), list)
        or not isinstance(body.get("planned_at_ns"), int)
    ):
        raise PlanRefusedError(
            "Plan was written for another format, algorithm or version pair, "
            "or is missing its order_stable list or plan time."
        )
    return body, recomputed


def load_plan(
    plan_path: str | Path, *, expect_digest: str, expect_count: int
) -> dict:
    """`read_plan`, then refuse unless the operator's numbers agree."""
    body, recomputed = read_plan(plan_path)
    if recomputed != expect_digest:
        raise PlanRefusedError(
            f"--expect-digest {expect_digest} does not match the plan "
            f"({recomputed})."
        )
    planned = body["order_stable"]
    if len(planned) != expect_count:
        raise PlanRefusedError(
            f"--expect-count {expect_count} does not match the plan's "
            f"{len(planned)} order-stable row(s)."
        )
    return body


# --- backup -----------------------------------------------------------------


def _user_table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: int(connection.execute(
            'SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"'
        ).fetchone()[0])
        for name in names
    }


def _backup_fingerprint(path: str) -> dict:
    stat = _stat(path)
    return {
        "st_dev": int(stat.st_dev),
        "st_ino": int(stat.st_ino),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def verify_backup(
    database: str | Path, body: dict, backup_report: str | Path
) -> dict:
    """Prove the reported backup is this plan's recoverable preimage.

    The report is only a pointer; every claim is checked on the artifact.

    1. The report parses, says ``verified: true`` with every check true,
       and its ``source_database`` is the plan's target (same file identity).
    2. The artifact it names exists, is a file, and is **not** the live
       database by any alias: not the same real path, not the same file id
       (catches hard links), and not one of its WAL/SHM sidecars.
    3. It is the file the report describes: size and mtime equal the
       report's ``backup_size_bytes`` / ``backup_modified_time_ns``, and its
       user-table row counts equal ``backup_table_counts``.
    4. Opened read-only through `read_consistent_snapshot`, its
       ``PRAGMA quick_check`` is ``ok``.
    5. **Freshness:** its mtime is at or after the plan's ``planned_at_ns``,
       so it was written after the planned snapshot began.
    6. **Preimage:** classifying the backup's own legacy rows reproduces the
       plan's classification exactly (`CLASSIFICATION_KEYS`) -- so it holds
       the approved pre-apply state of every row apply may change.
    7. It did not change while being verified.

    Rerun policy: the same plan and backup may be re-applied (the backup is
    still the preimage of the planned rows, and apply is idempotent). A new
    plan needs a new backup, because check 5 compares against that plan's
    time. Returns the artifact fingerprint for the under-lock re-check.
    """
    try:
        report = json.loads(Path(backup_report).read_text(encoding="utf-8"))
        backup = str(report["backup_database"])
        source = str(report["source_database"])
        checks = report["checks"]
        recorded_size = int(report["backup_size_bytes"])
        recorded_mtime = int(report["backup_modified_time_ns"])
        recorded_counts = dict(report["backup_table_counts"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PlanRefusedError(
            f"Unreadable backup report {backup_report}: {error}"
        ) from error

    if (
        report.get("verified") is not True
        or not isinstance(checks, dict)
        or not checks
        or not all(value is True for value in checks.values())
    ):
        raise PlanRefusedError(f"Backup report {backup_report} is not verified.")
    if file_identity(source) != body["target"]:
        raise PlanRefusedError(
            f"Backup report {backup_report} is for {source!r}, not the plan "
            "target."
        )

    if not Path(backup).is_file():
        raise PlanRefusedError(f"Backup artifact {backup} does not exist.")
    # One check covers every alias: `same_file` compares the normalised real
    # path (spelling, case, "..", symlinks, junctions) and then
    # os.path.samefile (hard links, same file id), against the database and
    # both derivations of its WAL/SHM sidecars.
    file_identity(backup)  # refuses a filesystem without file identity
    if any(
        output_guards.same_file(backup, target)
        for target, _ in output_guards.protected_database_paths(str(database))
    ):
        raise PlanRefusedError(
            f"Backup artifact {backup} is the live database or one of its "
            "aliases or sidecars."
        )

    before = _backup_fingerprint(backup)
    if (before["size"], before["mtime_ns"]) != (recorded_size, recorded_mtime):
        raise PlanRefusedError(
            f"Backup artifact {backup} is not the file the report verified "
            f"(size/mtime {before['size']}/{before['mtime_ns']}, report "
            f"{recorded_size}/{recorded_mtime})."
        )
    if before["mtime_ns"] < int(body["planned_at_ns"]):
        raise PlanRefusedError(
            f"Backup artifact {backup} predates the plan; take a new backup "
            "after planning."
        )

    def read(connection: sqlite3.Connection) -> dict:
        return {
            "counts": _user_table_counts(connection),
            "classified": build_plan_body(classify_legacy(connection)),
        }

    try:
        snapshot = read_consistent_snapshot(
            backup, read, context="backup preimage verification",
            integrity_check=quick_check,
        )
    except PlanRefusedError:
        raise
    except Exception as error:
        raise PlanRefusedError(
            f"Backup artifact {backup} failed verification: {error}"
        ) from error

    if snapshot.result["counts"] != recorded_counts:
        raise PlanRefusedError(
            f"Backup artifact {backup} row counts differ from its report."
        )
    classified = snapshot.result["classified"]
    differing = [
        key for key in CLASSIFICATION_KEYS if classified[key] != body[key]
    ]
    if differing:
        raise PlanRefusedError(
            f"Backup artifact {backup} is not this plan's preimage: "
            f"{', '.join(differing)} differ."
        )
    if _backup_fingerprint(backup) != before:
        raise PlanRefusedError(
            f"Backup artifact {backup} changed during verification."
        )
    return {"path": backup, **before}


# --- apply ----------------------------------------------------------------


def apply_plan(
    database: str | Path,
    plan_path: str | Path,
    *,
    expect_digest: str,
    expect_count: int,
    backup_report: str | Path,
) -> dict:
    """Relabel the planned order-stable rows that are still exactly as planned.

    Refusals (`PlanRefusedError`) happen before anything is written: the
    plan, target and backup checks run first, and the two cheap ones are
    repeated under the write lock. Per-row drift is not a refusal: that row
    is skipped and named, the others proceed, all inside one transaction.
    """
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"Database does not exist: {database}")
    body = load_plan(
        plan_path, expect_digest=expect_digest, expect_count=expect_count
    )
    verify_target(body, database)
    backup = verify_backup(database, body, backup_report)

    relabelled: list[int] = []
    already: list[int] = []
    skipped: list[dict] = []

    connection = open_connection(database)
    try:
        with transaction(connection):
            # Under the lock: the target is still the planned file, and the
            # verified backup is still there, unchanged. Raising here rolls
            # back a transaction that has written nothing.
            verify_target(body, database)
            try:
                still = _backup_fingerprint(backup["path"])
            except OSError:
                still = None
            if still != {k: v for k, v in backup.items() if k != "path"}:
                raise PlanRefusedError(
                    f"Backup artifact {backup['path']} moved or changed "
                    "after verification."
                )

            for planned in body["order_stable"]:
                archive_id = int(planned["archive_id"])
                # Re-derived inside the write lock: the plan is a claim about
                # an earlier snapshot, and only this read is what the UPDATE
                # will act on.
                outcome = planned_outcome(
                    classify_archive(connection, archive_id), planned
                )
                if outcome == "recertified":
                    already.append(archive_id)
                    continue
                if outcome != "legacy_as_planned":
                    skipped.append({"archive_id": archive_id, "reason": outcome})
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
        "backup": backup,
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
    """Read-only evidence counts and, given a plan, per-row outcomes.

    With a plan, ``body`` must already have passed `read_plan` (apply and the
    CLI both ensure it); its target is verified here before any read.
    ``passed`` needs every planned row to be ``recertified`` and no
    order-stable legacy row to remain. Remaining stale, unresolved, tied and
    inconsistent rows do not fail it; they are step 4's proposed population
    and are counted.
    """
    require_supported_transition()
    if body is not None:
        verify_target(body, database)
    planned = [] if body is None else list(body["order_stable"])

    def read(connection: sqlite3.Connection) -> dict:
        return {
            "counts": signature_evidence_counts(connection),
            "remaining": classify_legacy(connection),
            "outcomes": [
                {
                    "archive_id": int(item["archive_id"]),
                    "outcome": planned_outcome(
                        classify_archive(connection, int(item["archive_id"])),
                        item,
                    ),
                }
                for item in planned
            ],
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
    outcomes = snapshot.result["outcomes"]
    failed = [o for o in outcomes if o["outcome"] != "recertified"]
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
        "planned_outcomes": outcomes,
        "planned_outcome_counts": dict(
            sorted(Counter(o["outcome"] for o in outcomes).items())
        ),
        "planned_failures": failed,
        "planned_order_stable_still_legacy": [
            o["archive_id"] for o in outcomes
            if o["outcome"] == "legacy_as_planned"
        ],
        "passed": scoped[ORDER_STABLE] == 0 and not failed,
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
        help=(
            "backup_cli JSON report for a backup of this database taken "
            "after the plan; the backup file itself is verified."
        ),
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
            body, _ = read_plan(args.plan)
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
