"""Re-derivation and supersession evaluator for WIP versions (read-only).

Step 5 of ``docs/canonical_naming_and_version_supersession.md`` (§4.1,
§4.3-§4.6). For each version family that step 4 (`field_resolution`) has
*established*, this module decides which member is the keeper, which
members it supersedes, and what needs a human. It renames, moves,
quarantines and writes nothing: its output is a serialisable report that
the step-7 plan consumes.

Nothing here writes. Each archive is read with ``open(path, "rb")``,
held in memory when it is at most ``max_held_bytes`` and streamed
otherwise, and re-hashed from disk afterwards; the database is read only through
`field_resolution.load_page_evidence`, which goes through the DAL's
`read_guards.read_consistent_snapshot` (``mode=ro``, ``query_only``,
``data_version`` bracketing). The module opens no connection of its own.

Strong identity (§4.3)
----------------------

Every evaluated archive gets the full-file ``sha256`` of the bytes this
evaluation read. For an archive of at most ``max_held_bytes`` (512 MiB by
default) the page sequence is re-derived **from those same held bytes**
(`derive_archive`): the buffer is hashed, then opened as a ZIP, so the
sequence describes exactly the bytes the hash names. A larger archive is
streamed: hash and stat, derive from the path, hash and stat again; its
residual change-and-restore window is documented on `derive_archive`. The archive is admitted with the shared
`archive_limits.validate_archive_members`, and pages are hashed and ordered
by `page_hashing.hash_ordered_pages`, the same pass `calculate_page_hashes`
runs, so §1.2 order and per-page sha256 are defined once.

After the derivation the file on disk is hashed again. If it no longer has
the evaluated hash, the file changed during evaluation and the archive is
unresolved: the report would otherwise describe bytes that are no longer
at the path. A change that is reverted before the re-hash is not
detectable, and does not need to be: the evaluated bytes are exactly the
bytes the recorded hash names, and step 7 revalidates against that hash
before acting.

Stored rows nominate, never prove
---------------------------------

The stored exact rows (`load_page_evidence`) are compared with the
re-derived sequence. An archive with no stored rows, rows that are not
comparable evidence (legacy ordering contract, unresolved stored order,
incomplete inventory) or rows whose digests differ from the re-derived
sequence is **unresolved** with that reason (§4.3: "if the two disagree,
the stored rows are stale"). The comparison itself always uses the
re-derived sequence.

Comparable (§4.1)
-----------------

An archive is comparable when it is bound (read, hashed, re-hashed
unchanged), complete (every image member hashed -- a member that cannot be
read makes the whole archive unresolved, never a shorter sequence),
nonempty, and ordered (unique image member names), and its stored rows
agree. Only the exact ``sha256`` basis is used. The perceptual ``dhash``
basis is **not consulted at all** in this slice: §4.1 lets it nominate but
never prove, and nothing here nominates.

Relations (§4.4)
----------------

Within an established family only, for every pair, with A the shorter:

* equal pages: a whole-archive duplicate only when the full-file hashes are
  equal; otherwise review, listing the non-image members that differ;
* strict prefix: A is superseded by B, subject to the non-image payload
  check;
* strict ordered subsequence (order and multiplicity preserved, not a
  prefix): an inserted-pages supersession, always held for review;
* anything else: review.

**Non-image payload.** Every non-image member of A (raw member name and
sha256, as a multiset) must exist in B with identical bytes. A ComicInfo.xml
of A may be dropped only when the caller passes
``accept_comicinfo_drop=True`` (operator acceptance, default off); the drop
is then listed, not silent, and is allowed only when B carries its own
ComicInfo.xml (§8 question 6, the conservative reading). Otherwise the relation is downgraded to review,
listing A's unique members. The **index map** from A's pages to B's is the
greedy left-to-right walk over B, written into the relation.

Chains (§4.5)
-------------

Superseded and inserted-pages relations are the edges A -> B. The keeper is
the unique maximal member; every other member points at the keeper with
its own direct relation and index map, never at an intermediate. Two or
more maximal members (a fork, a payload downgrade, or byte-identical
duplicates at the top) send the family to review with no keeper. A cycle,
or a member whose direct relation to the keeper is not a supersession, is
an inconsistent relation set -- stale or corrupt inventory -- and raises
`InconsistentRelationsError`, which stops the run. Both are unreachable
from relations derived from bytes (every edge strictly lengthens the
sequence, and prefix, subsequence and payload containment are transitive);
they guard the contract, not a known path. Graph rank is never computed
into the output.

Never versions: decensor pairs and cross-family overlap (omnibus versus
issue, two editions) are refused by `establish_family`; this module
compares nothing outside an established family.

Keeper label (§4.6)
-------------------

The keeper's label is its own resolved ``version`` field from
`field_resolution`: a human decision, or the keeper's own name marker
classified as a version through the established family, with no
contradicting evidence (the resolver leaves a contradicted field
unresolved). A canonical-looking name is a candidate, not authority: the
marker counts only because the family is established. Otherwise the label
is ``held_for_decision``. No number is ever taken from another member or
from chain position.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comic_automation.archive.archive_limits import UnsafeArchiveError
from comic_automation.archive.field_resolution import (
    FAMILY_ESTABLISHED,
    FAMILY_INCOMPARABLE,
    ArchiveFacts,
    ArchiveResolution,
    FamilyEvidence,
    FolderFacts,
    HumanDecisionSource,
    NO_HUMAN_DECISIONS,
    PageEvidence,
    comicinfo_facts,
    load_page_evidence,
    read_comicinfo_documents,
    resolve_folder,
)
from comic_automation.archive.inspection import COMIC_INFO_FILENAME
from comic_automation.archive.page_hashing import (
    CONTENT_SIGNATURE_ALGORITHM,
    CONTENT_SIGNATURE_VERSION,
    DEFAULT_CHUNK_SIZE,
    PageContentHash,
    hash_ordered_pages,
    is_page_member,
)
from comic_automation.database.read_guards import ConsistentSnapshot


BASIS_EXACT = "sha256"

REL_DUPLICATE = "whole_archive_duplicate"
REL_EQUAL_PAGES = "equal_pages"
REL_PREFIX = "strict_prefix"
REL_SUBSEQUENCE = "ordered_subsequence"
REL_OTHER = "other"

OUTCOME_DUPLICATE = "duplicate"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_INSERTED_PAGES = "inserted_pages_review"
OUTCOME_REVIEW = "review"

# Outcomes that are edges of the version graph (older -> newer).
EDGE_OUTCOMES = frozenset({OUTCOME_SUPERSEDED, OUTCOME_INSERTED_PAGES})

FAMILY_NOT_ESTABLISHED = "not_established"
FAMILY_UNRESOLVED = "unresolved"
FAMILY_REVIEW = "review"
FAMILY_SUPERSESSION = "supersession"

LABEL_RESOLVED = "resolved"
LABEL_HELD = "held_for_decision"

STORED_AGREE = "agree"


class InconsistentRelationsError(RuntimeError):
    """The relation set is not a strict order: stale or corrupt inventory.

    §4.5: cycles "stop the run". Raised rather than reported, so no partial
    report built on an impossible relation set can reach a plan.
    """


# --- derivation from held bytes -----------------------------------------------


@dataclass(frozen=True)
class NonImageMember:
    """One non-image, non-directory member: raw name and sha256 of its bytes."""

    name: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "sha256": self.sha256}


@dataclass(frozen=True)
class DerivedArchive:
    """What one archive's bytes say, bound to their full-file sha256.

    ``sha256``/``file_size`` describe the bytes read (None when the file
    could not be read at all). ``problem`` is set when the derivation could
    not produce a bound, complete inventory: unreadable file, refused or
    corrupt archive, or a file that changed during evaluation. Pages and
    members are then empty and carry no meaning. ``read_mode`` is
    ``buffered`` or ``streamed`` (`derive_archive`); None when unread.
    """

    path: Path
    sha256: str | None
    file_size: int | None
    pages: tuple[PageContentHash, ...] = ()
    non_image: tuple[NonImageMember, ...] = ()
    comicinfo_documents: tuple[bytes, ...] = ()
    duplicate_names: tuple[str, ...] = ()
    problem: str | None = None
    read_mode: str | None = None

    @property
    def page_digests(self) -> tuple[str, ...]:
        return tuple(page.digest for page in self.pages)

    @property
    def unresolved_reason(self) -> str | None:
        """Why the re-derived sequence is not comparable (§4.1), or None."""
        if self.problem is not None:
            return self.problem
        if self.duplicate_names:
            return (
                "page order unresolved: duplicate image member names "
                f"{list(self.duplicate_names)}"
            )
        if not self.pages:
            return "no image pages: an empty sequence is never comparable"
        return None

    def page_evidence(self) -> PageEvidence | None:
        """The re-derived sequence as step-4 page evidence.

        None when the bytes could not be bound, so `establish_family` sees
        no page evidence rather than an empty sequence.
        """
        if self.problem is not None:
            return None
        return PageEvidence(
            algorithm=CONTENT_SIGNATURE_ALGORITHM,
            algorithm_version=CONTENT_SIGNATURE_VERSION,
            order_resolved=not self.duplicate_names,
            page_digests=self.page_digests,
        )


# Largest archive `derive_archive` holds in memory (512 MiB). At or under it
# the archive is read once into one buffer and everything is derived from
# that buffer. Above it the streaming path runs instead, so a multi-GB
# omnibus (admission allows up to 16 GiB declared) is never held whole.
# Conservative, not calibrated against the library.
DEFAULT_MAX_HELD_BYTES = 512 * 1024 * 1024

READ_BUFFERED = "buffered"
READ_STREAMED = "streamed"


def _read_capped(path: Path, limit: int) -> bytes | None:
    """The file's bytes, or None when it holds more than ``limit`` bytes.

    Reads at most ``limit + 1`` bytes, so a file that grew after it was
    sized is never read unbounded: it falls through to the streaming path.
    """
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    return None if len(data) > limit else data


def _file_sha256(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_identity(path: Path) -> tuple[int, int, int, int]:
    """(size, mtime_ns, ino, dev): a replacement or a touch changes one of them.

    ``st_ino``/``st_dev`` are the NTFS file index and volume serial on
    Windows, and 0 where a file system has none; equal zeros then compare
    equal and the size, mtime and hash checks still apply.
    """
    stat = path.stat()
    return (
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(getattr(stat, "st_ino", 0) or 0),
        int(getattr(stat, "st_dev", 0) or 0),
    )


def _hash_non_image_members(
    archive: zipfile.ZipFile, chunk_size: int
) -> tuple[NonImageMember, ...]:
    members = []
    for entry in archive.infolist():
        if entry.is_dir() or is_page_member(entry):
            continue
        digest = hashlib.sha256()
        with archive.open(entry, mode="r") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
        members.append(NonImageMember(entry.filename, digest.hexdigest()))
    return tuple(sorted(members, key=lambda m: (m.name, m.sha256)))


def _derive_members(source: Callable[[], Any], chunk_size: int) -> tuple[Any, ...]:
    """(pages, page order, non-image members, ComicInfo documents).

    ``source`` returns a fresh readable source on each call: an in-memory
    buffer or the path. Raises what ``zipfile`` and the admission check
    raise; `derive_archive` maps those to a ``problem``.
    """
    with zipfile.ZipFile(source(), mode="r") as archive:
        pages, order = hash_ordered_pages(archive, chunk_size=chunk_size)
        non_image = _hash_non_image_members(archive, chunk_size)
    comicinfo = read_comicinfo_documents(source())
    return pages, order, non_image, comicinfo


def derive_archive(
    path: str | Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_held_bytes: int = DEFAULT_MAX_HELD_BYTES,
) -> DerivedArchive:
    """Re-derive one archive's inventory from bytes bound to its sha256.

    **Buffered** (file at most ``max_held_bytes``): the file is read once
    into memory and that buffer is hashed; pages, non-image members and
    ComicInfo are derived from the same buffer, so they describe exactly
    the hashed bytes. The file is then re-hashed on disk, and a different
    hash means it changed during evaluation (unresolved).

    **Streamed** (larger files): ``stat`` (size, mtime_ns, ino, dev) is
    recorded and the file's sha256 streamed; the members are then derived
    from a ZipFile opened on the path; finally the sha256 is streamed again
    and the file re-stat'ed. Any difference in hash or stat makes the
    archive unresolved, "changed during evaluation". **Residual window:**
    here the members are read from the file, not from the hashed bytes, so
    a change made after the first hash and fully reverted before the second
    -- bytes *and* size, mtime and file identity restored -- goes
    undetected. That needs a writer that restores mtime deliberately; step
    7 revalidates against the recorded hash before acting either way.

    Failures are reported in ``problem``, never raised: one bad archive
    makes its family unresolved, it does not stop the folder.
    """
    if max_held_bytes < 0:
        raise ValueError("max_held_bytes must not be negative.")
    archive_path = Path(path)
    try:
        size = archive_path.stat().st_size
        data = (
            _read_capped(archive_path, max_held_bytes)
            if size <= max_held_bytes
            else None
        )
        if data is None:
            before_stat: tuple[int, int, int, int] | None = _stat_identity(archive_path)
            sha256 = _file_sha256(archive_path, chunk_size)
    except OSError as exc:
        return DerivedArchive(
            archive_path, None, None, problem=f"file could not be read: {exc}"
        )

    source: Callable[[], Any]
    if data is not None:
        mode = READ_BUFFERED
        sha256 = hashlib.sha256(data).hexdigest()
        file_size = len(data)
        before_stat = None
        source = lambda: io.BytesIO(data)  # noqa: E731
    else:
        mode = READ_STREAMED
        assert before_stat is not None
        file_size = before_stat[0]
        source = lambda: archive_path  # noqa: E731

    def unresolved(problem: str) -> DerivedArchive:
        return DerivedArchive(
            archive_path, sha256, file_size, problem=problem, read_mode=mode
        )

    try:
        pages, order, non_image, comicinfo = _derive_members(source, chunk_size)
    except UnsafeArchiveError as exc:
        return unresolved(f"archive refused by admission limits ({exc.reason}): {exc}")
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        RuntimeError,
        NotImplementedError,
    ) as exc:
        return unresolved(f"corrupt or unreadable archive: {type(exc).__name__}: {exc}")
    except OSError as exc:
        return unresolved(f"file vanished or became unreadable during evaluation: {exc}")

    # The binding check: the bytes evaluated must still be the file's bytes,
    # and on the streamed path the file must still be the same file.
    try:
        now = _file_sha256(archive_path, chunk_size)
        after_stat = None if before_stat is None else _stat_identity(archive_path)
    except OSError as exc:
        return unresolved(f"file vanished or became unreadable during evaluation: {exc}")
    if now != sha256:
        return unresolved(
            f"file changed during evaluation (sha256 {sha256} evaluated, "
            f"{now} on disk afterwards)"
        )
    if after_stat != before_stat:
        return unresolved(
            "file changed during evaluation (size, mtime_ns, ino, dev "
            f"{before_stat} before, {after_stat} afterwards)"
        )

    return DerivedArchive(
        path=archive_path,
        sha256=sha256,
        file_size=file_size,
        pages=pages,
        non_image=non_image,
        comicinfo_documents=comicinfo,
        duplicate_names=order.duplicate_names,
        read_mode=mode,
    )


def stored_rows_verdict(
    derived: DerivedArchive, stored: PageEvidence | None, archive_id: int | None
) -> str:
    """`STORED_AGREE`, or why the stored rows cannot stand beside the bytes.

    §4.3: stored rows that disagree with the re-derived sequence are stale,
    and the archive is unresolved. Missing rows and rows that are not
    comparable evidence are refused the same way: there is nothing current
    to agree with.
    """
    if archive_id is None:
        return "no stored inventory: no archive id was supplied for this file"
    if stored is None:
        return f"no stored inventory rows for archive id {archive_id}"
    if not stored.comparable:
        return f"stored rows are not comparable evidence: {stored.incomparable_reason}"
    if stored.page_digests != derived.page_digests:
        return (
            "stored rows are stale: they disagree with the sequence re-derived "
            f"from the current bytes ({len(stored.page_digests)} stored pages, "
            f"{len(derived.page_digests)} re-derived)"
        )
    return STORED_AGREE


# --- relations --------------------------------------------------------------


def greedy_index_map(
    older: Sequence[str], newer: Sequence[str]
) -> tuple[int, ...] | None:
    """Map each page of ``older`` to a page of ``newer``, left to right.

    The greedy walk takes the earliest unused match after the previous one,
    which preserves order and multiplicity: ``older`` repeats a page only as
    often as ``newer`` does after the earlier matches. None when ``older``
    is not an ordered subsequence of ``newer``.
    """
    mapping: list[int] = []
    position = 0
    for digest in older:
        while position < len(newer) and newer[position] != digest:
            position += 1
        if position == len(newer):
            return None
        mapping.append(position)
        position += 1
    return tuple(mapping)


def _is_comicinfo(name: str) -> bool:
    return name.replace("\\", "/").rsplit("/", 1)[-1].casefold() == COMIC_INFO_FILENAME


@dataclass(frozen=True)
class Relation:
    """The §4.4 relation of one pair in an established family.

    ``older``/``newer`` are archive keys; for unequal lengths ``older`` is
    the shorter, otherwise the pair's key order. ``index_map`` is set when
    ``older``'s pages are an ordered subsequence of ``newer``'s.
    ``unique_non_image`` are ``older``'s members missing from ``newer``
    (they block supersession); ``accepted_comicinfo_drops`` are ComicInfo
    members missing from ``newer`` that the operator accepted dropping;
    ``differing_non_image`` lists, for equal pages, every non-image member
    that is not in both.
    """

    older: str
    newer: str
    relation: str
    outcome: str
    reason: str
    index_map: tuple[int, ...] | None = None
    unique_non_image: tuple[NonImageMember, ...] = ()
    accepted_comicinfo_drops: tuple[NonImageMember, ...] = ()
    differing_non_image: tuple[dict[str, str], ...] = ()

    @property
    def is_edge(self) -> bool:
        return self.outcome in EDGE_OUTCOMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "older": self.older,
            "newer": self.newer,
            "relation": self.relation,
            "outcome": self.outcome,
            "reason": self.reason,
            "index_map": None if self.index_map is None else list(self.index_map),
            "unique_non_image_members": [m.as_dict() for m in self.unique_non_image],
            "accepted_comicinfo_drops": [
                m.as_dict() for m in self.accepted_comicinfo_drops
            ],
            "differing_non_image_members": [dict(d) for d in self.differing_non_image],
        }


def _payload(
    older: DerivedArchive, newer: DerivedArchive, accept_comicinfo_drop: bool
) -> tuple[tuple[NonImageMember, ...], tuple[NonImageMember, ...]]:
    """(unique, accepted drops): ``older``'s non-image members not in ``newer``."""
    missing = Counter(older.non_image) - Counter(newer.non_image)
    # §8 question 6: a ComicInfo may be dropped only when the archive that
    # supersedes it carries its own; otherwise the metadata would be lost.
    newer_has_comicinfo = any(_is_comicinfo(m.name) for m in newer.non_image)
    unique: list[NonImageMember] = []
    dropped: list[NonImageMember] = []
    for member in sorted(missing.elements(), key=lambda m: (m.name, m.sha256)):
        if (
            accept_comicinfo_drop
            and newer_has_comicinfo
            and _is_comicinfo(member.name)
        ):
            dropped.append(member)
        else:
            unique.append(member)
    return tuple(unique), tuple(dropped)


