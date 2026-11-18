"""Job reads and operator intent behind ConsoleApp's common security checks."""
from __future__ import annotations

import logging
import re
import sqlite3

from .jobs.store import JobRefused
from .jobs.actions import NotStoppable
from .state_dir import StateDirRefused

log = logging.getLogger("comic_automation.web")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_ACTIONS = frozenset({'stop', 'recheck', 'acknowledge'})


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
    if len(parts) == 5 and parts[4] in _ACTIONS:
        return _action(app, req, parts[3], parts[4])
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


def _action(app, req, job_id, action):
    """Validate strict intent; actor comes only from the authenticated session."""
    from .app import error_response, json_response
    if req.method != 'POST':
        return error_response(405, 'METHOD_NOT_ALLOWED', 'This job action requires POST.')
    try:
        fields = {'nonce', 'state_version'} | ({'note'} if action != 'stop' else set())
        if req.query or type(req.body) is not dict or set(req.body) != fields:
            raise JobRefused('INVALID_JOB_ACTION')
        service = getattr(app, 'jobs', None)
        handler = getattr(service, action + '_job', None)
        if not callable(handler) or req.session is None:
            raise JobRefused('JOB_SERVICE_UNAVAILABLE')
        payload = dict(req.body)
        version = payload.pop('state_version')
        return json_response(handler(
            job_id, payload.pop('nonce'), version, actor=req.session.operator, **payload))
    except NotStoppable as exc:
        return error_response(409, 'NOT_STOPPABLE', 'This job cannot be stopped by the current policy.',
                              {'reason': exc.reason})
    except JobRefused as exc:
        code = str(exc)
        if code == 'JOB_NOT_FOUND':
            return error_response(404, code, 'No such job.')
        if code in ('ACTION_FENCED', 'RECOVERY_FENCED'):
            return error_response(409, 'STALE', 'Job identity or version changed. Refresh the job.')
        if code == 'RECHECK_REQUIRES_UNCERTAIN':
            return error_response(409, 'INVALID_STATE', 'Only an uncertain job can be rechecked.')
        if code in ('INVALID_JOB_ID', 'INVALID_JOB_ACTION', 'OPERATOR_ACKNOWLEDGEMENT_REQUIRED'):
            return error_response(400, code, 'Invalid job action.')
        log.warning('Job action unavailable: %s', exc)
    except (StateDirRefused, OSError, sqlite3.Error):
        log.exception('Job action unavailable')
    return error_response(503, 'JOB_ACTION_UNAVAILABLE', 'Job action is unavailable. Refresh before trying again.')
