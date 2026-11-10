"""Keep-the-larger never deletes a copy that differs from the one it keeps.

Before this change the legacy conflict branches of ``_merge_directories`` and
``_move_loose_files`` unlinked the smaller copy: an incoming archive that lost
on size, or the library archive an incoming one replaced. Issue #174 showed
how a mistaken decision upstream then became a permanent loss. Now the loser
is moved to ``QUARANTINE_ROOT/<date>/<series>/`` (``_settle_by_size``); only a
byte-identical incoming copy is removed, which loses nothing.
"""

from __future__ import annotations

import shutil
import time
import zipfile
from pathlib import Path

import pytest

from scripts import cbz_core
from scripts import cbz_watcher as watcher

BIG_GAIN = watcher.REPLACEMENT_MIN_GAIN_BYTES + 5000


def _make_cbz(path: Path, payload_size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)
    return path


def _payload_size(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return len(zf.read("page1.jpg"))


def _quarantined(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "_quarantine").rglob("*.cbz"))


def _today_folder(tmp_path: Path, series: str) -> Path:
    return tmp_path / "_quarantine" / time.strftime("%Y-%m-%d") / series


def _settle(mover: str, src: Path, dest_dir: Path) -> None:
    """Run one collision through either mover, as the watcher would."""
    if mover == "merge":
        watcher._merge_directories(src.parent, dest_dir)
    else:
        watcher._move_loose_files([src], str(dest_dir.parent), dest_dir.name)


MOVERS = ["merge", "loose"]


@pytest.mark.parametrize("mover", MOVERS)
def test_a_meaningfully_larger_arrival_replaces_and_quarantines_the_library_copy(tmp_path, mover):
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100 + BIG_GAIN)

    _settle(mover, src, library)

    assert _payload_size(existing) == 100 + BIG_GAIN
    assert not src.exists()
    (old,) = _quarantined(tmp_path)
    assert old.parent == _today_folder(tmp_path, "Berserk") and _payload_size(old) == 100


@pytest.mark.parametrize("mover", MOVERS)
@pytest.mark.parametrize("incoming", [50, 100 + 10], ids=["smaller", "larger-below-threshold"])
def test_an_arrival_that_loses_on_size_is_quarantined_not_deleted(tmp_path, mover, incoming):
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    before = existing.read_bytes()
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", incoming)

    _settle(mover, src, library)

    assert existing.read_bytes() == before
    assert not src.exists()
    (kept,) = _quarantined(tmp_path)
    assert _payload_size(kept) == incoming


@pytest.mark.parametrize("mover", MOVERS)
def test_a_byte_identical_arrival_is_removed_and_nothing_is_quarantined(tmp_path, mover):
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    src = tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz"
    src.parent.mkdir(parents=True)
    shutil.copyfile(existing, src)

    _settle(mover, src, library)

    assert not src.exists() and existing.is_file()
    assert _quarantined(tmp_path) == []


@pytest.mark.parametrize("mover", MOVERS)
def test_an_equal_size_arrival_with_different_bytes_is_not_a_duplicate(tmp_path, mover):
    """Equal size is not equal bytes: only a digest match may delete."""
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    before = existing.read_bytes()
    src = tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz"
    src.parent.mkdir(parents=True)
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("page1.jpg", b"" * 100)
    assert src.stat().st_size == existing.stat().st_size

    _settle(mover, src, library)

    assert existing.read_bytes() == before
    (kept,) = _quarantined(tmp_path)
    with zipfile.ZipFile(kept) as zf:
        assert zf.read("page1.jpg") == b"" * 100


@pytest.mark.parametrize("mover", MOVERS)
def test_a_failed_quarantine_keeps_both_copies_where_they_were(tmp_path, monkeypatch, mover):
    """If the loser cannot be moved aside, nothing else moves: the library
    copy is untouched and the arrival stays for the next pass."""
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    before = existing.read_bytes()
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100 + BIG_GAIN)
    real_move = watcher.shutil.move

    def refuse_quarantine(source, destination):
        if "_quarantine" in str(destination):
            Path(destination).write_bytes(b"partial")  # a cross-volume copy that died midway
            raise PermissionError("quarantine unavailable")
        return real_move(source, destination)

    monkeypatch.setattr(watcher.shutil, "move", refuse_quarantine)
    _settle(mover, src, library)

    assert existing.read_bytes() == before
    assert src.is_file() and _payload_size(src) == 100 + BIG_GAIN
    assert _quarantined(tmp_path) == []  # the partial copy was cleaned up