def compare_pair(
    first_key: str,
    first: DerivedArchive,
    second_key: str,
    second: DerivedArchive,
    *,
    accept_comicinfo_drop: bool = False,
) -> Relation:
    """Classify one pair of comparable archives under §4.4."""
    a_pages, b_pages = first.page_digests, second.page_digests
    if len(a_pages) == len(b_pages):
        if a_pages != b_pages:
            return Relation(
                first_key, second_key, REL_OTHER, OUTCOME_REVIEW,
                "same page count, different or reordered pages",
            )
        if first.sha256 == second.sha256:
            return Relation(
                first_key, second_key, REL_DUPLICATE, OUTCOME_DUPLICATE,
                "equal full-file sha256: a whole-archive duplicate, not a "
                "supersession",
                index_map=tuple(range(len(a_pages))),
            )
        only_first = Counter(first.non_image) - Counter(second.non_image)
        only_second = Counter(second.non_image) - Counter(first.non_image)
        differing = tuple(
            {"archive": key, **member.as_dict()}
            for key, members in ((first_key, only_first), (second_key, only_second))
            for member in sorted(members.elements(), key=lambda m: (m.name, m.sha256))
        )
        return Relation(
            first_key, second_key, REL_EQUAL_PAGES, OUTCOME_REVIEW,
            "equal pages but different full files: the non-image members "
            "differ, so neither supersedes the other",
            index_map=tuple(range(len(a_pages))),
            differing_non_image=differing,
        )

    if len(a_pages) < len(b_pages):
        older_key, older, newer_key, newer = first_key, first, second_key, second
    else:
        older_key, older, newer_key, newer = second_key, second, first_key, first
    index_map = greedy_index_map(older.page_digests, newer.page_digests)
    if index_map is None:
        return Relation(
            older_key, newer_key, REL_OTHER, OUTCOME_REVIEW,
            "changed, removed or reordered pages: the shorter sequence is not "
            "an ordered subsequence of the longer",
        )
    is_prefix = newer.page_digests[: len(older.page_digests)] == older.page_digests
    relation = REL_PREFIX if is_prefix else REL_SUBSEQUENCE
    unique, dropped = _payload(older, newer, accept_comicinfo_drop)
    if unique:
        return Relation(
            older_key, newer_key, relation, OUTCOME_REVIEW,
            "non-image payload unique to the older archive blocks supersession: "
            + ", ".join(m.name for m in unique),
            index_map=index_map,
            unique_non_image=unique,
            accepted_comicinfo_drops=dropped,
        )
    if is_prefix:
        return Relation(
            older_key, newer_key, relation, OUTCOME_SUPERSEDED,
            "strict prefix with every non-image member carried",
            index_map=index_map,
            accepted_comicinfo_drops=dropped,
        )
    return Relation(
        older_key, newer_key, relation, OUTCOME_INSERTED_PAGES,
        "inserted pages: an ordered subsequence that is not a prefix; held "
        "for review",
        index_map=index_map,
        accepted_comicinfo_drops=dropped,
    )


