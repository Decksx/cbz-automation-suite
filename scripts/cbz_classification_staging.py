"""Review-case mechanics for archives that classification could not resolve.

This module is deliberately inert: it computes case identity, builds
manifests, chooses a transfer method, and plans. It moves nothing, and no
watcher call site consumes it yet. Separating the filesystem safety protocol
from concurrent watcher control flow keeps both reviewable.

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