@pytest.mark.parametrize("mover", MOVERS)
def test_a_failed_replacement_restores_the_quarantined_library_copy(tmp_path, monkeypatch, mover):
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    before = existing.read_bytes()
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100 + BIG_GAIN)
    real_move = watcher.shutil.move

    def refuse_replacement(source, destination):
        if Path(source) == src:
            raise PermissionError("library share went away")
        return real_move(source, destination)

    monkeypatch.setattr(watcher.shutil, "move", refuse_replacement)
    _settle(mover, src, library)

    assert existing.read_bytes() == before
    assert src.is_file()
    assert _quarantined(tmp_path) == []


def test_two_losers_with_one_name_on_one_day_both_survive(tmp_path):
    library = tmp_path / "library" / "Berserk"
    _make_cbz(library / "Berserk ch. 1.cbz", 100)
    for size in (40, 60):
        src = _make_cbz(tmp_path / "incoming" / f"drop-{size}" / "Berserk ch. 1.cbz", size)
        watcher._merge_directories(src.parent, library)

    assert sorted(_payload_size(p) for p in _quarantined(tmp_path)) == [40, 60]


def test_a_real_watcher_pass_quarantines_instead_of_discarding(tmp_path, monkeypatch):
    """End to end through the normal entry point (legacy routing)."""
    watch_root, library = tmp_path / "incoming", tmp_path / "library"
    watch_root.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    existing = _make_cbz(library / "Berserk" / "Berserk Ch. 1.cbz", 5000)
    before = existing.read_bytes()
    arrival = _make_cbz(watch_root / "Berserk" / "Berserk Ch. 1.cbz", 100)

    watcher.process_and_move_directory(arrival.parent)

    assert existing.read_bytes() == before
    assert [_payload_size(p) for p in _quarantined(tmp_path)] == [100]


def test_the_quarantine_root_is_configurable(tmp_path):
    args = watcher.build_argument_parser().parse_args(["--quarantine-root", str(tmp_path / "q")])
    assert args.quarantine_root == tmp_path / "q"
    default = watcher.build_argument_parser().parse_args([])
    assert default.quarantine_root == watcher.QUARANTINE_ROOT


# ---------------------------------------------------------------------------
# PR #176 review: every placement owns its path; a failure reports a failure
# ---------------------------------------------------------------------------

import threading
from concurrent.futures import ThreadPoolExecutor


def _payloads(root: Path) -> list[int]:
    return sorted(_payload_size(p) for p in root.rglob("*.cbz"))


@pytest.mark.parametrize("mover", MOVERS)
def test_two_concurrent_quarantines_of_one_name_keep_both_copies(tmp_path, monkeypatch, mover):
    """Both losers pick their candidate name before either moves (the gap
    between choosing a free name and using it). Neither may overwrite the
    other."""
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 10000)
    sources = [_make_cbz(tmp_path / "incoming" / f"drop-{n}" / "Berserk ch. 1.cbz", n)
               for n in (100, 200)]
    chosen = threading.Barrier(2, timeout=10)
    real_unique = watcher.unique_path

    def choose_then_wait(path):
        candidate = real_unique(path)
        chosen.wait()
        return candidate

    monkeypatch.setattr(watcher, "unique_path", choose_then_wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(_settle, mover, src, library) for src in sources]:
            future.result(timeout=20)

    assert _payload_size(existing) == 10000
    assert _payloads(tmp_path) == [100, 200, 10000]


