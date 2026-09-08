"""Read a written backfill plan back, and refuse anything short of proof.

The planner writes a plan as two files -- a JSON envelope and a CSV of
bindings -- and until this module nothing read either back. That gap is the
reason it exists: migration 015's executor must recompute the plan digest
from the artifact it is about to apply and refuse if it differs, and it
cannot recompute a digest over bindings it cannot reconstruct.

What "verified" means here
--------------------------

`read_plan_artifacts()` returns only when every one of these holds. Each is
a separate refusal with its own message, because an operator handed one
boolean learns nothing about which check fired:

```text
envelope   parseable JSON object, no duplicate keys, required fields
           present and correctly typed, planner version supported
csv        raw SHA-256 equals the envelope's artifacts.csv_sha256; header
           exactly CSV_COLUMNS, in order, no duplicates; every row the same
           width
rows       table, key_kind, side labels and table-specific values all legal;
           no column populated that the row's table does not use; no
           duplicate (table, key)
cross      every row's planner_version, snapshot_digest and plan_digest
           equal the envelope's
totals     plan_totals() over the reconstructed bindings equals the
           envelope's totals, field for field
digest     compute_plan_digest(reconstructed, snapshot_digest) equals the
           envelope's plan_digest
```

The digest check is last and is the one that makes the rest safe to rely
on, for a reason worth stating: it is the backstop for a *misparse*. The
reconstruction below has to make decisions the CSV does not spell out (see
the next section), and a wrong decision changes the canonical rendering --
`null` is not `""` and `7` is not `"7"` -- so the recomputed digest stops
matching. A reader that only validated fields could misread a plan
consistently and confidently; one that recomputes the digest cannot.

The empty cell is ambiguous, and that is a property of the artifact
--------------------------------------------------------------------

`csv.DictWriter(restval="")` fills every column a binding does not carry
with the empty string, and `str(None)` never reaches the file because the
writer renders `None` as `""` too. So in the written CSV:

```text
archive_hashes    inspector_version = ""   the column does not apply
archive_inspections
                  inspector_version = ""   the planned value IS None
```

Measured, not reasoned about: `_classify()` plans every inspection row with
`inspector_version=None` (`provenance_backfill_planner.py:954`, in the
`archive_inspections` loop), so the second case is not hypothetical -- it
is every inspection row in the real plan.

The two are indistinguishable *at the cell*. They are distinguishable by
**table**, which is what this reader uses: `ARTIFACT_COLUMNS[table]` says
which columns that table's bindings carry, a cell outside that set must be
empty and is refused if it is not, and a cell inside it is decoded
according to its declared type with `""` meaning `None`.

The residual limitation, stated because it is real: a plan whose text-typed
artifact column genuinely held the empty string would be read back as
`None`. No planner path emits one today -- every text artifact column is
either `None` or a non-empty literal -- and if one ever did, the recomputed
plan digest would **not** match, because `_canonical_json` renders `""` and
`null` differently. So the ambiguity fails closed rather than silently. It
is not repaired here; repairing it means changing the artifact format,
which would invalidate every plan already approved under the current one.

`bound` is recomputed, never trusted
------------------------------------

The CSV carries a `bound` column, and `PlannedBinding.bound` derives the
same fact from the sides. The reader parses the column, reconstructs the
binding, and refuses if the two disagree. A file that says `True` over
sides that are not all bound is not a file to reconcile a production
migration against, and the derived value is the one the digest was computed
from -- so accepting the column would let a plan pass whose own envelope
contradicts it.

`page_inventory` rows are read, not skipped
-------------------------------------------

They are slice 4p's and migration 015 does not apply them, but they are in
the plan and therefore in the plan digest. Dropping them at read time would
make every recomputed digest wrong. Excluding them is
`provenance_applied_projection.select_slice4_bindings()`'s job, at
projection time, where the exclusion is counted.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from comic_automation.archive.provenance_backfill_planner import (
    ARTIFACT_COLUMNS,
    CSV_COLUMNS,
    NATURAL_KEY_TABLES,
    PLAN_DIGEST_VERSION,
    PLANNER_VERSION,
    PlannedBinding,
    PlannerInvariantError,
    RECEIVING_TABLES,
    SideAttribution,
    compute_plan_digest,
    plan_totals,
)


# The artifact versions this reader understands. A plan written by a
# different planner is refused rather than read on a best effort: the
# reader's decoding rules (which column belongs to which table, what an
# empty cell means) are specific to a format version, so reading an unknown
# one would apply this format's rules to a file that never agreed to them.
SUPPORTED_PLANNER_VERSIONS: frozenset[str] = frozenset({PLANNER_VERSION})


# Envelope fields required to be present. Their absence is a refusal rather
# than a default, because every one of them participates in a check and a
# defaulted value would make that check compare something against itself.
REQUIRED_ENVELOPE_FIELDS: tuple[str, ...] = (
    "planner_version",
    "snapshot_digest",
    "plan_digest",
    "totals",
    "artifacts",
)


# Columns every row carries, whatever its table.
_COMMON_COLUMNS: tuple[str, ...] = (
    "table",
    "key_kind",
    "key",
    "archive_id",
    "bound",
    "planner_version",
    "snapshot_digest",
    "plan_digest",
)

# Attribution columns, by side label. The unlabelled form is what a
# single-sided table writes; the a/b forms are the pairwise one's.
_SIDE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "": ("source_revision_id", "provenance_basis"),
    "a": ("archive_a_id", "revision_a_id", "provenance_basis_a"),
    "b": ("archive_b_id", "revision_b_id", "provenance_basis_b"),
}


# How each column decodes. Declared per column rather than guessed from the
# value, so `"7"` cannot become an integer in one row and a string in
# another depending on what happened to be written.
_INT_COLUMNS: frozenset[str] = frozenset(
    {
        "key",
        "archive_id",
        "archive_a_id",
        "archive_b_id",
        "revision_a_id",
        "revision_b_id",
        "source_revision_id",
        "page_count",
        "location_id",
    }
)


class PlanArtifactError(RuntimeError):
    """A written plan could not be read back, or does not verify.

    One type for every refusal in this module. The distinctions that matter
    to an operator are in the message -- which check fired, on which row,
    with which values -- and not in a type they would have to catch
    separately to discover.
    """


@dataclass(frozen=True)
class LoadedPlan:
    """A plan read back from disk and verified against its own envelope.

    Carries the reconstructed bindings together with the envelope they were
    checked against, so a caller never has to re-open either file to learn
    what it just verified.
    """

    planner_version: str
    snapshot_digest: str
    plan_digest: str
    csv_sha256: str
    bindings: tuple[PlannedBinding, ...]
    envelope: Mapping[str, Any]
    json_path: Path
    csv_path: Path


def _no_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads` object hook that refuses a repeated key.

    Python's parser keeps the last occurrence and reports nothing, so
    `{"plan_digest": "<approved>", "plan_digest": "<other>"}` parses
    cleanly and yields the second. An envelope is an approval record; a
    field that can be silently overridden by appending another copy of it
    is not one.
    """
    seen: dict[str, Any] = {}

    for name, value in pairs:
        if name in seen:
            raise PlanArtifactError(
                f"the plan envelope repeats the key {name!r}. A repeated "
                "key is silently resolved to its last occurrence, so the "
                "file does not have one unambiguous meaning."
            )

        seen[name] = value

    return seen


