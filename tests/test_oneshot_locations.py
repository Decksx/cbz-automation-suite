"""Durable identity-reader refusals and stale-record preservation (#214)."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from comic_automation.library.oneshot_locations import (
    LOCATIONS_NAME, LocationsError, load_locations, parse_locations,
)


def _record(filename="Book.cbz", **changes):
    return {
        "relative_path": f"_oneshots/{filename}", "original_folder": "Original",
        "aliases": ["Other title"], "size": 12, "mtime_ns": 34,
        "sha256": "a" * 64, "operation_id": "operation:move-1", **changes,
    }


def _document(records=None, **changes):
    return {"version": 1, "generation": 7,
            "oneshots": {"Canonical": _record()} if records is None else records, **changes}


def _parse(document):
    return parse_locations(json.dumps(document, ensure_ascii=False))


def test_absent_file_is_empty_without_creating_control_files(tmp_path):
    snapshot = load_locations(tmp_path)
    assert snapshot.generation == 0 and snapshot.oneshots == ()
    assert list(tmp_path.iterdir()) == []


def test_missing_root_is_not_a_new_empty_library(tmp_path):
    with pytest.raises(LocationsError):
        load_locations(tmp_path / "missing")


def test_stale_book_identity_and_aliases_survive_without_rewriting(tmp_path):
    path = tmp_path / LOCATIONS_NAME
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_document()).encode())
    before = path.read_bytes()
    snapshot = load_locations(tmp_path)
    assert snapshot.generation == 7
    assert len(snapshot.oneshots) == 1
    row = snapshot.oneshots[0]
    assert (row.canonical_title, row.relative_path, row.aliases) == (
        "Canonical", "_oneshots/Book.cbz", ("Other title",))
    assert (row.size, row.mtime_ns, row.sha256, row.operation_id) == (
        12, 34, "a" * 64, "operation:move-1")
    assert not (tmp_path / "_oneshots").exists()
    assert path.read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        row.relative_path = "changed"


def test_new_import_can_have_no_original_folder_and_empty_aliases():
    row = _parse(_document({"New": _record(original_folder=None, aliases=[])})).oneshots[0]
    assert row.original_folder is None and row.aliases == ()


def test_cross_series_alias_ambiguity_is_retained_for_the_resolver():
    snapshot = _parse(_document({
        "One": _record("One.cbz"), "Two": _record("Two.cbz"),
    }))
    assert [row.canonical_title for row in snapshot.oneshots] == ["One", "Two"]
    assert [row.aliases for row in snapshot.oneshots] == [("Other title",)] * 2


@pytest.mark.parametrize("version", [True, 0, 2, "1", None])
def test_unknown_or_noninteger_version_refuses(version):
    with pytest.raises(LocationsError):
        _parse(_document(version=version))


@pytest.mark.parametrize("field", ["generation", "size", "mtime_ns"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "1", None])
def test_counters_never_accept_boolean_negative_or_coerced_values(field, value):
    document = _document()
    if field == "generation":
        document[field] = value
    else:
        document["oneshots"]["Canonical"][field] = value
    with pytest.raises(LocationsError):
        _parse(document)


@pytest.mark.parametrize("canonical", ["_oneshots", "_ONESHOTS", "../Series", "C:\\Book",
                                         "NUL", "Series.", "Series ", "bad\x00name"])
def test_container_and_unsafe_canonical_names_refuse(canonical):
    with pytest.raises(LocationsError):
        _parse(_document({canonical: _record()}))


@pytest.mark.parametrize("relative", ["../Book.cbz", "_oneshots/../Book.cbz",
    "_oneshots/", "_oneshots//Book.cbz", "_oneshots\\Book.cbz", "Other/Book.cbz",
    "C:/Book.cbz", "/_oneshots/Book.cbz", "_oneshots/COM1.cbz", "_oneshots/CON .cbz",
    "_oneshots/a.cbz:stream",
    "_oneshots/Book.cbz.", "_oneshots/Book.cbz ", "_oneshots/sub\\Book.cbz"])
def test_paths_cannot_escape_nest_or_use_unsafe_windows_names(relative):
    with pytest.raises(LocationsError):
        _parse(_document({"Canonical": _record(relative_path=relative)}))


def test_normalized_titles_cannot_silently_replace_each_other():
    with pytest.raises(LocationsError, match="Duplicate normalized canonical"):
        _parse(_document({"Café": _record("One.cbz"), "CAFE\u0301": _record("Two.cbz")}))


@pytest.mark.parametrize("second", ["book.CBZ", "BOOk.cbz"])
def test_two_series_cannot_own_the_same_normalized_target(second):
    with pytest.raises(LocationsError, match="Duplicate normalized target"):
        _parse(_document({"One": _record(), "Two": _record(second)}))


def test_unicode_equivalent_targets_are_a_collision():
    with pytest.raises(LocationsError, match="Duplicate normalized target"):
        _parse(_document({"One": _record("Café.cbz"), "Two": _record("CAFE\u0301.cbz")}))


@pytest.mark.parametrize("changes", [
    {"sha256": "g" * 64}, {"sha256": "a" * 63}, {"sha256": None},
    {"aliases": "alias"}, {"aliases": [None]}, {"aliases": [""]},
    {"operation_id": ""}, {"operation_id": "bad\noperation"},
    {"original_folder": ".."}, {"original_folder": "_oneshots"},
])
def test_invalid_evidence_refuses_without_inventing_defaults(changes):
    with pytest.raises(LocationsError):
        _parse(_document({"Canonical": _record(**changes)}))


def test_hex_digest_case_is_canonicalized():
    assert _parse(_document({"Canonical": _record(sha256="AB" * 32)})).oneshots[0].sha256 == "ab" * 32


@pytest.mark.parametrize("document", ["[]", "null", "{", '{"version":1,"version":1}',
    '{"version":1,"generation":0,"oneshots":{"Same":{},"Same":{}}}'])
def test_corrupt_or_duplicate_json_is_not_an_empty_snapshot(document):
    with pytest.raises(LocationsError):
        parse_locations(document)


@pytest.mark.parametrize("duplicate", ["generation", "record"])
def test_duplicate_members_are_refused_even_when_last_value_is_valid(duplicate):
    # Both alternatives are otherwise valid, so schema checks cannot mask this guard.
    if duplicate == "generation":
        document = json.dumps(_document()).replace('"generation": 7', '"generation": 7, "generation": 8')
    else:
        record = json.dumps(_record())
        document = '{"version":1,"generation":7,"oneshots":{"Canonical":' + record + ',"Canonical":' + record + '}}'
    with pytest.raises(LocationsError, match="Duplicate JSON member"):
        parse_locations(document)


def test_missing_or_unknown_fields_require_a_schema_decision():
    document = _document()
    del document["oneshots"]["Canonical"]["operation_id"]
    with pytest.raises(LocationsError):
        _parse(document)
    with pytest.raises(LocationsError):
        _parse(_document(extra="unrecognized"))


def test_directory_at_control_path_refuses(tmp_path):
    (tmp_path / LOCATIONS_NAME).mkdir()
    with pytest.raises(LocationsError):
        load_locations(tmp_path)


def test_reparse_control_file_is_refused_before_opening(tmp_path, monkeypatch):
    # Exercise Windows' reparse attribute even on hosts without symlink privilege.
    path = tmp_path / LOCATIONS_NAME
    path.write_text(json.dumps(_document()))
    original_lstat = Path.lstat
    opened = []

    def reparse(candidate, *args, **kwargs):
        result = original_lstat(candidate, *args, **kwargs)
        if candidate == path:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    def unexpected_open(*args, **kwargs):
        opened.append(True)
        raise AssertionError("Reparse control file must not be opened")

    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(Path, "open", unexpected_open)
    with pytest.raises(LocationsError, match="reparse"):
        load_locations(tmp_path)
    assert opened == []


def test_invalid_utf8_is_not_an_empty_snapshot(tmp_path):
    path = tmp_path / LOCATIONS_NAME
    path.write_bytes(b"\xff")
    with pytest.raises(LocationsError, match="Cannot read"):
        load_locations(tmp_path)
    assert path.read_bytes() == b"\xff"


def test_unreadable_control_file_never_falls_back_to_empty(tmp_path, monkeypatch):
    path = tmp_path / LOCATIONS_NAME
    path.write_text(json.dumps(_document()))
    original_open = Path.open

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("fixture denial")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(LocationsError, match="Cannot read"):
        load_locations(tmp_path)


def test_atomic_replacement_between_stat_and_open_refuses(tmp_path, monkeypatch):
    # Force the actual interleaving: the opened inode must not be the one checked.
    path = tmp_path / LOCATIONS_NAME
    path.write_text(json.dumps(_document()))
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(_document(generation=8)))
    original_open = Path.open
    replaced = []

    def replace_before_open(candidate, *args, **kwargs):
        if candidate == path:
            replacement.replace(path)
            replaced.append(True)
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_open)
    with pytest.raises(LocationsError, match="changed before opening"):
        load_locations(tmp_path)
    assert replaced == [True]
