"""Held archives: decided once, then moved out of the way.

Incident (2026-11-09): a 710-archive Usogui arrival had 54 archives refused
by media naming. The watcher deferred the whole directory, so none of the
656 good archives moved, and every restart re-processed all 710. The folder
also held two scanlations of 112 chapters, which passed through as
different books. Keeping held copies in the (MEGA-synced) watch folder then
needed a persisted memory of every hold, and each way of losing that memory
lost a book (issue #174, PRs #173 and #175).

With claim-first processing (``comic_automation.library.claims``) a held
archive leaves the arrival as soon as it is decided:

* a refused archive, or one whose AI rewrite was interrupted, goes to
  review (``REVIEW_ROOT``); its siblings land;
* a second translation of a chapter from another group goes to
  quarantine (``QUARANTINE_ROOT``); the kept copy lands;
* a refusal that may clear by itself (a locked file, a registry style
  refusal) stays in processing and is retried.

The folder an archive is in is its state, so nothing about a hold needs
remembering, and a new arrival can never release it.
"""

from __future__ import annotations

import json
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


def _placed(root: Path) -> list[str]:
    """Names placed anywhere below a review or quarantine root."""
    return sorted(p.name for p in root.rglob("*.cbz")) if root.exists() else []


def _payloads(root: Path) -> list[int]:
    return sorted(_payload_size(p) for p in root.rglob("*.cbz")) if root.exists() else []


@pytest.fixture
def watch(tmp_path, monkeypatch):
    """A tmp watch folder and library wired into the watcher, offline."""
    watch_root = tmp_path / "incoming"
    library = tmp_path / "library"
    watch_root.mkdir()
    library.mkdir()
    monkeypatch.setattr(watcher, "WATCH_FOLDER", str(watch_root))
    monkeypatch.setattr(watcher, "_routing_rules", [])
    monkeypatch.setattr(watcher, "_routing_default", str(library))
    monkeypatch.setattr(watcher, "POLL_INTERVAL", 0)
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


def _with_number(path: Path, number) -> Path:
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("ComicInfo.xml",
                         f"<ComicInfo><Series>Usogui</Series><Number>{number}</Number></ComicInfo>")
    return path


# ---------------------------------------------------------------------------
# A refusal holds back only the refused archive, which goes to review
# ---------------------------------------------------------------------------

def test_one_refused_archive_no_longer_holds_back_its_series(media_watch, tmp_path):
    """The Usogui shape: media naming refuses one archive (two different
    chapter numbers in its name). The other two land under their canonical
    names; the refused one goes to review, intact."""
    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    _make_cbz(arrival / "Team Duwang Vol.1 Ch.1 - Gambler.cbz", 101)
    _make_cbz(arrival / "Team Duwang Vol.1 Ch.2 - Loan.cbz", 102)
    _make_cbz(arrival / "Team Duwang Vol.30 Ch.323 - Chapter 1, Fin.cbz", 323)

    watcher.process_and_move_directory(arrival)

    assert _names(library / "Usogui") == ["Usogui v01 ch. 1.cbz", "Usogui v01 ch. 2.cbz"]
    assert _placed(tmp_path / "_review") == ["Team Duwang Vol.30 Ch.323 - Chapter 1, Fin.cbz"]
    assert _payloads(tmp_path / "_review") == [323]
    assert not arrival.exists()


def test_a_refused_placeholder_goes_to_review_while_its_numbered_sibling_lands(watch, tmp_path):
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
    assert _payloads(tmp_path / "_review") == [400]


def test_an_all_held_directory_creates_no_empty_series_folder(watch, tmp_path):
    """With every archive held there is nothing to land, so the series
    folder must not be created just to stay empty."""
    watch_root, library = watch
    arrival = watch_root / "Berserk Ch. 4"
    _make_cbz(arrival / "volvox_Chapter (Colored) (Uncensored).cbz", 400)

    watcher.process_and_move_directory(arrival)

    assert list(library.iterdir()) == []
    assert _payloads(tmp_path / "_review") == [400]


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
# A second translation of a chapter goes to quarantine, never deleted
# ---------------------------------------------------------------------------

def test_a_second_groups_copy_of_a_chapter_is_quarantined(watch, tmp_path):
    """Two scanlations of Ch.26. Team Duwang has more archives here, so its
    copy lands; Easy Going's copy goes to quarantine, intact."""
    watch_root, library = watch
    arrival = watch_root / "Usogui"
    for chapter in (26, 27, 28):
        _make_cbz(arrival / f"Team Duwang Vol.3 Ch.{chapter} - T{chapter}.cbz", 200 + chapter)
    _make_cbz(arrival / "Easy Going Vol.3 Ch.26 - I bet!.cbz", 926)  # larger, still not kept
    _make_cbz(arrival / "Easy Going Vol.3 Ch.30 - Other.cbz", 930)

    watcher.process_and_move_directory(arrival)

    landed = _names(library / "Usogui")
    assert "Team Duwang Vol.3 Ch.26 - T26.cbz" in landed
    assert "Easy Going Vol.3 Ch.30 - Other.cbz" in landed
    assert not any("I bet" in name for name in landed)
    assert _placed(tmp_path / "_quarantine") == ["Easy Going Vol.3 Ch.26 - I bet!.cbz"]
    assert _payloads(tmp_path / "_quarantine") == [926]


