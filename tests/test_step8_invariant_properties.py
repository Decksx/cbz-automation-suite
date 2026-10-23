"""Property-style tests for the roadmap's Step 8 invariants.

`docs/implementation_roadmap.md`, Step 8, lists ten "inexpensive invariants"
for property-based testing. This file covers the ones that can be tested
deterministically against the code as it stands, without production changes:

    1  normalization is idempotent             series_key, archive keys
    2  page ordering is deterministic          calculate_page_hashes
    4  repeated completed jobs do not create   page-hash and hash saves
       duplicate rows
    6  changed bytes create or select the      ArchiveHashRepository.save
       correct revision
    7  a current-revision pointer always       the same random walk
       belongs to its archive
    10 invalid job-state transitions are       JobQueue against a model
       rejected

plus two fault-injection tests from the "unverified" list (hash saves and
job acquisition), using `tests/fault_injection.py`.

No `hypothesis`
---------------

`hypothesis` is neither installed nor declared in `requirements.txt` or the
CI workflow, and adding a dependency was out of scope for this slice. These
are therefore *seeded* randomized tests built on the standard library's
`random.Random`: every input is reproducible from the seed in the test ID,
there is no shrinking, and the inputs are drawn from hand-chosen alphabets
aimed at the characters that have bitten this project before. That is
weaker than a real property-testing engine and is recorded as such.

Two invariants found false, both since fixed
--------------------------------------------

Running these properties found two places where the stated invariant was
false on master. Both are now fixed (below). Neither was fixed in the
slice that found them, and
neither is hidden by narrowing the generator until it passes. Each is split
into:

  * a passing property over the domain where the invariant *does* hold,
    named for that domain so it does not read as the whole claim; and
  * a strict ``xfail`` carrying the minimal counterexample, which turns
    into a hard failure (XPASS) the moment the defect is fixed, so the
    marker cannot outlive the bug.

``series_key`` was not idempotent when a marker word was glued to a word
character that a later step turns into a separator: ``_Uncensored``
survived marker removal (no ``\\b`` between ``_`` and ``U``), the
underscore then became a space, and a second pass removed the marker.
The practical consequence was a series split: ``Berserk_Uncensored`` keyed
to ``berserk uncensored`` while ``Berserk Uncensored`` keyed to
``berserk``. **Fixed by #111's reorder** (marker removal after the
separator rule); its two strict xfails are now ordinary passing tests.

Page order was not deterministic when two image names had equal natural
keys (``a.png``/``A.png``, ``01.png``/``1.png``): the sort was stable, so
physical archive order decided, and the content signature changed when the
same archive was merely re-packed in a different order. **Fixed by the
raw-name tie-break** (``page_hashing.page_order_key``, design
``docs/canonical_naming_and_version_supersession.md`` §1.2); its three
strict xfails are now ordinary passing tests. The fix holds for *unique*
names only: identical names cannot be ordered and are reported as
unresolved instead, which ``tests/test_page_order_tie_break.py`` covers.

Also probed, not pinned: ``cbz_core.sanitize``, ``clean_filename`` and
``clean_directory_name`` are not idempotent either. Minimal examples from
the same generator: ``"Batman].cC"`` -> ``"Batman.cC"`` -> ``""`` (removing
the stray bracket exposes something the URL rule then strips) and
``"&amp#1"``, where each pass unescapes one more level. Whether the
sanitizer is meant to be a fixpoint is a decision this slice does not
make, so no test asserts either answer.

No production database, no live library, no network. Every fixture is
synthetic and lives under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from comic_automation.archive.hashing import (
    ArchiveHashRepository,
    calculate_archive_hash,
)
from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
)
from comic_automation.database import dal
from comic_automation.database.connection import (
    connect_database,
    database_connection,
)
from comic_automation.database.migrations import apply_migrations
from comic_automation.jobs import (
    InvalidJobTransitionError,
    JobQueue,
    JobStatus,
)
from scripts import cbz_core
from scripts.cbz_routing import series_key
from tests import fault_injection as fi
from tests import golden_corpus as gc


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)

# Seeds are fixed and appear in every test ID, so a failure names the exact
# input stream that produced it and reruns identically.
SEEDS = (0, 1, 2, 3, 4, 5, 6, 7)


def _random_string(rng: random.Random, alphabet: list[str], *, max_len: int):
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_len)))


# --- invariant 1: normalization is idempotent ----------------------------

# Tokens rather than single characters, so the generator reaches the shapes
# that matter: decomposed accents, compatibility forms, case mappings that
# emit combining marks (U+0130 lowercases to "i" + U+0307), CJK, every
# separator the series rule unifies, and unusual whitespace. The marker
# words "uncensored"/"decensored" are deliberately absent here; they are
# drawn separately from `_MARKER_TOKENS` by the marker-word properties below
# (strict xfails until #114 fixed series_key).
_NAME_TOKENS = [
    "a", "B", "z", "Q", "0", "1", "9", "42", "Vol", "Ch", "v2",
    " ", "  ", "\t", " ", "　",
    "_", "-", ".", ",", "!", "?", "'", '"', "(", ")", "[", "]", "{", "}",
    "#", "&", ":", "/", "~", "+", "—", "：",
    "é", "é", "́", "ü", "ü", "ß", "İ",
    "Å", "Å", "Ｂ", "日本", "ワン",
    "Berserk", "Attack", "on", "Titan",
]
_MARKER_TOKENS = ["Uncensored", "decensored", "UNCENSORED"]


@pytest.mark.parametrize("seed", SEEDS)
def test_series_key_is_idempotent_on_marker_free_names(seed: int) -> None:
    """key(key(x)) == key(x) for names without marker words.

    Idempotence is what lets a stored key be re-keyed safely: SeriesIndex,
    the lock registry and library maintenance all feed previously computed
    keys back through the rule. A rule that moved on its own output would
    make "same series" depend on how many times a name had been normalized.

    Restricted to marker-free names because the full claim is false; see
    `test_series_key_is_idempotent_with_marker_words_present`.
    """
    rng = random.Random(seed)

    for _ in range(1500):
        name = _random_string(rng, _NAME_TOKENS, max_len=14)
        once = series_key(name)
        assert series_key(once) == once, ascii(name)


@pytest.mark.parametrize("seed", SEEDS)
def test_series_key_unifies_space_hyphen_and_underscore(seed: int) -> None:
    """Any mix of the three separators keys like single spaces (#44).

    Words are drawn without separators of their own, then joined with a
    random run of space, hyphen and underscore. The oracle is the same words
    joined by one space -- an independent spelling, not the function's own
    output for a related input.
    """
    rng = random.Random(seed)
    words = [token for token in _NAME_TOKENS if token.strip() and
             token.isalnum()]

    for _ in range(500):
        chosen = [rng.choice(words) for _ in range(rng.randint(1, 5))]
        joined = ""

        for index, word in enumerate(chosen):
            if index:
                joined += "".join(
                    rng.choice(" -_") for _ in range(rng.randint(1, 3))
                )
            joined += word

        assert series_key(joined) == series_key(" ".join(chosen)), ascii(
            joined
        )


@pytest.mark.parametrize("seed", SEEDS)
def test_series_key_ignores_nfc_versus_nfd_encoding(seed: int) -> None:
    """Two encodings of one character sequence are one series.

    Content from a macOS-side share is routinely NFD. The leading NFC call
    in `series_key` is what makes this hold; without it the punctuation
    rule turns each combining mark into a space.
    """
    rng = random.Random(seed)

    for _ in range(1000):
        name = _random_string(rng, _NAME_TOKENS + _MARKER_TOKENS, max_len=14)
        nfc = unicodedata.normalize("NFC", name)
        nfd = unicodedata.normalize("NFD", name)
        assert series_key(nfc) == series_key(nfd), ascii(name)


@pytest.mark.parametrize("seed", SEEDS[:2])
def test_series_key_is_idempotent_with_marker_words_present(
    seed: int,
) -> None:
    """The full idempotence claim, including glued marker words.

    A strict xfail until #111's fix moved marker removal after the
    separator rule. The counterexamples it carried ('_decensored',
    'decensored_', '_Uncensored', 'Uncensored_' and the U+0130 forms) are
    pinned individually in `tests/test_series_key_canonical.py`.
    """
    rng = random.Random(seed)

    for _ in range(3000):
        name = _random_string(rng, _NAME_TOKENS + _MARKER_TOKENS, max_len=14)
        once = series_key(name)
        assert series_key(once) == once, ascii(name)


@pytest.mark.parametrize(
    "joined",
    ["Berserk_Uncensored", "Berserk_decensored", "Uncensored_Berserk"],
)
def test_an_underscore_joined_marker_keys_like_a_space_joined_one(
    joined: str,
) -> None:
    """The idempotence defect's consequence, a series split, stays fixed.

    Releases that spell the same title with `_` and with spaces are one
    series by the #44 rule, and so are their uncensored editions. Before
    #111 the underscore spelling of an uncensored edition got an identity
    of its own.
    """
    assert series_key(joined) == series_key(joined.replace("_", " "))


@pytest.mark.parametrize("seed", SEEDS)
def test_archive_key_and_number_tokens_are_idempotent(seed: int) -> None:
    """The two duplicate-comparison normalizers in `cbz_core` are fixpoints.

    `normalise_archive_key` is compared across files to find duplicates and
    `normalise_number_tokens` strips zero padding from Vol./Ch. tokens; a
    second application that changed either result would make a name's
    duplicate status depend on how many passes it had seen.
    """
    rng = random.Random(seed)
    tokens = _NAME_TOKENS + ["Vol.01", "Ch.005", "Vol. 0010", "ch.0", "c.007"]

    for _ in range(1500):
        name = _random_string(rng, tokens, max_len=12)

        key = cbz_core.normalise_archive_key(name)
        assert cbz_core.normalise_archive_key(key) == key, ascii(name)

        numbers = cbz_core.normalise_number_tokens(name)
        assert cbz_core.normalise_number_tokens(numbers) == numbers, ascii(
            name
        )


# --- invariant 2: page ordering is deterministic -------------------------


def _build(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    return gc.build_cbz(path, entries)


@pytest.mark.parametrize("seed", SEEDS)
def test_page_order_ignores_member_order_for_distinct_natural_keys(
    seed: int, tmp_path: Path
) -> None:
    """Re-packing an archive in any order yields the same pages, same order.

    Each trial draws distinct page numbers rendered with random zero
    padding and random extension case, plus a ComicInfo and a non-image
    member that must not count. The expected order is numeric -- an oracle
    independent of production's `_natural_key` -- and the content signature
    must be identical for every physical permutation, because it is what
    `archive_content_signatures` compares across archives.

    Names here have distinct natural keys by construction. Equal keys are
    covered by the tie-break test below.
    """
    rng = random.Random(seed)

    for trial in range(12):
        numbers = rng.sample(range(0, 400), rng.randint(1, 12))
        prefix = rng.choice(["", "p", "Page ", "img_", "chapter 3/"])
        pages = [
            (
                f"{prefix}{number:0{rng.randint(1, 4)}d}"
                f"{rng.choice(['.png', '.PNG', '.jpg', '.Jpeg', '.webp'])}",
                f"page {number}".encode(),
            )
            for number in numbers
        ]
        extras = [(gc.COMIC_INFO, gc.comic_info_xml()), ("notes.txt", b"x")]
        expected = [
            name for name, _ in sorted(
                pages, key=lambda page: int(page[1].split()[1])
            )
        ]

        signatures = set()

        for variant in range(3):
            entries = pages + extras
            rng.shuffle(entries)
            result = calculate_page_hashes(
                _build(tmp_path / f"{trial}-{variant}.cbz", entries)
            )

            assert [p.entry_name for p in result.pages] == expected
            assert [p.page_index for p in result.pages] == list(
                range(len(expected))
            )
            signatures.add(result.content_digest)

        assert len(signatures) == 1, (trial, expected)


@pytest.mark.parametrize(
    "names",
    [("a.png", "A.png"), ("01.png", "1.png"), ("p1.png", "p01.png")],
    ids=["case-only", "padding-only", "padding-after-prefix"],
)
def test_page_order_is_deterministic_when_natural_keys_tie(
    names: tuple[str, str], tmp_path: Path
) -> None:
    """The full determinism claim, including tied (but distinct) names.

    These were the minimal counterexamples, pinned as strict xfails until
    the raw-name tie-break landed. Each pair ties on the natural key; the
    two packings are mirror images, so a sort that fell back to physical
    order would give opposite page orders and different signatures.
    """
    entries = [(name, f"payload {name}".encode()) for name in names]

    forward = calculate_page_hashes(_build(tmp_path / "f.cbz", entries))
    reverse = calculate_page_hashes(_build(tmp_path / "r.cbz", entries[::-1]))

    assert [p.entry_name for p in forward.pages] == [
        p.entry_name for p in reverse.pages
    ]
    assert forward.content_digest == reverse.content_digest


# --- shared database fixture ---------------------------------------------


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "properties.db"
    connection = connect_database(path)

    try:
        apply_migrations(connection, MIGRATIONS)
        connection.commit()
    finally:
        connection.close()

    return path


@pytest.fixture()
def connection(database: Path):
    conn = dal.open_connection(database)

    try:
        yield conn
    finally:
        conn.close()


def _seed_archive(conn: sqlite3.Connection, archive: Path) -> tuple[int, int]:
    """An archive identity and current location, as discovery creates them.

    `ArchiveRepository.create` also gives the archive its provisional
    origin revision, which is the state every revision walk starts from.
    """
    stat = archive.stat()

    with dal.transaction(conn):
        archive_id = dal.ArchiveRepository(conn).create(
            file_size=int(stat.st_size)
        )
        cursor = conn.execute(
            """
            INSERT INTO file_locations (
                archive_id, path, file_size, modified_time_ns
            )
            VALUES (?, ?, ?, ?)
            """,
            (archive_id, str(archive.resolve()), int(stat.st_size),
             int(stat.st_mtime_ns)),
        )

    return archive_id, int(cursor.lastrowid)


def _hash_and_save(conn, archive_id: int, location_id: int, archive: Path):
    with dal.transaction(conn):
        ArchiveHashRepository(conn).save(
            archive_id=archive_id,
            location_id=location_id,
            result=calculate_archive_hash(archive),
            enqueue_reinspection=False,
        )


# --- invariants 6 and 7: revisions under a random walk -------------------

# Shared between the archives on purpose: two archives holding the same
# bytes is the production norm (888 exact-duplicate groups measured), and it
# is exactly what a lookup that forgot `archive_id` would confuse.
_PAYLOADS = (b"state A", b"state B is longer", b"state C", b"state D!")


@pytest.mark.parametrize("seed", SEEDS)
def test_revision_walk_selects_or_creates_the_right_revision(
    seed: int, connection, tmp_path: Path
) -> None:
    """Every hash lands on the revision for the bytes actually on disk.

    Two archives each take a random walk over a shared pool of byte states,
    hashed after every step. A model records, per archive, the order in
    which distinct states were first seen. After every step:

      * the current revision's digest is the SHA-256 of the file on disk,
        and agrees with `archive_hashes` (invariant 6: changed bytes select
        or create the correct revision);
      * the lineage is the provisional origin followed by one established
        revision per distinct state, in first-seen order -- revisiting a
        state reuses its revision, it never mints a second;
      * ordinals run 1..n and each revision links to its predecessor;
      * every archive's current pointer names one of its *own* revisions
        (invariant 7), checked across the whole table by SQL rather than
        only for the archive just touched.
    """
    rng = random.Random(seed)
    revisions = dal.RevisionRepository(connection)
    archives = []

    for index in range(2):
        path = tmp_path / "library" / f"issue-{index}.cbz"
        gc.build_cbz(path, [("001.jpg", rng.choice(_PAYLOADS))])
        archive_id, location_id = _seed_archive(connection, path)
        archives.append((archive_id, location_id, path))

    seen: dict[int, list[str]] = {archive_id: [] for archive_id, _, _ in archives}

    for _ in range(14):
        archive_id, location_id, path = rng.choice(archives)

        # Rewriting with the payload already present is a legitimate step:
        # it is the unchanged-rehash case and must add nothing.
        gc.build_cbz(path, [("001.jpg", rng.choice(_PAYLOADS))])
        on_disk = hashlib.sha256(path.read_bytes()).hexdigest()

        _hash_and_save(connection, archive_id, location_id, path)

        if on_disk not in seen[archive_id]:
            seen[archive_id].append(on_disk)

        current = revisions.current_for(archive_id)
        stored = connection.execute(
            "SELECT digest FROM archive_hashes WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()["digest"]

        assert current.archive_id == archive_id
        assert current.archive_sha256 == on_disk == stored
        assert current.identity_state == "established"

        lineage = revisions.lineage_for(archive_id)

        assert lineage[0].identity_state == "provisional"
        assert [r.archive_sha256 for r in lineage[1:]] == seen[archive_id]
        assert [r.revision_ordinal for r in lineage] == list(
            range(1, len(lineage) + 1)
        )
        assert [r.previous_revision_id for r in lineage[1:]] == [
            r.revision_id for r in lineage[:-1]
        ]

        foreign = connection.execute(
            """
            SELECT COUNT(*)
            FROM archive_files AS f
            LEFT JOIN archive_revisions AS r
              ON r.id = f.current_revision_id
            WHERE r.id IS NULL OR r.archive_id != f.id
            """
        ).fetchone()[0]

        assert foreign == 0


# --- invariant 4: repeated completed jobs do not duplicate rows ----------


def _page_entries(rng: random.Random, count: int) -> list[tuple[str, bytes]]:
    return [
        (f"{index:03d}.png", f"page {rng.randint(0, 3)}".encode())
        for index in range(1, count + 1)
    ]


@pytest.mark.parametrize("seed", SEEDS)
def test_repeated_page_hash_saves_never_accumulate_rows(
    seed: int, connection, tmp_path: Path
) -> None:
    """A page-hash job completing again replaces, it does not append.

    Three archives are rehashed in random order, with the page count
    changing between saves (pages added and removed, the shapes the
    delete-then-reinsert design exists for). After every save the stored
    inventory must equal the latest result exactly: one `archive_pages` row
    per page, one `page_hashes` row per page row, no orphaned digests, and
    one content signature per archive holding the latest digest.
    """
    rng = random.Random(seed)
    archives = []

    for index in range(3):
        path = tmp_path / "library" / f"book-{index}.cbz"
        gc.build_cbz(path, _page_entries(rng, 2))
        archive_id, location_id = _seed_archive(connection, path)
        archives.append((archive_id, location_id, path))

    latest: dict[int, object] = {}
    repository = ArchivePageHashRepository(connection)

    for _ in range(12):
        archive_id, location_id, path = rng.choice(archives)
        gc.build_cbz(path, _page_entries(rng, rng.randint(0, 6)))
        result = calculate_page_hashes(path)

        # `save` owns its transaction when none is open, which is how the
        # handler calls it.
        repository.save(
            archive_id=archive_id, location_id=location_id, result=result
        )
        latest[archive_id] = result

        for known_id, known in latest.items():
            rows = connection.execute(
                """
                SELECT p.page_index, p.entry_name, h.digest
                FROM archive_pages AS p
                JOIN page_hashes AS h ON h.page_id = p.id
                WHERE p.archive_id = ?
                ORDER BY p.page_index
                """,
                (known_id,),
            ).fetchall()

            assert [tuple(row) for row in rows] == [
                (page.page_index, page.entry_name, page.digest)
                for page in known.pages
            ]

        totals = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM archive_pages),
                (SELECT COUNT(*) FROM page_hashes),
                (SELECT COUNT(*) FROM page_hashes
                 WHERE page_id NOT IN (SELECT id FROM archive_pages)),
                (SELECT COUNT(*) FROM archive_content_signatures)
            """
        ).fetchone()

        expected_pages = sum(known.page_count for known in latest.values())
        assert tuple(totals) == (expected_pages, expected_pages, 0,
                                 len(latest))

        for known_id, known in latest.items():
            signature = connection.execute(
                "SELECT digest, page_count FROM archive_content_signatures "
                "WHERE archive_id = ?",
                (known_id,),
            ).fetchone()
            assert tuple(signature) == (known.content_digest,
                                        known.page_count)


