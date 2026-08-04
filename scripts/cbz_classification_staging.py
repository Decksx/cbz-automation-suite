"""Review-case mechanics for archives that classification could not resolve.

It computes case identity, builds manifests, chooses a transfer method,
plans, revalidates a source against an inventory, and executes the transfer.
No watcher call site consumes any of it yet, and no routing mode selects it:
legacy v1 remains authoritative. Separating the filesystem safety protocol
from concurrent watcher control flow keeps both reviewable.

`execute_transfer` is the only thing here that changes the filesystem.
Everything above it is read-only.

A review case is one unresolved arrival, staged whole under a deterministic
identity so that retrying an unchanged source finds the same case instead of
creating a duplicate.

    X:\\_classification_review\\
        .partial\\<case-id>\\             transfer in progress, not authoritative
        pending\\<case-id>\\
            payload\\<series-directory>\\ the archives themselves
        manifests\\<case-id>.json
        rejected\\

Two digests, because they answer different questions:

    payload_inventory_digest  what these bytes are
    case_identity_digest      which case this is

The first allows duplicate-content analysis across separately arrived cases.
The second binds the payload to a series and a source path, so two arrivals of
identical bytes from different sources stay distinct cases.

Neither digest includes timestamps, file IDs, enumeration order, absolute
staged paths, the routing decision, or the config digest. Those are mutable
metadata or consequences of the case, not its payload identity -- and a
retry after a routing-config change must find the same physical case and
record a recomputed decision, not duplicate the payload because policy moved.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]

# Versioned, and part of the hashed material: a change to how identity is
# computed must not silently collide with cases minted under the old scheme.
CASE_CONTRACT = "classification-case-v1"

PARTIAL_DIR = ".partial"
PENDING_DIR = "pending"
MANIFESTS_DIR = "manifests"
REJECTED_DIR = "rejected"
PAYLOAD_DIR = "payload"

# Same-volume moves are a rename: atomic, and incapable of half-finishing.
# Across volumes the bytes are copied and verified before the source is given
# up, so the source stays authoritative until the copy is proven.
METHOD_RENAME = "rename"
METHOD_COPY_VERIFY = "copy_verify"

# Case lifecycle. Only `pending_review` and later mean the staged copy is
# authoritative; before that the original source still is.
STATE_PLANNED = "planned"
STATE_COPIED_VERIFIED = "copied_verified"
STATE_PENDING_REVIEW = "pending_review"
STATE_PROMOTED = "promoted"
STATE_REJECTED = "rejected"
STATE_ROLLED_BACK = "rolled_back"

_HASH_CHUNK = 1 << 20


class StagingError(RuntimeError):
    """Raised when a staging invariant cannot be satisfied."""


class PayloadChangedError(StagingError):
    """The payload moved under us while its inventory was being built.

    Distinct from other staging errors because it is transient: the caller
    should retry once the source has settled, not treat the case as broken.
    """


class StagedPayloadMismatchError(StagingError):
    """The staged copy does not match the manifest it was copied against.

    The copy is wrong, not the source. The source remains authoritative and
    untouched, and `.partial` is left in place as recoverable debris.
    """


class PartialRecoveryRequiredError(StagingError):
    """`.partial` may hold the only copy, and only recovery may decide.

    On the copy path the source survives the whole transfer, so `.partial` is
    genuinely disposable debris. On the rename path it is not: between moving
    the source in and publishing the case there is a window where the source
    is gone and `.partial` is the only copy that exists. Deleting it to
    restart -- which is safe on the copy path -- destroys the payload.

    So this is refused rather than cleaned, and refused without inspecting
    whether the payload is complete. Deciding that is recovery's job.
    """


class InvalidExecutionPlanError(StagingError):
    """The plan does not describe the payload it was measured against.

    StagingPlan is frozen but its CaseManifest is not, so the values that
    decide which directories are created, written, and deleted can be changed
    after planning. Checked before any of them is used.
    """


class CaseCollisionError(StagingError):
    """Something already occupies this case, and it is not ours to overwrite.

    Refused rather than merged. Merging two payloads under one case id would
    produce a case whose contents no manifest describes, and overwriting
    would destroy a decision an operator may already have made.
    """


class SourceChangedError(StagingError):
    """The source settled, but at different content than the case was minted from.

    Deliberately not a PayloadChangedError. That one means the tree was
    moving *while* it was read, and the same case may succeed once it stops.
    This one means the tree stopped moving somewhere else: the source is
    internally consistent and simply is not the payload this case describes.

    The response differs accordingly. Case identity is derived from the
    inventory, so by the identity contract a changed source is a *different
    case*. Abandon the transfer, leave the source authoritative, delete
    nothing, and let a retry mint a new case id rather than resuming this one.
    """


# ------------------------------------------------------------- inventory

@dataclass(frozen=True)
class FileEntry:
    """One payload file, identified by content rather than by metadata."""

    relative_path: str
    size_bytes: int
    sha256: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(stat_result: os.stat_result) -> tuple:
    """Identity and mutable state of one file, as the OS reports it.

    st_ino and st_dev are populated on Windows for Python 3.x and identify
    the file itself, so a same-size replacement is still detected.
    """
    return (stat_result.st_ino, stat_result.st_dev,
            stat_result.st_size, stat_result.st_mtime_ns)


def _hash_stable_file(path: Path) -> tuple[int, str, tuple]:
    """Hash one file, proving it did not change while being read.

    Size, identity, and mtime are compared either side of the read, and the
    byte count is compared with the size the file ended at. Reading a file
    that is growing would otherwise pair an old size with a new digest and
    mint a case ID that no stable source tree ever had.

    Known limit: an in-place rewrite that keeps the same length, the same
    file identity, and lands inside the filesystem's timestamp resolution is
    not detectable this way. Nothing short of re-reading the bytes would
    catch it. The watcher's settle delay is what makes that unlikely; this
    check is what makes the ordinary cases loud rather than silently wrong.

    This is the same window as the tmp_replace_same_size scenario in
    docs/archive_io_resource_audit.md, Finding 2 -- a size+mtime guard blind
    to a same-size replacement inside the volume's mtime quantum. Measured
    there at 11/16 silent clobbers on exFAT and 0/16 on NTFS, and `X:` was
    reformatted to NTFS on 2026-08-02 to remove the enabler.

    So the window's size is a property of the volume, not of this code.
    Moving staging to a volume with coarser timestamps would widen it again
    with nothing here changing, and nothing would report that it had. Treat
    a filesystem change under the staging root as a change to this check.
    """
    before = os.stat(path)
    digest = hashlib.sha256()
    read = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
            read += len(chunk)
    after = os.stat(path)

    if _fingerprint(before) != _fingerprint(after):
        raise PayloadChangedError(
            f"{path} changed while it was being hashed; "
            "retry after the payload settles"
        )
    if read != after.st_size:
        raise PayloadChangedError(
            f"{path} reported {after.st_size} bytes but {read} were read; "
            "retry after the payload settles"
        )
    return after.st_size, digest.hexdigest(), _fingerprint(after)


def _relative_posix(path: Path, root: Path) -> str:
    """A path key that does not vary with the host's separator."""
    return path.relative_to(root).as_posix()


