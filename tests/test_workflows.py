from argparse import Namespace
from pathlib import Path
import zipfile

from scripts.cbz_sanitizer import process_cbz_file
from apps.cbz_gui import TOOLS
from scripts.cbz_workflows import (
    build_maintenance_commands,
    build_parser,
    build_series_commands,
)


def test_series_workflow_combines_requested_stages():
    args = Namespace(
        root=Path("Library"),
        stages=["organize", "stage", "review", "compilations"],
        workers=4,
        dry_run=True,
        metadata_dedupe=False,
        uncensored_check=True,
        move_which="both",
        series_common_words=2,
        series_min_group_size=3,
        out=Path("proposal.json"),
    )

    commands = build_series_commands(args)

    assert [label for label, _ in commands] == [
        "Organize and stage series",
        "Generate series review proposal",
        "Resolve compilation overlaps",
    ]
    organize = commands[0][1]
    assert "organize-series" in organize
    assert "--possible-series-check" in organize
    assert "--no-metadata-dedupe" in organize
    assert "--dry-run" in organize
    assert "--move-which" in organize


def test_maintenance_workflow_runs_selected_stages_in_order():
    args = Namespace(
        root=Path("Library"),
        stages=["sanitize", "archive", "metadata", "names"],
        workers=8,
        dry_run=True,
        metadata_dedupe=True,
        uncensored_check=False,
        move_which="both",
        sort="alpha",
        full_rescan=True,
        restart=False,
        rules=["comicinfo", "translate"],
        names_only=True,
    )

    commands = build_maintenance_commands(args)

    assert len(commands) == 4
    assert "cbz_sanitizer.py" in commands[0][1][1]
    assert "--dry-run" in commands[0][1]
    assert "--full" in commands[0][1]
    assert "--rules=comicinfo,translate" in commands[0][1]
    assert "archive-clean" in commands[1][1]
    assert "metadata" in commands[2][1]
    assert "repair-names" in commands[3][1]
    assert "--names-only" in commands[3][1]


def test_workflow_parser_rejects_unknown_stage():
    parser = build_parser()
    try:
        parser.parse_args(["series", "Library", "--stages=unknown"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("unknown stage was accepted")


def test_sanitizer_dry_run_does_not_rename_or_rewrite_archive(tmp_path):
    archive = tmp_path / "001 - Chapter 1.cbz"
    source_xml = "<ComicInfo><Title>Chapter 1</Title></ComicInfo>"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("001.jpg", b"image")
        zf.writestr("ComicInfo.xml", source_xml)

    proposed = process_cbz_file(archive, dry_run=True)

    assert archive.exists()
    assert proposed != archive
    assert not proposed.exists()
    with zipfile.ZipFile(archive) as zf:
        assert zf.read("ComicInfo.xml").decode() == source_xml


def test_every_visible_gui_option_has_guidance():
    for tool in TOOLS:
        for option in tool.get("options", []):
            assert option.get("description"), (tool["id"], option["key"])
            assert option.get("example"), (tool["id"], option["key"])
            assert option.get("expected_result"), (tool["id"], option["key"])
