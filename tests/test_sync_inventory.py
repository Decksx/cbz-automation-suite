"""Private #108 filesystem evidence, including controlled drift and failures."""
import hashlib
import json
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from comic_automation.library import sync_inventory as sync


@pytest.fixture
def pair(tmp_path):
    tower, target = tmp_path / "tower", tmp_path / "target"
    tower.mkdir()
    target.mkdir()
    return sync.RootPair("manga", tower, target)


def put(root, name, payload):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_common_size_candidates_are_fully_hashed_once_and_sources_preserved(pair, monkeypatch):
    put(pair.tower, "Book/1.cbz", b"same")
    put(pair.target, "Renamed/1.cbz", b"same")
    put(pair.target, "Other/1.cbz", b"else")
    put(pair.tower, "unique.cbz", b"unique size")
    original = sync._hash
    calls = []
    def measured(root, row):
        calls.append((root, row["relative_path"]))
        return original(root, row)
    monkeypatch.setattr(sync, "_hash", measured)
    before = {p: p.read_bytes() for root in (pair.tower, pair.target) for p in root.rglob("*") if p.is_file()}
    report = sync.observe_pairs((pair,))
    assert report["complete"] and report["file_counts"] == {"target": 2, "tower": 2}
    assert len(calls) == len(set(calls)) == 3
    files = {row["relative_path"]: row for row in report["files"]}
    assert files["Book/1.cbz"]["sha256"] == files["Renamed/1.cbz"]["sha256"] == hashlib.sha256(b"same").hexdigest()
    assert files["Other/1.cbz"]["sha256"] != files["Book/1.cbz"]["sha256"]
    assert files["unique.cbz"]["sha256"] is None and files["unique.cbz"]["bytes_read"] == 0
    assert all(row["bytes_read"] == 4 for row in report["files"] if row["sha256"])
    assert before == {p: p.read_bytes() for p in before}
    assert sync.observe_pairs((pair,)) == report
    body = {k: v for k, v in report.items() if k != "observation_digest"}
    assert report["observation_digest"] == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def test_candidates_are_global_across_explicit_folder_ids(pair, tmp_path):
    other_tower, other_target = tmp_path / "other-tower", tmp_path / "other-target"
    other_tower.mkdir()
    other_target.mkdir()
    put(pair.tower, "source.cbz", b"abc")
    put(other_target, "match.cbz", b"abc")
    report = sync.observe_pairs((pair, sync.RootPair("comix", other_tower, other_target)))
    assert report["complete"] and all(row["sha256"] == hashlib.sha256(b"abc").hexdigest() for row in report["files"])
    assert {row["folder_id"] for row in report["files"]} == {"comix", "manga"}


@pytest.mark.parametrize("name", [".stversions/old.cbz", ".stfolder/marker", ".stignore", ".syncthing.book.cbz.tmp", "ai-decensor-originals/Book/1.cbz", "Book/AI_DECENSOR_ORIGINALS/1.cbz", "skip/private.cbz"])
def test_excluded_paths_are_recorded_and_never_hashed(pair, monkeypatch, name):
    for root in (pair.tower, pair.target):
        put(root, name, b"secret original")
    monkeypatch.setattr(sync, "_hash", lambda *args: pytest.fail("excluded payload was opened"))
    report = sync.observe_pairs((sync.RootPair("manga", pair.tower, pair.target, ("skip",)),))
    assert report["complete"] and report["files"] == []
    assert len(report["excluded"]) == 2
    assert all(row["reason"] for row in report["excluded"])


@pytest.mark.parametrize("mode", ["same", "nested", "missing", "file", "protected"])
def test_invalid_roots_prevent_payload_reads(pair, monkeypatch, mode):
    target = pair.target
    if mode == "same":
        target = pair.tower
    elif mode == "nested":
        target = pair.tower / "child"
        target.mkdir()
    elif mode == "missing":
        target /= "missing"
    elif mode == "file":
        target = put(pair.target, "file", b"x")
    else:
        target /= "ai-decensor-originals"
        target.mkdir()
    monkeypatch.setattr(sync, "_walk", lambda *args: pytest.fail("unbound roots were traversed"))
    report = sync.observe_pairs((sync.RootPair("manga", pair.tower, target),))
    assert not report["complete"] and report["problems"] and not report["files"]


def test_alias_or_nested_roots_across_pairs_are_refused(pair, tmp_path):
    third = tmp_path / "third"
    third.mkdir()
    report = sync.observe_pairs((pair, sync.RootPair("other", pair.target, third)))
    assert not report["complete"] and "overlap or alias" in report["problems"][0]["reason"]