@dataclass(frozen=True)
class PayloadInventory:
    """Every file under one payload root, in a stable, canonical order."""

    entries: tuple[FileEntry, ...]

    @property
    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

    @property
    def file_count(self) -> int:
        return len(self.entries)

    def canonical_bytes(self) -> bytes:
        """The hashed representation: path, size, and content digest only.

        NUL-delimited and newline-terminated per record so no combination of
        path and size can be confused with a different one -- a payload with
        a file literally named "a\\0100" must not collide with two files.
        """
        parts = []
        for entry in self.entries:
            parts.append(
                f"{entry.relative_path}\0{entry.size_bytes}\0{entry.sha256}\n"
                .encode("utf-8")
            )
        return b"".join(parts)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            CASE_CONTRACT.encode("utf-8") + b"\0payload\0"
            + self.canonical_bytes()
        ).hexdigest()

    def as_json(self) -> list[dict]:
        return [asdict(e) for e in self.entries]


def _members(root: Path) -> list[Path]:
    """Every file under *root*, ordered by relative path.

    Sorted by the relative path rather than by enumeration order: two hosts
    walking the same tree must produce the same inventory, and os.scandir
    order is not a promise.
    """
    return sorted((p for p in root.rglob("*") if p.is_file()),
                  key=lambda p: _relative_posix(p, root))


def build_inventory(root: Path) -> PayloadInventory:
    """Hash every file under *root*, against a payload proven not to move.

    Case identity is derived from this, so an inventory taken while the
    source is still arriving would mint an ID for a tree that never existed.
    Directory membership is enumerated before and after hashing, each file is
    checked either side of its own read, and any addition, removal, rename,
    replacement, or modification rejects the whole inventory.

    This detects a payload that moved; it does not prevent one from moving.
    The watcher's settle delay is what makes a stable read likely, and this
    is what makes an unstable one loud instead of silently wrong.
    """
    if not root.is_dir():
        raise StagingError(f"payload root is not a directory: {root}")

    first_pass = _members(root)
    before = {_relative_posix(p, root) for p in first_pass}

    entries = []
    fingerprints: dict[str, tuple] = {}
    for path in first_pass:
        relative = _relative_posix(path, root)
        try:
            size, digest, fingerprint = _hash_stable_file(path)
        except FileNotFoundError as exc:
            raise PayloadChangedError(
                f"{relative} disappeared while the inventory was being built; "
                "retry after the payload settles"
            ) from exc
        entries.append(FileEntry(relative, size, digest))
        fingerprints[relative] = fingerprint

    final_members = _members(root)
    after = {_relative_posix(p, root) for p in final_members}
    if after != before:
        added, removed = sorted(after - before), sorted(before - after)
        raise PayloadChangedError(
            "payload membership changed while the inventory was being built "
            f"(added {added}, removed {removed}); retry after it settles"
        )

    # Per-file checks only prove each file held still during its own read. A
    # file hashed early can still be rewritten while a later one is being
    # hashed, leaving an inventory that no longer describes the tree it
    # claims to. Re-fingerprint every member against the state captured
    # immediately after its own hash.
    for path in final_members:
        relative = _relative_posix(path, root)
        try:
            current = _fingerprint(os.stat(path))
        except FileNotFoundError as exc:
            raise PayloadChangedError(
                f"{relative} disappeared after it was hashed; "
                "retry after the payload settles"
            ) from exc
        if current != fingerprints[relative]:
            raise PayloadChangedError(
                f"{relative} changed after it was hashed; "
                "retry after the payload settles"
            )
    return PayloadInventory(tuple(entries))


# ------------------------------------------------------------- revalidation

def _summarise(label: str, paths: list[str], limit: int = 5) -> str:
    """One bounded clause of a difference report.

    Capped because a source can hold thousands of files and this text goes
    into an exception an operator reads. The count is always exact; only the
    listing is truncated.
    """
    if not paths:
        return ""
    shown = ", ".join(repr(p) for p in paths[:limit])
    if len(paths) > limit:
        shown += f", and {len(paths) - limit} more"
    return f"{label} {len(paths)} ({shown})"


def inventory_difference(expected: PayloadInventory,
                         current: PayloadInventory) -> str:
    """Describe how *current* differs from *expected*, for an operator.

    Compares exactly what the digest commits to -- path, size, and content
    hash -- so this report cannot come back empty while the digests disagree.
    Comparing content alone would: a manifest may carry a size_bytes that
    does not match its own sha256, since from_json recomputes the manifest
    against itself and never against real files. Such a manifest is
    internally consistent, is refused here correctly, and would otherwise be
    refused with a message saying nothing had changed.
    """
    before = {e.relative_path: e for e in expected.entries}
    after = {e.relative_path: e for e in current.entries}
    clauses = [
        _summarise("added", sorted(set(after) - set(before))),
        _summarise("removed", sorted(set(before) - set(after))),
        _summarise("modified", sorted(
            p for p in set(before) & set(after)
            if (before[p].size_bytes, before[p].sha256)
            != (after[p].size_bytes, after[p].sha256)
        )),
    ]
    return "; ".join(c for c in clauses if c) or "no per-file difference found"


def revalidate_source(staging_source_path: Path,
                      expected: PayloadInventory) -> PayloadInventory:
    """Prove the source still matches *expected*. Step 4 of the transfer.

    Verifying the staged copy proves only that the copy matches the inventory
    taken before it started. It proves nothing about the source, and the
    source is what gets deleted.

    The case this exists for: a drop appends a chapter after the inventory is
    taken but before the source is released. The staged copy still matches the
    manifest, so every destination check passes -- and the source, now
    correct and *larger* than what was staged, is deleted. Nothing is
    corrupt. The staged case is simply an older, smaller truth, and the newer
    one is destroyed to make room for it.

    Re-inventorying here catches that, and the check is content-based rather
    than metadata-based, so it also narrows the same-size-rewrite window that
    `_hash_stable_file` documents as beyond its reach: a torn or stale read
    at step 1 surfaces here as a digest mismatch because the bytes are read
    again. That holds on both transfer paths, including the same-volume
    rename, which otherwise never re-reads the payload at all.

    Raises SourceChangedError if the source settled at different content, and
    PayloadChangedError (from build_inventory) if it is still moving. Both
    mean abandon the transfer; only the second means this case id may be
    resumed.

    Returns the freshly built inventory, so a caller that needs to act on the
    current source does not have to walk it a third time.
    """
    current = build_inventory(staging_source_path)
    if current.digest != expected.digest:
        raise SourceChangedError(
            f"{staging_source_path} no longer matches the inventory this case "
            f"was minted from: {inventory_difference(expected, current)}. "
            "The source is authoritative and has not been touched; retry to "
            "stage the current payload under a new case id."
        )
    return current


# ------------------------------------------------------------- identity

def normalise_source_path(path: Path) -> str:
    """A stable textual form of a source path, for hashing.

    Case-folded because Windows paths are case-insensitive, so the same
    directory reached by differently-cased text is the same directory and
    must not mint two cases.
    """
    return os.path.normpath(str(path)).casefold().replace("\\", "/")


