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

import functools
import os
import shutil
import threading
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


MAX_CONTENT_ACL_CHECKS = 2000


def _refuse_loosened_contents(state: Path) -> None:
    """Refuse when anything inside the private directory grants access more widely.

    A private directory only sets the *default* for what is created in it: an
    explicit grant on a file inside (``icacls console.sqlite3 /grant Users:R``)
    still opens that file. Inheriting from the private directory is fine; an
    extra allow entry or a foreign owner is not. Bounded so a huge tree cannot
    stall start-up; past the bound, start-up refuses rather than skipping.
    """
    for count, entry in enumerate(state.rglob("*"), 1):
        if count > MAX_CONTENT_ACL_CHECKS:
            raise StateDirRefused(f"{state} holds more than {MAX_CONTENT_ACL_CHECKS} entries; cannot check them all.")
        try:
            problems = [p for p in acl.privacy_problems(entry) if p != "inherits access from its parent folder"]
        except acl.AclError as exc:
            raise StateDirRefused(str(exc)) from None
        if problems:
            raise StateDirRefused(
                f"{entry} inside the state directory is not private to you ({'; '.join(problems)}). "
                "Remove the extra access, or run `--init-state-dir` and remove it by hand."
            )


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


REMOTE_CHECK_TIMEOUT_SECONDS = 5


def _bounded(fn, *args):
    """Run ``fn`` with a time limit; raise TimeoutError if it does not finish.

    Used for the few calls that can block on the network (name resolution, a
    stat through a share). The call runs in a *daemon* thread, which an
    overrun leaves behind. Not a ThreadPoolExecutor: Python joins executor
    threads at exit, so a refused start-up would still hang until the blocked
    call returned (codex-lead's R4 on #151).
    """
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["value"] = fn(*args)
        except BaseException as exc:  # handed to the caller below
            outcome["error"] = exc

    worker = threading.Thread(target=target, name=f"bounded-{getattr(fn, '__name__', 'call')}", daemon=True)
    worker.start()
    worker.join(REMOTE_CHECK_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} took over {REMOTE_CHECK_TIMEOUT_SECONDS} s")
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]


def _unc_of(root: str) -> str | None:
    """The UNC form of a network root: itself if UNC, else its mapped drive's share."""
    if root.startswith(("\\\\", "//")):
        return root
    drive = os.path.splitdrive(root)[0]
    if os.name != "nt" or not drive:
        return None
    import ctypes
    from ctypes import wintypes

    size = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    # WNetGetConnectionW reads the local mapping table; it does not contact the server.
    if ctypes.windll.mpr.WNetGetConnectionW(drive, buf, ctypes.byref(size)) != 0:
        return None
    return buf.value + root[len(drive):]


@functools.lru_cache(maxsize=1)
def _local_addresses() -> frozenset[str]:
    import socket

    # Not getfqdn(): it took 1.3 s on OfficePC. A fully qualified local name is
    # still recognised, through _host_is_this_machine's lookup of its address.
    # The own-address lookup can block like any other (codex-lead's R4), so it
    # is bounded too. A timeout propagates: without this machine's addresses a
    # share on it could be mistaken for a remote one, so start-up refuses. Only
    # a successful result is cached.
    names = {"localhost", ".", "?", "127.0.0.1", "::1"}
    name = socket.gethostname()
    names.add(name.lower())
    try:
        names.update(_bounded(socket.gethostbyname_ex, name)[2])  # this machine's own addresses
    except TimeoutError:
        raise  # a subclass of OSError, so caught first: a stall is not "no addresses"
    except OSError:
        pass  # the name has no addresses; the loopback names above still apply
    return frozenset(names)


@functools.lru_cache(maxsize=64)
def _host_is_this_machine(host: str) -> bool:
    """True if a UNC host names this machine; TimeoutError/OSError if it cannot be told.

    Known local names and addresses are matched without the network. Any other
    name is resolved (bounded): addresses that are all elsewhere mean another
    machine, and an unresolvable name is an error, not a guess.
    """
    import socket

    local = _local_addresses()
    host = host.lower().strip("[]")
    if host in local or host.startswith("127."):
        return True
    infos = _bounded(socket.getaddrinfo, host, None)
    addresses = {info[4][0] for info in infos}
    return any(a in local or a.startswith("127.") or a == "::1" for a in addresses)