@pytest.mark.parametrize("prefix", ["../escape", "/absolute", "C:/drive", "skip//child", "NUL", "bad.", "bad "])
def test_invalid_exclusion_declarations_are_refused(pair, prefix):
    with pytest.raises(ValueError):
        sync.observe_pairs((sync.RootPair("manga", pair.tower, pair.target, (prefix,)),))


@pytest.mark.parametrize("name", ["CON", "aux.cbz", "COM1.txt", "dir/bad.", "dir/bad ", "a:b", "a\\b", "a?b", "a\x01b", "COM\u00b9.cbz", "LPT\u00b2.txt", "CON .cbz"])
def test_windows_unrepresentable_paths_are_rejected(name):
    with pytest.raises(ValueError):
        sync._portable(name)


def test_case_and_unicode_path_aliases_are_unresolved_even_on_windows(pair, monkeypatch):
    # Simulate two names from a case-sensitive Tower listing on this host.
    first = put(pair.tower, "Book.cbz", b"a")
    original = sync._key
    monkeypatch.setattr(sync, "_key", lambda name: "same-key" if name in {"Book.cbz", "Other.cbz"} else original(name))
    put(pair.tower, "Other.cbz", b"b")
    report = sync.observe_pairs((pair,))
    assert not report["complete"] and any("collision" in row["reason"] for row in report["problems"])
    assert sync._key("e\u0301/FILE.cbz") == sync._key("\u00e9/file.CBZ")
    assert first.read_bytes() == b"a"


@pytest.mark.parametrize("phase", ["before", "during", "after", "unreadable"])
def test_required_hash_failures_never_supply_a_trusted_hash(pair, monkeypatch, phase):
    source = put(pair.tower, "book.cbz", b"abcd")
    put(pair.target, "book.cbz", b"abcd")
    original_open = Path.open
    reads = []
    class Stream:
        def __init__(self, raw):
            self.raw = raw
            self.changed = False
        def __enter__(self):
            if phase == "before":
                source.write_bytes(b"changed")
            return self
        def __exit__(self, *args):
            self.raw.close()
            if phase == "after":
                source.write_bytes(b"changed")
        def fileno(self):
            return self.raw.fileno()
        def read(self, size):
            reads.append(size)
            chunk = self.raw.read(size)
            if phase == "during" and not self.changed:
                self.changed = True
                source.write_bytes(b"changed")
            return chunk
    def controlled(path, mode="r", *args, **kwargs):
        if path == source and mode == "rb":
            if phase == "unreadable":
                raise PermissionError("controlled unreadable payload")
            return Stream(original_open(path, mode, *args, **kwargs))
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", controlled)
    report = sync.observe_pairs((pair,))
    row = next(row for row in report["files"] if row["side"] == "tower")
    assert row["sha256"] is None and row["bytes_read"] == 0
    assert not report["complete"] and report["problems"]
    if phase == "before":
        assert reads == []  # reject the changed handle before reading its bytes


def test_late_tree_arrival_is_reported_even_when_original_files_are_stable(pair, monkeypatch):
    put(pair.tower, "book.cbz", b"abcd")
    put(pair.target, "book.cbz", b"abcd")
    original = sync._hash
    def arrival(root, row):
        result = original(root, row)
        if root == pair.target:
            put(pair.tower, "late.cbz", b"new")
        return result
    monkeypatch.setattr(sync, "_hash", arrival)
    report = sync.observe_pairs((pair,))
    assert not report["complete"] and any("tree changed" in row["reason"] for row in report["problems"])


def test_unlistable_directory_is_reported(pair, monkeypatch):
    original = os.scandir
    def deny(path):
        if Path(path) == pair.tower:
            raise PermissionError("controlled listing failure")
        return original(path)
    monkeypatch.setattr(os, "scandir", deny)
    report = sync.observe_pairs((pair,))
    assert not report["complete"] and any("listing failure" in row["reason"] for row in report["problems"])


def test_hard_link_is_reported_without_hashing(pair, monkeypatch):
    source = put(pair.tower, "book.cbz", b"abcd")
    os.link(source, pair.tower / "alias.cbz")
    monkeypatch.setattr(sync, "_hash", lambda *args: pytest.fail("hard link read"))
    report = sync.observe_pairs((pair,))
    assert not report["complete"] and any("hard-linked" in row["reason"] for row in report["problems"])


