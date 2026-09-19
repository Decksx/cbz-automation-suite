"""One-shot, non-destructive CBZ batch interface to Camelia's shared pipeline.

The watcher remains responsible for live imports. This standalone tool reads a
chosen CBZ or folder, checks Camelia's eligibility rules, and asks Camelia to
write tagged results into a separate Comix output tree. No model logic or CBZ
rebuilding is duplicated here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAMELIA_ROOT = Path(os.environ.get("CBZ_CAMELIA_ROOT", r"C:\git\camelia"))
DEFAULT_CAMELIA_PYTHON = Path(os.environ.get(
    "CBZ_CAMELIA_PYTHON", r"C:\ProgramData\miniconda3\envs\camelia_env\python.exe"
))
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "decensored" / "Comix"
MODEL_TYPES = ("black_bars", "transparent_black", "white_bars", "mosaic")
MAX_BATCH_ARCHIVES = 1000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Decensor CBZs with Camelia without changing originals")
    parser.add_argument("source", type=Path, help="One CBZ file or a folder containing CBZs")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT,
                        help="Separate Comix output root; existing files are never overwritten")
    parser.add_argument("--model-type", action="append", choices=MODEL_TYPES,
                        help="Processing stage; repeat in the desired order (default: all four)")
    parser.add_argument("--reprocess", action="store_true",
                        help="Run selected methods again even when already recorded in ComicInfo tags")
    parser.add_argument("--resume", action="store_true",
                        help=("Compatibility flag: matching existing outputs are always "
                              "verified and skipped"))
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating output or running models")
    parser.add_argument("--camelia-root", type=Path, default=DEFAULT_CAMELIA_ROOT)
    parser.add_argument("--camelia-python", type=Path, default=DEFAULT_CAMELIA_PYTHON)
    return parser


def discover_archives(source: Path) -> list[Path]:
    if source.is_symlink():
        raise ValueError(f"Symbolic-link sources are not supported: {source}")
    source = source.resolve(strict=True)
    if source.is_file():
        if source.suffix.casefold() != ".cbz":
            raise ValueError(f"Source file must be a CBZ archive: {source}")
        return [source]
    if not source.is_dir():
        raise ValueError(f"Source is not a CBZ file or folder: {source}")

    archives = sorted(
        (path for path in source.rglob("*") if path.is_file()
         and not path.is_symlink() and path.suffix.casefold() == ".cbz"),
        key=lambda path: str(path.relative_to(source)).casefold(),
    )
    if not archives:
        raise ValueError(f"No CBZ archives found under {source}")
    if len(archives) > MAX_BATCH_ARCHIVES:
        raise ValueError(
            f"Found more than {MAX_BATCH_ARCHIVES} CBZs; use Camelia's resumable library backlog instead"
        )
    return archives


def _is_within(path: Path, directory: Path) -> bool:
    try:
        common = os.path.commonpath((str(path), str(directory)))
        return os.path.normcase(common) == os.path.normcase(str(directory))
    except ValueError:  # Different Windows drives.
        return False


def validate_output_root(source: Path, output_root: Path) -> Path:
    source = source.resolve(strict=True)
    output_root = output_root.expanduser().resolve()
    if not any(part.casefold() == "comix" for part in output_root.parts):
        raise ValueError(f"Output must be under a directory named Comix: {output_root}")
    if source.is_dir() and _is_within(output_root, source):
        raise ValueError("Output directory cannot be inside the selected source folder")
    return output_root


def load_camelia_inspector(camelia_root: Path) -> Callable[[str], dict]:
    """Reuse Camelia's ZIP/ComicInfo eligibility decision without its web API."""
    module_path = camelia_root / "library_backlog.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"Camelia eligibility module was not found: {module_path}")
    spec = importlib.util.spec_from_file_location("camelia_library_backlog", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Camelia eligibility module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.inspect_cbz_eligibility


def methods_to_run(inspection: dict, sequence: list[str], reprocess: bool = False) -> list[str]:
    if reprocess:
        return sequence
    applied = set(inspection.get("applied_methods") or ())
    if applied:
        return [method for method in sequence if method not in applied]
    return [] if inspection["already_processed"] else sequence


def _comicinfo_identity(payload: bytes) -> tuple:
    """Compare source metadata while allowing Camelia to add method tags."""
    root = ElementTree.fromstring(payload)
    return tuple(
        (element.tag, tuple(sorted(element.attrib.items())), (element.text or "").strip())
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].casefold() != "tags"
    )


def _member_identity(member: zipfile.ZipInfo) -> tuple:
    return (
        member.filename, member.date_time, member.compress_type, member.comment,
        member.extra, member.create_system, member.create_version,
        member.extract_version, member.flag_bits & ~0x08, member.volume,
        member.internal_attr, member.external_attr,
    )


