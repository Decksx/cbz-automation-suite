"""Tests for series-name inference from a comic's on-disk path.

infer_series_name() walks up from a file's path toward the library root to
figure out which directory represents the "series" (as opposed to an
intermediate publisher folder like "Marvel" or a bookkeeping folder like
"Issues"). parse_comic_name(..., library_root=...) uses the same logic when
a library root is supplied, instead of just looking at the immediate parent
directory.
"""

from pathlib import Path

from scripts.cbz_core import infer_series_name, parse_comic_name


def test_infers_series_above_issues_folder():
    """Given .../Marvel/Batman/Issues/Batman Ch.5.cbz, the series should be
    resolved as "Batman" -- skipping past the "Issues" bookkeeping folder
    rather than treating it as the series name.
    """
    path = Path(r"\\tower\media\comics\Marvel\Batman\Issues\Batman Ch.5.cbz")
    root = Path(r"\\tower\media\comics")

    assert infer_series_name(path, root) == "Batman"


def test_parse_comic_name_uses_root_aware_series():
    """When parse_comic_name is given a library_root, it should route
    through the same root-aware series inference as infer_series_name,
    rather than falling back to naively using the immediate parent folder.
    """
    path = Path(r"\\tower\media\comics\Marvel\Batman\Issues\Batman Ch.5.cbz")
    root = Path(r"\\tower\media\comics")

    parsed = parse_comic_name(path, library_root=root)

    assert parsed.series == "Batman"
    assert parsed.chapter == "5"