def case_identity(series_key_value: str, staging_source_path: Path,
                  inventory: PayloadInventory) -> str:
    """Bind a payload to a series and the path it was staged from.

    Deliberately excludes the routing decision and the config digest: a retry
    after a policy change must resolve to the same physical case and record a
    recomputed decision against it, not duplicate the payload.
    """
    material = (
        CASE_CONTRACT.encode("utf-8") + b"\0"
        + series_key_value.encode("utf-8") + b"\0"
        + normalise_source_path(staging_source_path).encode("utf-8") + b"\0"
        + inventory.canonical_bytes()
    )
    return hashlib.sha256(material).hexdigest()


# ------------------------------------------------------------- volumes

def volume_identity(path: Path) -> int:
    """Identify the volume *path* lives on, walking up to an existing ancestor.

    Measured on this deployment: os.stat().st_dev is exactly the Windows
    volume serial number (C: st_dev 1344838785 == serial 0x50289C81; X: st_dev
    1720277553 == serial 0x66895A31). Drive letters are not the contract --
    mounted volumes, subst, and junctions all break letter comparison -- so
    the device identity is what decides.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.stat(probe).st_dev


def same_volume(a: Path, b: Path) -> bool:
    return volume_identity(a) == volume_identity(b)


def transfer_method(source: Path, target: Path) -> str:
    """Choose how the payload will be moved, from the paths themselves."""
    return METHOD_RENAME if same_volume(source, target) else METHOD_COPY_VERIFY


# ------------------------------------------------------------- layout

@dataclass(frozen=True)
class StagingLayout:
    """Where the review root's parts live. Creates nothing."""

    root: Path

    @property
    def partial(self) -> Path:
        return self.root / PARTIAL_DIR

    @property
    def pending(self) -> Path:
        return self.root / PENDING_DIR

    @property
    def manifests(self) -> Path:
        return self.root / MANIFESTS_DIR

    @property
    def rejected(self) -> Path:
        return self.root / REJECTED_DIR

    def partial_case(self, case_id: str) -> Path:
        return self.partial / case_id

    def pending_case(self, case_id: str) -> Path:
        return self.pending / case_id

    def payload(self, case_id: str) -> Path:
        return self.pending_case(case_id) / PAYLOAD_DIR

    def partial_payload(self, case_id: str) -> Path:
        """Where the payload lives before publication.

        Mirrors `payload` exactly, so publication is a rename of the case
        directory and never a restructure. A layout that differed either side
        of publication would make the atomic step non-atomic.
        """
        return self.partial_case(case_id) / PAYLOAD_DIR

    def manifest(self, case_id: str) -> Path:
        return self.manifests / f"{case_id}.json"

    def all_dirs(self) -> tuple[Path, ...]:
        return (self.partial, self.pending, self.manifests, self.rejected)


# ------------------------------------------------------------- validation

VALID_STATES = frozenset({
    STATE_PLANNED, STATE_COPIED_VERIFIED, STATE_PENDING_REVIEW,
    STATE_PROMOTED, STATE_REJECTED, STATE_ROLLED_BACK,
})
# No empty value: a persisted manifest must name the method that was
# actually chosen. CaseManifest still defaults to "" for in-memory
# construction, but such an object cannot round-trip until planning has
# assigned one.
VALID_METHODS = frozenset({METHOD_RENAME, METHOD_COPY_VERIFY})

_FILE_ENTRY_TYPES = {"relative_path": str, "size_bytes": int, "sha256": str}
# ReviewHint is exactly kind and value, both strings (see cbz_routing).
_REVIEW_HINT_TYPES = {"kind": str, "value": str}


def _check_type(label: str, value, declared) -> None:
    """Exact type check. bool is not an int here, and int is not a bool.

    Python treats bool as a subclass of int, so an isinstance check would let
    `true` satisfy an integer field and `1` satisfy a Boolean one. For
    persisted state that is a silent corruption, not a convenience.
    """
    expected = {"str": str, "int": int, "bool": bool, "list[dict]": list}
    wanted = expected.get(declared if isinstance(declared, str) else "")
    if wanted is None:
        return
    if type(value) is not wanted:
        raise StagingError(
            f"{label} must be {wanted.__name__}, got "
            f"{type(value).__name__} ({value!r})"
        )


def _validate_review_hints(hints) -> None:
    """Each hint is exactly kind and value, both strings.

    Validating only the outer list let rows like 1, null, {"kind": 7}, or an
    object carrying extra keys survive into what a reviewer is shown.
    """
    for position, row in enumerate(hints):
        if not isinstance(row, dict):
            raise StagingError(
                f"manifest.review_hints[{position}] must be an object, got "
                f"{type(row).__name__}"
            )
        if set(row) != set(_REVIEW_HINT_TYPES):
            raise StagingError(
                f"manifest.review_hints[{position}] keys must be exactly "
                f"{sorted(_REVIEW_HINT_TYPES)}, got {sorted(row)}"
            )
        for key, wanted in _REVIEW_HINT_TYPES.items():
            if type(row[key]) is not wanted:
                raise StagingError(
                    f"manifest.review_hints[{position}].{key} must be "
                    f"{wanted.__name__}, got {type(row[key]).__name__}"
                )


def _inventory_from_json(files) -> PayloadInventory:
    """Rebuild an inventory from a manifest's file list, validating each row."""
    entries = []
    for position, row in enumerate(files):
        if not isinstance(row, dict):
            raise StagingError(
                f"manifest.files[{position}] must be an object, got "
                f"{type(row).__name__}"
            )
        unknown = set(row) - set(_FILE_ENTRY_TYPES)
        missing = set(_FILE_ENTRY_TYPES) - set(row)
        if unknown or missing:
            raise StagingError(
                f"manifest.files[{position}] keys are wrong: "
                f"unknown {sorted(unknown)}, missing {sorted(missing)}"
            )
        for key, wanted in _FILE_ENTRY_TYPES.items():
            if type(row[key]) is not wanted:
                raise StagingError(
                    f"manifest.files[{position}].{key} must be "
                    f"{wanted.__name__}, got {type(row[key]).__name__}"
                )
        _reject_unsafe_relative_path(position, row["relative_path"])
        if row["size_bytes"] < 0:
            raise StagingError(
                f"manifest.files[{position}].size_bytes is negative"
            )
        entries.append(FileEntry(row["relative_path"], row["size_bytes"],
                                 row["sha256"]))

    paths = [e.relative_path for e in entries]
    if len(set(paths)) != len(paths):
        raise StagingError("manifest.files contains duplicate relative paths")
    if paths != sorted(paths):
        raise StagingError(
            "manifest.files is not in canonical relative-path order"
        )
    return PayloadInventory(tuple(entries))


