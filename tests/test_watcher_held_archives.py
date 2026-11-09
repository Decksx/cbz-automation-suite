"""Held archives: one refusal no longer holds back a whole series.

Incident (2026-11-09): a 710-archive Usogui arrival had 54 archives refused
by media naming. The watcher deferred the whole directory, so none of the
656 good archives moved, and every restart re-processed all 710 (about 45
minutes) only to defer them again. The folder also held two scanlations of
112 chapters, which the watcher passed through as different books.

These tests pin the three changes:

* only the refused archives stay in the import folder; their siblings move
  (``_move_cbz_dir``'s *held*), and a refused placeholder still never
  reaches the library;
* a second copy of a chapter from another group is held for the operator,
  never deleted (``_duplicate_chapter_copies``);
* a directory left holding only held archives is skipped while it is
  unchanged (``_held_directory_unchanged`` / ``_remember_held_directory``).
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from comic_automation.library.number_style import NumberStyleRegistry
from scripts import cbz_core
from scripts import cbz_watcher as watcher


def _make_cbz(path: Path, payload_size: int = 100) -> Path:
    """A one-page archive whose page size identifies it after a move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page1.jpg", b"\0" * payload_size)
    return path


def _payload_size(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return len(zf.read("page1.jpg"))


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("*.cbz")) if folder.exists() else []


@pytest.fixture
def watch(tmp_path, monkeypatch):
    """A tmp watch folder, library and held-state file, offline."""
    watch_root = tmp_path / "incoming"
    library = tmp_path / "library"
    watch_root.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
    monkeypatch.setattr(watcher, "HELD_STATE_FILE", tmp_path / "state" / "held.json")
    monkeypatch.setattr(watcher, "wait_for_file_stable", lambda *a, **k: True)
    monkeypatch.setattr(cbz_core, "translate_metadata_text", lambda text: (text, None))
    return watch_root, library


@pytest.fixture
def media_watch(watch, tmp_path, monkeypatch):
    """`watch` with media naming on: the library root registered as chapter style."""
    watch_root, library = watch
    registry = tmp_path / "number_styles.json"
    registry.write_text(json.dumps(
        {"version": 1, "roots": [{"path": str(library), "style": "chapter"}]}))
    monkeypatch.setattr(watcher, "_number_style_registry", NumberStyleRegistry.load(registry))
    monkeypatch.setattr(watcher, "_number_style_requested", "auto")
    return watch_root, library


def _held_state(tmp_path: Path) -> dict:
    path = tmp_path / "state" / "held.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------------------------------------------------------------------------
# Fix 1: only the refused archives stay behind
# ---------------------------------------------------------------------------

def test_one_refused_archive_no_longer_holds_back_its_series(media_watch):
    """The Usogui shape: media naming refuses one archive (two different
    chapter numbers in its name). The other two land under their canonical
    names; only the refused one stays in the import folder."""
    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    _make_cbz(arrival / "Team Duwang Vol.1 Ch.1 - Gambler.cbz", 101)
    _make_cbz(arrival / "Team Duwang Vol.1 Ch.2 - Loan.cbz", 102)
    _make_cbz(arrival / "Team Duwang Vol.30 Ch.323 - Chapter 1, Fin.cbz", 323)

    watcher.process_and_move_directory(arrival)

    assert _names(library / "Usogui") == ["Usogui v01 ch. 1.cbz", "Usogui v01 ch. 2.cbz"]
    assert _names(arrival) == ["Team Duwang Vol.30 Ch.323 - Chapter 1, Fin.cbz"]
    assert _payload_size(arrival / "Team Duwang Vol.30 Ch.323 - Chapter 1, Fin.cbz") == 323


def test_a_refused_placeholder_stays_while_its_numbered_sibling_lands(watch):
    """Legacy naming: the generic placeholder is refused (two editions) and
    must never reach the series folder, where the next chapter sharing its
    generic name would collide with it. Its numbered sibling lands."""
    watch_root, library = watch
    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "volvox_Chapter (Colored) (Uncensored).cbz", 400)
    _make_cbz(arrival / "Berserk Ch. 5.cbz", 500)

    watcher.process_and_move_directory(arrival)

    assert _names(library / "Berserk") == ["Berserk Ch. 5.cbz"]
    assert not list(library.rglob("*volvox*"))
    left = list(watch_root.rglob("*.cbz"))
    assert len(left) == 1 and _payload_size(left[0]) == 400


