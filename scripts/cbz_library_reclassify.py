"""Plan and apply the reclassification of a retiring library into active ones.

Written for the Horrorsplat retirement: its contents split between Manga and
Graphic Novels on origin alone, with no adult determination involved.

Classification reuses scripts.cbz_routing rather than reimplementing origin
detection, so series moved by this migration land where the watcher would
independently have routed them. A second, subtly different notion of "Asian
origin" living in a migration script is how a library drifts out of agreement
with its own router.

Two subcommands:

  plan   read-only. Opens archives for their ComicInfo, writes nothing inside
         the library, and emits a plan JSON carrying a digest of the source
         tree it was computed against.

  apply  refuses to run unless the recomputed digest and the expected series
         count both still match the plan. Never overwrites and never deletes:
         a colliding file goes to quarantine, and the emptied source
         directories are left in place for the operator to retire.

Origin is a property of a series, not of a chapter, so a series counts as
Asian-origin when *any* sampled archive says so. A chapter that arrived
without ComicInfo should not dilute the evidence of one that has it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.cbz_routing import (  # noqa: E402
    RoutingConfig,
    build_context,
    is_terminal_sample,
    parse,
    resolve,
    sample_rank,
    series_key,
)

COMICINFO_FIELDS = (
    "Series", "LanguageISO", "Manga", "Genre", "Tags",
    "Publisher", "Imprint", "Format", "Web", "AgeRating",
)


@dataclass
class SeriesPlan:
    series: str
    dest_key: str
    reason: str
    reason_rule: str
    file_count: int
    total_bytes: int
    sampled: int
    with_comicinfo: int
    target_exists: bool
    target_dir: str
    canonical: str
    needs_review: bool
    # Union of ComicInfo seen across the sample, so the plan is reviewable on
    # its own evidence instead of requiring the archives to be reopened.
    evidence: dict = field(default_factory=dict)


# ---------------------------------------------------------------- reading

def read_comic_info(cbz_path: Path) -> dict[str, str]:
    """Best-effort ComicInfo extraction. Unreadable archives yield {}."""
    try:
        with zipfile.ZipFile(cbz_path, "r") as zf:
            names = {n.lower(): n for n in zf.namelist()}
            key = next(
                (k for k in names if k.rsplit("/", 1)[-1] == "comicinfo.xml"),
                None,
            )
            if key is None:
                return {}
            root = ElementTree.fromstring(
                zf.read(names[key]).decode("utf-8", errors="replace")
            )
    except (zipfile.BadZipFile, OSError, ElementTree.ParseError):
        return {}

    out: dict[str, str] = {}
    for name in COMICINFO_FIELDS:
        element = root.find(name)
        if element is not None and (element.text or "").strip():
            out[name] = element.text.strip()
    return out


def spread_sample(archives: list[Path], limit: int) -> list[Path]:
    """Spread the sample across *archives* rather than taking the first N.

    Metadata quality is rarely uniform: a series often has ComicInfo on recent
    chapters and none on the back catalogue, so reading only the first few can
    miss the evidence entirely.

    Takes an explicit collection because a caller does not always want every
    archive under a directory. The watcher in particular must sample only the
    files it is about to move: a mixed drop point holds loose archives beside
    nested series directories, and walking the parent would classify one
    series using another's metadata.
    """
    ordered = sorted(archives)
    if len(ordered) <= limit:
        return ordered
    step = len(ordered) / limit
    return [ordered[int(i * step)] for i in range(limit)]


def sample_archives(series_dir: Path, limit: int) -> list[Path]:
    """Spread the sample across every archive under *series_dir*."""
    return spread_sample(
        [p for p in series_dir.rglob("*.cbz") if p.is_file()], limit
    )


def tree_digest(source: Path) -> tuple[str, int, int]:
    """Fingerprint the source library: (digest, series count, file count).

    Cheap enough to recompute at apply time and specific enough to notice a
    series added, removed, renamed, or changed in size since the plan was
    written -- which is exactly when the plan's decisions stop being valid.
    """
    digest = hashlib.sha256()
    total_files = 0
    series_dirs = sorted(
        (p for p in source.iterdir() if p.is_dir()),
        key=lambda p: p.name.casefold(),
    )
    for series_dir in series_dirs:
        archives = sorted(p for p in series_dir.rglob("*.cbz") if p.is_file())
        total_files += len(archives)
        digest.update(series_dir.name.encode("utf-8", "replace"))
        digest.update(str(len(archives)).encode())
        digest.update(str(sum(a.stat().st_size for a in archives)).encode())
    return digest.hexdigest(), len(series_dirs), total_files


# ---------------------------------------------------------------- planning

def plan_series(
    series_dir: Path,
    cfg: RoutingConfig,
    source_name: str,
    sample_limit: int,
    active_roots: dict[str, Path],
) -> SeriesPlan:
    archives = sorted(p for p in series_dir.rglob("*.cbz") if p.is_file())
    sampled = sample_archives(series_dir, sample_limit)

    decision = None
    with_info = 0
    evidence: dict[str, str] = {}
    for archive in sampled:
        info = read_comic_info(archive)
        if info:
            with_info += 1
        for key, value in info.items():
            evidence.setdefault(key, value)
        # The series name must be passed, or resolve() never consults the
        # manual overrides -- they are keyed on it.
        #
        # route_unresolved=False at every resolution site here, including the
        # empty-directory fallback below. This tool reclassifies a library
        # that already exists: every series in it has a home, so diverting an
        # unclassified one to a review destination would invent a move rather
        # than plan one. The decision is still marked unresolved -- only its
        # physical handling differs.
        candidate = resolve(
            cfg,
            build_context(source_name, series_dir.name, info),
            series_name=series_dir.name,
            route_unresolved=False,
        )
        # Rank the samples: authoritative > strong > weak > none. A strong or
        # authoritative signal ends the search; a weak one is held only until
        # something better turns up, and a no-match only until anything
        # matches.
        #
        # Ranked on the decision's own evidence_strength, never on its
        # rule_name. Reading strength out of a display string made this
        # coupled to config naming, so renaming a rule silently changed which
        # sample won.
        if decision is None or sample_rank(candidate) > sample_rank(decision):
            decision = candidate
        if is_terminal_sample(candidate):
            break

    if decision is None:                        # empty directory
        decision = resolve(
            cfg, build_context(source_name, series_dir.name, {}),
            series_name=series_dir.name,
            route_unresolved=False,
        )

    canonical = decision.canonical_series or series_dir.name
    key = series_key(canonical)
    root = active_roots[decision.dest_key]

    existing = None
    if root.is_dir():
        existing = next(
            (c for c in root.iterdir()
             if c.is_dir() and series_key(c.name) == key),
            None,
        )

    return SeriesPlan(
        series=series_dir.name,
        dest_key=decision.dest_key,
        reason=decision.reason,
        reason_rule=decision.rule_name or "default",
        file_count=len(archives),
        total_bytes=sum(a.stat().st_size for a in archives),
        sampled=len(sampled),
        with_comicinfo=with_info,
        target_exists=existing is not None,
        target_dir=str(existing if existing is not None else root / canonical),
        canonical=canonical,
        evidence=evidence,
        # Flag what the operator should look at: decided by absence of
        # evidence, or decided only by the unreliable genre words. An
        # override carries no evidence either, but is a human decision and
        # needs no review.
        needs_review=(
            (decision.evidence_strength == "none"
             and not decision.authoritative and with_info == 0)
            or decision.evidence_strength == "weak"
        ),
    )


def build_origin_config(manga_root: str, graphic_novels_root: str,
                        routing_path: Path | None = None) -> RoutingConfig:
    """Origin rules only -- no adult signal, since this split ignores Comix.

    Prefers a real v2 routing config when supplied, so the migration
    classifies with exactly the signals the watcher will use rather than a
    parallel copy that can drift. Destinations are narrowed to the two this
    migration targets and any Comix rule is dropped.
    """
    if routing_path is not None:
        from scripts.cbz_routing import load as load_routing
        cfg = load_routing(routing_path)
        cfg.destinations = {"manga": manga_root,
                            "graphic_novels": graphic_novels_root}
        cfg.default_key = "graphic_novels"
        cfg.rules = [r for r in cfg.rules if r.get("dest") != "comix"]
        cfg.series_overrides = tuple(
            o for o in cfg.series_overrides if o.dest_key != "comix"
        )
        return cfg

    return parse({
        "version": 2,
        "destinations": {"manga": manga_root,
                         "graphic_novels": graphic_novels_root},
        "default": "graphic_novels",
        "lists": {"asian_languages": ["ja", "ko", "zh", "zh-Hans", "zh-Hant"]},
        "signals": {
            "asian_origin_strong": {
                "any": [
                    {"field": "comicinfo.LanguageISO", "in_list": "asian_languages"},
                    {"field": "comicinfo.Manga", "in": ["Yes", "YesAndRightToLeft"]},
                ]
            },
        },
        "rules": [
            {"name": "Asian origin (strong)", "when": "asian_origin_strong",
             "dest": "manga"},
        ],
    })


def cmd_plan(args: argparse.Namespace) -> None:
    source = Path(args.source)
    if not source.is_dir():
        raise SystemExit(f"source library not found: {source}")

    cfg = build_origin_config(
        args.manga, args.graphic_novels,
        Path(args.routing) if args.routing else None,
    )
    if args.routing:
        print(f"classifying with {args.routing}: {len(cfg.rules)} rule(s), "
              f"{len(cfg.series_overrides)} override(s)")

    active_roots = {"manga": Path(args.manga),
                    "graphic_novels": Path(args.graphic_novels)}
    for key, root in active_roots.items():
        if not root.is_dir():
            raise SystemExit(f"destination {key} does not exist: {root}")

    digest, series_count, file_count = tree_digest(source)
    all_dirs = sorted(p for p in source.iterdir() if p.is_dir())

    # A directory with no archives is not a series -- it is the shell left
    # behind by an earlier merge. Planning a move for it would create an
    # empty series folder in the destination, which Komga would then show.
    empty = [d for d in all_dirs if not any(d.rglob("*.cbz"))]
    series_dirs = [d for d in all_dirs if d not in empty]

    print(f"planning {len(series_dirs)} series / {file_count} files from {source}")
    print(f"source digest: {digest[:16]}...")
    if empty:
        print(f"skipping {len(empty)} empty directory(ies): nothing to move")
    print()

    plans = [plan_series(d, cfg, source.name, args.sample, active_roots)
             for d in series_dirs]

    by_dest: dict[str, list[SeriesPlan]] = {}
    for item in plans:
        by_dest.setdefault(item.dest_key, []).append(item)

    for dest_key in sorted(by_dest):
        rows = by_dest[dest_key]
        size = sum(r.total_bytes for r in rows) / 1e9
        files = sum(r.file_count for r in rows)
        print(f"== {dest_key}: {len(rows)} series, {files} files, {size:.1f} GB")
        for row in sorted(rows, key=lambda r: r.series.casefold()):
            flags = []
            if row.target_exists:
                flags.append("MERGE")
            if row.with_comicinfo == 0:
                flags.append("NO-METADATA")
            elif "weak" in row.reason_rule:
                flags.append("WEAK")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"   {row.series[:52]:<52} {row.file_count:>4}f{suffix}")
        print()

    review = [r for r in plans if r.needs_review]
    merges = [r for r in plans if r.target_exists]
    print(f"total: {len(plans)} series")
    print(f"  strong signal         : "
          f"{len([r for r in plans if 'strong' in r.reason_rule])}")
    print(f"  weak signal only      : "
          f"{len([r for r in plans if 'weak' in r.reason_rule])}")
    print(f"  needs review total    : {len(review)}")
    print(f"  target already exists : {len(merges)}  <- would merge")

    if review:
        print("\n=== rows to review before apply ===")
        for row in sorted(review, key=lambda r: r.series.casefold()):
            why = "no metadata" if row.with_comicinfo == 0 else "weak signal only"
            print(f"\n  {row.series}  ->  {row.dest_key}   "
                  f"({why}, {row.file_count} files)")
            if row.evidence:
                for key, value in sorted(row.evidence.items()):
                    print(f"      {key:<12} {value[:88]}")
            else:
                print("      (no ComicInfo in any sampled archive)")

    if args.out:
        Path(args.out).write_text(
            json.dumps({
                "source": str(source),
                "digest": digest,
                "series_count": series_count,
                "file_count": file_count,
                "destinations": {k: str(v) for k, v in active_roots.items()},
                "routing": args.routing,
                "series": [asdict(p) for p in plans],
            }, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")


# ---------------------------------------------------------------- applying

@dataclass
class ApplyStats:
    series_moved: int = 0
    series_merged: int = 0
    files_moved: int = 0
    files_quarantined: int = 0
    errors: int = 0


def _same_volume(a: Path, b: Path) -> bool:
    probe = b if b.exists() else b.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return a.stat().st_dev == probe.stat().st_dev


def apply_series(row: dict, source: Path, quarantine: Path,
                 stats: ApplyStats, dry_run: bool) -> None:
    src_dir = source / row["series"]
    dst_dir = Path(row["target_dir"])

    if not src_dir.is_dir():
        print(f"  SKIP {row['series']}: source directory is gone")
        stats.errors += 1
        return

    if not dst_dir.exists():
        # Whole-directory move. Same volume means this is a rename: fast and
        # not a copy that could half-finish.
        print(f"  MOVE {row['series']}  ->  {dst_dir}")
        if not dry_run:
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_dir), str(dst_dir))
        stats.series_moved += 1
        stats.files_moved += row["file_count"]
        return

    # Merge, file by file. Never overwrite: a collision is quarantined so the
    # incoming copy is preserved for inspection rather than silently dropped
    # or silently winning.
    print(f"  MERGE {row['series']}  ->  {dst_dir}")
    stats.series_merged += 1
    for item in sorted(src_dir.rglob("*")):
        if item.is_dir():
            continue
        relative = item.relative_to(src_dir)
        target = dst_dir / relative
        if target.exists():
            holding = quarantine / row["series"] / relative
            print(f"      COLLISION {relative} -> quarantine")
            if not dry_run:
                holding.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(holding))
            stats.files_quarantined += 1
        else:
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(target))
            stats.files_moved += 1


def cmd_apply(args: argparse.Namespace) -> None:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    source = Path(plan["source"])
    if not source.is_dir():
        raise SystemExit(f"source library not found: {source}")

    # Preflight. Report before acting, and refuse to act on a tree that has
    # moved since the plan was written.
    digest, series_count, file_count = tree_digest(source)
    print("preflight")
    print(f"  source          : {source}")
    print(f"  plan digest     : {plan['digest'][:16]}...")
    print(f"  current digest  : {digest[:16]}...")
    print(f"  plan series     : {plan['series_count']} / files {plan['file_count']}")
    print(f"  current series  : {series_count} / files {file_count}")

    if digest != plan["digest"]:
        raise SystemExit(
            "REFUSING: the source tree has changed since this plan was made. "
            "Re-run `plan` and review it again."
        )
    if args.expect_series is None:
        raise SystemExit("REFUSING: --expect-series is required.")
    if args.expect_series != series_count:
        raise SystemExit(
            f"REFUSING: --expect-series {args.expect_series} but found "
            f"{series_count}."
        )

    for key, root in plan["destinations"].items():
        if not Path(root).is_dir():
            raise SystemExit(f"REFUSING: destination {key} missing: {root}")

    rows = plan["series"]
    if args.skip_review:
        held = [r for r in rows if r["needs_review"]]
        rows = [r for r in rows if not r["needs_review"]]
        print(f"  holding back {len(held)} row(s) flagged for review")
    if args.limit:
        rows = rows[:args.limit]

    for row in rows:
        if not _same_volume(source, Path(row["target_dir"])):
            raise SystemExit(
                "REFUSING: cross-volume move for "
                f"{row['series']} -> {row['target_dir']}. This tool only "
                "renames within a volume; a copy path would need its own "
                "verification and is deliberately not implemented."
            )

    quarantine = Path(args.quarantine)
    dry_run = not args.confirm
    print(f"\n{'DRY RUN' if dry_run else 'APPLYING'}: {len(rows)} series"
          f"   quarantine={quarantine}\n")

    stats = ApplyStats()
    for row in rows:
        apply_series(row, source, quarantine, stats, dry_run)

    print("\npostflight")
    print(f"  series moved      : {stats.series_moved}")
    print(f"  series merged     : {stats.series_merged}")
    print(f"  files moved       : {stats.files_moved}")
    print(f"  files quarantined : {stats.files_quarantined}")
    print(f"  errors            : {stats.errors}")

    if not dry_run:
        _, remaining_series, remaining_files = tree_digest(source)
        print(f"  source remaining  : {remaining_series} series / "
              f"{remaining_files} files")
        print("\nSource directories are left in place, including emptied ones. "
              "Retiring the library is a separate, deliberate step.")
    else:
        print("\nNothing was changed. Re-run with --confirm to apply.")


def cmd_move(args: argparse.Namespace) -> None:
    """Relocate a single series between libraries.

    For acting on a manual decision -- a series_overrides pin, or a misfile
    spotted by eye -- without planning a whole library. Uses the same
    non-destructive merge as apply: nothing is overwritten, collisions go to
    quarantine.
    """
    from_root, to_root = Path(args.from_root), Path(args.to_root)
    for label, root in (("--from", from_root), ("--to", to_root)):
        if not root.is_dir():
            raise SystemExit(f"REFUSING: {label} does not exist: {root}")

    key = series_key(args.series)
    src_dir = next(
        (c for c in from_root.iterdir() if c.is_dir() and series_key(c.name) == key),
        None,
    )
    if src_dir is None:
        raise SystemExit(f"REFUSING: no series matching {args.series!r} in {from_root}")

    existing = next(
        (c for c in to_root.iterdir() if c.is_dir() and series_key(c.name) == key),
        None,
    )
    target = existing if existing is not None else to_root / src_dir.name
    if not _same_volume(from_root, target):
        raise SystemExit("REFUSING: cross-volume move is not implemented.")

    archives = [p for p in src_dir.rglob("*.cbz") if p.is_file()]
    print(f"  from : {src_dir}  ({len(archives)} archives)")
    print(f"  to   : {target}"
          f"{'  (merging into existing)' if existing is not None else ''}")

    row = {"series": src_dir.name, "target_dir": str(target),
           "file_count": len(archives)}
    stats = ApplyStats()
    dry_run = not args.confirm
    print(f"\n{'DRY RUN' if dry_run else 'MOVING'}\n")
    apply_series(row, from_root, Path(args.quarantine), stats, dry_run)

    print(f"\n  files moved       : {stats.files_moved}")
    print(f"  files quarantined : {stats.files_quarantined}")
    print(f"  errors            : {stats.errors}")
    if dry_run:
        print("\nNothing was changed. Re-run with --confirm to apply.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="classify a retiring library (read-only)")
    p.add_argument("--source", required=True)
    p.add_argument("--manga", required=True)
    p.add_argument("--graphic-novels", required=True)
    p.add_argument("--routing", help="v2 routing config to classify with")
    p.add_argument("--sample", type=int, default=5)
    p.add_argument("--out")
    p.set_defaults(func=cmd_plan)

    a = sub.add_parser("apply", help="execute a reviewed plan")
    a.add_argument("--plan", required=True)
    a.add_argument("--expect-series", type=int,
                   help="required: series count the plan was reviewed against")
    a.add_argument("--quarantine", required=True,
                   help="where colliding files go; never deleted")
    a.add_argument("--skip-review", action="store_true",
                   help="hold back rows flagged as needing review")
    a.add_argument("--limit", type=int, help="apply only the first N series")
    a.add_argument("--confirm", action="store_true",
                   help="actually move files; omit for a dry run")
    a.set_defaults(func=cmd_apply)

    m = sub.add_parser("move", help="relocate one series between libraries")
    m.add_argument("--series", required=True)
    m.add_argument("--from", dest="from_root", required=True)
    m.add_argument("--to", dest="to_root", required=True)
    m.add_argument("--quarantine", required=True)
    m.add_argument("--confirm", action="store_true")
    m.set_defaults(func=cmd_move)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
