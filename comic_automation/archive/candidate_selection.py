"""One selection path for "may this archive be enqueued right now?".

The question is asked in two places -- a preflight report and
`enqueue_missing()` -- and before this module they answered it differently.
Preflight compared database rows to database rows; enqueue did the same and
then handed the worker a path that might not exist. The 2026-08-17 incident and
the 2026-08-18 eligibility finding are both that gap:

* 79 jobs failed `filesystem_not_found` because the eligibility predicate never
  stats the filesystem, so an archive moved after signing passed every gate
  until a worker opened it;
* retiring archive 45217's job returned the archive to the eligible set,
  because retirement was recorded against the job rather than the archive.

Measured on 2026-08-18: 12,555 archives satisfied the database rules and 226 of
them pointed at a path that did not exist.

The order of checks is deliberate:

1. **Database eligibility** -- supplied by the caller, because it is job-type
   specific (which hashes are missing, which job statuses block re-enqueue).
2. **Retirement** -- checked before anything touches the disk, so a retired
   archive is excluded *independently of filesystem state*. This is the whole
   point of retirement being durable: an archive stays out because someone
   decided it should, not because its file happens to be missing today.
3. **Exactly one current location** -- zero is unresolvable and more than one
   is ambiguous; neither may be guessed at.
4. **An accessible regular file** -- the only check that touches the disk, and
   the last one, so the cheap refusals never pay for it.

Rejections are returned, never dropped. A candidate that silently disappears
between preflight and enqueue is indistinguishable from one that was never
eligible, and that is precisely the confusion that let 3,578 broken locations
accumulate unnoticed.

Nothing here is a substitute for the worker's own error handling. A file can
vanish between this check and the worker opening it, and no amount of checking
closes that window -- it narrows it from "6% of the library" to "a genuine
race".
"""

from __future__ import annotations

import os
import sqlite3
import stat as stat_module
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


# Stable rejection slugs. These are written into reports and compared in
# tests, so they are part of the contract, not display strings.
ARCHIVE_RETIRED = "archive_retired"
NO_CURRENT_LOCATION = "no_current_location"
MULTIPLE_CURRENT_LOCATIONS = "multiple_current_locations"
PATH_MISSING = "path_missing"
PATH_NOT_A_REGULAR_FILE = "path_not_a_regular_file"
PATH_UNREADABLE = "path_unreadable"

REJECTION_REASONS = (
    ARCHIVE_RETIRED,
    NO_CURRENT_LOCATION,
    MULTIPLE_CURRENT_LOCATIONS,
    PATH_MISSING,
    PATH_NOT_A_REGULAR_FILE,
    PATH_UNREADABLE,
)


@dataclass(frozen=True)
class Candidate:
    """An archive that passed every check, with the path that passed."""

    archive_id: int
    path: str


@dataclass(frozen=True)
class Rejection:
    """An archive that did not, and why."""

    archive_id: int
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class Selection:
    accepted: list[Candidate] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def accepted_ids(self) -> list[int]:
        return [candidate.archive_id for candidate in self.accepted]

    def rejections_by_reason(self) -> dict[str, list[int]]:
        """Grouped for reporting, with every reason present as a key.

        Reasons with no rejections are included with an empty list rather
        than omitted, so a report cannot silently lose a category by
        having nothing in it today.
        """
        grouped: dict[str, list[int]] = {r: [] for r in REJECTION_REASONS}
        for rejection in self.rejected:
            grouped.setdefault(rejection.reason, []).append(
                rejection.archive_id
            )
        return grouped


def retired_archive_ids(connection: sqlite3.Connection) -> set[int]:
    """Every durably retired archive. Read-only."""
    return {
        int(row[0])
        for row in connection.execute(
            "SELECT archive_id FROM archive_retirements"
        )
    }


def current_locations(
    connection: sqlite3.Connection,
    archive_ids: Sequence[int],
) -> dict[int, list[str]]:
    """Current location paths per archive, as a list so 0 and 2+ are visible.

    Returning a single path would force this function to decide what a
    duplicate means, which is the caller's refusal to make, not its own.
    """
    locations: dict[int, list[str]] = {}

    if not archive_ids:
        return locations

    wanted = set(archive_ids)
    for archive_id, path in connection.execute(
        "SELECT archive_id, path FROM file_locations WHERE is_current = 1"
    ):
        identifier = int(archive_id)
        if identifier in wanted:
            locations.setdefault(identifier, []).append(str(path))

    return locations