def test_an_all_held_directory_creates_no_empty_series_folder(watch):
    """With every archive held there is nothing to move, so the series
    folder must not be created just to stay empty."""
    watch_root, library = watch
    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "volvox_Chapter (Colored) (Uncensored).cbz", 400)

    watcher.process_and_move_directory(arrival)

    assert list(library.iterdir()) == []
    assert len(list(watch_root.rglob("*.cbz"))) == 1


def test_held_archives_are_skipped_by_the_merge_itself(tmp_path):
    """Unit view of the guard inside _merge_directories: a held file is not
    moved even when its destination name is free."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    keep = _make_cbz(src / "A Ch. 1.cbz")
    move = _make_cbz(src / "A Ch. 2.cbz")
    dest.mkdir()

    watcher._merge_directories(src, dest, held=frozenset({keep}))

    assert keep.exists() and not move.exists()
    assert _names(dest) == ["A Ch. 2.cbz"]


# ---------------------------------------------------------------------------
# Fix 4: a second translation of a chapter is held, never deleted
# ---------------------------------------------------------------------------

def test_a_second_groups_copy_of_a_chapter_is_held_for_review(watch):
    """Two scanlations of Ch.26. Team Duwang has more archives here, so its
    copy lands; Easy Going's copy stays in the import folder, intact."""
    watch_root, library = watch
    arrival = watch_root / "Usogui"
    for chapter in (26, 27, 28):
        _make_cbz(arrival / f"Team Duwang Vol.3 Ch.{chapter} - T{chapter}.cbz", 200 + chapter)
    _make_cbz(arrival / "Easy Going Vol.3 Ch.26 - I bet!.cbz", 926)  # larger, still held
    _make_cbz(arrival / "Easy Going Vol.3 Ch.30 - Other.cbz", 930)

    watcher.process_and_move_directory(arrival)

    landed = _names(library / "Usogui")
    assert "Team Duwang Vol.3 Ch.26 - T26.cbz" in landed
    assert "Easy Going Vol.3 Ch.30 - Other.cbz" in landed
    assert not any("I bet" in name for name in landed)
    assert _names(arrival) == ["Easy Going Vol.3 Ch.26 - I bet!.cbz"]
    assert _payload_size(arrival / "Easy Going Vol.3 Ch.26 - I bet!.cbz") == 926


def _parsed(paths):
    return {p: cbz_core.ParsedComicName(p, p.name, "s", "s", None, None) for p in paths}


def test_copies_rank_by_group_size_then_file_size(tmp_path):
    big_group = [_make_cbz(tmp_path / f"Alpha Vol.1 Ch.{n}.cbz", 10) for n in (1, 2, 3)]
    small_group = [_make_cbz(tmp_path / f"Beta Vol.1 Ch.{n}.cbz", 999) for n in (1, 2)]
    assert watcher._duplicate_chapter_copies(_parsed(big_group + small_group), "Usogui") == {
        small_group[0]: big_group[0], small_group[1]: big_group[1]}

    # Equal group sizes: the larger file is kept.
    a = [_make_cbz(tmp_path / "eq" / f"Gamma Ch.{n}.cbz", 10) for n in (1, 2)]
    b = [_make_cbz(tmp_path / "eq" / f"Delta Ch.{n}.cbz", 20 if n == 1 else 5) for n in (1, 2)]
    assert watcher._duplicate_chapter_copies(_parsed(a + b), "Usogui") == {a[0]: b[0], b[1]: a[1]}


@pytest.mark.parametrize("series, names", [
    # One-shots that are each "#1": the prefix is a title, not a group.
    ("Oneshots", ["Alpha #1.cbz", "Beta #1.cbz"]),
    # One group, same number: not a second translation (parts, re-uploads).
    ("Alpha", ["Alpha Ch.5 - Part A.cbz", "Alpha Ch.5 - Part B.cbz", "Alpha Ch.6.cbz"]),
    # Different volumes are different books.
    ("Alpha", ["Alpha Vol.1 Ch.1.cbz", "Alpha Vol.1 Ch.2.cbz", "Beta Vol.2 Ch.1.cbz", "Beta Vol.2 Ch.3.cbz"]),
    # The series name plus more words is another title, not a group:
    # "Annual" is no special-vocabulary word, so only this rule stops it.
    ("Batman", ["Batman #1.cbz", "Batman #2.cbz", "Batman Annual #1.cbz", "Batman Annual #2.cbz"]),
])
def test_only_true_second_translations_are_held(tmp_path, series, names):
    paths = [_make_cbz(tmp_path / name) for name in names]
    assert watcher._duplicate_chapter_copies(_parsed(paths), series) == {}


# ---------------------------------------------------------------------------
# Fix 2: an unchanged held directory is not re-processed
# ---------------------------------------------------------------------------