def _reject_unsafe_relative_path(position: int, relative: str) -> None:
    """A payload path must stay inside the payload.

    A manifest is read back to decide what to promote or roll back, so an
    absolute path or a traversal component in it is a way to reach outside
    the case -- into a library, or over an unrelated file.
    """
    if not relative:
        raise StagingError(f"manifest.files[{position}].relative_path is empty")
    if relative != relative.strip():
        raise StagingError(
            f"manifest.files[{position}].relative_path has surrounding "
            f"whitespace: {relative!r}"
        )
    pure = PurePosixPath(relative)
    if pure.is_absolute() or relative.startswith("/") or "\\" in relative:
        raise StagingError(
            f"manifest.files[{position}].relative_path is not relative posix: "
            f"{relative!r}"
        )
    if ntpath.isabs(relative) or ntpath.splitdrive(relative)[0]:
        raise StagingError(
            f"manifest.files[{position}].relative_path is a Windows absolute "
            f"path: {relative!r}"
        )
    # Checked against the raw text, not PurePosixPath.parts: the parser
    # silently normalises "./here.cbz" to "here.cbz", so a parts-based check
    # would accept a path this canonical form never produces.
    if any(part in ("..", ".", "") for part in relative.split("/")):
        raise StagingError(
            f"manifest.files[{position}].relative_path contains a traversal or "
            f"empty component: {relative!r}"
        )


# ------------------------------------------------------------- manifest

