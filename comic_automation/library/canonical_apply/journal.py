"""The append-only, fsynced attempt journal (§5.4).

One file per attempt, ``<library-root>\\.cbz-canonical-naming\\attempts\\<attempt>.jsonl``.
Every record is one JSON object on one ``\\n``-terminated line, carrying the
journal contract, the attempt id, a sequence number and (for step records)
the action id and step index. Each record is flushed and ``fsync``-ed
before the caller proceeds, so a record that a later filesystem step relies
on is durable before that step runs.

Record kinds:

``attempt``
    Always record 0: mode (``apply`` or ``undo``), the approved plan
    digest, the library root, and for an undo the forward attempt and the
    ordered action ids it reverses.
``intent`` / ``done`` / ``confirmed``
    The step protocol's three receipts. ``intent`` carries the step's
    source, destination, kind and the action's strong identity; ``done``
    is the ownership receipt for the destination; ``confirmed`` follows
    the database commit.
``note``
    Informational only (a blocked unit, a stopped run, a finished run).
    Never consulted for ownership or recovery.

The reader is strict: a torn final line, an unparseable line, a foreign
contract or attempt id, or a gap in the sequence numbers refuses the whole
journal (`JournalCorruptError`). A journal that cannot be read in full can
prove nothing, and guessing which prefix to trust is exactly the inference
the receipts exist to avoid.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

JOURNAL_CONTRACT = "canonical-naming-journal-v1"
JOURNAL_DIRNAME = ".cbz-canonical-naming"
ATTEMPTS_DIRNAME = "attempts"

KIND_ATTEMPT = "attempt"
KIND_INTENT = "intent"
KIND_DONE = "done"
KIND_CONFIRMED = "confirmed"
KIND_NOTE = "note"

STEP_KINDS = (KIND_INTENT, KIND_DONE, KIND_CONFIRMED)
RECORD_KINDS = frozenset({KIND_ATTEMPT, KIND_NOTE, *STEP_KINDS})

MODE_APPLY = "apply"
MODE_UNDO = "undo"


class JournalError(RuntimeError):
    """Base class for journal refusals."""


class JournalCorruptError(JournalError):
    """The journal cannot be read in full and is refused."""


def attempts_dir(library_root: str | Path) -> Path:
    return Path(library_root) / JOURNAL_DIRNAME / ATTEMPTS_DIRNAME


def journal_path(library_root: str | Path, attempt_id: str) -> Path:
    return attempts_dir(library_root) / f"{attempt_id}.jsonl"


@dataclass(frozen=True)
class JournalRecord:
    """One parsed journal line."""

    seq: int
    kind: str
    data: Mapping[str, Any]

    @property
    def action_id(self) -> str | None:
        return self.data.get("action_id")

    @property
    def step(self) -> int | None:
        return self.data.get("step")


class JournalWriter:
    """Appends fsynced records to one attempt's journal.

    `create` makes the file exclusively (``xb``), so two attempts can never
    share a journal, and writes record 0. `open_existing` reopens a journal
    for a resumed attempt or a recovery, continuing its sequence.
    """

    def __init__(self, path: Path, attempt_id: str, next_seq: int) -> None:
        self.path = path
        self.attempt_id = attempt_id
        self._next_seq = next_seq
        self._handle = open(path, "ab")

    @classmethod
    def create(
        cls, library_root: str | Path, attempt_id: str, header: Mapping[str, Any]
    ) -> "JournalWriter":
        path = journal_path(library_root, attempt_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation: an existing journal for this id is refused, not
        # appended to.
        with open(path, "xb"):
            pass
        writer = cls(path, attempt_id, 0)
        writer.append(KIND_ATTEMPT, header)
        return writer

    @classmethod
    def open_existing(
        cls, library_root: str | Path, attempt_id: str
    ) -> "JournalWriter":
        records = read_journal(library_root, attempt_id)
        return cls(journal_path(library_root, attempt_id), attempt_id, len(records))

    def append(self, kind: str, data: Mapping[str, Any]) -> JournalRecord:
        """Write one record and fsync it before returning."""
        if kind not in RECORD_KINDS:
            raise JournalError(f"unknown journal record kind {kind!r}")
        record = {
            "contract": JOURNAL_CONTRACT,
            "attempt_id": self.attempt_id,
            "seq": self._next_seq,
            "kind": kind,
            **dict(data),
        }
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        self._handle.write(line.encode("utf-8"))
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._next_seq += 1
        return JournalRecord(record["seq"], kind, record)

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "JournalWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_journal(library_root: str | Path, attempt_id: str) -> tuple[JournalRecord, ...]:
    """Every record of ``attempt_id``'s journal, or `JournalCorruptError`."""
    path = journal_path(library_root, attempt_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise JournalCorruptError(f"no journal for attempt {attempt_id}: {path}") from exc
    if not raw:
        raise JournalCorruptError(f"journal {path} is empty: no attempt record")
    if not raw.endswith(b"\n"):
        # A torn final record. Its write was interrupted, so whatever it was
        # about to receipt cannot be shown to have happened or not.
        raise JournalCorruptError(
            f"journal {path} ends in a torn record (no trailing newline); "
            "refusing rather than guessing which prefix is trustworthy"
        )
    records: list[JournalRecord] = []
    for number, line in enumerate(raw.split(b"\n")[:-1]):
        try:
            data = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JournalCorruptError(f"journal {path} line {number} does not parse: {exc}") from exc
        if not isinstance(data, dict):
            raise JournalCorruptError(f"journal {path} line {number} is not an object")
        if data.get("contract") != JOURNAL_CONTRACT:
            raise JournalCorruptError(
                f"journal {path} line {number} has contract {data.get('contract')!r}"
            )
        if data.get("attempt_id") != attempt_id:
            raise JournalCorruptError(
                f"journal {path} line {number} belongs to attempt "
                f"{data.get('attempt_id')!r}, not {attempt_id!r}"
            )
        if data.get("seq") != number:
            raise JournalCorruptError(
                f"journal {path} line {number} has seq {data.get('seq')!r}: "
                "sequence numbers must be contiguous from 0"
            )
        kind = data.get("kind")
        if kind not in RECORD_KINDS:
            raise JournalCorruptError(f"journal {path} line {number} has kind {kind!r}")
        if (number == 0) != (kind == KIND_ATTEMPT):
            raise JournalCorruptError(
                f"journal {path}: the attempt record must be record 0 and only record 0"
            )
        records.append(JournalRecord(number, kind, data))
    return tuple(records)


def list_attempts(library_root: str | Path) -> tuple[str, ...]:
    """Attempt ids that have a journal under ``library_root``, sorted."""
    directory = attempts_dir(library_root)
    if not directory.is_dir():
        return ()
    return tuple(sorted(p.stem for p in directory.glob("*.jsonl")))


def step_records(
    records: tuple[JournalRecord, ...], action_id: str
) -> Iterator[JournalRecord]:
    """The intent/done/confirmed records of one action, in journal order."""
    for record in records:
        if record.kind in STEP_KINDS and record.action_id == action_id:
            yield record
