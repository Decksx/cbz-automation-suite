from __future__ import annotations

from pathlib import Path

from comic_automation.database.connection import (
    database_connection,
)
from comic_automation.jobs import JobQueue, JobStatus
from comic_automation.service import (
    ComicAutomationService,
)


def write_test_config(
    tmp_path: Path,
    *,
    cpu_workers: int = 1,
) -> Path:
    workspace = tmp_path / "workspace"
    library = tmp_path / "library"
    config_path = tmp_path / "service.toml"

    config_path.write_text(
        f"""
[workspace]
root = '{workspace.as_posix()}'
database = '{
    (workspace / "database" / "comics.db").as_posix()
}'
cache = '{(workspace / "cache").as_posix()}'
embeddings = '{
    (workspace / "embeddings").as_posix()
}'
staging = '{(workspace / "staging").as_posix()}'
temp = '{(workspace / "temp").as_posix()}'
logs = '{(workspace / "logs").as_posix()}'
backups = '{(workspace / "backups").as_posix()}'

[library]
root = '{library.as_posix()}'

[service]
poll_interval_seconds = 1
cpu_workers = {cpu_workers}
gpu_workers = 1
operating_mode = "audit"
""",
        encoding="utf-8",
    )

    return config_path


def test_service_initialize_creates_workspace_and_database(
    tmp_path: Path,
) -> None:
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)

    applied = service.initialize()

    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert service.config.workspace.root.is_dir()
    assert service.config.workspace.cache.is_dir()
    assert service.config.workspace.embeddings.is_dir()
    assert service.config.workspace.staging.is_dir()
    assert service.config.workspace.temp.is_dir()
    assert service.config.workspace.logs.is_dir()
    assert service.config.workspace.backups.is_dir()
    assert service.config.workspace.database.is_file()


def test_service_initialize_is_idempotent(
    tmp_path: Path,
) -> None:
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)

    first = service.initialize()
    second = service.initialize()

    assert first == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert second == []


def test_service_recovers_abandoned_jobs(
    tmp_path: Path,
) -> None:
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(
        config_path,
        abandoned_after_seconds=60,
    )
    service.initialize()

    with database_connection(
        service.config.workspace.database
    ) as connection:
        queue = JobQueue(connection)
        queued = queue.enqueue(
            "test_job",
            max_attempts=3,
        )
        claimed = queue.claim_next("dead-worker")

        assert claimed is not None

        connection.execute(
            """
            UPDATE jobs
            SET claimed_at = '2000-01-01 00:00:00'
            WHERE id = ?
            """,
            (claimed.id,),
        )

    service.initialize()

    with database_connection(
        service.config.workspace.database
    ) as connection:
        recovered = JobQueue(connection).get(queued.id)

    assert recovered.status == JobStatus.PENDING
    assert recovered.worker_id is None
    assert (
        recovered.error_message
        == "Recovered after worker abandonment."
    )


def test_service_does_not_create_or_modify_library_root(
    tmp_path: Path,
) -> None:
    config_path = write_test_config(tmp_path)
    service = ComicAutomationService(config_path)
    library_root = service.config.library_root

    assert library_root.exists() is False

    service.initialize()

    assert library_root.exists() is False