# --- invariant 10: invalid job-state transitions are rejected ------------


class _JobModel:
    """The job lifecycle, written out independently of `JobQueue`.

    pending -claim-> claimed -run-> running; claimed|running -complete->
    completed; claimed|running -fail-> pending (attempts remain, not
    permanent) or failed; pending|blocked -cancel-> cancelled. Everything
    else is invalid, and so is any claimed/running transition requested by
    a worker that does not hold the claim. `attempts` counts claims,
    because that is when the queue increments it (see
    `JobQueue.claim_next`).
    """

    def __init__(self, job_type: str, max_attempts: int) -> None:
        self.job_type = job_type
        self.status = JobStatus.PENDING
        self.attempts = 0
        self.max_attempts = max_attempts
        self.worker: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {JobStatus.COMPLETED, JobStatus.FAILED,
                               JobStatus.CANCELLED}


# `*_unowned` call without `worker_id`, which skips the ownership clause.
# They exist because the ownership clause otherwise masks the status
# clause: a pending job has no worker, so `mark_running(worker_id=...)` on it
# fails on ownership even if the status check were gone -- measured, a
# mutation letting `mark_running` accept 'pending' survived until these
# were added.
_OPERATIONS = ("claim", "run", "run_as_other", "run_unowned", "complete",
               "complete_as_other", "complete_unowned", "fail",
               "fail_permanent", "cancel")

