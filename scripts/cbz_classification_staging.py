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
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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


def build_inventory(root: Path) -> PayloadInventory:
    """Hash every file under *root*, ordered by relative path.

    Sorted by the relative path rather than by enumeration order: two hosts
    walking the same tree must produce the same inventory, and os.scandir
    order is not a promise.
    """
    if not root.is_dir():
        raise StagingError(f"payload root is not a directory: {root}")
    files = sorted((p for p in root.rglob("*") if p.is_file()),
                   key=lambda p: _relative_posix(p, root))
    return PayloadInventory(tuple(
        FileEntry(_relative_posix(p, root), p.stat().st_size, file_sha256(p))
        for p in files
    ))


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
        raw = json.loads(text)
        contract = raw.get("contract")
        if contract != CASE_CONTRACT:
            raise StagingError(
                f"manifest contract {contract!r} is not {CASE_CONTRACT!r}; "
                "refusing to interpret a case minted under another scheme"
            )
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            raise StagingError(f"manifest has unknown fields: {sorted(unknown)}")
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


# ------------------------------------------------------------- planning

@dataclass(frozen=True)
class StagingPlan:
    """What staging one case would do. Nothing has happened yet."""

    manifest: CaseManifest
    layout: StagingLayout
    inventory: PayloadInventory
    existing_case: bool
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
            f"case {m.case_id[:16]}...  ({'existing' if self.existing_case else 'new'})",
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
        existing_case=layout.manifest(case_id).exists()
        or layout.pending_case(case_id).exists(),
        free_bytes=free,
    )


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe
