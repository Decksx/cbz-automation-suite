from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    database: Path
    cache: Path
    embeddings: Path
    staging: Path
    temp: Path
    logs: Path
    backups: Path


@dataclass(frozen=True)
class ServiceConfig:
    poll_interval_seconds: int = 5
    cpu_workers: int = 8
    gpu_workers: int = 1
    operating_mode: str = "audit"


@dataclass(frozen=True)
class AppConfig:
    workspace: WorkspaceConfig
    library_root: Path
    service: ServiceConfig


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    workspace_raw = raw["workspace"]
    library_raw = raw["library"]
    service_raw = raw.get("service", {})

    workspace = WorkspaceConfig(
        root=Path(workspace_raw["root"]),
        database=Path(workspace_raw["database"]),
        cache=Path(workspace_raw["cache"]),
        embeddings=Path(workspace_raw["embeddings"]),
        staging=Path(workspace_raw["staging"]),
        temp=Path(workspace_raw["temp"]),
        logs=Path(workspace_raw["logs"]),
        backups=Path(workspace_raw["backups"]),
    )

    service = ServiceConfig(
        poll_interval_seconds=int(service_raw.get("poll_interval_seconds", 5)),
        cpu_workers=int(service_raw.get("cpu_workers", 8)),
        gpu_workers=int(service_raw.get("gpu_workers", 1)),
        operating_mode=str(service_raw.get("operating_mode", "audit")),
    )

    return AppConfig(
        workspace=workspace,
        library_root=Path(library_raw["root"]),
        service=service,
    )


def ensure_workspace(config: AppConfig) -> None:
    paths = (
        config.workspace.root,
        config.workspace.database.parent,
        config.workspace.cache,
        config.workspace.embeddings,
        config.workspace.staging,
        config.workspace.temp,
        config.workspace.logs,
        config.workspace.backups,
    )

    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
