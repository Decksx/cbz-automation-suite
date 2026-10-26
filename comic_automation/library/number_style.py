"""Shared, explicit media-number style selection for planner and watcher.

The registry is an operator-supplied JSON object with version 1 and a roots
array of {path, style} objects. No folder basename supplies media identity.
Physical ancestor identities make subfolder scans and drive/UNC aliases agree.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REGISTRY = Path(os.environ.get("CBZ_NUMBER_STYLE_REGISTRY", str(
    Path(__file__).resolve().parents[2] / "config" / "number-styles.json")))
STYLES = ("issue", "chapter")


class NumberStyleRefused(ValueError):
    """The selected media style cannot be established without guessing."""


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        raise NumberStyleRefused(f"unusable directory identity: {path}")
    return info.st_dev, info.st_ino


def _ancestors(path: Path) -> set[tuple[int, int]]:
    # Both chains matter: resolving a junction discards its access ancestry.
    # A final destination series directory may not exist yet; its existing
    # parent still establishes the registered root without creating anything.
    supplied = Path(os.path.abspath(path))
    identities = set()
    for chain in dict.fromkeys((supplied, supplied.resolve())):
        current = chain
        while True:
            try:
                current.stat()
                break
            except FileNotFoundError:
                if current.parent == current:
                    raise NumberStyleRefused(f"no existing ancestor: {path}")
                current = current.parent
        for ancestor in (current, *current.parents):
            identities.add(_identity(ancestor))
    return identities


@dataclass(frozen=True)
class StyleSelection:
    """Resolved style and sealed configuration evidence for one scope."""

    style: str
    source: str
    scope: str
    registry_path: str
    registry_sha256: str
    matched_root: str | None = None
    matched_identity: tuple[int, int] | None = None

    def as_dict(self) -> dict:
        """Return JSON provenance that the planner includes in its digest."""
        return dict(self.__dict__)


@dataclass(frozen=True)
class NumberStyleRegistry:
    """One immutable configuration read, shared by all decisions in a plan."""

    path: str
    sha256: str
    roots: tuple[tuple[Path, str], ...]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_REGISTRY) -> NumberStyleRegistry:
        """Read and validate the entire registry, retaining its exact hash."""
        target = Path(path).absolute()
        try:
            raw = target.read_bytes()
            document = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise NumberStyleRefused(f"cannot read number-style registry: {exc}") from exc
        if (not isinstance(document, dict) or set(document) != {"version", "roots"}
                or type(document["version"]) is not int or document["version"] != 1
                or not isinstance(document["roots"], list)):
            raise NumberStyleRefused("number-style registry requires version 1 and roots")
        roots = []
        for row in document["roots"]:
            if (not isinstance(row, dict) or set(row) != {"path", "style"}
                    or not isinstance(row["path"], str) or not row["path"]
                    or not Path(row["path"]).is_absolute() or row["style"] not in STYLES):
                raise NumberStyleRefused("registry roots require absolute path and issue/chapter style")
            roots.append((Path(row["path"]), row["style"]))
        return cls(str(target), hashlib.sha256(raw).hexdigest(), tuple(roots))

    def select(self, scope: str | Path, requested: str = "auto") -> StyleSelection:
        """Select an explicit override or exactly one physical registered root.

        Unknown, overlapping/nested (even same-style), inaccessible or identity-less
        roots refuse auto. Inaccessibility cannot prove that a second match
        is absent. Explicit overrides need no library filesystem access.
        """
        if requested not in ("auto", *STYLES):
            raise NumberStyleRefused(f"unknown number style: {requested}")
        matches = []
        if requested == "auto":
            try:
                ancestors = _ancestors(Path(scope))
                try:
                    scope_identity = _identity(Path(scope))
                except FileNotFoundError:
                    scope_identity = None
                for root, style in self.roots:
                    identity = _identity(root)
                    # Ancestor-only matching cannot assign one style to a
                    # parent scan that also contains a registered sub-library.
                    # Inspect physical access/resolved chains, never strings.
                    if (scope_identity is not None and identity != scope_identity
                            and scope_identity in _ancestors(root)):
                        raise NumberStyleRefused("nested registered root inside number-style scope")
                    if identity in ancestors:
                        matches.append((root, style, identity))
            except OSError as exc:
                raise NumberStyleRefused(f"cannot establish number-style root identity: {exc}") from exc
            if len(matches) != 1:
                raise NumberStyleRefused("auto number style needs exactly one registered root; use --number-style")
            requested = matches[0][1]
        return StyleSelection(requested, "registry" if matches else "explicit",
                              str(Path(scope).absolute()), self.path, self.sha256,
                              str(matches[0][0]) if matches else None,
                              matches[0][2] if matches else None)