def _count_processing(monkeypatch) -> list[Path]:
    calls: list[Path] = []
    real = watcher.process_cbz_file

    def counting(path, *args, **kwargs):
        calls.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(watcher, "process_cbz_file", counting)
    return calls


def _held_arrival(watch_root: Path) -> Path:
    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "volvox_Chapter (Colored) (Uncensored).cbz", 400)
    _make_cbz(arrival / "Berserk Ch. 5.cbz", 500)
    return arrival


def test_an_unchanged_held_directory_is_skipped_on_the_next_pass(watch, tmp_path, monkeypatch):
    watch_root, library = watch
    arrival = _held_arrival(watch_root)
    watcher.process_and_move_directory(arrival)
    state = _held_state(tmp_path)
    assert list(state) == [str(arrival)]
    assert state[str(arrival)]["held"] == ["volvox Chapter (Colored) (Uncensored).cbz"]

    calls = _count_processing(monkeypatch)
    watcher.process_and_move_directory(arrival)
    assert calls == []


def test_any_change_to_a_held_directory_retries_it(watch, tmp_path, monkeypatch):
    watch_root, library = watch
    arrival = _held_arrival(watch_root)
    watcher.process_and_move_directory(arrival)
    calls = _count_processing(monkeypatch)

    _make_cbz(arrival / "Berserk Ch. 6.cbz", 600)
    watcher.process_and_move_directory(arrival)

    assert calls  # re-processed
    assert "Berserk Ch. 6.cbz" in _names(library / "Berserk")

    # Touching the held file alone is also a change.
    calls.clear()
    held = next(arrival.glob("*.cbz"))
    stat = held.stat()
    os.utime(held, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    watcher.process_and_move_directory(arrival)
    assert calls == [held]


def test_a_watcher_upgrade_retries_every_held_directory(watch, monkeypatch):
    watch_root, library = watch
    arrival = _held_arrival(watch_root)
    watcher.process_and_move_directory(arrival)
    calls = _count_processing(monkeypatch)
    monkeypatch.setattr(watcher, "WATCHER_SOURCE_VERSION", "sha256:new")

    watcher.process_and_move_directory(arrival)

    assert calls


def test_deleting_the_entry_forces_a_retry(watch, tmp_path, monkeypatch):
    watch_root, library = watch
    arrival = _held_arrival(watch_root)
    watcher.process_and_move_directory(arrival)
    (tmp_path / "state" / "held.json").unlink()
    calls = _count_processing(monkeypatch)

    watcher.process_and_move_directory(arrival)

    assert calls


def test_a_failed_move_is_not_remembered_as_held(watch, tmp_path, monkeypatch):
    """A move that failed (the library share dropped, say) leaves archives
    that were never held. Recording the directory would turn a transient
    failure into a permanent skip, so nothing is recorded."""
    watch_root, library = watch
    arrival = _held_arrival(watch_root)
    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)

    watcher.process_and_move_directory(arrival)

    assert len(_names(arrival)) == 2
    assert _held_state(tmp_path) == {}


def test_an_emptied_directory_is_forgotten(watch, tmp_path):
    watch_root, library = watch
    arrival = _held_arrival(watch_root)
    watcher.process_and_move_directory(arrival)
    assert _held_state(tmp_path)

    # The operator resolves the held file by hand; the next pass lands the
    # replacement and the entry is dropped.
    for path in arrival.glob("*.cbz"):
        path.unlink()
    _make_cbz(arrival / "Berserk Ch. 7.cbz", 700)
    watcher.process_and_move_directory(arrival)

    assert _held_state(tmp_path) == {}
    assert "Berserk Ch. 7.cbz" in _names(library / "Berserk")


def test_a_registry_refusal_is_retried_not_remembered(media_watch, tmp_path, monkeypatch):
    """A style the registry cannot select (a root briefly inaccessible, say)
    refuses every archive without any file being wrong. Remembering that as
    held would skip the directory after the cause cleared, so it is not."""
    from comic_automation.library.number_style import NumberStyleRefused

    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    _make_cbz(arrival / "Team Duwang Vol.1 Ch.1 - Gambler.cbz")

    def refuse(*_args, **_kwargs):
        raise NumberStyleRefused("auto number style needs exactly one registered root")

    monkeypatch.setattr(type(watcher._number_style_registry), "select", refuse)
    watcher.process_and_move_directory(arrival)

    assert _names(arrival) == ["Team Duwang Vol.1 Ch.1 - Gambler.cbz"]
    assert _held_state(tmp_path) == {}


# ---------------------------------------------------------------------------
# PR #173 review (2026-11-09): five cases reproduced by Codex, plus the
# connector's P1. Each failed at c2805816.
# ---------------------------------------------------------------------------

