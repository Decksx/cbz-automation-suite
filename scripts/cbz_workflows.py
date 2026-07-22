"""Unified series and library-maintenance workflows for the CBZ suite."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_PROPOSAL = REPO_ROOT / "Logs" / "series_proposal.json"

SERIES_STAGES = ("organize", "stage", "review", "compilations")
MAINTENANCE_STAGES = ("sanitize", "archive", "organize", "metadata", "names")
DEFAULT_MAINTENANCE_STAGES = ("sanitize", "archive", "organize", "metadata", "names")


def _parse_stages(raw: str, valid: tuple[str, ...], default: tuple[str, ...]) -> list[str]:
    selected = [value.strip().lower() for value in raw.split(",") if value.strip()]
    if not selected:
        return list(default)
    unknown = sorted(set(selected) - set(valid))
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown stage(s): {', '.join(unknown)}; valid: {', '.join(valid)}"
        )
    return [stage for stage in valid if stage in selected]


def _python_command(script: str, *args: str) -> list[str]:
    return [sys.executable, str(SCRIPT_DIR / script), *args]


def build_series_commands(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    stages = set(args.stages)
    commands: list[tuple[str, list[str]]] = []

    if stages & {"organize", "stage"}:
        command = _python_command(
            "cbz_library_maintenance.py",
            "organize-series",
            str(args.root),
            "--workers",
            str(args.workers),
        )
        if args.dry_run:
            command.append("--dry-run")
        if not args.metadata_dedupe:
            command.append("--no-metadata-dedupe")
        if args.uncensored_check:
            command.extend(["--uncensored-check", "--move-which", args.move_which])
        if "stage" in stages:
            command.extend(
                [
                    "--possible-series-check",
                    "--series-common-words",
                    str(args.series_common_words),
                    "--series-min-group-size",
                    str(args.series_min_group_size),
                ]
            )
        commands.append(("Organize and stage series", command))

    if "review" in stages:
        commands.append(
            (
                "Generate series review proposal",
                _python_command(
                    "cbz_library_maintenance.py",
                    "propose-series",
                    str(args.root),
                    f"--out={args.out}",
                    "--series-common-words",
                    str(args.series_common_words),
                    "--series-min-group-size",
                    str(args.series_min_group_size),
                ),
            )
        )

    if "compilations" in stages:
        command = _python_command(
            "cbz_compilation_resolver.py",
            str(args.root),
            "--workers",
            str(args.workers),
        )
        if args.dry_run:
            command.append("--dry-run")
        commands.append(("Resolve compilation overlaps", command))

    return commands


def build_maintenance_commands(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    for stage in args.stages:
        if stage == "sanitize":
            command = _python_command(
                "cbz_sanitizer.py",
                f"--scan={args.root}",
                f"--sort={args.sort}",
                "--workers",
                str(args.workers),
            )
            if args.dry_run:
                command.append("--dry-run")
            if args.full_rescan:
                command.append("--full")
            if args.restart:
                command.append("--restart")
            if args.rules:
                command.append(f"--rules={','.join(args.rules)}")
            commands.append(("Sanitize CBZ files", command))
            continue

        subcommand = {
            "archive": "archive-clean",
            "organize": "organize-series",
            "metadata": "metadata",
            "names": "repair-names",
        }[stage]
        command = _python_command(
            "cbz_library_maintenance.py",
            subcommand,
            str(args.root),
        )
        if args.dry_run:
            command.append("--dry-run")
        if stage != "names":
            command.extend(["--workers", str(args.workers)])
        if stage in {"archive", "organize"} and not args.metadata_dedupe:
            command.append("--no-metadata-dedupe")
        if stage == "organize" and args.uncensored_check:
            command.extend(["--uncensored-check", "--move-which", args.move_which])
        if stage == "names" and args.names_only:
            command.append("--names-only")
        commands.append((f"Run {subcommand}", command))

    return commands


def run_commands(commands: list[tuple[str, list[str]]]) -> int:
    if not commands:
        print("ERROR: No workflow stages selected.", flush=True)
        return 2
    for index, (label, command) in enumerate(commands, start=1):
        print(f"\n=== Step {index}/{len(commands)}: {label} ===", flush=True)
        result = subprocess.run(command, cwd=REPO_ROOT)
        if result.returncode:
            print(
                f"ERROR: Workflow stopped because '{label}' exited "
                f"with code {result.returncode}.",
                flush=True,
            )
            return result.returncode
    print("\nWorkflow complete.", flush=True)
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("root", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-metadata-dedupe",
        dest="metadata_dedupe",
        action="store_false",
        default=True,
    )
    parser.add_argument("--uncensored-check", action="store_true")
    parser.add_argument("--move", "--move-which", dest="move_which", default="both")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="workflow", required=True)

    series = subparsers.add_parser(
        "series",
        help="Organize, stage, review, and resolve compilation overlaps.",
    )
    _add_common(series)
    series.add_argument(
        "--stages",
        type=lambda value: _parse_stages(value, SERIES_STAGES, ("organize",)),
        default=["organize"],
    )
    series.add_argument("--series-common-words", type=int, default=1)
    series.add_argument("--series-min-group-size", type=int, default=2)
    series.add_argument("--out", type=Path, default=DEFAULT_PROPOSAL)
    series.set_defaults(command_builder=build_series_commands)

    maintenance = subparsers.add_parser(
        "maintenance",
        help="Sanitize, clean archives, organize, repair metadata, and repair names.",
    )
    _add_common(maintenance)
    maintenance.add_argument(
        "--stages",
        type=lambda value: _parse_stages(
            value, MAINTENANCE_STAGES, DEFAULT_MAINTENANCE_STAGES
        ),
        default=list(DEFAULT_MAINTENANCE_STAGES),
    )
    maintenance.add_argument(
        "--rules",
        type=lambda value: [item for item in value.split(",") if item],
        default=[],
    )
    maintenance.add_argument(
        "--sort",
        choices=("newest", "oldest", "alpha", "alpha-reverse"),
        default="newest",
    )
    maintenance.add_argument("--full", dest="full_rescan", action="store_true")
    maintenance.add_argument("--restart", action="store_true")
    maintenance.add_argument("--names-only", action="store_true")
    maintenance.set_defaults(command_builder=build_maintenance_commands)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.root.exists() or not args.root.is_dir():
        print(f"ERROR: Library folder not found: {args.root}", flush=True)
        return 2
    return run_commands(args.command_builder(args))


if __name__ == "__main__":
    raise SystemExit(main())