# --- chains -----------------------------------------------------------------


def _find_cycle(keys: Sequence[str], edges: Mapping[str, set[str]]) -> list[str] | None:
    """A cycle in the edge graph as a key list, or None. Iterative DFS."""
    state: dict[str, int] = {}  # 1 = on the stack, 2 = finished
    for root in keys:
        if state.get(root):
            continue
        stack: list[tuple[str, list[str]]] = [(root, sorted(edges.get(root, ())))]
        path = [root]
        state[root] = 1
        while stack:
            node, pending = stack[-1]
            if pending:
                child = pending.pop(0)
                if state.get(child) == 1:
                    return path[path.index(child):] + [child]
                if not state.get(child):
                    state[child] = 1
                    path.append(child)
                    stack.append((child, sorted(edges.get(child, ()))))
            else:
                state[node] = 2
                stack.pop()
                path.pop()
    return None


@dataclass(frozen=True)
class Chain:
    """The keeper of a relation set, or why there is none."""

    keeper: str | None
    maximal: tuple[str, ...]
    pointers: tuple[Relation, ...]


def resolve_chain(keys: Sequence[str], relations: Sequence[Relation]) -> Chain:
    """Apply §4.5 to one family's relations.

    The keeper is the unique member with no outgoing edge. Every other
    member then points at the keeper through its own *direct* relation to
    it, never an intermediate. With more than one maximal member there is
    no keeper and no pointer. Raises `InconsistentRelationsError` on a cycle
    or a missing direct relation (see the module docstring).
    """
    edges: dict[str, set[str]] = {key: set() for key in keys}
    by_pair: dict[tuple[str, str], Relation] = {}
    for relation in relations:
        if relation.is_edge:
            edges[relation.older].add(relation.newer)
            by_pair[(relation.older, relation.newer)] = relation
    cycle = _find_cycle(keys, edges)
    if cycle is not None:
        raise InconsistentRelationsError(
            f"supersession cycle {' -> '.join(cycle)}: stale or corrupt "
            "inventory; the run stops"
        )
    maximal = tuple(sorted(key for key in keys if not edges[key]))
    if len(maximal) != 1:
        return Chain(keeper=None, maximal=maximal, pointers=())
    keeper = maximal[0]
    pointers = []
    for key in sorted(keys):
        if key == keeper:
            continue
        direct = by_pair.get((key, keeper))
        if direct is None:
            raise InconsistentRelationsError(
                f"{key} is below keeper {keeper} but has no direct "
                "supersession relation to it: inconsistent relations"
            )
        pointers.append(direct)
    return Chain(keeper=keeper, maximal=maximal, pointers=tuple(pointers))


