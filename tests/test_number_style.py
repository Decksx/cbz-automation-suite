"""Physical registry selection on temporary directories, never live libraries."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from comic_automation.library import number_style as ns


def registry(tmp_path, rows):
    path = tmp_path / "styles.json"
    path.write_text(json.dumps({"version": 1, "roots": rows}), encoding="utf-8")
    return ns.NumberStyleRegistry.load(path)


def test_subfolder_scan_and_uncreated_destination_inherit_the_registered_root(tmp_path):
    root = tmp_path / "Western"
    root.mkdir()
    child = root / "Series"
    child.mkdir()
    config = registry(tmp_path, [{"path": str(root), "style": "issue"}])
    chosen = config.select(child / "uncreated")
    assert chosen.style == "issue" and chosen.source == "registry"
    assert chosen.matched_root == str(root)
    assert chosen.registry_sha256 == hashlib.sha256(Path(config.path).read_bytes()).hexdigest()


@pytest.mark.parametrize("kind", ["unknown", "overlap", "duplicate", "unavailable"])
def test_auto_never_guesses_for_unknown_ambiguous_or_unreadable_roots(tmp_path, kind):
    root = tmp_path / "library"
    root.mkdir()
    rows = [{"path": str(root), "style": "chapter"}]
    if kind == "unknown":
        rows = []
    elif kind == "overlap":
        rows += [{"path": str(tmp_path), "style": "chapter"}]
    elif kind == "duplicate":
        rows *= 2
    else:
        rows += [{"path": str(tmp_path / "gone"), "style": "issue"}]
    config = registry(tmp_path, rows)
    with pytest.raises(ns.NumberStyleRefused):
        config.select(root)
    # An explicit decision is usable without probing inaccessible roots.
    assert config.select(root, "issue").source == "explicit"


def test_junction_alias_uses_file_identity_instead_of_path_spelling(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    alias = tmp_path / "alias"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(root)],
                       check=True, capture_output=True)
    else:
        alias.symlink_to(root, target_is_directory=True)
    try:
        config = registry(tmp_path, [{"path": str(alias), "style": "issue"}])
        assert config.select(root).style == "issue"
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC access alias")
def test_unc_registered_root_matches_local_scope(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    drive, tail = os.path.splitdrive(str(root))
    unc = "\\\\localhost\\" + drive[0] + "$" + tail
    # Establish this host's share access first; this is not a symlink skip.
    if not Path(unc).is_dir():
        pytest.skip("localhost administrative share unavailable")
    config = registry(tmp_path, [{"path": unc, "style": "chapter"}])
    assert config.select(root).style == "chapter"


def test_zero_identity_is_refused_even_with_a_matching_stat_result(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    config = registry(tmp_path, [{"path": str(root), "style": "issue"}])
    original = Path.stat

    def zero(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        return os.stat_result((value.st_mode, 0, value.st_dev, *tuple(value)[3:]))

    monkeypatch.setattr(Path, "stat", zero)
    with pytest.raises(ns.NumberStyleRefused, match="identity"):
        config.select(root)


@pytest.mark.parametrize("document", [None, {}, {"version": True, "roots": []},
    {"version": 1, "roots": [{"path": "relative", "style": "issue"}]},
    {"version": 1, "roots": [{"path": "C:/absolute", "style": "guess"}]}])
def test_invalid_registry_never_silently_drops_rows(tmp_path, document):
    path = tmp_path / "styles.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ns.NumberStyleRefused):
        ns.NumberStyleRegistry.load(path)


def test_one_external_registry_default_is_shared_by_both_cli_parsers(tmp_path, monkeypatch):
    path = tmp_path / "operator-registry.json"
    monkeypatch.setenv("CBZ_NUMBER_STYLE_REGISTRY", str(path))
    # Fresh process models a GUI/CLI launch; no running watcher is reconfigured.
    code = """
from comic_automation.library.number_style import DEFAULT_REGISTRY
from scripts.cbz_watcher import build_argument_parser
from scripts import cbz_canonical_plan as planner
print(DEFAULT_REGISTRY)
print(build_argument_parser().parse_args([]).number_style_registry)
def capture(*args, **kwargs):
    raise RuntimeError('registry=' + str(kwargs['number_style_registry']))
planner.build_plan = capture
assert planner.main(['--library-root', '.', '--database', 'unopened.db',
                     '--quarantine-date', '2026-10-26']) == 2
"""
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
    assert result.stdout.splitlines() == [str(path), str(path)]
    assert result.stderr.strip() == "Plan not produced: registry=" + str(path)