def _load_envelope(path: Path) -> dict[str, Any]:
    """Parse and structurally check the JSON envelope."""
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError as error:
        raise PlanArtifactError(
            f"could not read the plan envelope {path}: {error}"
        ) from error
    except UnicodeDecodeError as error:
        raise PlanArtifactError(
            f"the plan envelope {path} is not valid UTF-8: {error}"
        ) from error

    try:
        envelope = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as error:
        raise PlanArtifactError(
            f"the plan envelope {path} is not valid JSON: {error}"
        ) from error

    if not isinstance(envelope, dict):
        raise PlanArtifactError(
            f"the plan envelope {path} is a "
            f"{type(envelope).__name__}, not an object"
        )

    missing = [
        name for name in REQUIRED_ENVELOPE_FIELDS if name not in envelope
    ]

    if missing:
        raise PlanArtifactError(
            f"the plan envelope {path} is missing {missing}"
        )

    for name in ("planner_version", "snapshot_digest", "plan_digest"):
        if not isinstance(envelope[name], str) or not envelope[name]:
            raise PlanArtifactError(
                f"the plan envelope {path}: {name} is "
                f"{envelope[name]!r}, expected a non-empty string"
            )

    if envelope["planner_version"] not in SUPPORTED_PLANNER_VERSIONS:
        raise PlanArtifactError(
            f"the plan envelope {path} was written by "
            f"{envelope['planner_version']!r}; this reader supports "
            f"{sorted(SUPPORTED_PLANNER_VERSIONS)}. The decoding rules are "
            "specific to a format version and must not be applied to a "
            "file written under another one."
        )

    if not isinstance(envelope["totals"], dict):
        raise PlanArtifactError(
            f"the plan envelope {path}: totals is a "
            f"{type(envelope['totals']).__name__}, not an object"
        )

    artifacts = envelope["artifacts"]

    if not isinstance(artifacts, dict):
        raise PlanArtifactError(
            f"the plan envelope {path}: artifacts is a "
            f"{type(artifacts).__name__}, not an object"
        )

    csv_sha256 = artifacts.get("csv_sha256")

    if not isinstance(csv_sha256, str) or not csv_sha256:
        # `None` is what the writer records for an envelope written without
        # a CSV beside it. That is a legitimate artifact and an illegitimate
        # input here: there is nothing attesting to any bindings file, so no
        # CSV can be shown to be the one this envelope approved.
        raise PlanArtifactError(
            f"the plan envelope {path} records artifacts.csv_sha256 = "
            f"{csv_sha256!r}. An envelope written without a bindings file "
            "attests to no CSV, so no CSV can be proven to be the one it "
            "approved."
        )

    return envelope


