from __future__ import annotations

from scripts.benchmark_perceptual_hash_constants import run_benchmark


def test_constant_cache_benchmark_preserves_version_1_digest() -> None:
    result = run_benchmark(calls_per_round=1, rounds=1)

    assert result["exact_digest_equality"] is True
    assert result["digest"] == "bf3dc095c2d6c0c6"
    assert result["cache"] == {
        "max_size": 32,
        "current_size": 1,
    }