# Jobs reach a terminal status within a few steps, and a walk over only
# terminal jobs exercises nothing but rejection. Measured before these
# rules were added: across all eight seeds `run_as_other` met a *claimed*
# job zero times, so a mutation removing the ownership check from
# `mark_running` survived. The walk therefore prefers live jobs, claims a
# pending job half the time, and enqueues a replacement whenever one
# retires; the coverage assertion at the end of the test keeps it honest.
_LIVE_JOB_BIAS = 0.85
_MAX_JOBS = 80


@pytest.mark.parametrize("seed", SEEDS)
def test_random_job_operations_follow_the_lifecycle(
    seed: int, tmp_path: Path
) -> None:
    """Every operation either performs a legal transition or changes nothing.

    Random operations are applied to a pool of jobs and mirrored on
    `_JobModel`. A legal operation must produce the model's status,
    attempts and owner; an illegal one must raise
    `InvalidJobTransitionError` (or, for `claim_next`, return None) and
    leave the job's row exactly as it was. The `*_as_other` operations are
    the ownership check: a worker that does not hold the claim must be
    refused even when the status alone would allow the transition.

    Each job gets its own `job_type` so `claim_next` can target it; the
    queue's priority ordering is a separate property tested elsewhere. The
    test also asserts that the walk reached every (operation, status) pair
    it exists to check, so a generator change that starves a branch fails
    here instead of silently testing less.
    """
    rng = random.Random(seed)
    reached: set[tuple[str, JobStatus]] = set()

    with database_connection(tmp_path / "queue.db") as conn:
        apply_migrations(conn, MIGRATIONS)
        queue = JobQueue(conn)
        models: dict[int, _JobModel] = {}

        def spawn() -> None:
            job_type = f"type-{len(models)}"
            max_attempts = rng.randint(1, 3)
            job = queue.enqueue(job_type, max_attempts=max_attempts)
            models[job.id] = _JobModel(job_type, max_attempts)

        def row(job_id: int):
            return tuple(
                conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
                .fetchone()
            )

        for _ in range(3):
            spawn()

        for step in range(800):
            live = [job_id for job_id, m in models.items() if not m.terminal]

            if live and rng.random() < _LIVE_JOB_BIAS:
                job_id = rng.choice(live)
            else:
                job_id = rng.choice(list(models))

            model = models[job_id]

            # A pending job is claimed half the time; otherwise claimed and
            # running states are too short-lived for the walk to probe them.
            if model.status == JobStatus.PENDING and rng.random() < 0.5:
                operation = "claim"
            else:
                operation = rng.choice(_OPERATIONS)
            before = row(job_id)
            context = (seed, step, job_id, operation, model.status)
            reached.add((operation, model.status))

            if operation == "claim":
                claimed = queue.claim_next(
                    "worker-a", job_types=[model.job_type]
                )

                if model.status == JobStatus.PENDING:
                    assert claimed is not None, context
                    assert claimed.id == job_id, context
                    model.status = JobStatus.CLAIMED
                    model.attempts += 1
                    model.worker = "worker-a"
                else:
                    assert claimed is None, context
                    assert row(job_id) == before, context
                continue

            held = model.status in {JobStatus.CLAIMED, JobStatus.RUNNING}
            legal = {
                "run": model.status == JobStatus.CLAIMED,
                "run_as_other": False,
                "run_unowned": model.status == JobStatus.CLAIMED,
                "complete": held,
                "complete_as_other": False,
                "complete_unowned": held,
                "fail": held,
                "fail_permanent": held,
                "cancel": model.status in {JobStatus.PENDING,
                                           JobStatus.BLOCKED},
            }[operation]

            def apply():
                if operation == "run":
                    return queue.mark_running(job_id, worker_id="worker-a")
                if operation == "run_as_other":
                    return queue.mark_running(job_id, worker_id="worker-b")
                if operation == "run_unowned":
                    return queue.mark_running(job_id)
                if operation == "complete":
                    return queue.mark_completed(job_id, worker_id="worker-a")
                if operation == "complete_as_other":
                    return queue.mark_completed(job_id, worker_id="worker-b")
                if operation == "complete_unowned":
                    return queue.mark_completed(job_id)
                if operation in {"fail", "fail_permanent"}:
                    return queue.mark_failed(
                        job_id,
                        "injected by the property test",
                        worker_id="worker-a",
                        permanent=operation == "fail_permanent",
                    )
                return queue.cancel(job_id, "property test")

            if not legal:
                with pytest.raises(InvalidJobTransitionError):
                    apply()
                assert row(job_id) == before, context
                assert not conn.in_transaction, context
                continue

            result = apply()

            if operation in {"run", "run_unowned"}:
                model.status = JobStatus.RUNNING
            elif operation in {"complete", "complete_unowned"}:
                model.status = JobStatus.COMPLETED
            elif operation == "cancel":
                model.status = JobStatus.CANCELLED
            elif (operation == "fail"
                  and model.attempts < model.max_attempts):
                model.status = JobStatus.PENDING
                model.worker = None
            else:
                model.status = JobStatus.FAILED

            assert result.status == model.status, context
            assert result.attempts == model.attempts, context

            if model.status == JobStatus.PENDING:
                assert result.worker_id is None, context

            if model.terminal and len(models) < _MAX_JOBS:
                spawn()

    # Coverage of the walk itself: the pairs where a missing guard would
    # let an illegal transition through, plus each legal transition.
    required = {
        ("run_as_other", JobStatus.CLAIMED),
        ("complete_as_other", JobStatus.CLAIMED),
        ("complete_as_other", JobStatus.RUNNING),
        ("cancel", JobStatus.CLAIMED),
        ("cancel", JobStatus.RUNNING),
        ("run", JobStatus.PENDING),
        ("run_unowned", JobStatus.PENDING),
        ("run_unowned", JobStatus.RUNNING),
        ("complete_unowned", JobStatus.PENDING),
        ("complete_unowned", JobStatus.COMPLETED),
        ("claim", JobStatus.CLAIMED),
        ("claim", JobStatus.PENDING),
        ("run", JobStatus.CLAIMED),
        ("complete", JobStatus.RUNNING),
        ("fail", JobStatus.RUNNING),
        ("cancel", JobStatus.PENDING),
    }
    assert required <= reached, sorted(required - reached)


