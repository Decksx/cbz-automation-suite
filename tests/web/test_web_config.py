"""console.toml loading (design §4.3): fail-closed validation and value provenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from comic_automation.web.config import (
    DEFAULT_PORT,
    EXAMPLE_CONFIG,
    REPO_ROOT,
    ConfigError,
    load_console_config,
    watcher_watch_folder,
)
from web_support import write_config


def test_the_shipped_example_loads_and_every_value_has_a_source():
    cfg = load_console_config(EXAMPLE_CONFIG, environ={})
    assert cfg.port == DEFAULT_PORT
    assert cfg.paths["library"] == ("\\\\tower\\media\\comics", "Z:\\comics")
    report = cfg.report()
    assert report["server.port"] == {"value": DEFAULT_PORT, "source": "console.toml", "editable": False,
                                     "restart_required": True}
    assert all({"value", "source", "editable", "restart_required"} <= set(v) for v in report.values())


def test_the_default_port_is_a_new_one_each_start(tmp_path):
    """0 = OS-chosen, so the console's origin changes every start (codex-lead R3)."""
    assert DEFAULT_PORT == 0
    assert load_console_config(EXAMPLE_CONFIG, environ={}).port == 0
    assert load_console_config(write_config(tmp_path, "[server]\nport = 8765\n"), environ={}).port == 8765


def test_an_empty_file_gives_defaults(tmp_path):
    cfg = load_console_config(write_config(tmp_path), environ={})
    assert cfg.port == DEFAULT_PORT
    assert cfg.settings["server.port"].source == "default"
    assert cfg.paths["library"] == ()


def test_registry_precedence_is_toml_then_environment_then_default(tmp_path):
    env = {"CBZ_NUMBER_STYLE_REGISTRY": r"C:\env\styles.json"}
    default = load_console_config(write_config(tmp_path), environ={})
    assert default.settings["naming.number_style_registry"].source == "default"
    assert default.number_style_registry == str(REPO_ROOT / "config" / "number-styles.json")

    from_env = load_console_config(write_config(tmp_path), environ=env)
    assert (from_env.number_style_registry, from_env.settings["naming.number_style_registry"].source) == (
        r"C:\env\styles.json", "environment")

    # A TOML literal string ('...') keeps backslashes as written.
    from_toml = load_console_config(write_config(tmp_path, "[naming]\nnumber_style_registry = 'C:\\t\\s.json'\n"),
                                    environ=env)
    assert (from_toml.number_style_registry, from_toml.settings["naming.number_style_registry"].source) == (
        r"C:\t\s.json", "console.toml")


@pytest.mark.parametrize("text, fragment", [
    ("[serverr]\nport = 9000\n", "unknown section [serverr]"),
    ("[paths]\nlibary = []\n", "unknown key paths.libary"),
    ("[server]\nport = '9000'\n", "server.port must be int"),
    ("[server]\nport = true\n", "server.port must be int"),          # bool is not a port
    ("[server]\nport = 80\n", "between 1024 and 65535"),
    ("[paths]\nlibrary = ['comics']\n", "is not absolute"),          # relative root would follow the cwd
    ("[paths]\nlibrary = ['']\n", "non-empty strings"),
    ("[paths]\nlibrary = 'Z:\\\\comics'\n", "paths.library must be list"),
    ("server = 1\n", "[server] must be a table"),
    ("[server\n", "console.toml"),                                    # TOML syntax error
])
def test_invalid_config_refuses_instead_of_being_ignored(tmp_path, text, fragment):
    with pytest.raises(ConfigError, match=None) as info:
        load_console_config(write_config(tmp_path, text), environ={})
    assert fragment in str(info.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.168.42.117", "::"])
def test_a_non_loopback_host_refuses_startup(tmp_path, host):
    with pytest.raises(ConfigError, match="server.host must be 127.0.0.1"):
        load_console_config(write_config(tmp_path, f"[server]\nhost = '{host}'\n"), environ={})


def test_stating_the_loopback_host_is_tolerated(tmp_path):
    cfg = load_console_config(write_config(tmp_path, "[server]\nhost = '127.0.0.1'\n"), environ={})
    assert cfg.settings["server.host"].value == "127.0.0.1"


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_console_config(tmp_path / "absent.toml", environ={})


def test_watch_folder_is_read_from_the_watcher_source_without_importing_it(tmp_path):
    assert watcher_watch_folder() == r"C:\Temp\Mega\Mega Uploads\book2"
    fake = tmp_path / "w.py"
    fake.write_text("WATCH_FOLDER = os.environ['X']\n")      # not a literal: reported as unknown
    assert watcher_watch_folder(fake) is None
    assert watcher_watch_folder(tmp_path / "missing.py") is None
