"""No-replace arrival renames, including a two-step Windows case change.

The watcher holds its directory/series exclusion while calling this helper.
Native Windows rename refuses an occupied destination, including races after
the preliminary check. No replacement or copy fallback is used.
"""

from pathlib import Path
from uuid import uuid4


class CaseRenameRecoveryError(OSError):
    """The archive remains at retained_path after a failed case-rename rollback."""

    def __init__(self, retained_path: Path):
        self.retained_path = retained_path
        super().__init__(f"case rename rollback refused; archive retained at {retained_path}")


def rename_arrival(source: Path, target: Path) -> Path:
    """Rename without replacing another arrival, preserving recoverable bytes.

    An existing target is accepted only for a same-file case-only spelling.
    Such a rename passes through an unpredictable sibling name. If the final
    move fails, restore the source without replacement; a rollback collision
    leaves the owned temporary archive intact and exposes its actual path.
    """
    if source == target and source.name == target.name:
        return source
    case_only = source.name != target.name and source.name.casefold() == target.name.casefold()
    if target.exists() and (not case_only or not source.samefile(target)):
        raise FileExistsError(f"media destination already exists: {target}")
    if not case_only:
        return source.rename(target)
    temporary = source.with_name(f".media-case-{uuid4().hex}.tmp")
    source.rename(temporary)
    try:
        return temporary.rename(target)
    except OSError:
        try:
            temporary.rename(source)
        except OSError as rollback_error:
            raise CaseRenameRecoveryError(temporary) from rollback_error
        raise