# The one call that touches the disk, bound here rather than called as
# os.stat() inside the function. A test proving that a retired archive is
# refused *without* consulting the filesystem has to replace this call; doing
# that by patching os.stat globally reaches pytest's own tmpdir cleanup and
# breaks the run. An explicit module-level seam keeps the substitution inside
# this module, where it belongs.
_stat = os.stat


def _inspect_path(path: str) -> tuple[str, str] | None:
    """Return (reason, detail) if *path* is not an accessible regular file.

    `FileNotFoundError` and a broader `OSError` are kept apart on purpose.
    An unreadable path is not evidence of absence -- treating a permission
    or I/O error as "missing" is what previously sent repair hunting for a
    replacement file that was never gone.
    """
    try:
        result = _stat(path)
    except FileNotFoundError:
        return PATH_MISSING, path
    except OSError as error:
        return PATH_UNREADABLE, "%s: %s" % (path, error)

    if not stat_module.S_ISREG(result.st_mode):
        kind = "directory" if stat_module.S_ISDIR(result.st_mode) else "special file"
        return PATH_NOT_A_REGULAR_FILE, "%s (%s)" % (path, kind)

    return None


def select_candidates(
    connection: sqlite3.Connection,
    archive_ids: Iterable[int],
    *,
    check_filesystem: bool = True,
) -> Selection:
    """Filter database-eligible *archive_ids* down to what may be enqueued.

    *archive_ids* has already satisfied the caller's database eligibility
    rules. This applies the rules every caller shares, in the order
    documented at module level.

    `check_filesystem=False` skips only step 4, for a caller that wants the
    database-level answer alone. Retirement and location checks always run:
    they are the ones that must not depend on the disk.
    """
    ordered = list(dict.fromkeys(int(a) for a in archive_ids))
    retired = retired_archive_ids(connection)
    locations = current_locations(connection, ordered)

    accepted: list[Candidate] = []
    rejected: list[Rejection] = []

    for archive_id in ordered:
        if archive_id in retired:
            rejected.append(
                Rejection(
                    archive_id,
                    ARCHIVE_RETIRED,
                    "retired at archive level; filesystem state not consulted",
                )
            )
            continue

        paths = locations.get(archive_id, [])

        if not paths:
            rejected.append(
                Rejection(archive_id, NO_CURRENT_LOCATION, "no is_current row")
            )
            continue

        if len(paths) > 1:
            rejected.append(
                Rejection(
                    archive_id,
                    MULTIPLE_CURRENT_LOCATIONS,
                    "%d current locations" % len(paths),
                )
            )
            continue

        path = paths[0]

        if check_filesystem:
            problem = _inspect_path(path)
            if problem is not None:
                reason, detail = problem
                rejected.append(Rejection(archive_id, reason, detail))
                continue

        accepted.append(Candidate(archive_id, path))

    return Selection(accepted=accepted, rejected=rejected)


def revalidate_for_enqueue(
    connection: sqlite3.Connection,
    archive_id: int,
) -> Rejection | None:
    """Re-check the database conditions at enqueue time.

    Selection and enqueue are separated by however long the caller takes,
    and the database can move in between -- an archive retired by another
    operator, a location row rewritten by repair. Those are cheap to
    re-read and are therefore checked again here.

    The filesystem is deliberately *not* re-checked. Re-statting would only
    move the race, not remove it, and would make enqueue cost a syscall per
    archive to buy a guarantee it still could not give. A file that
    disappears after this point is the worker's to report, which is what
    the worker's `filesystem_not_found` handling is for.
    """
    retired = connection.execute(
        "SELECT reason FROM archive_retirements WHERE archive_id = ?",
        (archive_id,),
    ).fetchone()

    if retired is not None:
        return Rejection(
            archive_id,
            ARCHIVE_RETIRED,
            "retired between selection and enqueue: %s" % (retired[0],),
        )

    paths = [
        str(row[0])
        for row in connection.execute(
            "SELECT path FROM file_locations "
            "WHERE archive_id = ? AND is_current = 1",
            (archive_id,),
        )
    ]

    if not paths:
        return Rejection(
            archive_id,
            NO_CURRENT_LOCATION,
            "current location disappeared between selection and enqueue",
        )

    if len(paths) > 1:
        return Rejection(
            archive_id,
            MULTIPLE_CURRENT_LOCATIONS,
            "%d current locations at enqueue time" % len(paths),
        )

    return None
