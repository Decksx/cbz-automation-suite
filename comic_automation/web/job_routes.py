"""Read-only job routes, dispatched only after ConsoleApp's security checks."""
from __future__ import annotations

import logging
import re
import sqlite3

from .jobs.store import JobRefused
from .state_dir import StateDirRefused

log = logging.getLogger("comic_automation.web")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


def _query(query, allowed):
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise JobRefused("INVALID_JOB_QUERY")
    result = {}
    for name, values in query.items():
        value = values[0]
        if not _DECIMAL.fullmatch(value) or int(value) >= 2**63:
            raise JobRefused("INVALID_JOB_QUERY")
        result[name] = int(value)
    return result


def dispatch(app, req):
    """Return a protected job response or None for a different route family."""
    from .app import error_response, json_response

    if req.path != "/api/jobs" and not req.path.startswith("/api/jobs/"):
        return None
    parts = req.path.split("/")
    if req.path != "/api/jobs" and (len(parts) != 4 or not parts[3]):
        return error_response(404, "NOT_FOUND", "No such endpoint.")
    if req.method not in ("GET", "HEAD"):
        return error_response(405, "METHOD_NOT_ALLOWED", "This job view is read-only.")
    try:
        query = _query(req.query, {"before", "limit"} if len(parts) == 3 else {"after", "limit"})
        service = getattr(app, "jobs", None)
        if service is None:
            raise JobRefused("JOB_SERVICE_UNAVAILABLE")
        if len(parts) == 3:
            return json_response(service.read_jobs(**query))
        return json_response(service.read_job(parts[3], **query))
    except JobRefused as exc:
        code = str(exc)
        if code == "JOB_NOT_FOUND":
            return error_response(404, code, "No such job.")
        if code in {"INVALID_JOB_ID", "INVALID_JOB_PAGE", "INVALID_EVENT_PAGE", "INVALID_JOB_QUERY"}:
            return error_response(400, code, "Invalid job page or query.")
        log.warning("Job view unavailable: %s", exc)
    except (StateDirRefused, OSError, sqlite3.Error):
        log.exception("Job view unavailable")
    return error_response(503, "JOB_VIEW_UNAVAILABLE", "Job history is unavailable. Try refreshing.")
