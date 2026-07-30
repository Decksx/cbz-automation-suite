from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, features

from comic_automation.archive.perceptual_hashing import (
    difference_hash,
    perceptual_hash,
)


@dataclass(frozen=True)
class Version1Vector:
    name: str
    image_format: str
    mode: str
    size: tuple[int, int]
    hash_size: int
    high_frequency_factor: int
    expected_dhash: str
    expected_phash: str


VERSION_1_VECTORS = (
    Version1Vector(
        name="jpeg_rgb_standard",
        image_format="JPEG",
        mode="RGB",
        size=(96, 128),
        hash_size=8,
        high_frequency_factor=4,
        expected_dhash="878c888e8caca738",
        expected_phash="bf3bc09dc0d6c0c6",
    ),
    Version1Vector(
        name="png_rgba_wide",
        image_format="PNG",
        mode="RGBA",
        size=(257, 31),
        hash_size=8,
        high_frequency_factor=4,
        expected_dhash="85ccc8caccaca51a",
        expected_phash="bf3fc09dc2d240c6",
    ),
    Version1Vector(
        name="webp_rgb_tall",
        image_format="WEBP",
        mode="RGB",
        size=(37, 263),
        hash_size=8,
        high_frequency_factor=4,
        expected_dhash="a7ac888a8caca738",
        expected_phash="bf3bc09dc2d640c6",
    ),
    Version1Vector(
        name="gif_palette",
        image_format="GIF",
        mode="P",
        size=(65, 97),
        hash_size=8,
        high_frequency_factor=4,
        expected_dhash="a3a6ac82a4c48538",
        expected_phash="bf79c097c4c4c1c5",
    ),
    Version1Vector(
        name="tiff_grayscale_small",
        image_format="TIFF",
        mode="L",
        size=(3, 5),
        hash_size=8,
        high_frequency_factor=4,
        expected_dhash="0000000000000000",
        expected_phash="b86cf87cf0f0e0e0",
    ),
    Version1Vector(
        name="png_grayscale_large",
        image_format="PNG",
        mode="L",
        size=(1536, 1024),
        hash_size=8,
        high_frequency_factor=4,
        expected_dhash="4008012204801001",
        expected_phash="94e623687be23c5c",
    ),
    Version1Vector(
        name="png_rgb_custom_4x2",
        image_format="PNG",
        mode="RGB",
        size=(113, 79),
        hash_size=4,
        high_frequency_factor=2,
        expected_dhash="8aa1",
        expected_phash="b389",
    ),
    Version1Vector(
        name="png_rgb_custom_12x3",
        image_format="PNG",
        mode="RGB",
        size=(127, 191),
        hash_size=12,
        high_frequency_factor=3,
        expected_dhash="d03d1ad30d24d2cd34d24c30d99d069f0200",
        expected_phash="bfd3fdc029d1c26d62c05c65e00ed5cc131f",
    ),
)


def _pattern(mode: str, size: tuple[int, int]) -> Image.Image:
    width, height = size

    if mode == "L":
        image = Image.new("L", size)
        image.putdata([
            (
                (column * 17)
                + (row * 31)
                + ((column * row) % 53)
            ) % 256
            for row in range(height)
            for column in range(width)
        ])
        return image

    base = Image.new("RGBA", size, (241, 239, 227, 255))
    drawing = ImageDraw.Draw(base)
    drawing.rectangle(
        (
            max(0, width // 11),
            max(0, height // 13),
            max(1, width * 5 // 11),
            max(1, height * 10 // 13),
        ),
        fill=(17, 43, 91, 255),
    )
    drawing.ellipse(
        (
            max(0, width // 2),
            max(0, height // 7),
            max(1, width * 10 // 11),
            max(1, height * 5 // 7),
        ),
        fill=(201, 71, 39, 173),
    )
    drawing.line(
        (0, height - 1, width - 1, 0),
        fill=(52, 173, 112, 255),
        width=max(1, min(width, height) // 19),
    )

    if mode == "RGBA":
        return base
    if mode == "RGB":
        return base.convert("RGB")
    if mode == "P":
        return base.convert(
            "P",
            palette=Image.Palette.ADAPTIVE,
            colors=32,
        )

    raise ValueError(f"Unsupported regression-vector mode: {mode}")


def _encoded_vector(vector: Version1Vector) -> bytes:
    image = _pattern(vector.mode, vector.size)
    output = BytesIO()
    image.save(
        output,
        format=vector.image_format,
        quality=91,
        lossless=vector.image_format == "WEBP",
    )
    return output.getvalue()


@pytest.mark.parametrize(
    "vector",
    VERSION_1_VECTORS,
    ids=lambda vector: vector.name,
)
def test_version_1_hash_regression(vector: Version1Vector) -> None:
    if (
        vector.image_format == "WEBP"
        and not features.check("webp")
    ):
        pytest.skip("Pillow was built without WebP support.")

    with Image.open(BytesIO(_encoded_vector(vector))) as image:
        image.load()

        actual_dhash = difference_hash(
            image,
            hash_size=vector.hash_size,
        )
        actual_phash = perceptual_hash(
            image,
            hash_size=vector.hash_size,
            high_frequency_factor=vector.high_frequency_factor,
        )

        assert image.format == vector.image_format
        assert image.mode == vector.mode
        assert image.size == vector.size
        assert actual_dhash == vector.expected_dhash
        assert actual_phash == vector.expected_phash
