"""Promote staged series directories from a staging root into the library.

Since 2026-08-19 the watcher routes arrivals into `X:\\_staging\\...` instead of
straight into `X:\\Comix` and `X:\\Manga`, so ingest can no longer overwrite a
file the database has already inspected. That closed the drift source and left
a gap: nothing moves the staged archives onward. This is that step.

Why this is not `cbz_classification_staging.promote_case`
---------------------------------------------------------

That module solves a different problem well. It stages *one unresolved
arrival* as a review case with a manifest and a case-identity digest, and its
`promote_case()` deliberately **refuses when the destination already exists** --
"two series directories with the same name are not the same series, and merging
them is not a promotion."

That refusal is right for a review case and wrong here. Routine promotion is
almost entirely new chapters joining a series directory that already exists, so
a tool that refuses on an occupied destination would refuse nearly every time.
Staged arrivals also have no case id and no manifest, so `promote_case()` could
not be called on them even if the semantics fit.

What this does instead
----------------------

Merges each staged series directory into its library counterpart using the
watcher's own `_merge_directories`, so promotion and ingest resolve a filename
collision by exactly the same rule -- including the 10 KB minimum replacement
gain. One definition for one question, rather than a second merge implementation
that could drift from the first.

Read-only unless `--apply`. The dry run reports, per pair, how many archives are
new, how many collide, and -- the number worth reading before accepting -- **how
many existing library archives would be replaced**, because each of those
invalidates the recorded page inventory, archive hash, and content signature of
a file the database has already inspected. That cost is now visible before it is
paid rather than discovered by a worker weeks later.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.cbz_watcher import (  # noqa: E402
    REPLACEMENT_MIN_GAIN_BYTES,
    _find_archive_collision,
    _merge_directories,
    _replacement_gain_is_meaningful,
)

ARCHIVE_SUFFIXES = {".cbz", ".cbr"}


@dataclass
class SeriesPlan:
    """What promoting one staged series directory would do."""

    name: str
    source: Path
    destination: Path
    new_files: list[Path] = field(default_factory=list)
    replacing: list[tuple[Path, int, int]] = field(default_factory=list)
    kept_below_threshold: list[tuple[Path, int, int]] = field(
        default_factory=list
    )
    kept_not_larger: list[Path] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.new_files)
            + len(self.replacing)
            + len(self.kept_below_threshold)
            + len(self.kept_not_larger)
        )


def plan_series(source: Path, destination: Path) -> SeriesPlan:
    """Predict the outcome for one staged series directory. Read-only.

    Uses the same collision lookup and the same gain rule the merge itself
    uses, so the report cannot disagree with what `--apply` then does.
    """
    plan = SeriesPlan(name=source.name, source=source, destination=destination)

    for item in sorted(source.rglob("*")):
        if item.is_dir():
            continue

        target = destination / item.relative_to(source)

        if not target.exists() and item.suffix.lower() in ARCHIVE_SUFFIXES:
            match = _find_archive_collision(target.parent, item.stem)
            if match is not None:
                target = match

        if not target.exists():
            plan.new_files.append(item)
            continue

        incoming = item.stat().st_size
        existing = target.stat().st_size

        if _replacement_gain_is_meaningful(incoming, existing):
            plan.replacing.append((target, existing, incoming))
        elif incoming > existing:
            plan.kept_below_threshold.append((target, existing, incoming))
        else:
            plan.kept_not_larger.append(target)

    return plan


def staged_series(staging_root: Path) -> list[Path]:
    """Immediate child directories of *staging_root*, which are series dirs."""
    if not staging_root.exists():
        return []
    return sorted(
        child for child in staging_root.iterdir() if child.is_dir()
    )


def promote(
    staging_root: Path,
    library_root: Path,
    *,
    apply: bool = False,
    limit: int | None = None,
) -> list[SeriesPlan]:
    plans: list[SeriesPlan] = []

    for source in staged_series(staging_root):
        if limit is not None and len(plans) >= limit:
            break

        destination = library_root / source.name
        plan = plan_series(source, destination)
        plans.append(plan)

        if not apply:
            continue

        destination.mkdir(parents=True, exist_ok=True)
        _merge_directories(source, destination)

        # _merge_directories moves files but leaves the emptied source tree
        # behind. Remove it only when it really is empty: anything left is
        # something the merge chose not to move, and deleting that would
        # discard a file without a decision.
        _remove_if_empty(source)

    return plans


def _remove_if_empty(directory: Path) -> bool:
    for child in sorted(directory.iterdir(), reverse=True):
        if child.is_dir():
            _remove_if_empty(child)

    if not any(directory.iterdir()):
        directory.rmdir()
        return True
    return False


def render(plans: Sequence[SeriesPlan], *, applied: bool) -> str:
    lines: list[str] = []
    verb = "promoted" if applied else "would promote"

    new_total = sum(len(p.new_files) for p in plans)
    replace_total = sum(len(p.replacing) for p in plans)
    below_total = sum(len(p.kept_below_threshold) for p in plans)
    smaller_total = sum(len(p.kept_not_larger) for p in plans)

    for plan in plans:
        if not plan.total:
            continue
        lines.append("  %s  ->  %s" % (plan.name, plan.destination))
        lines.append(
            "      new %d | replacing %d | below threshold %d | not larger %d"
            % (
                len(plan.new_files),
                len(plan.replacing),
                len(plan.kept_below_threshold),
                len(plan.kept_not_larger),
            )
        )
        for target, existing, incoming in plan.replacing:
            lines.append(
                "        REPLACES %s (%s B -> %s B, +%s)"
                % (
                    target.name,
                    format(existing, ","),
                    format(incoming, ","),
                    format(incoming - existing, ","),
                )
            )

    lines.append("")
    lines.append("  series directories : %d" % len(plans))
    lines.append("  new archives       : %d  %s" % (new_total, verb))
    lines.append(
        "  replacing existing : %d  <- each invalidates recorded evidence"
        % replace_total
    )
    lines.append(
        "  kept, gain < %s B : %d" % (
            format(REPLACEMENT_MIN_GAIN_BYTES, ","), below_total)
    )
    lines.append("  kept, not larger   : %d" % smaller_total)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Promote staged series directories into the library, merging with "
            "the same collision rule the watcher uses. Read-only without "
            "--apply."
        )
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        metavar="STAGING=LIBRARY",
        help=(
            "Staging root and the library root it promotes into. Repeatable. "
            "Explicit rather than read from routing.json, because this tool "
            "moves files and the operator should see both ends in the command."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the promotion. Without it, nothing is moved.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Promote at most this many series directories per pair.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.pair:
        print("No --pair given; nothing to do.")
        return 2

    pairs: list[tuple[Path, Path]] = []
    for raw in args.pair:
        if "=" not in raw:
            print("Invalid --pair %r; expected STAGING=LIBRARY." % raw)
            return 2
        staging, library = raw.split("=", 1)
        pairs.append((Path(staging), Path(library)))

    for staging_root, library_root in pairs:
        print("=== %s -> %s ===" % (staging_root, library_root))
        if not staging_root.exists():
            print("  staging root does not exist; skipping.")
            continue

        plans = promote(
            staging_root, library_root, apply=args.apply, limit=args.limit
        )

        if not plans:
            print("  nothing staged.")
            continue

        print(render(plans, applied=args.apply))
        print()

    if not args.apply:
        print("Read-only. Re-run with --apply to move these files.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
