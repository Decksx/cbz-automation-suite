#!/usr/bin/env python3
"""Compare cached pHash constants with the Version 1 reference path."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageDraw, __version__ as pillow_version


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comic_automation.archive.perceptual_hashing import (
    DEFAULT_HASH_SIZE,
    DEFAULT_HIGH_FREQUENCY_FACTOR,
    _bits_to_hex,
    _perceptual_hash_constants,
    perceptual_hash,
)


def _uncached_perceptual_hash(
    image: Image.Image,
    *,
    hash_size: int = DEFAULT_HASH_SIZE,
    high_frequency_factor: int = DEFAULT_HIGH_FREQUENCY_FACTOR,
) -> str:
    """Preserve the pre-cache Version 1 implementation for comparison."""
    if hash_size < 2:
        raise ValueError("hash_size must be at least 2.")
    if high_frequency_factor < 1:
        raise ValueError(
            "high_frequency_factor must be at least 1."
        )

    sample_size = hash_size * high_frequency_factor
    grayscale = image.convert("L").resize(
        (sample_size, sample_size),
        Image.Resampling.LANCZOS,
    )
    pixels = grayscale.tobytes()
    scale = math.pi / (2 * sample_size)
    cosine = [
        [
            math.cos((2 * position + 1) * frequency * scale)
            for position in range(sample_size)
        ]
        for frequency in range(hash_size)
    ]
    normalization = [
        (
            math.sqrt(1 / sample_size)
            if frequency == 0
            else math.sqrt(2 / sample_size)
        )
        for frequency in range(hash_size)
    ]
    coefficients: list[float] = []

    for vertical_frequency in range(hash_size):
        for horizontal_frequency in range(hash_size):
            coefficient = 0.0

            for row in range(sample_size):
                row_factor = cosine[vertical_frequency][row]
                offset = row * sample_size

                for column in range(sample_size):
                    coefficient += (
                        pixels[offset + column]
                        * row_factor
                        * cosine[horizontal_frequency][column]
                    )

            coefficients.append(
                coefficient
                * normalization[vertical_frequency]
                * normalization[horizontal_frequency]
            )

    threshold = statistics.median(coefficients[1:])
    return _bits_to_hex(
        [coefficient > threshold for coefficient in coefficients]
    )


def _benchmark_image() -> Image.Image:
    image = Image.new(
        "RGBA",
        (256, 384),
        (241, 239, 227, 255),
    )
    drawing = ImageDraw.Draw(image)
    drawing.rectangle(
        (23, 29, 116, 295),
        fill=(17, 43, 91, 255),
    )
    drawing.ellipse(
        (128, 54, 232, 274),
        fill=(201, 71, 39, 173),
    )
    drawing.line(
        (0, 383, 255, 0),
        fill=(52, 173, 112, 255),
        width=13,
    )
    return image.convert("RGB")


def _time_calls(
    function: Callable[[Image.Image], str],
    image: Image.Image,
    calls: int,
) -> float:
    started = time.perf_counter()

    for _ in range(calls):
        function(image)

    return time.perf_counter() - started


def run_benchmark(
    *,
    calls_per_round: int,
    rounds: int,
) -> dict:
    if calls_per_round < 1:
        raise ValueError("calls_per_round must be at least 1.")
    if rounds < 1:
        raise ValueError("rounds must be at least 1.")

    image = _benchmark_image()
    reference_digest = _uncached_perceptual_hash(image)
    cached_digest = perceptual_hash(image)

    if cached_digest != reference_digest:
        raise RuntimeError(
            "Cached pHash output differs from Version 1 reference."
        )

    _perceptual_hash_constants.cache_clear()
    perceptual_hash(image)
    uncached_seconds: list[float] = []
    cached_seconds: list[float] = []

    for round_number in range(rounds):
        functions = [
            ("uncached", _uncached_perceptual_hash),
            ("cached", perceptual_hash),
        ]

        if round_number % 2:
            functions.reverse()

        for name, function in functions:
            elapsed = _time_calls(
                function,
                image,
                calls_per_round,
            )
            if name == "uncached":
                uncached_seconds.append(elapsed)
            else:
                cached_seconds.append(elapsed)

    uncached_median = statistics.median(uncached_seconds)
    cached_median = statistics.median(cached_seconds)

    return {
        "benchmark": "perceptual_hash_constant_cache",
        "python_version": platform.python_version(),
        "pillow_version": pillow_version,
        "platform": platform.platform(),
        "image_mode": image.mode,
        "image_size": list(image.size),
        "hash_size": DEFAULT_HASH_SIZE,
        "high_frequency_factor": (
            DEFAULT_HIGH_FREQUENCY_FACTOR
        ),
        "calls_per_round": calls_per_round,
        "rounds": rounds,
        "exact_digest_equality": (
            cached_digest == reference_digest
        ),
        "digest": cached_digest,
        "uncached_round_seconds": uncached_seconds,
        "cached_round_seconds": cached_seconds,
        "uncached_median_seconds": uncached_median,
        "cached_median_seconds": cached_median,
        "uncached_hashes_per_second": (
            calls_per_round / uncached_median
        ),
        "cached_hashes_per_second": (
            calls_per_round / cached_median
        ),
        "throughput_improvement_percent": (
            (uncached_median / cached_median) - 1
        ) * 100,
        "elapsed_reduction_percent": (
            1 - (cached_median / uncached_median)
        ) * 100,
        "cache": {
            "max_size": (
                _perceptual_hash_constants.cache_info().maxsize
            ),
            "current_size": (
                _perceptual_hash_constants.cache_info().currsize
            ),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark cached pHash constants against the exact "
            "pre-cache Version 1 reference implementation."
        )
    )
    parser.add_argument(
        "--calls-per-round",
        type=int,
        default=100,
    )
    parser.add_argument("--rounds", type=int, default=7)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = run_benchmark(
            calls_per_round=args.calls_per_round,
            rounds=args.rounds,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