# --- fault injection: hash saves and job acquisition ---------------------

_HASH_TABLES = (
    "archive_hashes",
    "archive_files",
    "file_locations",
    "archive_revisions",
    "archive_revision_observations",
)


def test_a_fault_at_the_last_write_of_a_hash_save_leaves_nothing(
    connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash save interrupted at its final statement rolls back entirely.

    `ArchiveHashRepository.save` writes the hash upsert, the location's
    size/mtime, the archive's file size, a new revision, the pointer move
    and finally an observation. The fault is injected at that last step,
    on a *changed* rehash so that every earlier write has something real to
    leave behind. All five tables must be exactly as they were.

    The existing atomicity test in `test_hashing_updates_revisions.py`
    raises after `save` returns; this one fails inside it, which is the
    case that would expose a statement committing on its own.

    Injected by patching `RevisionRepository.observe` rather than through
    `fi.failing_execute`, because a connection proxy is not the connection
    `dal.transaction` registered as owning the transaction, and
    `require_transaction` rightly refuses it.
    """
    archive = gc.build_cbz(tmp_path / "library" / "issue.cbz",
                           [("001.jpg", b"first bytes")])
    archive_id, location_id = _seed_archive(connection, archive)
    _hash_and_save(connection, archive_id, location_id, archive)

    gc.build_cbz(archive, [("001.jpg", b"different bytes entirely")])
    before = fi.table_snapshot(connection, _HASH_TABLES)
    calls = {"observe": 0}

    def failing_observe(self, **kwargs):
        calls["observe"] += 1
        raise fi.InjectedFailure("injected at the observation write")

    monkeypatch.setattr(dal.RevisionRepository, "observe", failing_observe)

    with pytest.raises(fi.InjectedFailure):
        _hash_and_save(connection, archive_id, location_id, archive)

    # The fault really fired, inside save, after the earlier writes ran.
    assert calls["observe"] == 1
    assert not connection.in_transaction
    assert fi.table_snapshot(connection, _HASH_TABLES) == before


def test_a_fault_while_claiming_leaves_the_job_claimable(
    database: Path,
) -> None:
    """A failed claim must not consume an attempt or keep the write lock.

    `claim_next` takes `BEGIN IMMEDIATE`, selects a candidate, then updates
    it. The fault is injected at that update. Afterwards the job must still
    be pending with zero attempts, the connection must hold no transaction
    (a leaked one would keep the write lock and stall every other worker),
    and a second connection must be able to claim the job normally.
    """
    with database_connection(database) as conn:
        queue = JobQueue(conn)
        job = queue.enqueue("inspect_archive")
        before = fi.table_snapshot(conn, ("jobs",))

        failing = fi.failing_execute(conn, fail_on="UPDATE jobs")

        with pytest.raises(fi.InjectedFailure):
            JobQueue(failing).claim_next("worker-a")

        assert failing.matches == 1
        assert not conn.in_transaction
        assert fi.table_snapshot(conn, ("jobs",)) == before
        assert queue.get(job.id).status == JobStatus.PENDING
        assert queue.get(job.id).attempts == 0

        with database_connection(database) as other:
            claimed = JobQueue(other).claim_next("worker-b")

        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.attempts == 1
