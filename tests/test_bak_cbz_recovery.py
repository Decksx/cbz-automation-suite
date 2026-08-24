"""The `.bak.cbz` strand: an interrupted rewrite must not lose the archive.

All three read-rebuild rewrite paths swapped the original into place the
same way::

    cbz_path.rename(bak_path)     # original -> .bak.cbz
    tmp_path.rename(cbz_path)     # rebuild  -> original
    bak_path.unlink()

Between those two renames nothing exists at ``cbz_path``. If the second one
failed, every one of them then *deleted the rebuild* in its error handler,
leaving the only surviving copy at a ``.bak.cbz`` name that nothing else
looks for -- and the watcher's startup cleanup deleted ``*.bak.cbz``
outright on its next run, which is the step that made the loss permanent.

The tests here drive that exact interruption deterministically by failing
the second rename, rather than by racing a real filesystem. Each one
asserts the archive is still readable at its recorded path afterwards,
which is the property that actually matters to every other tool and to the
database.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts import cbz_library_maintenance, cbz_sanitizer, cbz_watcher

NEW_XML = "<ComicInfo><Title>New</Title></ComicInfo>"
ORIGINAL_PAGE = b"original page bytes"


def _make_cbz(path: Path, page: bytes = ORIGINAL_PAGE) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ComicInfo.xml", "<ComicInfo><Title>Old</Title></ComicInfo>")
        zf.writestr("001.jpg", page)


def _page_of(path: Path) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("001.jpg")


def _fail_renames_from(monkeypatch, *sources: Path) -> None:
    """Make `Path.rename` fail for the named source paths only.

    Targeting the *source* is what lets one helper express both cases: the
    rebuild swap failing on its own, and the restore failing as well.
    """
    real_rename = Path.rename
    doomed = {p.resolve() for p in sources}

    def fake_rename(self: Path, target):
        if self.resolve() in doomed:
            raise OSError(13, f"simulated failure renaming {self.name}")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fake_rename)


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "Series #1.cbz"
    _make_cbz(path)
    return path


# --- write_comicinfo (cbz_library_maintenance) ---------------------------


def test_maintenance_restores_the_original_when_the_swap_fails(
    archive: Path, monkeypatch
) -> None:
    """The recorded path still holds the original archive afterwards.

    Before the fix this left nothing at `archive` at all: the original was
    parked at `.bak.cbz` and the rebuild had been deleted by the handler.
    """
    tmp_path = archive.with_suffix(".tmp.cbz")
    _fail_renames_from(monkeypatch, tmp_path)

    result = cbz_library_maintenance.write_comicinfo(
        archive, "ComicInfo.xml", NEW_XML, dry_run=False
    )

    assert result is False
    assert archive.exists(), "the archive must be back at its recorded path"
    assert _page_of(archive) == ORIGINAL_PAGE
    assert not archive.with_suffix(".bak.cbz").exists()
    assert not tmp_path.exists()


def test_maintenance_keeps_both_copies_when_the_restore_also_fails(
    archive: Path, monkeypatch, caplog
) -> None:
    """The one case that cannot be repaired in-process must not lose bytes.

    With both renames failing the archive genuinely is not at its recorded
    path, and the old handler would still have deleted the rebuild. Both
    copies are kept and both locations are named at CRITICAL.
    """
    tmp_path = archive.with_suffix(".tmp.cbz")
    bak_path = archive.with_suffix(".bak.cbz")
    _fail_renames_from(monkeypatch, tmp_path, bak_path)

    with caplog.at_level("CRITICAL"):
        result = cbz_library_maintenance.write_comicinfo(
            archive, "ComicInfo.xml", NEW_XML, dry_run=False
        )

    assert result is False
    assert bak_path.exists(), "the original bytes must survive"
    assert _page_of(bak_path) == ORIGINAL_PAGE
    assert tmp_path.exists(), (
        "the rebuild must not be deleted while the archive is missing from "
        "its recorded path"
    )
    assert "could not be restored" in caplog.text
    assert str(bak_path) in caplog.text


def test_maintenance_still_succeeds_normally(archive: Path) -> None:
    """The guard did not break the path it guards.

    A refusal test that passes because the function is broken outright is
    worth nothing, so the success case is asserted beside it.
    """
    result = cbz_library_maintenance.write_comicinfo(
        archive, "ComicInfo.xml", NEW_XML, dry_run=False
    )

    assert result is True
    assert not archive.with_suffix(".bak.cbz").exists()
    assert not archive.with_suffix(".tmp.cbz").exists()
    with zipfile.ZipFile(archive) as zf:
        assert zf.read("ComicInfo.xml").decode("utf-8") == NEW_XML
        assert zf.read("001.jpg") == ORIGINAL_PAGE


# --- _write_cbz_with_comicinfo (cbz_watcher, cbz_sanitizer) --------------
#
# Both retry on OSError, which compounds the original defect: the retry
# re-opened `cbz_path`, which the failed swap had left missing, so every
# remaining attempt failed on an absent file.


@pytest.mark.parametrize(
    "module", [cbz_watcher, cbz_sanitizer], ids=["watcher", "sanitizer"]
)
def test_retrying_writers_restore_the_original_between_attempts(
    archive: Path, monkeypatch, module
) -> None:
    tmp_path = archive.with_suffix(".tmp.cbz")
    # The retry loops sleep between attempts; the test asserts recovery,
    # not timing, so the waits are removed rather than waited out.
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    _fail_renames_from(monkeypatch, tmp_path)

    module._write_cbz_with_comicinfo(
        archive, NEW_XML, replace_entry="ComicInfo.xml"
    )

    assert archive.exists(), "the archive must be back at its recorded path"
    assert _page_of(archive) == ORIGINAL_PAGE
    assert not archive.with_suffix(".bak.cbz").exists()


@pytest.mark.parametrize(
    "module", [cbz_watcher, cbz_sanitizer], ids=["watcher", "sanitizer"]
)
def test_retrying_writers_still_succeed_normally(
    archive: Path, monkeypatch, module
) -> None:
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)

    module._write_cbz_with_comicinfo(
        archive, NEW_XML, replace_entry="ComicInfo.xml"
    )

    with zipfile.ZipFile(archive) as zf:
        assert zf.read("ComicInfo.xml").decode("utf-8") == NEW_XML
        assert zf.read("001.jpg") == ORIGINAL_PAGE
    assert not archive.with_suffix(".bak.cbz").exists()
    assert not archive.with_suffix(".tmp.cbz").exists()


# --- the watcher's startup cleanup ---------------------------------------


def test_a_leftover_is_deleted_when_its_archive_is_present(
    tmp_path: Path,
) -> None:
    """The ordinary case: a genuinely stale leftover is still cleaned up."""
    archive = tmp_path / "Series #1.cbz"
    _make_cbz(archive)
    stale_bak = tmp_path / "Series #1.bak.cbz"
    stale_tmp = tmp_path / "Series #1.tmp.cbz"
    stale_bak.write_bytes(b"stale")
    stale_tmp.write_bytes(b"stale")

    cbz_watcher._clean_up_stale_rewrite_files(tmp_path)

    assert not stale_bak.exists()
    assert not stale_tmp.exists()
    assert archive.exists()


@pytest.mark.parametrize("marker", [".bak.cbz", ".tmp.cbz"])
def test_a_leftover_is_kept_when_its_archive_is_missing(
    tmp_path: Path, caplog, marker: str
) -> None:
    """The data-loss step, refused.

    With no archive at the recorded path the leftover is not stale -- it is
    the surviving copy of an interrupted rewrite. Deleting it here is what
    made an interruption permanent.
    """
    orphan = tmp_path / f"Series #1{marker}"
    _make_cbz(orphan)

    with caplog.at_level("CRITICAL"):
        cbz_watcher._clean_up_stale_rewrite_files(tmp_path)

    assert orphan.exists(), "the only surviving copy must not be deleted"
    assert _page_of(orphan) == ORIGINAL_PAGE
    assert "KEPT" in caplog.text
    assert str(orphan) in caplog.text


def test_cleanup_recurses_and_judges_each_leftover_separately(
    tmp_path: Path,
) -> None:
    """One orphan must not spare an unrelated stale file, or vice versa.

    A cleanup that bailed out entirely on finding an orphan would leave
    real leftovers behind; one that judged the whole batch by the first
    entry would delete the orphan.
    """
    nested = tmp_path / "Publisher" / "Series"
    nested.mkdir(parents=True)

    kept = nested / "Orphan #1.bak.cbz"
    _make_cbz(kept)

    paired_archive = nested / "Paired #2.cbz"
    _make_cbz(paired_archive)
    deleted = nested / "Paired #2.bak.cbz"
    deleted.write_bytes(b"stale")

    cbz_watcher._clean_up_stale_rewrite_files(tmp_path)

    assert kept.exists()
    assert not deleted.exists()
    assert paired_archive.exists()


def test_the_shadowed_archive_name_is_recovered_exactly() -> None:
    """`.bak.cbz` and `.tmp.cbz` map back to the archive that produced them.

    Both are built with `Path.with_suffix` on the original, so getting this
    mapping wrong would make every leftover look like an orphan and the
    cleanup would stop deleting anything at all.
    """
    resolve = cbz_watcher._shadowed_archive_path

    assert resolve(Path("x/Series #1.bak.cbz")) == Path("x/Series #1.cbz")
    assert resolve(Path("x/Series #1.tmp.cbz")) == Path("x/Series #1.cbz")
    # A name with dots of its own still resolves to its own archive.
    assert resolve(Path("x/Vol. 1.5.bak.cbz")) == Path("x/Vol. 1.5.cbz")
    # Anything that is not one of the two shapes is not a leftover.
    assert resolve(Path("x/Series #1.cbz")) is None
    assert resolve(Path("x/notes.txt")) is None


def test_cleanup_leaves_unrelated_files_alone(tmp_path: Path) -> None:
    """The scan is narrow on purpose; it deletes files."""
    keeper = tmp_path / "Series #1.cbz"
    _make_cbz(keeper)
    notes = tmp_path / "notes.txt"
    notes.write_text("keep me", encoding="utf-8")

    cbz_watcher._clean_up_stale_rewrite_files(tmp_path)

    assert keeper.exists()
    assert notes.exists()


# --- the retry actually retries ------------------------------------------


def _fail_only_the_first_swap(monkeypatch, tmp_path: Path) -> dict[str, int]:
    """Fail the rebuild swap once, then let everything through.

    The parameterised recovery test above fails *every* swap, which proves
    the original is restored each time but would pass just as well if the
    function gave up after attempt one. A transient fault is the case that
    separates "restores and retries" from "restores and stops", so it gets
    its own injection.

    Counts swap attempts so the test can assert the retry happened rather
    than inferring it from the end state.
    """
    real_rename = Path.rename
    doomed = tmp_path.resolve()
    state = {"swap_attempts": 0}

    def fake_rename(self: Path, target):
        if self.resolve() == doomed:
            state["swap_attempts"] += 1

            if state["swap_attempts"] == 1:
                raise OSError(13, "simulated transient lock on the first swap")

        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", fake_rename)
    return state


@pytest.mark.parametrize(
    "module", [cbz_watcher, cbz_sanitizer], ids=["watcher", "sanitizer"]
)
def test_a_transient_swap_failure_recovers_and_then_succeeds(
    archive: Path, monkeypatch, module
) -> None:
    """The whole point of restoring: the next attempt can actually work.

    Before the fix the retry re-opened `cbz_path`, which the failed swap
    had left missing, so every remaining attempt failed on an absent file
    and the rewrite was lost. With the original put back, attempt two
    reads a real archive and completes.
    """
    tmp_path = archive.with_suffix(".tmp.cbz")
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    state = _fail_only_the_first_swap(monkeypatch, tmp_path)

    module._write_cbz_with_comicinfo(
        archive, NEW_XML, replace_entry="ComicInfo.xml"
    )

    # Two swap attempts: the injected failure, then the one that worked.
    # Without this the test would pass if the fault never fired at all.
    assert state["swap_attempts"] == 2

    assert archive.exists()
    with zipfile.ZipFile(archive) as zf:
        assert zf.read("ComicInfo.xml").decode("utf-8") == NEW_XML
        assert zf.read("001.jpg") == ORIGINAL_PAGE

    assert not archive.with_suffix(".bak.cbz").exists()
    assert not tmp_path.exists()


# --- the unrecoverable state, per implementation -------------------------


@pytest.mark.parametrize(
    "module", [cbz_watcher, cbz_sanitizer], ids=["watcher", "sanitizer"]
)
def test_when_swap_and_restore_both_fail_nothing_is_deleted(
    archive: Path, monkeypatch, caplog, module
) -> None:
    """The one state this code cannot repair, asserted at each writer.

    Both renames fail, so the archive genuinely is not at its recorded
    path. What must not happen is either surviving copy being deleted --
    including by the four further retries that follow, each of which fails
    on the now-absent original and runs the handler again.

    Both copies must also still be readable archives: a rebuild that
    survived as a truncated file would be worthless.
    """
    tmp_path = archive.with_suffix(".tmp.cbz")
    bak_path = archive.with_suffix(".bak.cbz")
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    _fail_renames_from(monkeypatch, tmp_path, bak_path)

    with caplog.at_level("CRITICAL"):
        module._write_cbz_with_comicinfo(
            archive, NEW_XML, replace_entry="ComicInfo.xml"
        )

    assert not archive.exists(), "the recorded path is genuinely empty here"

    # The original, still readable, still the original bytes.
    assert bak_path.exists()
    assert zipfile.is_zipfile(bak_path)
    assert _page_of(bak_path) == ORIGINAL_PAGE

    # The rebuild, still readable, carrying the update that was in flight.
    assert tmp_path.exists()
    assert zipfile.is_zipfile(tmp_path)
    with zipfile.ZipFile(tmp_path) as zf:
        assert zf.read("ComicInfo.xml").decode("utf-8") == NEW_XML
        assert zf.read("001.jpg") == ORIGINAL_PAGE

    # The CRITICAL record has to name both locations, or the operator it
    # exists for cannot find the bytes it is telling them about.
    assert "could not be restored" in caplog.text
    assert str(bak_path) in caplog.text
    assert str(tmp_path) in caplog.text


@pytest.mark.parametrize(
    "module", [cbz_watcher, cbz_sanitizer], ids=["watcher", "sanitizer"]
)
def test_the_startup_cleanup_preserves_that_exact_joint_state(
    archive: Path, monkeypatch, caplog, module
) -> None:
    """The two halves of the defect, composed.

    The state left by an unrecoverable rewrite -- original missing, both a
    `.bak.cbz` and a `.tmp.cbz` present -- is precisely what the watcher's
    startup cleanup used to walk into and delete. Built here by running the
    real failure rather than by planting files, so the two halves are shown
    to compose rather than merely being tested apart.
    """
    watch_root = archive.parent
    tmp_path = archive.with_suffix(".tmp.cbz")
    bak_path = archive.with_suffix(".bak.cbz")

    monkeypatch.setattr(module.time, "sleep", lambda *_: None)

    with monkeypatch.context() as patch:
        _fail_renames_from(patch, tmp_path, bak_path)
        module._write_cbz_with_comicinfo(
            archive, NEW_XML, replace_entry="ComicInfo.xml"
        )

    # Precondition: the joint state really was produced.
    assert not archive.exists()
    assert bak_path.exists() and tmp_path.exists()

    with caplog.at_level("CRITICAL"):
        cbz_watcher._clean_up_stale_rewrite_files(watch_root)

    assert bak_path.exists(), "the original must survive the cleanup"
    assert tmp_path.exists(), "the rebuild must survive the cleanup"
    assert _page_of(bak_path) == ORIGINAL_PAGE
    assert zipfile.is_zipfile(tmp_path)

    # Both are reported, not silently kept.
    assert caplog.text.count("KEPT") == 2
    assert str(bak_path) in caplog.text
    assert str(tmp_path) in caplog.text