# --- results ----------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveEvaluation:
    """One archive: strong identity, re-derived inventory, and comparability."""

    archive_key: str
    filename: str
    archive_id: int | None
    derived: DerivedArchive
    stored_rows: str

    @property
    def unresolved_reasons(self) -> tuple[str, ...]:
        reasons = []
        if self.derived.unresolved_reason is not None:
            reasons.append(self.derived.unresolved_reason)
        if self.derived.problem is None and self.stored_rows != STORED_AGREE:
            reasons.append(self.stored_rows)
        return tuple(reasons)

    @property
    def comparable(self) -> bool:
        return not self.unresolved_reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "archive": self.archive_key,
            "filename": self.filename,
            "archive_id": self.archive_id,
            "sha256": self.derived.sha256,
            "file_size": self.derived.file_size,
            "read_mode": self.derived.read_mode,
            "comparable": self.comparable,
            "unresolved_reasons": list(self.unresolved_reasons),
            "stored_rows": self.stored_rows,
            "pages": [
                {
                    "index": page.page_index,
                    "entry_name": page.entry_name,
                    "sha256": page.digest,
                }
                for page in self.derived.pages
            ],
            "non_image_members": [m.as_dict() for m in self.derived.non_image],
        }


@dataclass(frozen=True)
class KeeperLabel:
    """The keeper's version label (§4.6), or why it is held for a decision."""

    status: str
    value: str | None
    display: str | None
    source: str | None
    reason: str
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "display": self.display,
            "source": self.source,
            "reason": self.reason,
            "notes": list(self.notes),
        }