def _identity(path: str) -> tuple[int, int] | None:
    st = os.stat(path)
    return (st.st_dev, st.st_ino) if st.st_dev and st.st_ino else None


def _ancestor_identities(state: Path) -> set[tuple[int, int]]:
    """Physical identities of ``state`` and every directory above it, by both spellings."""
    ids = set()
    for start in {Path(os.path.abspath(state)), Path(os.path.realpath(state))}:
        for p in (start, *start.parents):
            try:
                ident = _identity(str(p))
            except OSError:
                continue
            if ident:
                ids.add(ident)
    return ids


def _containing_root(state: Path, roots: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The first (label, root) that contains ``state``; StateDirRefused if one cannot be checked.

    Three tests, any of which is a hit:

    1. **Lexical**, on the written and resolved forms of both paths, following
       the planner's containment idiom.
    2. **Physical identity**: the root's ``(st_dev, st_ino)`` equals the state
       directory's or one of its ancestors'. This catches every alias that
       spelling cannot: junctions, ``subst`` drives, and a share that exposes
       a local folder (``\\\\localhost\\C$\\…``, measured to report the same
       identity as the local path).
    3. **Network roots** are never resolved or stat'ed through a server that
       is not this machine. Doing so blocks 20 s or more when Tower is offline,
       and the state directory is local (refused otherwise in
       :func:`open_state_dir`), so a share on another machine cannot contain
       it. Whether the host is this machine is decided from local names and
       addresses, or a bounded name lookup; a root whose host cannot be
       determined is refused, never assumed to be disjoint.
    """
    lexical = _norm(str(state))
    resolved = _norm(os.path.realpath(state))
    ancestors: set[tuple[int, int]] | None = None  # computed on first need
    for label, root in roots:
        network = _is_network(root)
        candidates = {_norm(root)}
        if not network:
            try:
                candidates.add(_norm(os.path.realpath(root)))
            except (OSError, ValueError):
                pass
        # Spelling first: free, and needs no server or name lookup.
        for r in candidates:
            if _inside(lexical, r) or _inside(resolved, r):
                return label, root
        check_identity = True
        stat_target = root
        if network:
            unc = _unc_of(root)
            stat_target = unc or root  # a mapped drive is stat'ed through its share
            host = unc.lstrip("\\/").replace("/", "\\").split("\\", 1)[0] if unc else ""
            try:
                if not host:
                    raise OSError("cannot tell which server it is on")
                check_identity = _host_is_this_machine(host)
            except (OSError, TimeoutError) as exc:
                raise StateDirRefused(f"Cannot check whether the state directory is inside {label} ({root}): {exc}. "
                                      "Fix the configured path or the name lookup.") from None
        if check_identity:
            try:
                ident = _bounded(_identity, stat_target) if _is_network(root) else _identity(root)
            except FileNotFoundError:
                continue  # a root that does not exist cannot contain the state directory
            except (OSError, TimeoutError) as exc:
                raise StateDirRefused(f"Cannot check whether the state directory is inside {label} ({root}): {exc}.") from None
            if ident is None and _is_network(root):
                raise StateDirRefused(f"Cannot check whether the state directory is inside {label} ({root}): "
                                      "the share reports no file identity.")
            if ancestors is None:
                ancestors = _ancestor_identities(state)
            if ident and ident in ancestors:
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
    # A local volume only. SQLite's locking is unreliable over SMB, and it is
    # what makes the lexical-only comparison of network roots sound: a network
    # root cannot contain a local directory. Without this, a state directory
    # spelled \\tower\media\comics\... would slip past a root spelled Z:\comics.
    if _is_network(str(path)) or _is_network(os.path.realpath(path)):
        raise StateDirRefused(f"The state directory {path} is on a network location; it must be on a local drive.")
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
    _refuse_loosened_contents(path)

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
