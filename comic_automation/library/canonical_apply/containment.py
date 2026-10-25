"""Physical containment of every path a canonical-naming run touches.

The plan's checks (`plan.parse_plan`) are lexical: they prove that a path
*names* a place under the library root. They cannot prove that the bytes
end up there. A junction or symlink at any component between the root and
the target (``<root>\\.cbz-canonical-quarantine`` pointing at a sibling
directory, say) sends an ``os.rename`` outside the library while every
string still looks contained, and ``st_dev`` equality does not help when
the outside directory is on the same volume (PR #128 review).

So every source, destination, temporary name, quarantine path and piece
of run state (lock, owner file, journal directory) is checked physically
by `require_contained`:

1. lexically under the root (`relative_parts`, case-insensitive, UNC
   aware);
2. **no reparse point** on any existing component from just below the
   root down to the target -- a symlink, a junction, or anything else
   carrying ``FILE_ATTRIBUTE_REPARSE_POINT``. Components that do not exist
   yet (a destination, a quarantine directory still to be created) end
   the walk: nothing below a missing component can exist;
3. the physically resolved target (``os.path.realpath``) is under the
   physically resolved root.

The root itself may be reached through a mapping or a link (a mapped
drive, a UNC share); only what lies below it is constrained. Both
filesystem calls are injectable so the logic can be exercised on
UNC-shaped paths without touching a share.

The check is repeated immediately before each ``os.rename`` and before
the quarantine directory is created, so a reparse point introduced after
the run's initial check (during a lock wait, say) still refuses before
anything moves. A window remains between that final check and the rename
itself; closing it would need handle-relative renames, which this
prototype does not attempt (step 7 note 21).
"""

from __future__ import annotations

import ntpath
import os
import stat
from collections.abc import Callable

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ContainmentError(RuntimeError):
    """A path is not physically inside the library root."""


def _key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def relative_parts(root: str, target: str) -> tuple[str, ...]:
    """The components of ``target`` below ``root``, or `ContainmentError`.

    Windows path semantics whatever the host: case-insensitive, ``/`` and
    ``\\`` equivalent, and a UNC ``\\\\server\\share`` treated as a drive, so
    a target on another share is never "under" the root.
    """
    root_n, target_n = ntpath.normpath(root), ntpath.normpath(target)
    try:
        relative = ntpath.relpath(target_n, root_n)
    except ValueError as exc:
        raise ContainmentError(f"{target} is not on the library root's drive or share {root}") from exc
    if relative == "." or relative == ".." or relative.startswith("..\\") or ntpath.isabs(relative):
        raise ContainmentError(f"{target} is not under the library root {root}")
    return tuple(relative.split("\\"))


def is_reparse(status: os.stat_result) -> bool:
    """A symlink, junction or any other reparse point."""
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def require_contained(
    root: str,
    target: str,
    *,
    lstat: Callable[[str], os.stat_result] = os.lstat,
    realpath: Callable[[str], str] = os.path.realpath,
) -> None:
    """Refuse unless ``target`` is physically inside ``root`` (module docstring)."""
    parts = relative_parts(root, target)
    current = ntpath.normpath(root)
    for part in parts:
        current = ntpath.join(current, part)
        try:
            status = lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            break
        except OSError as exc:
            raise ContainmentError(f"{current} cannot be inspected: {exc}") from exc
        if is_reparse(status):
            raise ContainmentError(
                f"{current} is a reparse point (junction or symlink) between the "
                f"library root and {target}; refusing to traverse it"
            )
    try:
        relative_parts(realpath(root), realpath(target))
    except ContainmentError as exc:
        raise ContainmentError(
            f"{target} resolves outside the physically resolved library root: {exc}"
        ) from exc


def same_root(first: str, second: str, *, realpath: Callable[[str], str] = os.path.realpath) -> bool:
    """True when two root spellings resolve to the same physical directory."""
    return _key(realpath(first)) == _key(realpath(second))
