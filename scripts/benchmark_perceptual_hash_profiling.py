#!/usr/bin/env python3
"""Measure optional perceptual-hash profiling overhead and phases."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import shutil
import statistics
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, __version__ as pillow_version
from PIL import features


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comic_automation.archive.page_hashing import (
    ArchivePageHashRepository,
    calculate_page_hashes,
)
from comic_automation.archive.perceptual_hash_cli import (
    run_perceptual_hashing,
)
from comic_automation.archive.perceptual_hashing import perceptual_hash
from comic_automation.database.connection import database_connection
from comic_automation.database.migrations import apply_migrations


MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "comic_automation"
    / "database"
    / "migrations"
)


def _image_payload(
    *,
    image_format: str,
    size: tuple[int, int],
    seed: int,
) -> bytes:
    width, height = size
    image = Image.new(
        "RGBA",
        size,
        (
            (seed * 29) % 256,
            (seed * 53) % 256,
            (seed * 71) % 256,
            255,
        ),
    )
    drawing = ImageDraw.Draw(image)
    drawing.rectangle(
        (
            width // 12,
            height // 10,
            max(width // 2, 1),
            max(height * 4 // 5, 1),
        ),
        fill=(17, 43, 91, 255),
    )
    drawing.ellipse(
        (
            width // 2,
            height // 7,
            max(width * 11 // 12, 1),
            max(height * 5 // 7, 1),
        ),
        fill=(201, 71, 39, 181),
    )
    drawing.line(
        (0, height - 1, width - 1, 0),
        fill=(52, 173, 112, 255),
        width=max(1, min(width, height) // 24),
    )

    if image_format in {"JPEG", "WEBP"}:
        encoded_image = image.convert("RGB")
    elif image_format == "GIF":
        encoded_image = image.convert(
            "P",
            palette=Image.Palette.ADAPTIVE,
            colors=64,
        )
    elif image_format == "TIFF":
        encoded_image = image.convert("L")
    else:
        encoded_image = image

    output = BytesIO()
    save_options: dict[str, object] = {}
    if image_format == "JPEG":
        save_options["quality"] = 88
    elif image_format == "WEBP":
        save_options["lossless"] = True

    encoded_image.save(
        output,
        format=image_format,
        **save_options,
    )
    return output.getvalue()


def _create_archives(
    root: Path,
    *,
    archive_count: int,
    pages_per_archive: int,
) -> list[Path]:
    formats = ["PNG", "JPEG", "GIF", "TIFF"]
    if features.check("webp"):
        formats.append("WEBP")
    extensions = {
        "PNG": ".png",
        "JPEG": ".jpg",
        "GIF": ".gif",
        "TIFF": ".tiff",
        "WEBP": ".webp",
    }
    archives: list[Path] = []

    for archive_index in range(archive_count):
        archive_path = root / f"sample-{archive_index:03}.cbz"
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for page_index in range(pages_per_archive):
                image_format = formats[
                    (archive_index + page_index) % len(formats)
                ]
                width = 96 + (
                    (archive_index * 37 + page_index * 53) % 5
                ) * 64
                height = 128 + (
                    (archive_index * 41 + page_index * 47) % 5
                ) * 96
                if (archive_index + page_index) % 9 == 0:
                    width, height = height * 2, width // 2

                payload = _image_payload(
                    image_format=image_format,
                    size=(max(width, 16), max(height, 16)),
                    seed=(archive_index * pages_per_archive)
                    + page_index
                    + 1,
                )
                archive.writestr(
                    (
                        f"{page_index + 1:03}"
                        f"{extensions[image_format]}"
                    ),
                    payload,
                )
        archives.append(archive_path)

    return archives


def _seed_database(
    database: Path,
    archives: list[Path],
) -> None:
    with database_connection(database) as connection:
        apply_migrations(connection, MIGRATIONS)
        repository = ArchivePageHashRepository(connection)

        for archive_path in archives:
            stat = archive_path.stat()
            archive = connection.execute(
                """
                INSERT INTO archive_files (file_size)
                VALUES (?)
                """,
                (stat.st_size,),
            )
            archive_id = int(archive.lastrowid)
            location = connection.execute(
                """
                INSERT INTO file_locations (
                    archive_id,
                    path,
                    file_size,
                    modified_time_ns
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    archive_id,
                    str(archive_path.resolve()),
                    stat.st_size,
                    stat.st_mtime_ns,
                ),
            )
            repository.save(
                archive_id=archive_id,
                location_id=int(location.lastrowid),
                result=calculate_page_hashes(archive_path),
            )


def _formats_in_archives(archives: list[Path]) -> list[str]:
    formats: set[str] = set()

    for archive_path in archives:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            formats.update(
                Path(name).suffix.casefold()
                for name in archive.namelist()
            )

    return sorted(formats)


