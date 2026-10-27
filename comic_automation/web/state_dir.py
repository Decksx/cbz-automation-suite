"""The console's runtime state directory on G: and the checks that guard it.

Design: ``docs/web_console_v1_design.md`` §4.2. Everything the console persists
(jobs, approvals, logs, its own configuration) lives in one directory,
``G:\\ComicAutomation\\web-console`` by default. The server refuses to start
when that directory is unusable. There is deliberately **no fallback
location**: a second state directory would mean a second jobs database, and two
databases would split admission (§5.2) so two consoles could each run a writing
job at once.

For the same reason a *missing* directory is refused rather than created. If
G: were remounted as a different disk, silently creating a fresh directory
would start a second, empty ledger. Creating it is an explicit operator step
(:func:`init_state_dir`, ``python -m comic_automation.web.server
--init-state-dir``).

Checks, in the order :func:`open_state_dir` applies them:

1. the directory exists and is a directory;
2. it is private to the operator (:mod:`.acl`): G:\\ComicAutomation grants
   every signed-in account Modify, and the directory holds the launcher
   secret and the approval ledger;
3. ``locks\\console.lock`` can be locked, so only one console runs;
4. the directory is writable (a probe file is created and removed);
5. it has a non-zero physical identity (``st_dev``, ``st_ino``);
6. ``console.toml`` loads (it is copied from the example on first run);
7. it is not inside the repository, the watcher's watch folder, or any
   configured library, quarantine, readiness-hold or decensor-archive root;
8. its identity matches the one pinned in ``console.sqlite3`` on first run.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Mapping

from ..database import dal
from . import acl
from .config import EXAMPLE_CONFIG, REPO_ROOT, ConfigError, ConsoleConfig, load_console_config, watcher_watch_folder

DEFAULT_STATE_DIR = Path(r"G:\ComicAutomation\web-console")
# Tests only (§4.2). Production always uses the G: default.
STATE_DIR_ENV = "CBZ_WEB_CONSOLE_HOME"

# Roles the state directory must stay out of. plan_import and database are
# not here: the console's own plans\ lives inside the state directory, and the
# planner database root is operational data next to it, not a library.
_FORBIDDEN_ROLES = ("library", "quarantine", "decensor_archive", "readiness_hold")


class StateDirRefused(RuntimeError):
    """The state directory cannot be used; the server must not start."""


@dataclass
class StateDir:
    """An opened, validated state directory. Holds the single-console lock."""

    path: Path
    config: ConsoleConfig
    identity: tuple[int, int]
    _lock: IO[bytes] | None

    @property
    def database(self) -> Path:
        return self.path / "console.sqlite3"

    @property
    def session_file(self) -> Path:
        return self.path / "session.json"

    @property
    def server_log(self) -> Path:
        return self.path / "console.log"

    def close(self) -> None:
        """Release the single-console lock (also released when the process exits)."""
        if self._lock is not None:
            _unlock(self._lock)
            self._lock.close()
            self._lock = None


def state_dir_from_env(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(STATE_DIR_ENV)
    return Path(override) if override else DEFAULT_STATE_DIR


def init_state_dir(path: Path) -> Path:
    """Create the state directory: the deliberate first-run step.

    The parent must already exist. Creating ``G:\\ComicAutomation`` as well
    would turn a missing or unmounted G: into a directory on whatever disk the
    letter now points at, which is the failure this module exists to refuse.
    """
    if not path.parent.is_dir():
        raise StateDirRefused(f"Cannot create {path}: its parent {path.parent} does not exist. Is the drive mounted?")
    try:
        path.mkdir(exist_ok=True)
        # Also applied to an existing directory, so re-running the step is how
        # an operator repairs a directory the start-up check found not private.
        acl.make_private(path, directory=True)
        problems = acl.privacy_problems(path)
    except (OSError, acl.AclError) as exc:
        raise StateDirRefused(f"Cannot prepare {path}: {exc}") from None
    if problems:
        raise StateDirRefused(f"{path} is still not private after setting its access: {'; '.join(problems)}.")
    return path


# --------------------------------------------------------------- the lock
try:
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[bytes]) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

except ImportError:  # pragma: no cover - the console is Windows-first; this keeps imports working elsewhere
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_console_lock(state: Path) -> IO[bytes]:
    locks = state / "locks"
    try:
        locks.mkdir(exist_ok=True)
        handle = open(locks / "console.lock", "a+b")
    except OSError as exc:  # e.g. a read-only or full G:
        raise StateDirRefused(f"Cannot create the console lock in {locks}: {exc}") from None
    if not _try_lock(handle):
        handle.close()
        raise StateDirRefused("Another web console is already running (locks\\console.lock is held).")
    return handle


# ------------------------------------------------------------ path rule
def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives, or UNC against a drive letter
        return False


def _is_network(root: str) -> bool:
    """A UNC path or a drive letter mapped to a network share."""
    if root.startswith(("\\\\", "//")):
        return True
    drive = os.path.splitdrive(root)[0]
    if os.name != "nt" or not drive:
        return False
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE


def _containing_root(state: Path, roots: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The first (label, root) that contains ``state``, checked lexically and resolved.

    Both forms are checked, following the planner's containment idiom: the
    lexical check catches a root spelled the same way, the resolved one a
    junction or mapped path that leads into the root under another name.

    Network roots (UNC, or a drive mapped to a share such as Z: on Tower) are
    compared lexically only. Resolving one blocks for 20 s or more when the
    server is offline, which would stall start-up. A network root cannot
    contain a local state directory, and if the state directory itself is on
    the share, its own lexical and resolved forms are still compared.
    """
    lexical = _norm(str(state))
    resolved = _norm(os.path.realpath(state))
    for label, root in roots:
        candidates = {_norm(root)}
        if not _is_network(root):
            try:
                candidates.add(_norm(os.path.realpath(root)))
            except (OSError, ValueError):
                pass
        for r in candidates:
            if _inside(lexical, r) or _inside(resolved, r):
                return label, root
    return None


