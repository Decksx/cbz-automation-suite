"""Console configuration: ``console.toml`` loading and the effective-config report.

Design: ``docs/web_console_v1_design.md`` §4.3. The file lives in the console's
state directory (§4.2) and is copied from ``config/web_console.example.toml`` on
first run. Loading fails closed: an unknown section or key, a wrong type, or a
non-loopback host refuses startup instead of being ignored, because a typo in a
path allowlist would otherwise silently narrow or widen what the console may
touch.

Every effective value carries its source (``default``, ``console.toml`` or
``environment``) so the Settings page (§7.4) can show where it came from. v1
never edits the file, so every value is reported as not editable and as needing
a restart.
"""

from __future__ import annotations

import ast
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "web_console.example.toml"
# 0: a free port chosen at every start. Browser state such as a service worker
# belongs to an origin, which includes the port, so a fresh port means nothing
# left on an earlier address by another program can intercept the console's
# pages. The launcher reads the port from session.json, so nothing needs it fixed.
DEFAULT_PORT = 0
LOOPBACK_HOST = "127.0.0.1"

# Path roles a tool parameter may point into (§4.3, §9 "Paths"). The console's
# own plans\ directory is always allowed for plan_import and is added at use.
PATH_ROLES = ("library", "quarantine", "decensor_archive", "readiness_hold", "database", "plan_import")

_SCHEMA: dict[str, dict[str, type]] = {
    "server": {"port": int, "host": str},
    "paths": {role: list for role in PATH_ROLES},
    "naming": {"number_style_registry": str},
    "python": {"executable": str},
}


class ConfigError(ValueError):
    """``console.toml`` is unreadable or invalid; the server must not start."""


@dataclass(frozen=True)
class Setting:
    """One effective configuration value and where it came from."""

    value: Any
    source: str  # "default", "console.toml" or "environment"
    editable: bool = False
    restart_required: bool = True


@dataclass(frozen=True)
class ConsoleConfig:
    """The validated, effective console configuration."""

    config_path: Path
    port: int
    paths: dict[str, tuple[str, ...]]
    number_style_registry: str
    python_executable: str
    settings: dict[str, Setting] = field(default_factory=dict)

    def report(self) -> dict[str, dict[str, Any]]:
        """The ``GET /api/config`` payload: every value with its provenance."""
        return {
            key: {
                "value": s.value,
                "source": s.source,
                "editable": s.editable,
                "restart_required": s.restart_required,
            }
            for key, s in sorted(self.settings.items())
        }


def _check_schema(raw: Mapping[str, Any], path: Path) -> None:
    for section, body in raw.items():
        if section not in _SCHEMA:
            raise ConfigError(f"{path}: unknown section [{section}]")
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        for key, value in body.items():
            expected = _SCHEMA[section].get(key)
            if expected is None:
                raise ConfigError(f"{path}: unknown key {section}.{key}")
            # bool is an int subclass; a port of `true` is a typo, not 1.
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ConfigError(f"{path}: {section}.{key} must be {expected.__name__}")


def _check_root(role: str, root: Any, path: Path) -> str:
    if not isinstance(root, str) or not root.strip():
        raise ConfigError(f"{path}: paths.{role} entries must be non-empty strings")
    if "\0" in root:
        raise ConfigError(f"{path}: paths.{role} entry contains NUL")
    # Roots are Windows paths whatever platform the tests run on: an allowlist
    # root that is relative would be resolved against the server's cwd.
    if not PureWindowsPath(root).is_absolute():
        raise ConfigError(f"{path}: paths.{role} entry {root!r} is not absolute")
    return root


def load_console_config(config_path: Path, environ: Mapping[str, str] | None = None) -> ConsoleConfig:
    """Load and validate ``console.toml``; raise :class:`ConfigError` on any problem.

    Precedence is per key: ``console.toml`` wins over the environment, which
    wins over the built-in default. Only ``number_style_registry`` has an
    environment source (``CBZ_NUMBER_STYLE_REGISTRY``, matching the watcher
    and the planner).
    """
    env = os.environ if environ is None else environ
    try:
        with open(config_path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"{config_path}: not found") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{config_path}: {exc}") from None
    _check_schema(raw, config_path)

    settings: dict[str, Setting] = {}
    server = raw.get("server", {})

    # The bind host is not configurable (§9). A loopback value is tolerated so an
    # operator can state the fact; anything else refuses startup outright.
    host = server.get("host", LOOPBACK_HOST)
    if host != LOOPBACK_HOST:
        raise ConfigError(f"{config_path}: server.host must be {LOOPBACK_HOST}; the console never binds elsewhere")
    settings["server.host"] = Setting(LOOPBACK_HOST, "console.toml" if "host" in server else "default")

    port = server.get("port", DEFAULT_PORT)
    if port != 0 and not 1024 <= port <= 65535:
        raise ConfigError(f"{config_path}: server.port must be 0 (a new port each start) or between 1024 and 65535")
    settings["server.port"] = Setting(port, "console.toml" if "port" in server else "default")

    paths: dict[str, tuple[str, ...]] = {}
    raw_paths = raw.get("paths", {})
    for role in PATH_ROLES:
        roots = tuple(_check_root(role, r, config_path) for r in raw_paths.get(role, []))
        paths[role] = roots
        settings[f"paths.{role}"] = Setting(list(roots), "console.toml" if role in raw_paths else "default")

    registry = raw.get("naming", {}).get("number_style_registry", "")
    if registry:
        settings["naming.number_style_registry"] = Setting(registry, "console.toml")
    elif env.get("CBZ_NUMBER_STYLE_REGISTRY"):
        registry = env["CBZ_NUMBER_STYLE_REGISTRY"]
        settings["naming.number_style_registry"] = Setting(registry, "environment")
    else:
        registry = str(REPO_ROOT / "config" / "number-styles.json")
        settings["naming.number_style_registry"] = Setting(registry, "default")

    executable = raw.get("python", {}).get("executable", "")
    settings["python.executable"] = Setting(executable, "console.toml" if executable else "default")

    return ConsoleConfig(
        config_path=config_path,
        port=port,
        paths=paths,
        number_style_registry=registry,
        python_executable=executable,
        settings=settings,
    )


def watcher_watch_folder(source: Path | None = None) -> str | None:
    """The watcher's hard-coded ``WATCH_FOLDER``, read from its source text.

    Importing ``scripts.cbz_watcher`` would create its log directory and
    configure logging as import side effects, so the literal is read with
    ``ast`` instead. Returns None when the assignment is missing or is not a
    plain string, so a refactor of the watcher weakens nothing silently: the
    caller treats None as "unknown" and says so.
    """
    path = source or REPO_ROOT / "scripts" / "cbz_watcher.py"
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "WATCH_FOLDER" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None