def test_empty_files_have_the_full_empty_digest(pair):
    put(pair.tower, "empty.cbz", b"")
    put(pair.target, "empty.cbz", b"")
    report = sync.observe_pairs((pair,))
    assert report["complete"] and len(report["files"]) == 2
    assert all(row["sha256"] == hashlib.sha256(b"").hexdigest() and row["bytes_read"] == 0 for row in report["files"])


def altered(info, **changes):
    values = {key: getattr(info, key) for key in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")}
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize("place", ["root", "descendant", "ancestor"])
def test_reparse_paths_are_refused_before_traversal(pair, monkeypatch, place):
    nested = pair.tower / "nested"
    put(nested, "book.cbz", b"abcd")
    put(pair.target, "book.cbz", b"abcd")
    marked = {"root": pair.tower, "descendant": nested, "ancestor": pair.tower.parent}[place]
    original = Path.lstat
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        return altered(info, st_file_attributes=0x400) if path == marked else info
    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(sync, "_hash", lambda *args: pytest.fail("a reparse subtree was opened"))
    report = sync.observe_pairs((pair,))
    assert not report["complete"] and report["problems"]


def test_file_without_physical_identity_is_unresolved(pair, monkeypatch):
    source = put(pair.tower, "book.cbz", b"unshared size")
    original = Path.lstat
    def unknown(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        return altered(info, st_ino=0) if path == source else info
    monkeypatch.setattr(Path, "lstat", unknown)
    report = sync.observe_pairs((pair,))
    assert not report["complete"] and any("identity" in row["reason"] for row in report["problems"])


def test_a_changed_post_read_handle_is_unresolved_even_if_the_final_walk_matches(pair, monkeypatch):
    source = put(pair.tower, "book.cbz", b"abcd")
    put(pair.target, "book.cbz", b"abcd")
    original = os.fstat
    calls = []
    inode = source.stat().st_ino
    def transient(handle):
        info = original(handle)
        if info.st_ino == inode:
            calls.append(1)
            if len(calls) == 2:
                return altered(info, st_mtime_ns=info.st_mtime_ns + 1)
        return info
    monkeypatch.setattr(os, "fstat", transient)
    report = sync.observe_pairs((pair,))
    row = next(row for row in report["files"] if row["side"] == "tower")
    assert not report["complete"] and row["sha256"] is None


def test_a_truncated_read_cannot_be_accepted_on_unchanged_stat_values(pair, monkeypatch):
    source = put(pair.tower, "book.cbz", b"abcd")
    put(pair.target, "book.cbz", b"abcd")
    original = Path.open
    class ShortRead:
        def __init__(self, raw):
            self.raw = raw
            self.first = True
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.raw.close()
        def fileno(self):
            return self.raw.fileno()
        def read(self, size):
            if self.first:
                self.first = False
                return self.raw.read(size)[:-1]
            return b""
    def short(path, mode="r", *args, **kwargs):
        raw = original(path, mode, *args, **kwargs)
        return ShortRead(raw) if path == source and mode == "rb" else raw
    monkeypatch.setattr(Path, "open", short)
    report = sync.observe_pairs((pair,))
    row = next(row for row in report["files"] if row["side"] == "tower")
    assert not report["complete"] and row["sha256"] is None


def test_invalid_pair_identifiers_are_rejected_before_any_walk(pair):
    for pairs in ((), (pair, pair), (sync.RootPair("", pair.tower, pair.target),)):
        with pytest.raises(ValueError):
            sync.observe_pairs(pairs)


def test_exclusion_scope_is_bound_even_when_the_trees_are_empty(pair):
    plain = sync.observe_pairs((pair,))
    scoped = sync.observe_pairs((sync.RootPair("manga", pair.tower, pair.target, ("private",)),))
    assert plain["complete"] and scoped["complete"]
    assert plain["observation_digest"] != scoped["observation_digest"]
    assert all(root["exclude_prefixes"] == ["private"] for root in scoped["roots"])
    assert "ai-decensor-originals" in scoped["exclusion_policy"]["component_names"]


def test_a_root_replaced_between_binding_and_first_walk_is_unresolved(pair, monkeypatch):
    original = sync._walk
    replaced = []
    def replacement(root, prefixes):
        if root == pair.tower and not replaced:
            root.rename(root.with_name("former-tower"))
            root.mkdir()
            replaced.append(True)
        return original(root, prefixes)
    monkeypatch.setattr(sync, "_walk", replacement)
    report = sync.observe_pairs((pair,))
    assert replaced and not report["complete"]
    assert any("tree changed" in row["reason"] for row in report["problems"])
