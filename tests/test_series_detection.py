from pathlib import Path

from scripts.cbz_core import infer_series_name, parse_comic_name


def test_infers_series_above_issues_folder():
    path = Path(r"\\tower\media\comics\Marvel\Batman\Issues\Batman Ch.5.cbz")
    root = Path(r"\\tower\media\comics")

    assert infer_series_name(path, root) == "Batman"


def test_parse_comic_name_uses_root_aware_series():
    path = Path(r"\\tower\media\comics\Marvel\Batman\Issues\Batman Ch.5.cbz")
    root = Path(r"\\tower\media\comics")

    parsed = parse_comic_name(path, library_root=root)

    assert parsed.series == "Batman"
    assert parsed.chapter == "5"
