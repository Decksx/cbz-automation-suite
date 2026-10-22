"""Operator reports preserve the complete declared input population (#98)."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import cbz_series_ambiguity_report as cli


@pytest.fixture
def configured(tmp_path):
    roots = {name: tmp_path / name for name in ("comix", "manga", "review")}
    for root in roots.values():
        root.mkdir()
    raw = {
        "version": 2, "destinations": {name: str(path) for name, path in roots.items()},
        "default": "manga", "rules": [],
        "series_index": {"enabled": True, "destinations": ["comix", "manga"]},
    }
    config = tmp_path / "routing.json"
    config.write_text(json.dumps(raw), encoding="utf-8")
    return config, roots, raw


def _write_config(config, raw):
    config.write_text(json.dumps(raw), encoding="utf-8")


def _run(config, capsys, scope=("comix", "manga")):
    code = cli.main(["--routing", str(config), "--scope", *scope])
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_all_diagnostic_populations_and_their_paths_are_reported(configured, capsys):
    config, roots, _ = configured
    names = {
        "comix": ["Mr. Name", "Mr. Name - Uncensored", "Shared", "Uncensored", ".STFOLDER"],
        "manga": ["Shared", ".hack", "f47ac10b-58cc-4372-a567-0e02b2c3d479", ".stversions"],
    }
    for key, children in names.items():
        for name in children:
            (roots[key] / name).mkdir()
    (roots["manga"] / "not-a-directory.cbz").write_bytes(b"not inspected")
    (roots["review"] / "Shared").mkdir()  # outside the configured index scope

    code, report = _run(config, capsys)
    assert code == 1 and report["diagnostics"]["clear"] is False
    collisions = {row["key"]: row["entries"] for row in report["diagnostics"]["collisions"]}
    assert set(collisions) == {"mr name", "shared"}
    assert {row["path"] for row in collisions["mr name"]} == {
        str(roots["comix"] / "Mr. Name"), str(roots["comix"] / "Mr. Name - Uncensored"),
    }
    assert [row["dest_key"] for row in collisions["shared"]] == ["comix", "manga"]
    assert report["diagnostics"]["empty_keys"] == [
        {"dest_key": "comix", "path": str(roots["comix"] / "Uncensored")},
    ]
    assert {row["path"] for row in report["diagnostics"]["excluded"]} == {
        str(roots["comix"] / ".STFOLDER"), str(roots["manga"] / ".stversions"),
    }
    assert report["counts"] == {
        "directory_observations": 9, "canonical_keys": 4, "collision_keys": 2,
        "empty_key_paths": 1, "excluded_paths": 2, "scan_problems": 0,
    }
    assert [row["root"] for row in report["scope"]] == [str(roots["comix"]), str(roots["manga"])]
    assert all(row["status"] == "enumerated" for row in report["scope"])


def test_a_clean_empty_scope_is_an_observation_not_activation(configured, capsys):
    config, _, _ = configured
    code, report = _run(config, capsys)
    assert code == 0 and report["diagnostics"]["clear"] is True
    assert report["counts"]["directory_observations"] == 0
    assert report["observation"] == {
        "atomic_filesystem_snapshot": False, "archive_contents_read": False,
        "physical_aliases_resolved": False, "reviewed_exceptions_evaluated": False,
    }


@pytest.mark.parametrize("scope", [("comix",), ("comix", "manga", "review"),
                                    ("comix", "manga", "manga"), ("unknown",)])
def test_invalid_scope_refuses_before_enumeration(configured, capsys, monkeypatch, scope):
    config, _, _ = configured

    def forbidden(root):
        pytest.fail(f"unconfirmed root was enumerated: {root}")

    monkeypatch.setattr(cli, "_directory_paths", forbidden)
    assert cli.main(["--routing", str(config), "--scope", *scope]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "every configured index destination exactly once" in captured.err


def test_duplicate_configured_destinations_refuse_before_enumeration(configured, capsys, monkeypatch):
    config, _, raw = configured
    raw["series_index"]["destinations"] = ["comix", "manga", "comix"]
    _write_config(config, raw)
    monkeypatch.setattr(cli, "_directory_paths", lambda root: pytest.fail("scope was not validated"))
    assert cli.main(["--routing", str(config), "--scope", "comix", "manga"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "contain duplicates" in captured.err


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_an_unavailable_root_blocks_the_report(configured, capsys, kind):
    config, roots, _ = configured
    roots["comix"].rmdir()
    if kind == "file":
        roots["comix"].write_bytes(b"not a directory")
    (roots["manga"] / "Visible").mkdir()
    code, report = _run(config, capsys)
    assert code == 1 and report["diagnostics"]["clear"] is False
    assert report["scope"][0]["status"] == "failed"
    assert report["scope"][0]["directories"] == []
    assert report["counts"]["scan_problems"] == 1
    assert report["counts"]["canonical_keys"] == 1
    assert "comix" in report["diagnostics"]["scan_problems"][0]


def test_permission_error_is_reported_without_certifying_a_clean_scan(configured, capsys, monkeypatch):
    config, roots, _ = configured
    enumerate_root = cli._directory_paths

    def deny_one(root):
        if root == roots["manga"]:
            raise PermissionError("fixture denies enumeration")
        return enumerate_root(root)

    monkeypatch.setattr(cli, "_directory_paths", deny_one)
    code, report = _run(config, capsys)
    assert code == 1 and not report["diagnostics"]["clear"]
    assert report["scope"][1]["status"] == "failed"
    assert "PermissionError" in report["scope"][1]["error"]


def test_disabled_index_is_blocked_and_reads_no_roots(configured, capsys, monkeypatch):
    config, _, raw = configured
    raw["series_index"]["enabled"] = False
    _write_config(config, raw)
    monkeypatch.setattr(cli, "_directory_paths", lambda root: pytest.fail("disabled scan ran"))
    code, report = _run(config, capsys)
    assert code == 1 and not report["index_enabled"]
    assert not report["diagnostics"]["clear"]
    assert all(row["status"] == "not_scanned" for row in report["scope"])
    assert report["counts"]["directory_observations"] == 0
    assert "disabled" in report["diagnostics"]["scan_problems"][0]


def test_implicit_index_destination_list_still_requires_the_full_scope(configured, capsys):
    config, roots, raw = configured
    raw["series_index"]["destinations"] = []
    _write_config(config, raw)
    code, report = _run(config, capsys, scope=tuple(roots))
    assert code == 0 and report["priority"] == list(roots)
    assert [row["dest_key"] for row in report["scope"]] == list(roots)


def test_enumeration_and_confirmation_order_do_not_change_the_fingerprint(configured, monkeypatch):
    config, roots, _ = configured
    for name in ("Zebra", "alpha", "日本語"):
        (roots["manga"] / name).mkdir()
    original = cli.build_report(config, ["comix", "manga"])
    iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: iter(reversed(list(iterdir(path)))))
    reordered = cli.build_report(config, ["manga", "comix"])
    assert reordered == original
    digest = reordered.pop("report_sha256")
    serialized = json.dumps(reordered, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert digest == hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def test_noncolliding_directory_changes_are_bound_by_the_fingerprint(configured):
    config, roots, _ = configured
    first = cli.build_report(config, ["comix", "manga"])
    (roots["manga"] / "New Title").mkdir()
    second = cli.build_report(config, ["comix", "manga"])
    assert first["diagnostics"] == second["diagnostics"]
    assert first["report_sha256"] != second["report_sha256"]
    assert second["scope"][1]["directories"] == [str(roots["manga"] / "New Title")]


def test_config_bytes_and_priority_are_bound_by_the_fingerprint(configured):
    config, _, raw = configured
    first = cli.build_report(config, ["comix", "manga"])
    config.write_bytes(config.read_bytes() + b"\n")
    reformatted = cli.build_report(config, ["comix", "manga"])
    assert first["config"]["sha256"] != reformatted["config"]["sha256"]
    assert first["report_sha256"] != reformatted["report_sha256"]
    raw["series_index"]["destinations"] = ["manga", "comix"]
    _write_config(config, raw)
    reordered = cli.build_report(config, ["comix", "manga"])
    assert reordered["priority"] == ["manga", "comix"]
    assert reordered["report_sha256"] != first["report_sha256"]


def test_alias_labels_do_not_invent_a_collision_and_each_root_is_read_once(configured, monkeypatch):
    config, roots, raw = configured
    raw["destinations"]["manga"] = str(roots["comix"])
    _write_config(config, raw)
    (roots["comix"] / "Title").mkdir()
    calls = []
    enumerate_root = cli._directory_paths

    def observe(root):
        calls.append(root)
        return enumerate_root(root)

    monkeypatch.setattr(cli, "_directory_paths", observe)
    report = cli.build_report(config, ["comix", "manga"])
    assert calls == [roots["comix"]]
    assert report["diagnostics"]["clear"]
    assert report["counts"]["canonical_keys"] == 1
    assert report["counts"]["directory_observations"] == 2  # two labels, one lexical path


def test_reporting_reads_no_archives_and_changes_no_inputs(configured, monkeypatch):
    config, roots, _ = configured
    series = roots["manga"] / "Title"
    series.mkdir()
    archive = series / "chapter.cbz"
    archive.write_bytes(b"unreadable archive contents are irrelevant")
    before = {p: p.read_bytes() for p in (config, archive)}
    open_path = Path.open

    def only_config(path, *args, **kwargs):
        assert path == config, f"unexpected content read: {path}"
        mode = args[0] if args else kwargs.get("mode", "r")
        assert mode in ("r", "rb")
        return open_path(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", only_config)
        cli.build_report(config, ["comix", "manga"])
    assert {p: p.read_bytes() for p in before} == before
    assert list(series.iterdir()) == [archive]


@pytest.mark.parametrize("payload", ["[]", "null", "{", '{"version": 100}'])
def test_invalid_config_has_no_report(configured, capsys, payload):
    config, _, _ = configured
    config.write_text(payload, encoding="utf-8")
    assert cli.main(["--routing", str(config), "--scope", "comix", "manga"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Report not produced:")


def test_missing_config_has_no_report(tmp_path, capsys):
    assert cli.main(["--routing", str(tmp_path / "absent.json"), "--scope", "manga"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Report not produced:")


def test_standalone_cli_works_outside_the_repository_and_has_no_output_file(configured):
    config, roots, _ = configured
    (roots["manga"] / "日本語").mkdir()
    run = subprocess.run(
        [sys.executable, "-B", str(Path(cli.__file__).resolve()), "--routing", str(config),
         "--scope", "comix", "manga"], cwd=config.parent, capture_output=True, text=True,
        check=False,
    )
    assert run.returncode == 0 and run.stderr == ""
    report = json.loads(run.stdout)
    assert report["scope"][1]["directories"] == [str(roots["manga"] / "日本語")]
    assert sorted(p.name for p in config.parent.iterdir()) == ["comix", "manga", "review", "routing.json"]


@pytest.mark.parametrize("args", [[], ["--scope", "manga"], ["--routing", "absent.json"]])
def test_cli_requires_both_explicit_inputs(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