def _parsed(paths):
    return {p: cbz_core.ParsedComicName(p, p.name, "s", "s", None, None) for p in paths}


def _held_copies(paths, series):
    """Held copy -> kept copy, with nothing refused by naming."""
    return watcher._second_copies(watcher._duplicate_chapter_copies(_parsed(paths), series), set())


def test_copies_rank_by_group_size_then_file_size(tmp_path):
    big_group = [_make_cbz(tmp_path / f"Alpha Vol.1 Ch.{n}.cbz", 10) for n in (1, 2, 3)]
    small_group = [_make_cbz(tmp_path / f"Beta Vol.1 Ch.{n}.cbz", 999) for n in (1, 2)]
    assert _held_copies(big_group + small_group, "Usogui") == {
        small_group[0]: big_group[0], small_group[1]: big_group[1]}

    # Equal group sizes: the larger file is kept.
    a = [_make_cbz(tmp_path / "eq" / f"Gamma Ch.{n}.cbz", 10) for n in (1, 2)]
    b = [_make_cbz(tmp_path / "eq" / f"Delta Ch.{n}.cbz", 20 if n == 1 else 5) for n in (1, 2)]
    assert _held_copies(a + b, "Usogui") == {a[0]: b[0], b[1]: a[1]}


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
    assert _held_copies(paths, series) == {}


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


def test_part_markers_are_not_second_translations(watch):
    """Part A and Part B of chapter 1 are two halves, not two translations."""
    watch_root, library = watch
    arrival = watch_root / "Usogui"
    for group, part in (("Alpha", "A"), ("Beta", "B")):
        for chapter in (1, 2):
            _make_cbz(arrival / f"{group} Ch.{chapter} - Part {part}.cbz")

    watcher.process_and_move_directory(arrival)

    assert len(list(library.rglob("*.cbz"))) == 4


def test_a_refused_preferred_copy_does_not_hold_back_the_valid_one(media_watch, tmp_path):
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
    assert _placed(tmp_path / "_review") == ["Beta Ch.1.cbz"]


def test_a_refused_kept_copy_promotes_the_next_copy(tmp_path):
    """Unit view of _second_copies: the kept copy is the best one naming
    did not refuse, and refused copies hold nothing back."""
    alpha = [_make_cbz(tmp_path / f"Alpha Ch.{n}.cbz", 10) for n in (1, 2, 3)]
    beta = [_make_cbz(tmp_path / f"Beta Ch.{n}.cbz", 10) for n in (1, 2)]
    clusters = watcher._duplicate_chapter_copies(_parsed(alpha + beta), "Usogui")

    assert watcher._second_copies(clusters, set()) == {beta[0]: alpha[0], beta[1]: alpha[1]}
    assert watcher._second_copies(clusters, {alpha[0]}) == {beta[1]: alpha[1]}
    assert watcher._second_copies(clusters, {alpha[0], beta[0]}) == {beta[1]: alpha[1]}


def test_a_groups_own_second_copy_is_not_held_behind_it(tmp_path):
    """Two same-numbered archives from the kept copy's own group are parts
    or re-uploads, not a second translation; only the other group's copy
    is held."""
    alpha = [_make_cbz(tmp_path / name, 10) for name in (
        "Alpha Ch.1 - Bet.cbz", "Alpha Ch.1 - Wager.cbz", "Alpha Ch.2.cbz")]
    beta = [_make_cbz(tmp_path / name, 10) for name in ("Beta Ch.1.cbz", "Beta Ch.2.cbz")]
    held = _held_copies(alpha + beta, "Usogui")
    assert set(held) == {beta[0], beta[1]}


# ---------------------------------------------------------------------------
# A decided hold is out of the watch folder: no later arrival can release it
# ---------------------------------------------------------------------------