@dataclass
class CaseManifest:
    """The record of one review case. Serialised as the case's own file."""

    case_id: str
    state: str
    contract: str = CASE_CONTRACT

    # Two paths, because they differ and both matter. arrival_path is where
    # the watcher first saw it; staging_source_path is the post-cleanup,
    # post-series-resolution path the transfer actually reads, and is what
    # case identity is computed from -- an ID derived from a path the watcher
    # is about to rename would not survive a retry.
    arrival_path: str = ""
    staging_source_path: str = ""
    staged_path: str = ""

    series_name: str = ""
    series_key: str = ""
    source_name: str = ""

    # Identity. case_identity_digest binds payload to series and source;
    # payload_inventory_digest covers the bytes alone, so duplicate content
    # across separately arrived cases stays detectable.
    case_identity_digest: str = ""
    payload_inventory_digest: str = ""

    transfer_method: str = ""
    source_volume: int = 0
    staging_volume: int = 0

    routing_config_digest: str = ""
    decision_dest_key: str = ""
    decision_confidence: str = ""
    decision_authoritative: bool = False
    decision_evidence_strength: str = ""
    decision_rule_name: str = ""
    decision_reason: str = ""
    review_hints: list[dict] = field(default_factory=list)

    file_count: int = 0
    total_bytes: int = 0
    files: list[dict] = field(default_factory=list)

    created_at: str = ""
    updated_at: str = ""

    def touch(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.created_at = self.created_at or now
        self.updated_at = now

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False,
                          sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "CaseManifest":
        """Parse and fully validate a manifest, or raise.

        This is persisted recovery metadata: a manifest that survives parsing
        is treated as authoritative about what is staged and what may be
        promoted, so it gets at least the strictness the routing config gets.
        Every field is type-checked, every declared invariant is recomputed,
        and anything that fails is not recoverable state.
        """
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StagingError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise StagingError(
                f"manifest must be a JSON object, got {type(raw).__name__}"
            )

        contract = raw.get("contract")
        if contract != CASE_CONTRACT:
            raise StagingError(
                f"manifest contract {contract!r} is not {CASE_CONTRACT!r}; "
                "refusing to interpret a case minted under another scheme"
            )

        fields = cls.__dataclass_fields__
        unknown = set(raw) - set(fields)
        if unknown:
            raise StagingError(f"manifest has unknown fields: {sorted(unknown)}")
        missing = set(fields) - set(raw)
        if missing:
            raise StagingError(f"manifest is missing fields: {sorted(missing)}")

        for name, spec in fields.items():
            _check_type(f"manifest.{name}", raw[name], spec.type)

        if raw["state"] not in VALID_STATES:
            raise StagingError(
                f"manifest.state {raw['state']!r} is not one of "
                f"{sorted(VALID_STATES)}"
            )
        _validate_review_hints(raw["review_hints"])
        if raw["transfer_method"] not in VALID_METHODS:
            raise StagingError(
                f"manifest.transfer_method {raw['transfer_method']!r} is not "
                f"one of {sorted(VALID_METHODS)}"
            )

        inventory = _inventory_from_json(raw["files"])
        if raw["file_count"] != inventory.file_count:
            raise StagingError(
                f"manifest.file_count {raw['file_count']} does not match "
                f"{inventory.file_count} entries"
            )
        if raw["total_bytes"] != inventory.total_bytes:
            raise StagingError(
                f"manifest.total_bytes {raw['total_bytes']} does not match "
                f"{inventory.total_bytes} from the inventory"
            )
        if raw["payload_inventory_digest"] != inventory.digest:
            raise StagingError(
                "manifest.payload_inventory_digest does not recompute from "
                "its own file list"
            )

        recomputed = case_identity(
            raw["series_key"], Path(raw["staging_source_path"]), inventory)
        if raw["case_identity_digest"] != recomputed:
            raise StagingError(
                "manifest.case_identity_digest does not recompute from the "
                "contract, series key, source path, and inventory"
            )
        if raw["case_id"] != raw["case_identity_digest"]:
            raise StagingError(
                "manifest.case_id does not equal its case_identity_digest"
            )
        return cls(**raw)

    def write(self, path: Path) -> None:
        """Write the manifest whole, never in place.

        A manifest truncated by a crash mid-write is worse than one that is
        absent: absence is a recoverable partial case, corruption is not.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        os.replace(tmp, path)


# ------------------------------------------------------------- inspection

# A case still in flight may be resumed; a terminal one may not. Reporting
# a promoted or rejected case as recoverable would invite a retry to reopen
# a decision an operator already made.
RECOVERABLE_STATES = frozenset({
    STATE_PLANNED, STATE_COPIED_VERIFIED, STATE_PENDING_REVIEW,
})
TERMINAL_STATES = frozenset({
    STATE_PROMOTED, STATE_REJECTED, STATE_ROLLED_BACK,
})

STATUS_ABSENT = "absent"
STATUS_VALID = "valid"
STATUS_PARTIAL = "partial"
STATUS_ORPHANED_PENDING = "orphaned_pending"


@dataclass(frozen=True)
class ExistingCase:
    """What is already on disk for one case id.

    A boolean was wrong here. "Something exists at that path" and "a case
    exists" are different claims, and only the second may suppress creating
    or recovering the real case. A truncated, wrong-contract, or unrelated
    manifest must raise rather than silently count as the case being present.
    """

    status: str
    manifest: CaseManifest | None = None

    @property
    def is_recoverable(self) -> bool:
        """Whether a retry should resume rather than start over.

        State-aware: a valid manifest is not enough. A promoted, rejected, or
        rolled-back case is finished, and reporting it as resumable would let
        a retry reopen a decision an operator already made.
        """
        if self.status == STATUS_PARTIAL:
            return True
        if self.status != STATUS_VALID or self.manifest is None:
            return False
        return self.manifest.state in RECOVERABLE_STATES


def inspect_case(layout: StagingLayout, case_id: str) -> ExistingCase:
    """Report what exists for *case_id*, validating anything it finds.

    Raises rather than reporting a status when what is on disk is
    self-inconsistent: a manifest that will not parse, was minted under
    another contract, or describes a different case is a condition an
    operator has to see, not one to route around.
    """
    manifest_path = layout.manifest(case_id)
    if manifest_path.exists():
        try:
            text = manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StagingError(f"cannot read {manifest_path}: {exc}") from exc
        manifest = CaseManifest.from_json(text)          # validates fully
        if manifest.case_id != case_id:
            raise StagingError(
                f"{manifest_path} describes case {manifest.case_id} but is "
                f"filed as {case_id}"
            )
        return ExistingCase(STATUS_VALID, manifest)

    # A published payload with no manifest cannot be interpreted: nothing
    # records what it was, what decided it, or whether its bytes are complete.
    if layout.pending_case(case_id).exists():
        return ExistingCase(STATUS_ORPHANED_PENDING)

    # A .partial directory is expected debris from an interrupted transfer.
    # The source is still authoritative, so this is recoverable by retrying.
    if layout.partial_case(case_id).exists():
        return ExistingCase(STATUS_PARTIAL)

    return ExistingCase(STATUS_ABSENT)


# ------------------------------------------------------------- planning

@dataclass(frozen=True)
class StagingPlan:
    """What staging one case would do. Nothing has happened yet."""

    manifest: CaseManifest
    layout: StagingLayout
    inventory: PayloadInventory
    existing: ExistingCase
    free_bytes: int

    @property
    def case_id(self) -> str:
        return self.manifest.case_id

    @property
    def needs_space(self) -> bool:
        return self.manifest.transfer_method == METHOD_COPY_VERIFY

    def describe(self) -> list[str]:
        m = self.manifest
        lines = [
            f"case {m.case_id[:16]}...  ({self.existing.status})",
            f"  series        : {m.series_name!r} (key {m.series_key!r})",
            f"  arrival       : {m.arrival_path}",
            f"  staging source: {m.staging_source_path}",
            f"  payload       : {m.file_count} file(s), {m.total_bytes / 1e6:,.1f} MB",
            f"  method        : {m.transfer_method} "
            f"(source vol {m.source_volume}, staging vol {m.staging_volume})",
            f"  decision      : {m.decision_dest_key or '-'} "
            f"[{m.decision_confidence or '-'}]",
            f"  -> {self.layout.pending_case(m.case_id)}",
        ]
        if self.needs_space:
            fits = self.free_bytes >= m.total_bytes
            lines.append(
                f"  space         : {self.free_bytes / 1e9:,.1f} GB free, "
                f"{'sufficient' if fits else 'INSUFFICIENT'}"
            )
        return lines


def plan_case(staging_root: Path, arrival_path: Path, staging_source_path: Path,
              series_name: str, series_key_value: str, source_name: str,
              decision=None, routing_config_digest: str = "") -> StagingPlan:
    """Compute a case's identity and manifest without touching anything.

    Read-only: hashes the payload, resolves the transfer method from the real
    volumes, and reports whether this exact case is already staged.
    """
    layout = StagingLayout(staging_root)
    inventory = build_inventory(staging_source_path)
    if not inventory.entries:
        raise StagingError(f"nothing to stage: {staging_source_path} has no files")

    case_id = case_identity(series_key_value, staging_source_path, inventory)
    source_vol = volume_identity(staging_source_path)
    staging_vol = volume_identity(staging_root)

    manifest = CaseManifest(
        case_id=case_id,
        state=STATE_PLANNED,
        arrival_path=str(arrival_path),
        staging_source_path=str(staging_source_path),
        staged_path=str(layout.pending_case(case_id)),
        series_name=series_name,
        series_key=series_key_value,
        source_name=source_name,
        case_identity_digest=case_id,
        payload_inventory_digest=inventory.digest,
        transfer_method=(METHOD_RENAME if source_vol == staging_vol
                         else METHOD_COPY_VERIFY),
        source_volume=source_vol,
        staging_volume=staging_vol,
        routing_config_digest=routing_config_digest,
        file_count=inventory.file_count,
        total_bytes=inventory.total_bytes,
        files=inventory.as_json(),
    )
    if decision is not None:
        manifest.decision_dest_key = decision.dest_key
        manifest.decision_confidence = decision.confidence
        manifest.decision_authoritative = decision.authoritative
        manifest.decision_evidence_strength = decision.evidence_strength
        manifest.decision_rule_name = decision.rule_name or ""
        manifest.decision_reason = decision.reason
        manifest.review_hints = [
            {"kind": h.kind, "value": h.value} for h in decision.review_hints
        ]
    manifest.touch()

    try:
        free = shutil.disk_usage(_existing_ancestor(staging_root)).free
    except OSError:
        free = 0

    return StagingPlan(
        manifest=manifest,
        layout=layout,
        inventory=inventory,
        existing=inspect_case(layout, case_id),
        free_bytes=free,
    )


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


# ------------------------------------------------------------- transfer

def _verify_staged_payload(staged_payload_root: Path,
                           expected: PayloadInventory) -> None:
    """Prove the staged copy matches the manifest. Step 3.

    Re-reads the copied bytes rather than trusting that copytree reported
    success: a copy that silently truncated, or landed on a volume that
    rewrote what it was given, is exactly what this gate exists to catch.
    """
    current = build_inventory(staged_payload_root)
    if current.digest != expected.digest:
        raise StagedPayloadMismatchError(
            f"the staged copy at {staged_payload_root} does not match the "
            f"manifest: {inventory_difference(expected, current)}. The source "
            "has not been touched and remains authoritative."
        )


def _refuse_collisions(layout: StagingLayout, existing: ExistingCase,
                       case_id: str) -> None:
    """Refuse anything already occupying this case. Never merge, never overwrite.

    A published payload is refused whether or not a manifest explains it: an
    orphaned `pending` directory is uninterpretable, and a valid one may
    already carry an operator's decision. A terminal manifest is refused for
    the same reason -- promoted, rejected, and rolled back are decisions, not
    states to redo.
    """
    if layout.pending_case(case_id).exists():
        raise CaseCollisionError(
            f"{layout.pending_case(case_id)} already exists; refusing to "
            "merge or overwrite a published case"
        )
    if existing.status == STATUS_VALID and existing.manifest is not None:
        if existing.manifest.state in TERMINAL_STATES:
            raise CaseCollisionError(
                f"case {case_id} is {existing.manifest.state}; refusing to "
                "reopen a decision that has already been made"
            )


def validate_execution_plan(plan: StagingPlan) -> CaseManifest:
    """Prove the plan still describes what it was measured against.

    `StagingPlan` is frozen; the `CaseManifest` inside it is not. Between
    planning and execution its fields can change, and the executor uses them
    to decide which directory to create, which manifest to overwrite, and --
    on the copy path -- which directory to delete. A mutated `case_id`
    redirects all three at once.

    Round-tripping through `from_json` re-runs every internal invariant: the
    contract, the field types, the file rows, the recomputed payload digest,
    and the case identity recomputed from series key, source path, and
    inventory. That catches a single field edited in isolation.

    What it cannot catch is the manifest being rewritten wholesale and
    consistently, because then it is self-consistent by construction. So the
    checks below anchor it to `plan.inventory` -- the one thing the plan holds
    that was measured from the filesystem rather than copied out of the
    manifest. A caller that replaces both in step is beyond what this can
    see, and is stated here rather than implied to be covered.

    Returns the validated manifest, which is what execution then uses; the
    plan's own object is left alone so a plan stays a plan.
    """
    try:
        validated = CaseManifest.from_json(plan.manifest.to_json())
    except StagingError as exc:
        raise InvalidExecutionPlanError(
            f"the plan's manifest is not internally consistent: {exc}"
        ) from exc

    checks = (
        ("payload_inventory_digest", validated.payload_inventory_digest,
         plan.inventory.digest),
        ("file_count", validated.file_count, plan.inventory.file_count),
        ("total_bytes", validated.total_bytes, plan.inventory.total_bytes),
        ("files", validated.files, plan.inventory.as_json()),
        ("staged_path", validated.staged_path,
         str(plan.layout.pending_case(validated.case_id))),
    )
    for name, found, wanted in checks:
        if found != wanted:
            raise InvalidExecutionPlanError(
                f"manifest.{name} does not match the inventory this plan was "
                f"built from: {found!r} != {wanted!r}"
            )

    # Anchors case_id to the measured inventory rather than to the manifest's
    # own copy of it, so a re-minted identity pointing somewhere else is
    # caught before it can steer a path.
    expected_id = case_identity(validated.series_key,
                                Path(validated.staging_source_path),
                                plan.inventory)
    if validated.case_id != expected_id:
        raise InvalidExecutionPlanError(
            "manifest.case_id does not recompute from the series key, source "
            "path, and the inventory this plan measured"
        )

    # The method is a consequence of the volumes the plan recorded, not a free
    # choice: flipping it alone would take the rename branch across volumes,
    # where os.replace cannot work and nothing else would notice.
    implied = (METHOD_RENAME if validated.source_volume == validated.staging_volume
               else METHOD_COPY_VERIFY)
    if validated.transfer_method != implied:
        raise InvalidExecutionPlanError(
            f"manifest.transfer_method {validated.transfer_method!r} "
            f"contradicts the recorded volumes (source {validated.source_volume}, "
            f"staging {validated.staging_volume}), which imply {implied!r}"
        )
    return validated


def execute_transfer(plan: StagingPlan, *,
                     delete_copied_source: bool = False) -> CaseManifest:
    """Stage one case. The source stays authoritative until publication.

    Implements the ordering from #35, whose whole point is that the last
    checks are the ones easy to leave out::

        1. inventory the source              (done by plan_case)
        2. copy into .partial/<case-id>      (copy_verify only)
        3. verify the staged copy            (copy_verify only)
        4. revalidate the SOURCE             <- both paths, always
        5. publish: .partial -> pending      (atomic rename)
        6. release the source                (copy_verify only)

    Steps 3 and 4 are separate mandatory gates on the copy path. Step 3 asks
    "are the bytes I wrote the bytes I meant to write"; step 4 asks "is the
    thing I am about to release still the thing I inventoried". Passing the
    first says nothing about the second, and it is the second that guards
    what gets destroyed.

    The same-volume rename skips 2 and 3 -- there are no copied bytes to
    verify -- but not 4. It is revalidated immediately before the rename,
    which is the last moment anything can be checked. The residual race
    between that check and the rename is a filesystem TOCTOU boundary and is
    accepted, not closed: no guard that precedes an operation can cover the
    instant the operation happens.

    The manifest is written before anything moves, so a crash at any point
    leaves an interpretable case rather than untracked bytes.

    Whether `.partial` may be discarded depends on the method, and the two
    are not alike. On the copy path the source survives the whole transfer,
    so debris is disposable and the transfer is redone rather than resumed.
    On the rename path there is a window between moving the source in and
    publishing the case where the source is gone and `.partial` holds the
    only copy; deleting it to restart would destroy the payload. So a rename
    finding `.partial` present refuses, and does not inspect whether the
    payload is complete -- that is recovery's decision, and recovery is a
    separate scope bullet in #35.

    On failure the source is left in place, no `pending` entry is created,
    and no final-library index entry is recorded.

    `delete_copied_source` applies to the copy path alone and defaults to
    off, so the destructive step is opt-in: this repository's standing rule
    is to quarantine rather than delete, and #35's step 6 is the specific
    exception a caller must ask for. The rename path never had a second copy
    to release -- its original is moved, not deleted -- so the flag is
    irrelevant there and does not silently mean something different.
    """
    layout = plan.layout
    manifest = validate_execution_plan(plan)
    case_id = manifest.case_id
    source = Path(manifest.staging_source_path)

    # Re-inspected here rather than trusting plan.existing: a case can be
    # published, promoted, or rejected between planning and execution, and
    # this is the last moment before anything is created or destroyed.
    _refuse_collisions(layout, inspect_case(layout, case_id), case_id)

    partial_case = layout.partial_case(case_id)
    if partial_case.exists():
        if manifest.transfer_method == METHOD_RENAME:
            raise PartialRecoveryRequiredError(
                f"{partial_case} already exists and this is a rename "
                "transfer, so it may hold the only copy of the payload. "
                "Refusing to discard it; recover the case explicitly."
            )
        shutil.rmtree(partial_case)

    for directory in layout.all_dirs():
        directory.mkdir(parents=True, exist_ok=True)

    # Written before anything moves: a case with no manifest is debris nobody
    # can interpret, and that is a worse failure than one that is merely
    # incomplete.
    manifest.state = STATE_PLANNED
    manifest.touch()
    manifest.write(layout.manifest(case_id))

    staged_payload = layout.partial_payload(case_id) / source.name
    staged_payload.parent.mkdir(parents=True, exist_ok=True)

    if manifest.transfer_method == METHOD_COPY_VERIFY:
        if plan.free_bytes < manifest.total_bytes:
            raise StagingError(
                f"insufficient space to stage {case_id}: "
                f"{manifest.total_bytes} bytes needed, "
                f"{plan.free_bytes} free at {layout.root}"
            )
        shutil.copytree(source, staged_payload)
        _verify_staged_payload(staged_payload, plan.inventory)   # gate 1
        manifest.state = STATE_COPIED_VERIFIED
        manifest.touch()
        manifest.write(layout.manifest(case_id))

    revalidate_source(source, plan.inventory)                    # gate 2

    if manifest.transfer_method == METHOD_RENAME:
        os.replace(source, staged_payload)

    os.replace(partial_case, layout.pending_case(case_id))       # publish

    manifest.state = STATE_PENDING_REVIEW
    manifest.staged_path = str(layout.pending_case(case_id))
    manifest.touch()
    manifest.write(layout.manifest(case_id))

    if manifest.transfer_method == METHOD_COPY_VERIFY and delete_copied_source:
        shutil.rmtree(source)

    return manifest


# ------------------------------------------------------------- recovery

# The seven states of #35's recovery interruption matrix, plus the three
# conditions that are not interruptions at all. Named for the action they
# imply rather than for what is on disk, because the action is what a caller
# has to decide.
RECOVERY_NOTHING_STAGED = "nothing_staged"
RECOVERY_RESUME_PUBLICATION = "resume_publication"      # matrix 1
RECOVERY_RESTART_FROM_SOURCE = "restart_from_source"    # matrix 2
RECOVERY_PUBLISH_PARTIAL = "publish_partial"            # matrix 3
RECOVERY_PARTIAL_UNUSABLE = "partial_unusable"          # matrix 4
RECOVERY_COMPLETE = "complete"                          # matrix 5
RECOVERY_ORPHANED_PENDING = "orphaned_pending"          # matrix 6
RECOVERY_PENDING_INCONSISTENT = "pending_inconsistent"  # matrix 7
RECOVERY_TERMINAL = "terminal"
RECOVERY_OPERATOR_REQUIRED = "operator_required"

# Only these may be acted on without an operator. Everything else either has
# nothing to do or has a question only a person can answer -- and in every
# such case the rule is to preserve what exists rather than resolve it.
AUTOMATIC_RECOVERY_STATES = frozenset({
    RECOVERY_RESUME_PUBLICATION, RECOVERY_RESTART_FROM_SOURCE,
    RECOVERY_PUBLISH_PARTIAL, RECOVERY_COMPLETE,
})


@dataclass(frozen=True)
class RecoveryAssessment:
    """What interrupted a case, and what may be done about it. Reads only."""

    state: str
    case_id: str
    manifest: CaseManifest | None
    detail: str

    @property
    def automatic(self) -> bool:
        return self.state in AUTOMATIC_RECOVERY_STATES

    def describe(self) -> str:
        route = "automatic" if self.automatic else "operator"
        return f"{self.case_id[:16]}...  {self.state}  ({route})\n  {self.detail}"


def _payload_agrees(root: Path, expected: PayloadInventory) -> tuple[bool, str]:
    """Whether the tree at *root* is exactly the payload *expected* describes.

    Content-based, and it re-reads the bytes. A payload that is still moving
    counts as disagreeing rather than raising: recovery runs after an
    interruption, when a tree in motion is an ordinary thing to find, and the
    answer to "may I act on this" is no either way.
    """
    if not root.is_dir():
        return False, f"{root} is absent"
    try:
        current = build_inventory(root)
    except PayloadChangedError as exc:
        return False, f"{root} is still changing ({exc})"
    except StagingError as exc:
        return False, f"{root} cannot be inventoried ({exc})"
    if current.digest != expected.digest:
        return False, inventory_difference(expected, current)
    return True, f"{current.file_count} file(s) matching the manifest"


def assess_recovery(layout: StagingLayout, case_id: str) -> RecoveryAssessment:
    """Classify an interrupted case from disk alone. Changes nothing.

    Reconstructs from the persisted manifest and the filesystem, and takes no
    `StagingPlan`. That is deliberate and is the point of the function: a plan
    surviving in memory proves only that an exception was caught, never that
    the process died and the case was rebuilt from what was written down.

    Where the transfer was interrupted decides who may act, and the two
    methods differ in a way that matters:

        copy_verify   the source survives the whole transfer, so `.partial`
                      is a replaceable copy and a bad one may be discarded
        rename        between the move and the publication the source is
                      gone and `.partial` is the only copy in existence

    So a `.partial` that fails to verify is disposable on one path and
    irreplaceable on the other, and the same observation produces opposite
    instructions. Nothing here acts on either; it reports.

    A manifest that will not validate is reported rather than raised, unlike
    `inspect_case`. That function gates whether a case may be created, where
    refusing loudly is right. This one exists to tell an operator what is on
    disk, and "the manifest is corrupt" is the most important thing it could
    have to say.
    """
    manifest_path = layout.manifest(case_id)
    pending_case = layout.pending_case(case_id)
    partial_case = layout.partial_case(case_id)

    def assessed(state: str, detail: str,
                 manifest: CaseManifest | None = None) -> RecoveryAssessment:
        return RecoveryAssessment(state, case_id, manifest, detail)

    if not manifest_path.exists():
        if pending_case.exists():
            return assessed(
                RECOVERY_ORPHANED_PENDING,
                f"{pending_case} holds a published payload with no manifest. "
                "Nothing records what it was, what decided it, or whether its "
                "bytes are complete, and none of that can be recovered from "
                "the payload itself.")
        if partial_case.exists():
            return assessed(
                RECOVERY_OPERATOR_REQUIRED,
                f"{partial_case} exists with no manifest. The executor writes "
                "the manifest before it moves anything, so this cannot be "
                "attributed to a case and must not be assumed to belong to "
                "one.")
        return assessed(RECOVERY_NOTHING_STAGED,
                        f"no manifest, no payload, nothing staged for {case_id}")

    try:
        manifest = CaseManifest.from_json(
            manifest_path.read_text(encoding="utf-8"))
    except (OSError, StagingError) as exc:
        return assessed(
            RECOVERY_OPERATOR_REQUIRED,
            f"{manifest_path} does not validate ({exc}). Recovery reads and "
            "fully validates the manifest before acting, so nothing here may "
            "be interpreted.")

    if manifest.case_id != case_id:
        return assessed(
            RECOVERY_OPERATOR_REQUIRED,
            f"{manifest_path} describes case {manifest.case_id} but is filed "
            f"as {case_id}.", manifest)

    if manifest.state in TERMINAL_STATES:
        return assessed(
            RECOVERY_TERMINAL,
            f"case is {manifest.state}, which is an operator's decision "
            "rather than an interrupted transfer.", manifest)

    expected = _inventory_from_json(manifest.files)
    source = Path(manifest.staging_source_path)
    payload_name = source.name

    if pending_case.exists():
        agrees, why = _payload_agrees(layout.payload(case_id) / payload_name,
                                      expected)
        if agrees:
            return assessed(
                RECOVERY_COMPLETE,
                f"already published and verified ({why}); the manifest state "
                f"is {manifest.state}.", manifest)
        return assessed(
            RECOVERY_PENDING_INCONSISTENT,
            f"{pending_case} exists but its payload does not verify: {why}. "
            "Refusing to recreate, recopy, or republish a case that claims to "
            "be published.", manifest)

    if manifest.state == STATE_PENDING_REVIEW:
        return assessed(
            RECOVERY_PENDING_INCONSISTENT,
            f"the manifest says {STATE_PENDING_REVIEW} but {pending_case} does "
            "not exist. This is a contradiction, not a resumable transfer.",
            manifest)

    partial_payload = layout.partial_payload(case_id) / payload_name

    if manifest.transfer_method == METHOD_COPY_VERIFY:
        if not source.is_dir():
            return assessed(
                RECOVERY_OPERATOR_REQUIRED,
                f"a copy transfer whose source {source} is gone. The copy path "
                "never releases the source before publication, so its absence "
                "is unexplained and .partial must not be trusted to stand in "
                "for it.", manifest)
        agrees, why = _payload_agrees(partial_payload, expected)
        if agrees:
            return assessed(
                RECOVERY_RESUME_PUBLICATION,
                f"the staged copy verifies ({why}) and the source is still "
                "present; publication can resume without copying again.",
                manifest)
        return assessed(
            RECOVERY_RESTART_FROM_SOURCE,
            f"the staged copy does not verify: {why}. The source is present "
            "and authoritative, so .partial may be discarded and the copy "
            "redone.", manifest)

    # Same-volume rename.
    if source.is_dir():
        if partial_payload.exists():
            return assessed(
                RECOVERY_OPERATOR_REQUIRED,
                f"both {source} and {partial_payload} exist for a rename "
                "transfer. A rename moves rather than copies, so a payload in "
                "both places is unexplained, and the standing rule is that a "
                "rename-path .partial is never discarded automatically.",
                manifest)
        return assessed(
            RECOVERY_RESTART_FROM_SOURCE,
            f"the source {source} is still present and .partial holds no "
            "payload, so the rename had not happened; the empty scaffolding "
            "can be cleared and the move made from the source.", manifest)

    agrees, why = _payload_agrees(partial_payload, expected)
    if agrees:
        return assessed(
            RECOVERY_PUBLISH_PARTIAL,
            f"the source is gone and .partial verifies ({why}), so it is the "
            "authoritative and possibly only copy; it can be published "
            "without the original source path existing.", manifest)

    if not partial_payload.exists():
        return assessed(
            RECOVERY_OPERATOR_REQUIRED,
            f"the source {source} is gone and {partial_payload} does not "
            "exist either. The payload is in neither location.", manifest)

    return assessed(
        RECOVERY_PARTIAL_UNUSABLE,
        f"the source is gone and .partial does not verify: {why}. It may "
        "still be the only copy of this payload, so it must not be deleted, "
        "replaced, merged, or overwritten.", manifest)


@dataclass(frozen=True)
class RecoveryOutcome:
    """What recovery did, or declined to do. Always returned, never guessed."""

    acted: bool
    state: str
    case_id: str
    manifest: CaseManifest | None
    detail: str


def _publish(layout: StagingLayout, manifest: CaseManifest) -> None:
    """Rename .partial to pending and record the case as published."""
    os.replace(layout.partial_case(manifest.case_id),
               layout.pending_case(manifest.case_id))
    manifest.state = STATE_PENDING_REVIEW
    manifest.staged_path = str(layout.pending_case(manifest.case_id))
    manifest.touch()
    manifest.write(layout.manifest(manifest.case_id))


def recover_case(layout: StagingLayout, case_id: str, *,
                 delete_copied_source: bool = False) -> RecoveryOutcome:
    """Finish or restart an interrupted transfer, from disk alone.

    Takes no `StagingPlan`, for the same reason `assess_recovery` does not:
    recovery has to work after the process that planned the transfer is gone,
    and a function that accepts a plan can always be handed one by a caller
    that still has it, hiding whether the persisted state was ever sufficient.

    Branches only on the classification `assess_recovery` returns, and acts on
    exactly four of them. Everything else -- a malformed rename `.partial`, an
    orphaned pending directory, a manifest contradicting its payload, and the
    combinations the matrix does not specify -- returns a structured refusal
    and touches nothing. Refusing is a result here, not an error: an operator
    asking "what happened to this case" deserves an answer rather than a
    traceback.

    What it does not swallow is a source that changed underneath it.
    `SourceChangedError` propagates, because by the identity contract the
    source is then a different case, and returning that as a refusal would
    make it easy to treat as "nothing to do".

    Everything is re-verified immediately before it is acted on. The
    assessment is a report about a moment that has already passed, and the
    gap between reading and acting is exactly where a payload can move.
    """
    found = assess_recovery(layout, case_id)
    manifest = found.manifest

    if not found.automatic or manifest is None:
        return RecoveryOutcome(
            False, found.state, case_id, manifest,
            f"no automatic action available: {found.detail}")

    expected = _inventory_from_json(manifest.files)
    source = Path(manifest.staging_source_path)
    partial_case = layout.partial_case(case_id)
    partial_payload = layout.partial_payload(case_id) / source.name

    if found.state == RECOVERY_COMPLETE:
        if manifest.state == STATE_PENDING_REVIEW:
            return RecoveryOutcome(
                False, found.state, case_id, manifest,
                "already published and already recorded as pending_review; "
                "nothing to reconcile.")
        previous = manifest.state
        manifest.state = STATE_PENDING_REVIEW
        manifest.staged_path = str(layout.pending_case(case_id))
        manifest.touch()
        manifest.write(layout.manifest(case_id))
        return RecoveryOutcome(
            True, found.state, case_id, manifest,
            f"payload verified; manifest state reconciled from {previous} to "
            f"{STATE_PENDING_REVIEW}.")

    if found.state == RECOVERY_RESUME_PUBLICATION:
        revalidate_source(source, expected)
        agrees, why = _payload_agrees(partial_payload, expected)
        if not agrees:
            return RecoveryOutcome(
                False, found.state, case_id, manifest,
                f"the staged copy stopped verifying between assessment and "
                f"publication: {why}")
        _publish(layout, manifest)
        if delete_copied_source:
            shutil.rmtree(source)
        return RecoveryOutcome(
            True, found.state, case_id, manifest,
            "published the already-verified staged copy without copying "
            "again.")

    if found.state == RECOVERY_PUBLISH_PARTIAL:
        agrees, why = _payload_agrees(partial_payload, expected)
        if not agrees:
            return RecoveryOutcome(
                False, found.state, case_id, manifest,
                f"the authoritative .partial stopped verifying between "
                f"assessment and publication: {why}")
        _publish(layout, manifest)
        return RecoveryOutcome(
            True, found.state, case_id, manifest,
            "published the authoritative .partial; the original source path "
            "was not required.")

    # RECOVERY_RESTART_FROM_SOURCE. The source is present and authoritative on
    # both paths here, which is what makes clearing .partial safe.
    revalidate_source(source, expected)

    if manifest.transfer_method == METHOD_RENAME:
        # Assessment reaches this only with no payload under .partial. Checked
        # again rather than trusted: this is the one place recovery deletes
        # anything on the path where .partial can be irreplaceable.
        if partial_payload.exists():
            return RecoveryOutcome(
                False, found.state, case_id, manifest,
                f"refusing to clear {partial_case}: a rename-path payload "
                "appeared between assessment and recovery.")
        if partial_case.exists():
            shutil.rmtree(partial_case)
        partial_payload.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, partial_payload)
        _publish(layout, manifest)
        return RecoveryOutcome(
            True, found.state, case_id, manifest,
            "cleared empty scaffolding and completed the rename.")

    if partial_case.exists():
        shutil.rmtree(partial_case)
    partial_payload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, partial_payload)
    _verify_staged_payload(partial_payload, expected)
    revalidate_source(source, expected)
    _publish(layout, manifest)
    if delete_copied_source:
        shutil.rmtree(source)
    return RecoveryOutcome(
        True, found.state, case_id, manifest,
        "discarded the unusable staged copy and recopied, verified, "
        "revalidated, and published from the authoritative source.")
