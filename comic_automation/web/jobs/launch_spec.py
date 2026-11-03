"""Internal reconstructed launch seals; no browser parameters or production adapter.

M4 owns the trusted registry which constructs these values. This module checks
the reconstructed command and hashes the interpreter, source and registry again
immediately before birth. The accepted check-to-import window remains open.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
from pathlib import Path
import re
from types import MappingProxyType
from typing import Callable, Mapping

from .store import JobRefused, _json


@dataclass(frozen=True)
class FileSeal:
    """One absolute trusted input and its SHA256, identified by registry role."""
    role: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class LaunchSpec:
    """Trusted registry output, never deserialized from an immutable command.

    The command contains only seal_document(), not executable launch authority.
    Only wait-for-natural-exit stop policy is implemented here; M4 must supply
    an accepted interruption gate before any user-requested stop is delivered.
    outcome_id binds the registry's mapper identity into the immutable seal.
    """
    executable: Path
    arguments: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    files: tuple[FileSeal, ...]
    outcome_id: str
    outcome_mapper: Callable[[int], str]
    stop_policy: str = "wait"

    def __post_init__(self):
        # Freeze the explicit environment so a registry-owned mutable dictionary
        # cannot change the native launch after its seal was checked.
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def seal_document(self) -> dict:
        """Describe the reconstructed launch exactly, without callable objects."""
        if (not isinstance(self.executable, Path) or not self.executable.is_absolute()
                or not isinstance(self.cwd, Path) or not self.cwd.is_absolute() or not self.cwd.is_dir()
                or not isinstance(self.arguments, tuple)
                or any(not isinstance(a, str) or "\0" in a for a in self.arguments)
                or not isinstance(self.environment, Mapping) or not self.environment
                or any(not isinstance(k, str) or not k or "=" in k or "\0" in k
                       or not isinstance(v, str) or "\0" in v for k, v in self.environment.items())
                or len({k.upper() for k in self.environment}) != len(self.environment)
                or not isinstance(self.files, tuple) or len(self.files) != 3
                or any(not isinstance(f, FileSeal) or not isinstance(f.path, Path)
                       or not f.path.is_absolute() or not isinstance(f.sha256, str)
                       or not re.fullmatch("[0-9a-f]{64}", f.sha256) for f in self.files)
                or {f.role for f in self.files} != {"interpreter", "source", "registry"}
                or next(f.path for f in self.files if f.role == "interpreter") != self.executable
                or not isinstance(self.outcome_id, str) or not 1 <= len(self.outcome_id) <= 256
                or not callable(self.outcome_mapper) or self.stop_policy != "wait"):
            raise JobRefused("INVALID_TRUSTED_LAUNCH_SPEC")
        return dict(version=1, executable=str(self.executable), arguments=list(self.arguments),
            cwd=str(self.cwd), environment=dict(self.environment), stop_policy=self.stop_policy,
            outcome_id=self.outcome_id, files=[dict(role=f.role, path=str(f.path), sha256=f.sha256)
                                             for f in sorted(self.files, key=lambda f: f.role)])

    def validate(self, command: dict) -> None:
        """Bind reconstruction to the ledger seal and rehash actual input bytes."""
        if _json(self.seal_document()) != _json(command["seal"]):
            raise JobRefused("RECONSTRUCTED_LAUNCH_CHANGED")
        for sealed in self.files:
            digest = hashlib.sha256()
            with sealed.path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sealed.sha256:
                raise JobRefused("LAUNCH_INPUT_CHANGED:" + sealed.role)


def reconstruct(command: dict, registry) -> LaunchSpec:
    """Require an internal registry dependency; JSON cannot name an import path."""
    if not callable(registry):
        raise JobRefused("TRUSTED_LAUNCH_REGISTRY_REQUIRED")
    spec = registry(copy.deepcopy(command))
    if not isinstance(spec, LaunchSpec):
        raise JobRefused("INVALID_TRUSTED_LAUNCH_SPEC")
    spec.validate(command)
    return spec