def keeper_label(
    keeper: ArchiveResolution, others: Sequence[ArchiveResolution]
) -> KeeperLabel:
    """The keeper's own resolved version, never another member's.

    Reads only ``keeper.fields["version"]``. ``others`` contribute notes
    for review (their markers are shown, never used).
    """
    version = keeper.fields["version"]
    notes = tuple(
        f"{other.archive_key} carries version candidate(s) "
        f"{sorted({e.display_value for e in other.fields['version'].evidence})}; "
        "never used for the keeper's label"
        for other in others
        if other.fields["version"].evidence
    )
    if version.resolved:
        return KeeperLabel(
            status=LABEL_RESOLVED,
            value=version.value,
            display=version.display_value,
            source=version.source,
            reason=f"the keeper's own version, from {version.source}",
            notes=notes,
        )
    return KeeperLabel(
        status=LABEL_HELD,
        value=None,
        display=None,
        source=None,
        reason=(
            f"the keeper's own version is unresolved ({version.reason}); the "
            "label is held for a decision"
        ),
        notes=notes,
    )


@dataclass(frozen=True)
class FamilySupersession:
    """The step-5 verdict for one family.

    ``status`` is ``not_established`` (step 4 refused the family: nothing
    compared), ``unresolved`` (a member is not comparable: nothing
    compared), ``review`` (compared, but a relation or the chain needs a
    human), or ``supersession`` (a unique keeper, every other member a
    strict prefix of it with its payload carried). Only ``supersession`` is
    actionable by a later step. ``keeper``/``pointers``/``label`` are set
    whenever a unique keeper exists, including under ``review``, so the
    reviewer sees the proposal.
    """

    status: str
    members: tuple[str, ...]
    family: FamilyEvidence
    reasons: tuple[str, ...]
    relations: tuple[Relation, ...] = ()
    keeper: str | None = None
    pointers: tuple[Relation, ...] = ()
    label: KeeperLabel | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "members": list(self.members),
            "family": self.family.as_dict(),
            "reasons": list(self.reasons),
            "basis": BASIS_EXACT,
            "relations": [r.as_dict() for r in self.relations],
            "keeper": self.keeper,
            "superseded": [
                {
                    "archive": p.older,
                    "keeper": p.newer,
                    "relation": p.relation,
                    "outcome": p.outcome,
                    "index_map": None if p.index_map is None else list(p.index_map),
                }
                for p in self.pointers
            ],
            "label": None if self.label is None else self.label.as_dict(),
        }