def _load_csv_rows(path: Path, expected_sha256: str) -> list[list[str]]:
    """Verify the CSV's raw digest, then parse it into rows.

    The digest is taken over the **raw bytes**, before any decoding, and it
    has to be: the writer computes it over `render_plan_csv(plan)` encoded
    UTF-8 and writes exactly those bytes through `os.write` in binary mode.
    Those bytes contain CRLF line terminators, because that is the `csv`
    module's default. Reading this file in text mode would translate them,
    change the digest, and reject every valid plan on Windows -- so the read
    is binary and the parse takes an explicit `newline=""` stream.
    """
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PlanArtifactError(
            f"could not read the plan bindings {path}: {error}"
        ) from error

    actual = hashlib.sha256(data).hexdigest()

    if actual != expected_sha256:
        raise PlanArtifactError(
            f"the plan bindings {path} do not match the envelope: "
            f"artifacts.csv_sha256 is {expected_sha256}, the file hashes to "
            f"{actual}. The envelope approves one specific bindings file; "
            "this is not it."
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PlanArtifactError(
            f"the plan bindings {path} are not valid UTF-8: {error}"
        ) from error

    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as error:
        raise PlanArtifactError(
            f"the plan bindings {path} are not valid CSV: {error}"
        ) from error

    if not rows:
        raise PlanArtifactError(
            f"the plan bindings {path} are empty; even a plan with no "
            "bindings carries a header row"
        )

    header = rows[0]

    # Exact equality, order included. `csv.DictReader` would collapse a
    # duplicated header into one key and silently drop a column, and a
    # reordered header would pair every value with the wrong name while
    # parsing without error.
    if header != list(CSV_COLUMNS):
        duplicated = sorted(
            {name for name in header if header.count(name) > 1}
        )
        detail = (
            f" (duplicated columns {duplicated})" if duplicated else ""
        )
        raise PlanArtifactError(
            f"the plan bindings {path} have an unexpected header{detail}. "
            f"Expected exactly {list(CSV_COLUMNS)}, in order; found "
            f"{header}."
        )

    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(CSV_COLUMNS):
            raise PlanArtifactError(
                f"the plan bindings {path} line {number}: {len(row)} "
                f"field(s), expected {len(CSV_COLUMNS)}"
            )

    return rows[1:]


def _decode_int(label: str, raw: str) -> int | None:
    """Decode an integer cell, with the empty string meaning `None`.

    `int()` is not used directly on the raw text: it accepts surrounding
    whitespace, a leading `+`, and underscores as digit separators, so
    `" 7"`, `"+7"` and `"1_0"` would all parse. None of those is anything
    the writer emits, and each would reconstruct a value whose canonical
    rendering differs from what the digest was computed over -- so they are
    refused here rather than left for the digest to catch, which gives the
    operator the offending cell instead of a whole-plan mismatch.
    """
    if raw == "":
        return None

    body = raw[1:] if raw.startswith("-") else raw

    # `isascii` as well as `isdigit`, because `isdigit()` is true for
    # characters like "²" that `int()` then refuses -- the check would
    # pass and the conversion would crash.
    if not body or not body.isascii() or not body.isdigit():
        raise PlanArtifactError(
            f"{label}: {raw!r} is not a plain integer"
        )

    return int(raw)


def _decode_bound(label: str, raw: str) -> bool:
    """Decode the `bound` cell.

    `True` / `False` capitalised, because the writer renders a Python bool
    through `str()`. Anything else -- `"true"`, `"1"`, `""` -- is refused
    rather than coerced: this column is cross-checked against the value
    derived from the sides, and a coercion that guessed would turn a
    corrupt file into a passing one.
    """
    if raw == "True":
        return True

    if raw == "False":
        return False

    raise PlanArtifactError(
        f"{label}: bound is {raw!r}, expected 'True' or 'False'"
    )


def _row_columns(table: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """Which columns a row of `table` populates, and its side labels.

    Derived from the planner's own `ARTIFACT_COLUMNS` and side shape rather
    than listed here, so a table whose artifact columns change cannot leave
    this reader silently accepting the old set.
    """
    labels = ("a", "b") if table == "near_duplicate_candidates" else ("",)

    used = set(_COMMON_COLUMNS)

    for label in labels:
        used.update(_SIDE_COLUMNS[label])

    used.update(ARTIFACT_COLUMNS[table])

    return frozenset(used), labels


def _binding_from_row(
    path: Path,
    number: int,
    cells: Mapping[str, str],
) -> tuple[PlannedBinding, bool]:
    """Reconstruct one binding, and return the file's own `bound` claim.

    The claim is returned rather than applied. The caller compares it to
    `PlannedBinding.bound`, which is derived from the sides -- see the
    module docstring.
    """
    where = f"the plan bindings {path} line {number}"

    table = cells["table"]

    if table not in RECEIVING_TABLES:
        raise PlanArtifactError(
            f"{where}: {table!r} is not a receiving table; expected one of "
            f"{list(RECEIVING_TABLES)}"
        )

    used, labels = _row_columns(table)

    # Every column this table does not use must be empty. This is the check
    # that makes the empty cell unambiguous: a populated cell outside the
    # table's own set is a row that does not describe the table it names,
    # and reading it would silently import another table's shape.
    populated_but_unused = sorted(
        name for name in CSV_COLUMNS
        if name not in used and cells[name] != ""
    )

    if populated_but_unused:
        raise PlanArtifactError(
            f"{where}: table {table} does not use "
            f"{populated_but_unused}, but the row populates them"
        )

    expected_kind = (
        "archive_id" if table in NATURAL_KEY_TABLES else "row_id"
    )

    if cells["key_kind"] != expected_kind:
        raise PlanArtifactError(
            f"{where}: key_kind is {cells['key_kind']!r}; {table} is keyed "
            f"by {expected_kind!r}"
        )

    key = _decode_int(f"{where} key", cells["key"])
    archive_id = _decode_int(f"{where} archive_id", cells["archive_id"])

    for name, value in (("key", key), ("archive_id", archive_id)):
        if value is None:
            raise PlanArtifactError(f"{where}: {name} is empty")

    sides: list[SideAttribution] = []

    for label in labels:
        if label:
            archive_column, revision_column, basis_column = (
                _SIDE_COLUMNS[label]
            )
            side_archive = _decode_int(
                f"{where} {archive_column}", cells[archive_column]
            )

            if side_archive is None:
                raise PlanArtifactError(
                    f"{where}: {archive_column} is empty, but "
                    f"{table} binds side {label!r} independently"
                )
        else:
            revision_column, basis_column = _SIDE_COLUMNS[label]
            side_archive = archive_id

        basis = cells[basis_column]

        if basis == "":
            raise PlanArtifactError(
                f"{where}: {basis_column} is empty. Every side carries a "
                "basis -- an unresolved side carries an unresolved one, "
                "never none at all."
            )

        sides.append(
            SideAttribution(
                label=label,
                archive_id=side_archive,
                source_revision_id=_decode_int(
                    f"{where} {revision_column}", cells[revision_column]
                ),
                provenance_basis=basis,
            )
        )

    values: dict[str, Any] = {}

    for name in ARTIFACT_COLUMNS[table]:
        raw = cells[name]

        if name in _INT_COLUMNS:
            values[name] = _decode_int(f"{where} {name}", raw)
        else:
            # The empty string decodes to None. This is the ambiguity the
            # module docstring names: no planner path writes an empty text
            # value today, and one that did would fail the plan-digest
            # check rather than be misread in silence.
            values[name] = raw if raw != "" else None

    claimed_bound = _decode_bound(where, cells["bound"])

    try:
        binding = PlannedBinding(
            table=table,
            key=key,
            key_kind=cells["key_kind"],
            archive_id=archive_id,
            sides=tuple(sides),
            values=values,
        )
    except PlannerInvariantError as error:
        # The planner's own invariants, applied to a file rather than to a
        # freshly classified row. Re-raised as a plan-artifact refusal so a
        # caller reading a file catches one type, with the line named --
        # a PlannerInvariantError escaping here would read as a classifier
        # defect rather than a bad artifact.
        raise PlanArtifactError(f"{where}: {error}") from error

    return binding, claimed_bound


def _check_row_consistency(
    path: Path,
    number: int,
    cells: Mapping[str, str],
    envelope: Mapping[str, Any],
) -> None:
    """Every row repeats the envelope's identity; every repetition must agree.

    The writer stamps `planner_version`, `snapshot_digest` and
    `plan_digest` onto every CSV row. Rows spliced in from another plan
    parse perfectly and would otherwise be reconciled against this
    envelope, so the repetition is checked rather than ignored.
    """
    where = f"the plan bindings {path} line {number}"

    for column in ("planner_version", "snapshot_digest", "plan_digest"):
        if cells[column] != envelope[column]:
            raise PlanArtifactError(
                f"{where}: {column} is {cells[column]!r}, but the envelope "
                f"records {envelope[column]!r}"
            )


def _check_totals(
    envelope: Mapping[str, Any],
    bindings: Sequence[PlannedBinding],
    json_path: Path,
) -> None:
    """Recount the reconstructed bindings and compare to the envelope.

    `plan_totals()` is the planner's own reconciliation, run again over
    what was read back. Comparing its output to the envelope's catches a
    plan whose rows were removed or duplicated in ways that still parse --
    and it is the planner's function rather than a recount written here,
    so the two sides cannot disagree about what a total means.
    """
    try:
        recounted = plan_totals(bindings)
    except PlannerInvariantError as error:
        raise PlanArtifactError(
            f"the plan bindings do not reconcile: {error}"
        ) from error

    recorded = envelope["totals"]

    # Compared through their canonical JSON rather than with `==` on the
    # dicts: the envelope's copy has been through JSON, so its integer keys
    # and tuple values are already normalised, and comparing the two
    # renderings avoids a mismatch that is really a round-trip artefact.
    if json.dumps(recounted, sort_keys=True) != json.dumps(
        recorded, sort_keys=True
    ):
        raise PlanArtifactError(
            f"the plan envelope {json_path} records totals that do not "
            f"match the bindings beside it.\n  envelope:  "
            f"{json.dumps(recorded, sort_keys=True)}\n  recounted: "
            f"{json.dumps(recounted, sort_keys=True)}"
        )


def read_plan_artifacts(
    json_path: str | Path,
    csv_path: str | Path,
) -> LoadedPlan:
    """Read an approved plan pair back, verified end to end.

    Returns a `LoadedPlan` only when every check in the module docstring
    passes; raises `PlanArtifactError` naming the first that does not.

    Reads only. Opens no database, writes nothing, and takes no view on
    whether the plan *should* be applied -- `BackfillPlan.gate_failures`
    answers that at plan time, and the executor's own preconditions
    (design section 12.1) answer it at apply time. This call answers one
    question: are these two files the plan they claim to be?
    """
    json_path = Path(json_path)
    csv_path = Path(csv_path)

    envelope = _load_envelope(json_path)
    rows = _load_csv_rows(csv_path, envelope["artifacts"]["csv_sha256"])

    bindings: list[PlannedBinding] = []
    seen: dict[tuple[str, int], int] = {}

    for number, row in enumerate(rows, start=2):
        cells = dict(zip(CSV_COLUMNS, row))

        _check_row_consistency(csv_path, number, cells, envelope)
        binding, claimed_bound = _binding_from_row(csv_path, number, cells)

        if binding.bound != claimed_bound:
            raise PlanArtifactError(
                f"the plan bindings {csv_path} line {number}: the row "
                f"claims bound={claimed_bound}, but its sides derive "
                f"bound={binding.bound}. The derived value is what the plan "
                "digest was computed over, so the file contradicts its own "
                "envelope."
            )

        identity = (binding.table, binding.key)

        if identity in seen:
            raise PlanArtifactError(
                f"the plan bindings {csv_path} line {number}: "
                f"{binding.table} row {binding.key} was already read at "
                f"line {seen[identity]}. Migration 015 stages the plan in a "
                "table keyed by (table_name, row_id), so a duplicate could "
                "not be staged and one of the two would be lost."
            )

        seen[identity] = number
        bindings.append(binding)

    _check_totals(envelope, bindings, json_path)

    # Last, and the backstop for every decoding decision above. The
    # bindings are sorted into the planner's own order first: the digest is
    # computed over a sequence, and `build_plan()` sorts by (table, key)
    # before computing it, so a correctly-ordered file and a shuffled one
    # must reduce to the same plan.
    ordered = sorted(bindings, key=lambda b: (b.table, b.key))
    recomputed = compute_plan_digest(ordered, envelope["snapshot_digest"])

    if recomputed != envelope["plan_digest"]:
        raise PlanArtifactError(
            f"the plan does not verify: {PLAN_DIGEST_VERSION} over the "
            f"bindings in {csv_path} is {recomputed}, but the envelope "
            f"{json_path} records {envelope['plan_digest']}. The approved "
            "plan and the plan on disk are not the same plan."
        )

    return LoadedPlan(
        planner_version=envelope["planner_version"],
        snapshot_digest=envelope["snapshot_digest"],
        plan_digest=envelope["plan_digest"],
        csv_sha256=envelope["artifacts"]["csv_sha256"],
        bindings=tuple(ordered),
        envelope=envelope,
        json_path=json_path,
        csv_path=csv_path,
    )