def verify_completed_output(
    archive: Path, destination: Path, sequence: list[str], inspector: Callable[[str], dict],
) -> None:
    """Accept an existing copy only when its tags and source manifest agree."""
    try:
        inspection = inspector(str(destination))
        if not inspection.get("already_processed"):
            raise ValueError("uncensored marker is missing")
        missing = set(sequence) - set(inspection.get("applied_methods") or ())
        if missing:
            raise ValueError(f"method tags are missing: {', '.join(sorted(missing))}")

        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(destination) as output:
            if source.testzip() is not None or output.testzip() is not None:
                raise ValueError("archive CRC check failed")
            before, after = source.infolist(), output.infolist()
            source_has_comicinfo = any(
                member.filename.replace("\\", "/").rsplit("/", 1)[-1].casefold() == "comicinfo.xml"
                for member in before
            )
            if not source_has_comicinfo:
                if not after or after[-1].filename != "ComicInfo.xml":
                    raise ValueError("generated ComicInfo.xml is missing or misplaced")
                after = after[:-1]
            if source.comment != output.comment or len(before) != len(after):
                raise ValueError("archive comment or member count differs")
            for original, processed in zip(before, after):
                if _member_identity(original) != _member_identity(processed):
                    raise ValueError(f"member manifest differs: {original.filename!r}")
                if original.is_dir():
                    continue
                name = original.filename.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                if name == "comicinfo.xml":
                    if _comicinfo_identity(source.read(original)) != _comicinfo_identity(output.read(processed)):
                        raise ValueError("ComicInfo metadata differs beyond method tags")
                elif original.filename.rsplit(".", 1)[-1].casefold() in {"png", "jpg", "jpeg", "webp", "avif"}:
                    with output.open(processed) as page, Image.open(page) as image:
                        image.verify()
                elif source.read(original) != output.read(processed):
                    raise ValueError(f"non-image member changed: {original.filename!r}")
    except Exception as exc:
        raise FileExistsError(
            f"Refusing to resume existing output {destination}: {type(exc).__name__}: {exc}"
        ) from exc


def plan_batch(source: Path, output_root: Path, inspector: Callable[[str], dict],
               sequence: list[str] | None = None, reprocess: bool = False,
               resume: bool = False) -> tuple[list[tuple[Path, Path]], int, int]:
    source = source.resolve(strict=True)
    output_root = validate_output_root(source, output_root)
    planned = []
    skipped = 0
    resumed = 0
    sequence = sequence or list(MODEL_TYPES)
    for archive in discover_archives(source):
        inspection = inspector(str(archive))
        if inspection.get("reason") == "Archive could not be inspected":
            raise ValueError(f"Cannot inspect {archive}: {inspection.get('parse_error')}")
        pending = methods_to_run(inspection, sequence, reprocess)
        if not pending:
            print(f"SKIP methods already recorded or legacy marker: {archive} ({inspection['reason']})", flush=True)
            skipped += 1
            continue
        if not inspection["eligible"] and not inspection["already_processed"]:
            raise ValueError(f"Cannot inspect {archive}: {inspection['reason']} ({inspection.get('parse_error')})")
        if inspection.get("parse_error"):
            print(f"WARNING metadata on {archive}: {inspection['parse_error']}", flush=True)
        relative = Path(archive.name) if source.is_file() else archive.relative_to(source)
        destination = output_root / relative
        if destination.exists():
            if not inspection["already_processed"]:
                verify_completed_output(archive, destination, pending, inspector)
                print(f"SKIP verified existing output: {destination}", flush=True)
                resumed += 1
                continue
            destination = output_root / "_additional_methods" / "+".join(pending) / relative
            if destination.exists():
                verify_completed_output(archive, destination, pending, inspector)
                print(f"SKIP verified existing output: {destination}", flush=True)
                resumed += 1
                continue
        planned.append((archive, destination))
    return planned, skipped, resumed


def build_camelia_command(
    archive: Path, destination: Path, sequence: list[str],
    camelia_root: Path, camelia_python: Path, reprocess: bool = False,
) -> list[str]:
    return [
        str(camelia_python), str(camelia_root / "scripts" / "process_cbz.py"), str(archive),
        "--copy", "--output-dir", str(destination.parent),
        *(item for stage in sequence for item in ("--model-type", stage)),
        *(["--reprocess"] if reprocess else []),
    ]


