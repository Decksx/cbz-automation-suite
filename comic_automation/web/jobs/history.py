"""Ledger-first archival of inactive terminal jobs inside the pinned home.

No history row/event is deleted. Immutable batches are published and verified,
then pointers/events commit together, then hot copies may be removed. Restart
cleanup requires that committed pointer and a fresh sealed/source verification.
The maintenance API is explicit; M3d supplies idle scheduling before M4 wiring.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import sys

from ...database import dal
from .. import state_dir
from . import archives
from .store import JobRefused, LockBusy, _ALPHABET, _digest, validate_job_id

DEFAULT_HOT_JOBS = 200
DEFAULT_HOT_AGE_MS = 7 * 24 * 60 * 60 * 1000


class JobHistory:
    """Archive maintenance and verified hot/cold command reads for one store."""
    def __init__(self, store):
        self.store = store

    @property
    def directory(self) -> Path:
        """The only archive root, inside the physically pinned private home."""
        return self.store.path / "archive"

    def initialize(self) -> None:
        """Explicitly prepare the archive directory; archive() never creates it."""
        with self.store._home():
            self.directory.mkdir(exist_ok=True)
            state_dir.check_private(self.store.path, [self.directory])

    def _batch_path(self, name: str) -> Path:
        if not isinstance(name, str) or not name.startswith("jobs-") or not name.endswith(".zip"):
            raise archives.ArchiveRefused("Invalid archive pointer")
        validate_job_id(name[5:-4])
        path = self.directory / name
        state_dir.check_private(self.store.path, [self.directory, path])
        return path

    def _source(self, row) -> archives.ArchiveJob:
        root = self.store.path / "jobs" / validate_job_id(row["job_id"])
        self.store._check_job(row["job_id"])
        if os.path.lexists(root):
            directories = []
            files = archives._files(root, directories)
            state_dir.check_private(self.store.path, [*directories, *files])
        return archives.ArchiveJob(row["job_id"], row["state"], row["command_digest"], root)

    def _rows(self, db, name, digest):
        return db.execute("""SELECT j.*,a.nonce AS archive_nonce,a.command_digest AS archive_command,
            a.terminal_state AS archive_state FROM job_archives a JOIN jobs j ON j.job_id=a.job_id
            WHERE a.batch_file=? AND a.batch_sha256=? ORDER BY j.admission_seq""", (name, digest)).fetchall()

    @staticmethod
    def _matching(rows) -> None:
        if not rows or any(row["state"] not in archives.TERMINAL or row["state"] != row["archive_state"]
                or row["nonce"] != row["archive_nonce"] or row["command_digest"] != row["archive_command"] for row in rows):
            raise archives.ArchiveRefused("Archive pointer no longer matches the terminal ledger")

    def _release_handles(self, handles, primary) -> None:
        failures = []
        for job_id, handle in reversed(handles):
            if not handle.closed:
                try:
                    self.store._close_runner_lock(job_id, handle)
                except BaseException as exc:
                    failures.append(exc)
        if failures:
            if primary is not None:
                for failure in failures:
                    primary.add_note(f"History runner-lock cleanup also failed: {failure}")
            else:
                raise failures[0]

    def archive(self, *, keep: int = DEFAULT_HOT_JOBS, age_ms: int = DEFAULT_HOT_AGE_MS,
                batch_size: int = archives.MAX_JOBS) -> str | None:
        """Archive a bounded terminal batch by age OR count; retain every audit row.

        Call repeatedly during idle maintenance to reach the hot-set policy.
        A held runner is skipped. A missing/private-root failure, changed seal,
        clock contradiction or persistence failure preserves the hot evidence.
        """
        if (type(keep) is not int or not 0 <= keep <= 10000 or type(age_ms) is not int
                or not 0 <= age_ms < 2**63 or type(batch_size) is not int or not 1 <= batch_size <= archives.MAX_JOBS):
            raise JobRefused("INVALID_HISTORY_POLICY")
        handles = []
        with self.store._home():
            state_dir.check_private(self.store.path, [self.directory])
            if not self.directory.is_dir():
                raise archives.ArchiveRefused("Archive directory is missing; initialize it explicitly")
            with self.store._write() as db:
                now = self.store._time(db)
                eligible = "j.state IN ('succeeded','failed','stopped','lost','admission_expired','refused') AND a.job_id IS NULL"
                threshold = db.execute("SELECT j.admission_seq FROM jobs j LEFT JOIN job_archives a ON a.job_id=j.job_id "
                    f"WHERE {eligible} ORDER BY j.admission_seq DESC LIMIT 1 OFFSET ?", (keep,)).fetchone()
                rows = db.execute("SELECT j.* FROM jobs j LEFT JOIN job_archives a ON a.job_id=j.job_id "
                    f"WHERE {eligible} AND (j.updated_ms<=? OR j.admission_seq<=?) ORDER BY j.admission_seq LIMIT ?",
                    (now - age_ms, threshold[0] if threshold else -1, batch_size)).fetchall()
            try:
                selected, sources, selected_bytes = [], {}, 0
                for row in rows:
                    self.store._check_job(row["job_id"])
                    try:
                        handle = self.store._lock(Path("jobs") / f"{row['job_id']}.runner.lock", retry=False)
                    except LockBusy:
                        continue
                    handles.append((row["job_id"], handle))
                    source = self._source(row)
                    if not source.root.is_dir():
                        raise archives.ArchiveRefused("Terminal hot evidence is missing before archival")
                    size = sum(path.stat().st_size for path in archives._files(source.root))
                    if size > archives.MAX_BATCH_BYTES:
                        raise archives.ArchiveRefused("One job exceeds the archive byte budget")
                    if selected and selected_bytes + size > archives.MAX_BATCH_BYTES:
                        break  # publish the oldest fitting prefix; next call continues
                    selected_bytes += size
                    selected.append(row)
                    sources[row["job_id"]] = source
                if not selected:
                    return None
                value = (now << 80) | secrets.randbits(80)
                batch_id = "".join(_ALPHABET[(value >> (5 * n)) & 31] for n in reversed(range(26)))
                receipt = archives.write_batch(self.directory, batch_id, list(sources.values()), now)
                state_dir.check_private(self.store.path, [self.directory / receipt.file_name])
                with self.store._write() as db:
                    committed_ms = self.store._time(db)
                    for old in selected:
                        current = db.execute("SELECT * FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()
                        if (current is None or current["state"] not in archives.TERMINAL
                                or any(current[key] != old[key] for key in ("nonce", "state", "state_version", "command_digest"))):
                            raise archives.ArchiveRefused("Terminal row changed before archive commit")
                        db.execute("INSERT INTO job_archives VALUES(?,?,?,?,?,?,?)", (old["job_id"], receipt.file_name,
                            receipt.sha256, committed_ms, old["nonce"], old["command_digest"], old["state"]))
                        record = next(r for r in receipt.manifest["jobs"] if r["job_id"] == old["job_id"])
                        for member in record["members"]:
                            db.execute("INSERT INTO job_archive_members VALUES(?,?,?,?)", (old["job_id"],
                                member["path"].split("/", 1)[1], member["size"], member["sha256"]))
                        self.store._event(db, current, "archived", "retention", committed_ms,
                                          {"batch_file": receipt.file_name, "batch_sha256": receipt.sha256})
                # Pointer is durable. The same routine handles restart cleanup.
                self._cleanup_batch(receipt.file_name, receipt.sha256, selected, sources)
                return receipt.file_name
            finally:
                self._release_handles(handles, sys.exc_info()[1])

    def _cleanup_batch(self, name, digest, rows, sources) -> list[str]:
        """Called under admission and every selected job lock, after pointer proof."""
        with dal.connection_scope(self.store.database, readonly=True) as db:
            committed = self._rows(db, name, digest)
        self._matching(committed)
        if {row["job_id"] for row in committed} != {row["job_id"] for row in rows}:
            raise archives.ArchiveRefused("Incomplete committed archive batch")
        path = self._batch_path(name)
        archives.verify_batch(path, digest, jobs=sources, check_sources=True, allow_missing_sources=True)
        removed = []
        for row in committed:
            source = sources[row["job_id"]]
            if not os.path.lexists(source.root):
                continue
            self._source(row)  # fresh private/reparse checks before recursive removal
            resolved = source.root.resolve(strict=True)
            intended = (self.store.path / "jobs").resolve(strict=True)
            if resolved.parent != intended or resolved.name != row["job_id"]:
                raise archives.ArchiveRefused("Hot cleanup path escaped the pinned jobs directory")
            shutil.rmtree(source.root)
            removed.append(row["job_id"])
        with self.store._write() as db:
            now = self.store._time(db)
            for row in committed:
                prior = db.execute("SELECT 1 FROM job_events WHERE job_id=? AND event='archive_cleanup' LIMIT 1",
                                   (row["job_id"],)).fetchone()
                if prior is None:
                    self.store._event(db, row, "archive_cleanup", "retention", now,
                                      {"batch_file": name, "batch_sha256": digest})
        return removed

    def cleanup(self, *, batch_limit: int = 10) -> list[str]:
        """Resume verified post-commit cleanup; unreferenced batches are untouched."""
        if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
            raise JobRefused("INVALID_HISTORY_BATCH_LIMIT")
        removed = []
        with self.store._home(), dal.connection_scope(self.store.database, readonly=True) as db:
            batches = db.execute("SELECT DISTINCT a.batch_file,a.batch_sha256 FROM job_archives a "
                "WHERE NOT EXISTS(SELECT 1 FROM job_events e WHERE e.job_id=a.job_id AND e.event='archive_cleanup') "
                "ORDER BY a.archived_ms LIMIT ?",
                                 (batch_limit,)).fetchall()
        for batch in batches:
            handles = []
            with self.store._home():
                with dal.connection_scope(self.store.database, readonly=True) as db:
                    rows = self._rows(db, *batch)
                self._matching(rows)
                try:
                    for row in rows:
                        handles.append((row["job_id"], self.store._lock(Path("jobs") / f"{row['job_id']}.runner.lock", retry=False)))
                    sources = {row["job_id"]: self._source(row) for row in rows}
                    removed.extend(self._cleanup_batch(*batch, rows, sources))
                except LockBusy:
                    continue
                finally:
                    self._release_handles(handles, sys.exc_info()[1])
        return removed

    def read_command(self, job_id: str) -> bytes:
        """Read verified hot evidence or its committed cold copy, never an orphan."""
        validate_job_id(job_id)
        with self.store._home(job_id=job_id), dal.connection_scope(self.store.database, readonly=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            pointer = db.execute("SELECT * FROM job_archives WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobRefused("JOB_NOT_FOUND")
            cold = None
            if pointer is not None:
                if (pointer["nonce"] != row["nonce"] or pointer["command_digest"] != row["command_digest"]
                        or pointer["terminal_state"] != row["state"]):
                    raise archives.ArchiveRefused("Archive pointer changed")
                cold = archives.read_command(self._batch_path(pointer["batch_file"]), pointer["batch_sha256"],
                                              job_id, row["command_digest"])
            hot = self.store.path / "jobs" / job_id / "command.json"
            if os.path.lexists(hot):
                with open(hot, "rb") as stream:
                    data = stream.read(archives.MAX_COMMAND_BYTES + 1)
                if len(data) > archives.MAX_COMMAND_BYTES or _digest(data) != row["command_digest"]:
                    raise archives.ArchiveRefused("Hot command digest mismatch")
                if cold is not None and cold != data:
                    raise archives.ArchiveRefused("Hot and cold command evidence disagree")
                return data
            if cold is None:
                raise archives.ArchiveRefused("No verified hot or archived command")
            return cold