@pytest.mark.parametrize("mover", MOVERS)
def test_a_failed_quarantine_never_removes_another_placements_file(tmp_path, monkeypatch, mover):
    library = tmp_path / "library" / "Berserk"
    _make_cbz(library / "Berserk ch. 1.cbz", 10000)
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100)
    real_unique, real_move = watcher.unique_path, watcher.shutil.move
    others: list[Path] = []

    def another_placement_takes_it(path):
        candidate = real_unique(path)
        others.append(_make_cbz(candidate, 300))  # completed before this one moves
        return candidate

    def refuse(source, target):
        if Path(source) == src:
            raise PermissionError("quarantine refused")
        return real_move(source, target)

    monkeypatch.setattr(watcher, "unique_path", another_placement_takes_it)
    monkeypatch.setattr(watcher.shutil, "move", refuse)
    _settle(mover, src, library)

    assert others[0].is_file() and _payload_size(others[0]) == 300
    assert _payloads(tmp_path) == [100, 300, 10000]


@pytest.mark.parametrize("mover", MOVERS)
def test_two_concurrent_landings_of_one_new_name_keep_both(tmp_path, monkeypatch, mover):
    """Two arrivals land the same new name at once; the second must not
    overwrite the first."""
    library = tmp_path / "library" / "Berserk"
    library.mkdir(parents=True)
    sources = [_make_cbz(tmp_path / "incoming" / f"drop-{n}" / "Berserk ch. 9.cbz", n)
               for n in (100, 200)]
    both_checked = threading.Barrier(2, timeout=10)
    real_exists = Path.exists

    def exists_then_wait(self):
        result = real_exists(self)
        if self.name == "Berserk ch. 9.cbz" and self.parent == library:
            try:
                both_checked.wait()
            except threading.BrokenBarrierError:
                pass
        return result

    monkeypatch.setattr(Path, "exists", exists_then_wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(_settle, mover, src, library) for src in sources]:
            future.result(timeout=20)
    monkeypatch.setattr(Path, "exists", real_exists)

    assert _payloads(tmp_path) == [100, 200]


@pytest.mark.parametrize("failure", ["quarantine", "replacement"])
def test_a_failed_loose_settlement_is_not_reported_as_landed(tmp_path, monkeypatch, failure):
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    before = existing.read_bytes()
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100 + BIG_GAIN)
    real_move = watcher.shutil.move

    def refuse(source, target):
        if ((failure == "quarantine" and "_quarantine" in Path(target).parts)
                or (failure == "replacement" and Path(source) == src)):
            raise PermissionError("placement refused")
        return real_move(source, target)

    monkeypatch.setattr(watcher.shutil, "move", refuse)
    landed = watcher._move_loose_files([src], str(library.parent), library.name)

    assert landed is None
    assert src.is_file() and existing.read_bytes() == before


def test_a_loose_file_whose_name_was_taken_meanwhile_is_not_reported_as_landed(tmp_path, monkeypatch):
    """The name was free when checked and taken before the landing (another
    arrival won the race): nothing lands, nothing is overwritten, and the
    batch is not reported as a landing."""
    library = tmp_path / "library" / "Berserk"
    winner = _make_cbz(library / "Berserk ch. 9.cbz", 300)
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 9.cbz", 100)
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists",
                        lambda self: False if self == winner else real_exists(self))
    monkeypatch.setattr(watcher, "_find_archive_collision", lambda *a, **k: None)

    landed = watcher._move_loose_files([src], str(library.parent), library.name)
    monkeypatch.setattr(Path, "exists", real_exists)

    assert landed is None
    assert src.is_file() and _payload_size(winner) == 300


# ---------------------------------------------------------------------------
# PR #176 review round 2: a placement in flight is never a library book
# ---------------------------------------------------------------------------

def _all_payloads(root: Path) -> list[int]:
    found = []
    for path in root.rglob("*.cbz"):
        try:
            found.append(_payload_size(path))
        except (zipfile.BadZipFile, KeyError, OSError):
            pass  # an empty reservation or a partial copy is not a book
    return sorted(found)


