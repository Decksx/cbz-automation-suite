from __future__ import annotations

from scripts.benchmark_perceptual_hash_profiling import run_benchmark


def test_profiling_benchmark_exercises_enabled_and_disabled_paths(
) -> None:
    result = run_benchmark(
        archive_count=2,
        pages_per_archive=1,
        rounds=1,
    )

    assert result["archive_count"] == 2
    assert result["pages_per_run"] == 2
    assert result["aggregate_profiled_archives"] == 2
    assert result["aggregate_profiled_pages"] == 2
    assert set(result["aggregate_phase_seconds"]) == {
        "zip_open_and_inventory_seconds",
        "zip_entry_read_seconds",
        "image_open_and_decode_seconds",
        "dhash_seconds",
        "phash_seconds",
        "database_lookup_seconds",
        "database_save_seconds",
    }
    assert sum(
        result["aggregate_phase_percentages"].values()
    ) > 99.9
