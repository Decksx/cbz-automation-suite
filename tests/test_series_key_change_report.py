"""The #111 key-change report: what the series_key fix re-keys, read-only.

Every test builds folder names in tmp_path. The report reads directory
names only, so these pin which names it counts as changed, merged and split,
that it writes nothing, and that it fails loudly rather than reporting an
empty, reassuring scan.
"""
from __future__ import annotations

import json

import pytest

from scripts import cbz_series_key_change_report as report_mod
from scripts.cbz_routing import series_key


def _library(tmp_path, *names):
    root = tmp_path / "lib"
    root.mkdir()
    for name in names:
        (root / name).mkdir()
    return root


def _tree(path):
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*"))


def test_the_legacy_rule_reproduces_the_old_split():
    # Guards the comparison baseline itself: if the legacy copy were
    # accidentally "fixed" too, every report would say nothing changes.
    assert report_mod.legacy_series_key("Berserk_Uncensored") == "berserk uncensored"
    assert report_mod.legacy_series_key("Berserk Uncensored") == "berserk"
    assert series_key("Berserk_Uncensored") == "berserk"


def test_only_glued_marker_names_are_reported_as_changed(tmp_path):
    root = _library(tmp_path, "Berserk", "Berserk_Uncensored",
                    "Berserk (Uncensored)", "Attack_on_Titan", "One Piece")
    report = report_mod.build_report([root])

    assert report["counts"]["directories"] == 5
    assert [r["name"] for r in report["changed"]] == ["Berserk_Uncensored"]
    assert report["changed"][0]["legacy_key"] == "berserk uncensored"
    assert report["changed"][0]["key"] == "berserk"


def test_a_series_the_old_index_kept_apart_is_reported_as_a_merge(tmp_path):
    root = _library(tmp_path, "Berserk", "Berserk_Uncensored", "One Piece")
    report = report_mod.build_report([root])

    assert report["counts"]["merges"] == 1
    merge = report["merges"][0]
    assert merge["key"] == "berserk"
    assert sorted(m["name"] for m in merge["members"]) == ["Berserk", "Berserk_Uncensored"]


def test_merges_are_found_across_roots(tmp_path):
    comix = tmp_path / "Comix"
    manga = tmp_path / "Manga"
    (comix / "Berserk").mkdir(parents=True)
    (manga / "Berserk_decensored").mkdir(parents=True)
    report = report_mod.build_report([comix, manga])

    assert report["counts"]["merges"] == 1
    assert {m["root"] for m in report["merges"][0]["members"]} == {str(comix), str(manga)}


def test_an_unaffected_library_reports_nothing_and_exits_zero(tmp_path, capsys):
    root = _library(tmp_path, "Berserk", "Attack on Titan")
    assert report_mod.main(["--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"] == {"directories": 2, "changed": 0, "merges": 0,
                             "empty_keys": 0, "splits": 0, "excluded": 0}


def test_a_changed_library_exits_one(tmp_path, capsys):
    root = _library(tmp_path, "Berserk_Uncensored")
    assert report_mod.main(["--root", str(root)]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["changed"] == 1


def test_a_missing_root_is_an_error_not_an_empty_scan(tmp_path, capsys):
    assert report_mod.main(["--root", str(tmp_path / "absent")]) == 2
    assert "Report not produced" in capsys.readouterr().err


def test_the_same_root_twice_is_refused(tmp_path):
    root = _library(tmp_path, "Berserk")
    with pytest.raises(ValueError, match="more than once"):
        report_mod.build_report([root, root])


def test_the_report_writes_nothing(tmp_path):
    root = _library(tmp_path, "Berserk", "Berserk_Uncensored")
    (root / "Berserk" / "Berserk 01.cbz").write_bytes(b"not read")
    before = _tree(tmp_path)
    report_mod.build_report([root])
    assert _tree(tmp_path) == before


def test_files_are_not_counted_as_series_folders(tmp_path):
    root = _library(tmp_path, "Berserk")
    (root / "Berserk_Uncensored.cbz").write_bytes(b"x")
    report = report_mod.build_report([root])
    assert report["counts"]["directories"] == 1
    assert report["changed"] == []


def test_the_fingerprint_binds_the_observed_names(tmp_path):
    a = report_mod.build_report([_library(tmp_path, "Berserk_Uncensored")])
    (tmp_path / "lib" / "Extra").mkdir()
    b = report_mod.build_report([tmp_path / "lib"])
    assert a["report_sha256"] != b["report_sha256"]


def test_renaming_an_unaffected_folder_changes_the_fingerprint(tmp_path):
    # Codex's probe on #114: a same-count rename of a folder the fix does
    # not touch must not produce an identical report.
    root = _library(tmp_path, "Attack on Titan", "Berserk_Uncensored")
    before = report_mod.build_report([root])
    (root / "Attack on Titan").rename(root / "One Piece")
    after = report_mod.build_report([root])
    assert before["counts"] == after["counts"]
    assert before["changed"] == after["changed"]
    assert before["report_sha256"] != after["report_sha256"]
    assert after["roots"][0]["names"] == ["Berserk_Uncensored", "One Piece"]


def test_empty_new_keys_are_listed_but_never_merged(tmp_path):
    # SeriesIndex excludes an empty identity, so two folders that both key
    # to "" are not one series and must not be reported as joining.
    root = _library(tmp_path, "_decensored", "Uncensored_", "Berserk")
    report = report_mod.build_report([root])
    assert report["counts"]["merges"] == 0
    assert sorted(r["name"] for r in report["empty_keys"]) == ["Uncensored_", "_decensored"]
    assert {r["name"] for r in report["changed"]} == {"_decensored", "Uncensored_"}


def test_non_series_directories_are_excluded_from_comparison_but_listed(tmp_path):
    # Codex's counterexample on #114: '.stfolder' keys to 'stfolder', so
    # without the SeriesIndex exclusion it "merged" with stfolder_Uncensored
    # although the real index has no collision there.
    from scripts.cbz_routing import NON_SERIES_DIRECTORIES
    root = _library(tmp_path, ".stfolder", "stfolder_Uncensored", "Berserk")
    report = report_mod.build_report([root])
    assert ".stfolder" in NON_SERIES_DIRECTORIES
    assert report["counts"]["merges"] == 0
    assert report["counts"]["excluded"] == 1
    assert report["excluded"] == [{"root": str(root), "name": ".stfolder"}]
    assert ".stfolder" in report["roots"][0]["names"]
    assert all(r["name"] != ".stfolder" for r in report["changed"])