def _run_sample(
    *,
    template_database: Path,
    run_database: Path,
    archive_count: int,
    profile: bool,
) -> dict:
    shutil.copy2(template_database, run_database)
    captured_progress = io.StringIO()

    with contextlib.redirect_stdout(captured_progress):
        result = run_perceptual_hashing(
            database=run_database,
            limit=archive_count,
            progress_every=archive_count,
            enqueue_missing=True,
            report_only=False,
            json_output=None,
            profile=profile,
        )

    if result["processed"] != archive_count:
        raise RuntimeError(
            f"Processed {result['processed']} archives; "
            f"expected {archive_count}."
        )
    if result["terminally_failed"] or result["retry_scheduled"]:
        raise RuntimeError(
            "Synthetic profiling benchmark encountered job failures."
        )
    return result


def run_benchmark(
    *,
    archive_count: int,
    pages_per_archive: int,
    rounds: int,
) -> dict:
    if archive_count < 1:
        raise ValueError("archive_count must be at least 1.")
    if pages_per_archive < 1:
        raise ValueError(
            "pages_per_archive must be at least 1."
        )
    if rounds < 1:
        raise ValueError("rounds must be at least 1.")

    with tempfile.TemporaryDirectory(
        prefix="perceptual-profile-benchmark-"
    ) as temporary:
        root = Path(temporary)
        archives = _create_archives(
            root,
            archive_count=archive_count,
            pages_per_archive=pages_per_archive,
        )
        template_database = root / "template.db"
        _seed_database(template_database, archives)

        with zipfile.ZipFile(
            archives[0],
            mode="r",
        ) as warmup_archive:
            first_name = warmup_archive.namelist()[0]
            warmup_payload = warmup_archive.read(first_name)

        with Image.open(BytesIO(warmup_payload)) as warmup_image:
            warmup_image.load()
            perceptual_hash(warmup_image)

        unprofiled_elapsed: list[float] = []
        profiled_elapsed: list[float] = []
        profile_results: list[dict] = []

        for round_number in range(rounds):
            configurations = [False, True]
            if round_number % 2:
                configurations.reverse()

            for profile in configurations:
                label = "profiled" if profile else "unprofiled"
                run_database = (
                    root
                    / f"round-{round_number}-{label}.db"
                )
                result = _run_sample(
                    template_database=template_database,
                    run_database=run_database,
                    archive_count=archive_count,
                    profile=profile,
                )
                if profile:
                    profiled_elapsed.append(
                        float(result["elapsed_seconds"])
                    )
                    profile_results.append(
                        result["phase_timing"]
                    )
                else:
                    unprofiled_elapsed.append(
                        float(result["elapsed_seconds"])
                    )

        unprofiled_median = statistics.median(
            unprofiled_elapsed
        )
        profiled_median = statistics.median(profiled_elapsed)
        aggregate_phases = {
            name: round(
                sum(
                    result["phase_seconds"][name]
                    for result in profile_results
                ),
                6,
            )
            for name in profile_results[0]["phase_seconds"]
        }
        aggregate_timed = sum(aggregate_phases.values())

        return {
            "benchmark": "perceptual_hash_phase_profiling",
            "python_version": platform.python_version(),
            "pillow_version": pillow_version,
            "platform": platform.platform(),
            "storage": "local_temporary_directory",
            "archive_count": archive_count,
            "pages_per_archive": pages_per_archive,
            "pages_per_run": archive_count * pages_per_archive,
            "rounds": rounds,
            "formats": _formats_in_archives(archives),
            "unprofiled_elapsed_seconds": unprofiled_elapsed,
            "profiled_elapsed_seconds": profiled_elapsed,
            "unprofiled_median_seconds": unprofiled_median,
            "profiled_median_seconds": profiled_median,
            "profiling_overhead_percent": (
                (profiled_median / unprofiled_median) - 1
            ) * 100,
            "aggregate_profiled_archives": sum(
                result["profiled_archives"]
                for result in profile_results
            ),
            "aggregate_profiled_pages": sum(
                result["profiled_pages"]
                for result in profile_results
            ),
            "aggregate_phase_seconds": aggregate_phases,
            "aggregate_phase_percentages": {
                name: (
                    round(
                        (seconds / aggregate_timed) * 100,
                        3,
                    )
                    if aggregate_timed > 0
                    else 0.0
                )
                for name, seconds in aggregate_phases.items()
            },
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark optional perceptual-hash phase profiling "
            "against the unprofiled worker path."
        )
    )
    parser.add_argument("--archives", type=int, default=50)
    parser.add_argument("--pages-per-archive", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=3)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = run_benchmark(
            archive_count=args.archives,
            pages_per_archive=args.pages_per_archive,
            rounds=args.rounds,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Profiling benchmark failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