def test_quarantined_second_copies_are_untouched_when_a_new_chapter_arrives(media_watch, tmp_path):
    """Issue #174's shape. The second copies left the arrival on the first
    pass, so a new chapter arriving under the same series name is just a
    new arrival: nothing can rename them onto the library's names."""
    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    for chapter in (1, 2, 3):
        _make_cbz(arrival / f"Alpha Ch.{chapter}.cbz", 100 + chapter)
    for chapter in (1, 2):
        _make_cbz(arrival / f"Beta Ch.{chapter}.cbz", 900 + chapter)
    watcher.process_and_move_directory(arrival)
    assert _payloads(tmp_path / "_quarantine") == [901, 902]

    _make_cbz(watch_root / "Usogui" / "Alpha Ch.4.cbz", 104)
    watcher.process_and_move_directory(watch_root / "Usogui")

    assert _payloads(tmp_path / "_quarantine") == [901, 902]
    landed = library / "Usogui"
    assert _names(landed) == [f"Usogui ch. {n}.cbz" for n in (1, 2, 3, 4)]
    assert _payload_size(landed / "Usogui ch. 1.cbz") == 101


def test_a_second_copy_never_replaces_a_different_library_book(media_watch, tmp_path):
    """A kept copy that lands on a name the library already holds with
    different bytes goes to review; the library copy is untouched."""
    watch_root, library = media_watch
    existing = _make_cbz(library / "Usogui" / "Usogui ch. 1.cbz", 50)
    before = existing.read_bytes()
    arrival = watch_root / "Usogui"
    for chapter in (1, 2, 3):
        _make_cbz(arrival / f"Alpha Ch.{chapter}.cbz", 100 + chapter)
    for chapter in (1, 2):
        _make_cbz(arrival / f"Beta Ch.{chapter}.cbz", 900 + chapter)

    watcher.process_and_move_directory(arrival)

    assert existing.read_bytes() == before
    assert _payloads(tmp_path / "_review") == [101]
    assert _payloads(tmp_path / "_quarantine") == [901, 902]


# ---------------------------------------------------------------------------
# A failure that may clear by itself stays in processing and is retried
# ---------------------------------------------------------------------------

def test_a_failed_move_stays_in_processing_and_lands_on_the_next_pass(watch, tmp_path, monkeypatch):
    watch_root, library = watch
    arrival = watch_root / "Solo Leveling"
    _make_cbz(arrival / "Solo Leveling ch. 4 (s2).cbz")
    real_move = watcher._move_cbz_dir
    monkeypatch.setattr(watcher, "_move_cbz_dir", lambda *a, **k: None)
    watcher.process_and_move_directory(arrival)
    assert not arrival.exists()
    assert len(list((tmp_path / "_processing").rglob("*.cbz"))) == 1
    assert _placed(tmp_path / "_review") == []

    monkeypatch.setattr(watcher, "_move_cbz_dir", real_move)
    watcher.process_and_move_directory(watch_root)
    assert (library / "Solo Leveling" / "Solo Leveling ch. 4.cbz").is_file()
    assert not list((tmp_path / "_processing").rglob("*.cbz"))


def test_a_registry_refusal_stays_in_processing_not_review(media_watch, tmp_path, monkeypatch):
    """A style the registry cannot select (a root briefly inaccessible, say)
    refuses every archive without any file being wrong: that is retried,
    not sent to review as a decision."""
    from comic_automation.library.number_style import NumberStyleRefused

    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    _make_cbz(arrival / "Team Duwang Vol.1 Ch.1 - Gambler.cbz")

    def refuse(*_args, **_kwargs):
        raise NumberStyleRefused("auto number style needs exactly one registered root")

    real_select = type(watcher._number_style_registry).select
    monkeypatch.setattr(type(watcher._number_style_registry), "select", refuse)
    watcher.process_and_move_directory(arrival)
    assert _placed(tmp_path / "_review") == []
    assert len(list((tmp_path / "_processing").rglob("*.cbz"))) == 1

    monkeypatch.setattr(type(watcher._number_style_registry), "select", real_select)
    watcher.process_and_move_directory(watch_root)
    assert _names(library / "Usogui") == ["Usogui v01 ch. 1.cbz"]


def test_a_failed_rename_stays_in_processing_and_lands_when_it_clears(media_watch, tmp_path, monkeypatch):
    """An OSError during naming (a locked archive) refuses without anything
    being wrong with the archive. It is retried, and lands when the lock
    clears."""
    watch_root, library = media_watch
    arrival = watch_root / "Usogui"
    _make_cbz(arrival / "Team Duwang Vol.1 Ch.1 - Gambler.cbz")

    def locked(*_args, **_kwargs):
        raise PermissionError("locked by another process")

    real_rename = watcher.rename_arrival
    monkeypatch.setattr(watcher, "rename_arrival", locked)
    watcher.process_and_move_directory(arrival)
    assert _placed(tmp_path / "_review") == []

    monkeypatch.setattr(watcher, "rename_arrival", real_rename)
    watcher.process_and_move_directory(watch_root)
    assert _names(library / "Usogui") == ["Usogui v01 ch. 1.cbz"]