def _with_number(path: Path, number) -> Path:
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("ComicInfo.xml",
                         f"<ComicInfo><Series>Usogui</Series><Number>{number}</Number></ComicInfo>")
    return path


def test_a_failed_move_of_a_deferred_tagged_arrival_is_retried(watch, tmp_path, monkeypatch):
    """A deferred tag-stripping rename is a plan, not a hold. If the move
    fails, the arrival was never held on purpose and must be retried."""
    watch_root, library = watch
    arrival = watch_root / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz")
    real_move = watcher._move_cbz_dir
    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)
    watcher.process_and_move_directory(arrival)
    assert _held_state(tmp_path) == {}

    monkeypatch.setattr(watcher, "_move_cbz_dir", real_move)
    watcher.process_and_move_directory(arrival)
    assert (library / "Solo Leveling" / "Solo Leveling ch. 4.cbz").is_file()


@pytest.mark.parametrize("tag_a, tag_b", [
    pytest.param("(2019)", "(2020)", id="years"),
    pytest.param("(s1)", "(s2)", id="seasons"),
])
def test_year_and_season_variants_are_not_second_translations(watch, tag_a, tag_b):
    watch_root, library = watch
    arrival = watch_root / "Usogui"
    for group, tag in (("Alpha", tag_a), ("Beta", tag_b)):
        for chapter in (1, 2):
            _make_cbz(arrival / f"{group} Ch.{chapter} {tag}.cbz")

    watcher.process_and_move_directory(arrival)

    assert len(list(library.rglob("*.cbz"))) == 4
    assert not list(arrival.glob("*.cbz"))


def test_a_replacement_with_the_same_size_and_mtime_is_retried(watch, monkeypatch):
    """Different page bytes, same archive length and mtime, swapped in with
    os.replace: names, sizes and mtimes alone cannot see it."""
    watch_root, library = watch
    arrival = _held_arrival(watch_root)
    watcher.process_and_move_directory(arrival)
    held = next(arrival.glob("*.cbz"))
    before = held.stat()
    replacement = held.with_suffix(".new")
    with zipfile.ZipFile(held) as source, zipfile.ZipFile(replacement, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            target.writestr(info, b"\1" * len(data) if info.filename == "page1.jpg" else data)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, held)
    assert held.stat().st_size == before.st_size and held.stat().st_mtime_ns == before.st_mtime_ns

    calls = _count_processing(monkeypatch)
    watcher.process_and_move_directory(arrival)
    assert calls


def test_a_refused_preferred_copy_does_not_hold_back_the_valid_one(media_watch):
    """Beta has more archives, so its Ch.1 is preferred, but its ComicInfo
    Number (99) conflicts and naming refuses it. Alpha's valid Ch.1 must
    then land instead of waiting behind a copy that cannot."""
    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    for group, chapters in (("Alpha", (1, 10)), ("Beta", (1, 2, 3))):
        for chapter in chapters:
            number = 99 if (group, chapter) == ("Beta", 1) else chapter
            _with_number(_make_cbz(arrival / f"{group} Ch.{chapter}.cbz"), number)

    watcher.process_and_move_directory(arrival)

    assert (library / "Usogui" / "Usogui ch. 1.cbz").is_file()
    assert _names(arrival) == ["Beta Ch.1.cbz"]


def test_held_second_copies_stay_held_when_a_new_chapter_arrives(media_watch):
    """The connector's P1. After the first pass the kept copies are in the
    library, so the intake alone no longer shows the held copies as second
    translations. A new arrival must not release them into a merge that
    renames them onto the library's names and discards one."""
    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    for chapter in (1, 2, 3):
        _make_cbz(arrival / f"Alpha Ch.{chapter}.cbz", 100 + chapter)
    for chapter in (1, 2):
        _make_cbz(arrival / f"Beta Ch.{chapter}.cbz", 900 + chapter)
    watcher.process_and_move_directory(arrival)
    assert _names(arrival) == ["Beta Ch.1.cbz", "Beta Ch.2.cbz"]

    _make_cbz(arrival / "Alpha Ch.4.cbz", 104)
    watcher.process_and_move_directory(arrival)

    assert _names(arrival) == ["Beta Ch.1.cbz", "Beta Ch.2.cbz"]
    assert _payload_size(arrival / "Beta Ch.1.cbz") == 901
    landed = library / "Usogui"
    assert _names(landed) == [f"Usogui ch. {n}.cbz" for n in (1, 2, 3, 4)]
    assert _payload_size(landed / "Usogui ch. 1.cbz") == 101