@dataclass(frozen=True)
class FolderSupersession:
    """The evaluator's output for one folder, with its database snapshot."""

    directory: str
    archives: tuple[ArchiveEvaluation, ...]
    families: tuple[FamilySupersession, ...]
    snapshot: Mapping[str, Any]
    accept_comicinfo_drop: bool
    perceptual: str = "not consulted"

    def families_of(self, archive_key: str) -> tuple[FamilySupersession, ...]:
        """Every family verdict naming ``archive_key`` (usually one)."""
        return tuple(f for f in self.families if archive_key in f.members)

    def as_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "basis": BASIS_EXACT,
            "perceptual": self.perceptual,
            "accept_comicinfo_drop": self.accept_comicinfo_drop,
            "snapshot": dict(self.snapshot),
            "archives": [a.as_dict() for a in self.archives],
            "families": [f.as_dict() for f in self.families],
        }


# --- the family evaluator ---------------------------------------------------


def evaluate_family(
    family: FamilyEvidence,
    archives: Mapping[str, ArchiveEvaluation],
    resolutions: Mapping[str, ArchiveResolution],
    *,
    accept_comicinfo_drop: bool = False,
) -> FamilySupersession:
    """§4.4-§4.6 for one step-4 family verdict. Pure.

    Nothing is compared unless step 4 *established* the family, and nothing
    is compared if any member is not comparable: an unresolved member might
    be the true keeper, so the rest cannot be ranked without it.
    """
    members = tuple(sorted(family.members))
    if family.status != FAMILY_ESTABLISHED:
        status = (
            FAMILY_UNRESOLVED
            if family.status == FAMILY_INCOMPARABLE
            else FAMILY_NOT_ESTABLISHED
        )
        reasons = list(family.reasons)
        if status == FAMILY_UNRESOLVED:
            reasons += [
                f"{key}: {reason}"
                for key in members
                for reason in archives[key].unresolved_reasons
            ]
        return FamilySupersession(status, members, family, tuple(reasons))

    unresolved = [
        f"{key}: {reason}"
        for key in members
        for reason in archives[key].unresolved_reasons
    ]
    if unresolved:
        return FamilySupersession(FAMILY_UNRESOLVED, members, family, tuple(unresolved))

    relations = tuple(
        compare_pair(
            first, archives[first].derived, second, archives[second].derived,
            accept_comicinfo_drop=accept_comicinfo_drop,
        )
        for index, first in enumerate(members)
        for second in members[index + 1:]
    )
    reasons = [
        f"{r.older} / {r.newer}: {r.outcome}: {r.reason}"
        for r in relations
        if r.outcome in (OUTCOME_REVIEW, OUTCOME_INSERTED_PAGES)
    ]
    chain = resolve_chain(members, relations)
    if chain.keeper is None:
        duplicates = {
            (r.older, r.newer)
            for r in relations
            if r.outcome == OUTCOME_DUPLICATE
            and r.older in chain.maximal
            and r.newer in chain.maximal
        }
        reasons.append(
            f"no unique keeper: maximal members {list(chain.maximal)}"
            + (
                f" (whole-archive duplicates among them: {sorted(duplicates)})"
                if duplicates
                else " (a fork, or a relation downgraded to review)"
            )
        )
        return FamilySupersession(
            FAMILY_REVIEW, members, family, tuple(reasons), relations
        )

    label = keeper_label(
        resolutions[chain.keeper],
        [resolutions[key] for key in members if key != chain.keeper],
    )
    return FamilySupersession(
        status=FAMILY_REVIEW if reasons else FAMILY_SUPERSESSION,
        members=members,
        family=family,
        reasons=tuple(reasons),
        relations=relations,
        keeper=chain.keeper,
        pointers=chain.pointers,
        label=label,
    )


