from pathlib import Path

from comic_automation.config import ensure_workspace, load_config


def test_load_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    workspace = tmp_path / "workspace"
    library = tmp_path / "library"

    config_path.write_text(
        f"""
[workspace]
root = '{workspace}'
database = '{workspace / "database" / "comics.db"}'
cache = '{workspace / "cache"}'
embeddings = '{workspace / "embeddings"}'
staging = '{workspace / "staging"}'
temp = '{workspace / "temp"}'
logs = '{workspace / "logs"}'
backups = '{workspace / "backups"}'

[library]
root = '{library}'

[service]
poll_interval_seconds = 7
cpu_workers = 4
gpu_workers = 1
operating_mode = "audit"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.workspace.root == workspace
    assert config.workspace.database == workspace / "database" / "comics.db"
    assert config.library_root == library
    assert config.service.poll_interval_seconds == 7
    assert config.service.cpu_workers == 4
    assert config.service.gpu_workers == 1
    assert config.service.operating_mode == "audit"


def test_ensure_workspace_creates_directories(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    workspace = tmp_path / "workspace"

    config_path.write_text(
        f"""
[workspace]
root = '{workspace}'
database = '{workspace / "database" / "comics.db"}'
cache = '{workspace / "cache"}'
embeddings = '{workspace / "embeddings"}'
staging = '{workspace / "staging"}'
temp = '{workspace / "temp"}'
logs = '{workspace / "logs"}'
backups = '{workspace / "backups"}'

[library]
root = '{tmp_path / "library"}'
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    ensure_workspace(config)

    assert config.workspace.root.is_dir()
    assert config.workspace.database.parent.is_dir()
    assert config.workspace.cache.is_dir()
    assert config.workspace.embeddings.is_dir()
    assert config.workspace.staging.is_dir()
    assert config.workspace.temp.is_dir()
    assert config.workspace.logs.is_dir()
    assert config.workspace.backups.is_dir()