def _forbidden_roots(config: ConsoleConfig) -> list[tuple[str, str]]:
    roots = [("the repository", str(REPO_ROOT))]
    watch = watcher_watch_folder()
    if watch:
        roots.append(("the watcher's watch folder", watch))
    for role in _FORBIDDEN_ROLES:
        roots.extend((f"paths.{role}", r) for r in config.paths[role])
    return roots


# -------------------------------------------------------- identity pin
def _pin_identity(database: Path, identity: tuple[int, int]) -> None:
    """Record the identity on first run; refuse if it has changed since.

    The pin lives inside the directory it describes. A copy of the directory
    restored onto another disk therefore carries the old identity and is
    refused, which is the point: it would otherwise resume as if it were the
    original ledger.
    """
    current = f"{identity[0]}:{identity[1]}"
    try:
        # Through the shared database layer, like every other connection in the
        # codebase (tests/test_database_dal.py enforces it); the scope closes
        # the connection on every path, including the refusal below.
        with dal.connection_scope(database) as db, dal.transaction(db):
            db.execute("CREATE TABLE IF NOT EXISTS console_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = db.execute("SELECT value FROM console_meta WHERE key = 'state_dir_identity'").fetchone()
            if row is None:
                db.execute("INSERT INTO console_meta (key, value) VALUES ('state_dir_identity', ?)", (current,))
            elif row[0] != current:
                raise StateDirRefused(
                    f"The state directory's identity changed (pinned {row[0]}, now {current}). "
                    "It may have been copied or the drive remapped; refusing so a second job ledger is not started."
                )
    except sqlite3.Error as exc:
        raise StateDirRefused(f"Cannot open {database}: {exc}") from None


def open_state_dir(path: Path, environ: Mapping[str, str] | None = None) -> StateDir:
    """Validate ``path`` as the state directory and take the single-console lock.

    Raises :class:`StateDirRefused` with an operator-readable reason. On any
    refusal after the lock is taken, the lock is released again.
    """
    if not path.exists():
        raise StateDirRefused(
            f"The console state directory {path} does not exist. If this is the first run, create it with "
            "`python -m comic_automation.web.server --init-state-dir`; otherwise check that the drive is mounted."
        )
    if not path.is_dir():
        raise StateDirRefused(f"{path} is not a directory.")
    # Checked before anything is written into it, including the lock file.
    try:
        problems = acl.privacy_problems(path)
    except acl.AclError as exc:
        raise StateDirRefused(str(exc)) from None
    if problems:
        raise StateDirRefused(
            f"The state directory {path} is not private to you ({'; '.join(problems)}). It will hold the "
            "launcher secret and the approval ledger. Run `python -m comic_automation.web.server "
            "--init-state-dir` to make it private."
        )

    lock = _acquire_console_lock(path)
    try:
        probe = path / f".write-probe-{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise StateDirRefused(f"The state directory {path} is not writable: {exc}") from None

        st = os.stat(path)
        identity = (st.st_dev, st.st_ino)
        # Zero means the filesystem gave no stable identity, so a change of disk
        # could not be detected. Refused for the same reason M1 refuses it.
        if not identity[0] or not identity[1]:
            raise StateDirRefused(f"{path} has no usable file identity ({identity}); cannot pin it.")

        config_path = path / "console.toml"
        if not config_path.exists():
            shutil.copyfile(EXAMPLE_CONFIG, config_path)
        try:
            config = load_console_config(config_path, environ)
        except ConfigError as exc:
            raise StateDirRefused(f"console.toml is invalid: {exc}") from None

        hit = _containing_root(path, _forbidden_roots(config))
        if hit:
            raise StateDirRefused(f"The state directory {path} is inside {hit[0]} ({hit[1]}); move it out.")

        _pin_identity(path / "console.sqlite3", identity)
    except BaseException:
        _unlock(lock)
        lock.close()
        raise
    return StateDir(path=path, config=config, identity=identity, _lock=lock)