def run_camelia_book(
    archive: Path, destination: Path, sequence: list[str],
    camelia_root: Path, camelia_python: Path, inspector: Callable[[str], dict],
    reprocess: bool = False,
) -> None:
    command = build_camelia_command(archive, destination, sequence, camelia_root, camelia_python, reprocess)
    result = None
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "UTF-8"
    child_env["PYTHONUNBUFFERED"] = "1"
    with subprocess.Popen(
        command, cwd=camelia_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env,
    ) as child:
        for line in child.stdout:
            line = line.rstrip()
            if line.startswith("CAMELIA_RESULT="):
                result = json.loads(line.partition("=")[2])
            elif line:
                print(line, flush=True)
        return_code = child.wait()
    if return_code != 0:
        raise RuntimeError(f"Camelia failed for {archive.name} (exit code {return_code})")
    if not isinstance(result, dict) or Path(result.get("path", "")).resolve() != destination.resolve():
        raise RuntimeError(f"Camelia returned an unexpected output for {archive.name}")
    if result.get("original_deleted") or result.get("backup_path"):
        raise RuntimeError(f"Camelia unexpectedly modified the source for {archive.name}")
    output_inspection = inspector(str(destination)) if destination.is_file() else {}
    if not output_inspection.get("already_processed"):
        raise RuntimeError(f"Output is missing its uncensored marker: {destination}")
    if "applied_methods" in output_inspection and not set(sequence).issubset(output_inspection["applied_methods"]):
        raise RuntimeError(f"Output is missing completed method tags: {destination}")


def run_batch(args: argparse.Namespace, inspector: Callable[[str], dict] | None = None) -> int:
    source_argument = args.source.expanduser()
    if source_argument.is_symlink():
        raise ValueError(f"Symbolic-link sources are not supported: {source_argument}")
    source = source_argument.resolve(strict=True)
    output_root = validate_output_root(source, args.output_dir)
    camelia_root = args.camelia_root.expanduser().resolve()
    inspector = inspector or load_camelia_inspector(camelia_root)
    sequence = list(dict.fromkeys(args.model_type or MODEL_TYPES))
    reprocess = getattr(args, "reprocess", False)
    resume = getattr(args, "resume", False)
    if resume and reprocess:
        raise ValueError("--resume and --reprocess cannot be used together")
    planned, skipped, resumed = plan_batch(source, output_root, inspector, sequence, reprocess, resume)
    print(f"Camelia stages: {' -> '.join(sequence)}", flush=True)
    print(f"Source: {source}", flush=True)
    print(f"Output: {output_root}", flush=True)
    print(f"Books to process: {len(planned)}; already marked: {skipped}; verified outputs: {resumed}", flush=True)
    if args.dry_run:
        for archive, destination in planned:
            print(f"DRY RUN {archive} -> {destination}", flush=True)
        return 0

    if not planned:
        print(f"Completed 0 book(s); skipped {skipped} already marked book(s) and {resumed} verified output(s)", flush=True)
        return 0

    cli = camelia_root / "scripts" / "process_cbz.py"
    if not cli.is_file():
        raise FileNotFoundError(f"Camelia CLI was not found: {cli}")
    camelia_python = args.camelia_python.expanduser().resolve()
    if not camelia_python.is_file():
        raise FileNotFoundError(f"Camelia Python was not found: {camelia_python}")
    print(f"CBZ_PROGRESS 0/{len(planned)} 0% books", flush=True)
    completed_count = 0
    skipped_waiting = 0
    for index, (archive, destination) in enumerate(planned, start=1):
        book_sequence = methods_to_run(inspector(str(archive)), sequence, reprocess)
        if not book_sequence:
            print(f"SKIP methods completed while batch was waiting: {archive}", flush=True)
            skipped_waiting += 1
            print(f"CBZ_PROGRESS {index}/{len(planned)} {int(index * 100 / len(planned))}% books", flush=True)
            continue
        # Recheck immediately before each expensive run: another process may
        # have created the target after the batch preflight.
        if destination.exists():
            verify_completed_output(archive, destination, book_sequence, inspector)
            print(f"SKIP verified output created during batch: {destination}", flush=True)
            resumed += 1
            print(f"CBZ_PROGRESS {index}/{len(planned)} {int(index * 100 / len(planned))}% books", flush=True)
            continue
        print(f"BOOK {index}/{len(planned)}: {archive}", flush=True)
        run_camelia_book(archive, destination, book_sequence, camelia_root, camelia_python, inspector, reprocess)
        completed_count += 1
        print(f"DONE {destination}", flush=True)
        print(f"CBZ_PROGRESS {index}/{len(planned)} {int(index * 100 / len(planned))}% books", flush=True)
    print(f"Completed {completed_count} book(s); skipped {skipped + skipped_waiting} already marked book(s) and {resumed} verified output(s)", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        return run_batch(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Camelia batch failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
