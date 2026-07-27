from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class JobStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Job:
    id: int
    job_type: str
    status: JobStatus
    priority: int
    archive_id: int | None
    payload_json: str | None
    attempts: int
    max_attempts: int
    available_at: str
    claimed_at: str | None
    started_at: str | None
    completed_at: str | None
    worker_id: str | None
    error_message: str | None
    failure_category: str | None
    created_at: str
    updated_at: str

    @property
    def payload(self) -> dict[str, Any]:
        if not self.payload_json:
            return {}

        value = json.loads(self.payload_json)

        if not isinstance(value, dict):
            raise ValueError(
                f"Job {self.id} payload must decode to an object."
            )

        return value


def encode_payload(
    payload: Mapping[str, Any] | None,
) -> str | None:
    if payload is None:
        return None

    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