# --- the folder entry point -------------------------------------------------


def evaluate_folder(
    directory: str | Path,
    *,
    database: str | Path,
    archive_ids: Mapping[str, int],
    series_folder: str | None = None,
    decisions: HumanDecisionSource = NO_HUMAN_DECISIONS,
    accept_comicinfo_drop: bool = False,
    integrity_check: Callable[[sqlite3.Connection], str] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_held_bytes: int = DEFAULT_MAX_HELD_BYTES,
) -> FolderSupersession:
    """Evaluate every ``*.cbz`` directly in ``directory``. Read-only.

    ``archive_ids`` maps a filename in the folder to its ``archive_files.id``
    so its stored rows can be compared (a filename without one is
    unresolved). Steps:

    1. stored rows for every supplied id, in one guarded snapshot
       (`load_page_evidence`; a concurrent commit raises
       `DatabaseChangedError` and nothing is reported);
    2. each archive re-derived from bytes bound to its sha256
       (`derive_archive`) and checked against its stored rows;
    3. step 4 (`resolve_folder`) over the re-derived page evidence and the
       ComicInfo read from the same bytes, which establishes or refuses
       families;
    4. `evaluate_family` for every family verdict.

    ``accept_comicinfo_drop`` is the operator's acceptance of dropping a
    superseded member's ComicInfo.xml (§4.4, §8 question 6); default off.
    ``max_held_bytes`` bounds the memory `derive_archive` holds per archive.
    """
    root = Path(directory)
    files = sorted(
        (
            child
            for child in root.iterdir()
            if child.is_file() and child.suffix.casefold() == ".cbz"
        ),
        key=lambda child: child.name,
    )
    unknown = sorted(set(archive_ids) - {child.name for child in files})
    if unknown:
        raise ValueError(f"archive ids supplied for files not in the folder: {unknown}")

    kwargs = {} if integrity_check is None else {"integrity_check": integrity_check}
    snapshot: ConsistentSnapshot[dict[int, PageEvidence]] = load_page_evidence(
        database, sorted(set(archive_ids.values())), **kwargs
    )

    evaluations: dict[str, ArchiveEvaluation] = {}
    facts: list[ArchiveFacts] = []
    for child in files:
        key = str(child)
        archive_id = archive_ids.get(child.name)
        derived = derive_archive(
            child, chunk_size=chunk_size, max_held_bytes=max_held_bytes
        )
        stored = None if archive_id is None else snapshot.result.get(archive_id)
        evaluations[key] = ArchiveEvaluation(
            archive_key=key,
            filename=child.name,
            archive_id=archive_id,
            derived=derived,
            stored_rows=stored_rows_verdict(derived, stored, archive_id),
        )
        facts.append(
            ArchiveFacts(
                archive_key=key,
                filename=child.name,
                comicinfo=comicinfo_facts(derived.comicinfo_documents),
                pages=derived.page_evidence(),
            )
        )

    resolution = resolve_folder(
        FolderFacts(series_folder=series_folder, archives=tuple(facts)), decisions
    )
    resolutions = resolution.by_key()

    # An archive sits in two families only when its human family decisions
    # conflict, and that conflict refuses every one of them. Two
    # *established* families sharing a member would let one archive be
    # superseded twice; that is not reachable from step 4 and stops the run.
    seen: set[str] = set()
    for family in resolution.families:
        if family.established:
            if seen & set(family.members):
                raise InconsistentRelationsError(
                    f"archives {sorted(seen & set(family.members))} are in two "
                    "established families"
                )
            seen |= set(family.members)

    families = tuple(
        evaluate_family(
            family,
            evaluations,
            resolutions,
            accept_comicinfo_drop=accept_comicinfo_drop,
        )
        for family in resolution.families
    )
    return FolderSupersession(
        directory=str(root),
        archives=tuple(evaluations[str(child)] for child in files),
        families=families,
        snapshot={
            "database": str(snapshot.database),
            **snapshot.report_fields(),
        },
        accept_comicinfo_drop=accept_comicinfo_drop,
    )