@pytest.mark.parametrize("mover", MOVERS + ["resolver-merge"])
@pytest.mark.parametrize("first_fails", [False, True], ids=["completes", "fails"])
@pytest.mark.parametrize("partial", [False, True], ids=["empty", "partial-copy"])
def test_a_second_collision_defers_to_a_landing_in_flight(tmp_path, monkeypatch, mover, first_fails, partial):
    """The first arrival owns the new library name and pauses mid-landing
    (its reservation empty, or part-copied). A second arrival for the same
    name must not treat that as a library book to quarantine or replace:
    it defers. Whether the first then completes or fails, both copies
    survive."""
    library = tmp_path / "library" / "Berserk"
    library.mkdir(parents=True)
    dest = library / "Berserk ch. 9.cbz"
    first = _make_cbz(tmp_path / "incoming" / "first" / dest.name, 10000)
    second = _make_cbz(tmp_path / "incoming" / "second" / dest.name, 20000)
    paused, release = threading.Event(), threading.Event()
    real_move = watcher.shutil.move

    def pause_first(source, target):
        if Path(source) == first and Path(target) == dest:
            assert list(library.glob(f".{dest.name}{watcher.RESERVE_MARK}*"))
            if partial:
                dest.write_bytes(b"partial" * 9)
            paused.set()
            assert release.wait(timeout=10)
            if first_fails:
                raise PermissionError("first landing failed")
        return real_move(source, target)

    def settle(src):
        if mover == "loose":
            return watcher._move_loose_files([src], str(library.parent), library.name)
        quarantine = tmp_path / "_resolver_q" if mover == "resolver-merge" else None
        return watcher._merge_directories(src.parent, library, conflict_quarantine_root=quarantine)

    monkeypatch.setattr(watcher.shutil, "move", pause_first)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_done = pool.submit(settle, first)
        assert paused.wait(timeout=10)
        try:
            second_result = settle(second)
        finally:
            release.set()
        if first_fails:
            with pytest.raises(PermissionError):
                first_done.result(timeout=10)
        else:
            first_done.result(timeout=10)

    assert second.is_file()  # deferred, not settled against the placement in flight
    if mover == "loose":
        assert second_result is None
    assert _all_payloads(tmp_path) == [10000, 20000]
    assert not list(library.glob(f".*{watcher.RESERVE_MARK}*"))  # the owner cleaned up its marker


@pytest.mark.parametrize("mover", MOVERS)
def test_another_watchers_marker_defers_the_collision(tmp_path, mover):
    """A marker left by another watcher (same library, another process) is
    respected the same way: the arrival stays, nothing is replaced."""
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    before = existing.read_bytes()
    (library / f".{existing.name}{watcher.RESERVE_MARK}other-process").write_text("{}")
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100 + BIG_GAIN)

    _settle(mover, src, library)

    assert src.is_file() and existing.read_bytes() == before
    assert _quarantined(tmp_path) == []


@pytest.mark.parametrize("failure", ["quarantine", "replacement"])
def test_an_all_failed_directory_merge_has_no_landing_receipt(tmp_path, monkeypatch, failure):
    """The directory version of the loose-file receipt: nothing completed,
    so ``_move_cbz_dir`` must not return the library folder as landed."""
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    before = existing.read_bytes()
    src = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100 + BIG_GAIN)
    real_move = watcher.shutil.move

    def refuse(source, target):
        if ((failure == "quarantine" and "_quarantine" in Path(target).parts)
                or (failure == "replacement" and Path(source) == src)):
            raise PermissionError("placement refused")
        return real_move(source, target)

    monkeypatch.setattr(watcher.shutil, "move", refuse)
    landed = watcher._move_cbz_dir(src.parent, str(library.parent), target_name=library.name)

    assert landed is None
    assert src.is_file() and existing.read_bytes() == before


def test_a_directory_merge_with_one_completed_archive_is_a_landing(tmp_path, monkeypatch):
    library = tmp_path / "library" / "Berserk"
    existing = _make_cbz(library / "Berserk ch. 1.cbz", 100)
    failed = _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 1.cbz", 100 + BIG_GAIN)
    _make_cbz(tmp_path / "incoming" / "Berserk" / "Berserk ch. 2.cbz", 200)
    real_move = watcher.shutil.move

    def refuse(source, target):
        if "_quarantine" in Path(target).parts:
            raise PermissionError("quarantine refused")
        return real_move(source, target)

    monkeypatch.setattr(watcher.shutil, "move", refuse)
    landed = watcher._move_cbz_dir(failed.parent, str(library.parent), target_name=library.name)

    assert landed == library
    assert (library / "Berserk ch. 2.cbz").is_file() and failed.is_file()
