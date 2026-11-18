"""Fenced operator intent, without a tool-interruption or submission adapter.

Direct callers and HTTP use the same validation and admission exclusion. The
current policy can refuse an unclaimed admission; it cannot signal a runner.
Recovery owns its own admission lock, so recheck must not nest that lock here.
"""
from __future__ import annotations

import re

from . import recovery
from .store import JobRefused, validate_job_id
from .views import _summary

_NONCE = re.compile(r'[0-9a-f]{32}\Z')
_TERMINAL = frozenset({'admission_expired', 'refused', 'succeeded', 'failed', 'stopped', 'lost'})


class NotStoppable(JobRefused):
    """A fixed policy reason, with no persisted intent or native action."""
    def __init__(self, reason):
        if reason not in ('WAIT_ONLY', 'RECHECK_REQUIRED'):
            raise ValueError('Unknown stop policy reason')
        super().__init__('NOT_STOPPABLE')
        self.reason = reason


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        return False
    try:
        value.encode('utf-8')
        return True
    except UnicodeEncodeError:
        return False


def _validate(job_id, nonce, version, actor, *, note=None, require_note=False):
    validate_job_id(job_id)
    if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(version) is not int or not 0 < version < 2**63
            or not _text(actor, 256) or require_note and not _text(note, 4096)):
        raise JobRefused('INVALID_JOB_ACTION')


def _row(db, job_id, nonce, version):
    row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None:
        raise JobRefused('JOB_NOT_FOUND')
    if row['nonce'] != nonce or row['state_version'] != version:
        raise JobRefused('ACTION_FENCED')
    return dict(row)


class JobActions:
    """Apply one authenticated operator's bounded, version-fenced intent."""
    def __init__(self, store):
        self.store = store

    def stop(self, job_id, nonce, version, *, actor):
        """Refuse only an admitted job; terminal jobs return unchanged.

        Admission exclusion serialises this transition with runner claim. A
        winner increments the version and becomes terminal, so a runner can
        never mistake this row for its version-2 claimed generation.
        """
        _validate(job_id, nonce, version, actor)
        with self.store._home(job_id=job_id), self.store._write() as db:
            old = _row(db, job_id, nonce, version)
            if old['state'] in _TERMINAL:
                return {'job': _summary(old)}
            if old['state'] != 'admitted':
                raise NotStoppable('RECHECK_REQUIRED' if old['state'] == 'uncertain' else 'WAIT_ONLY')
            now = self.store._time(db)
            changed = db.execute("UPDATE jobs SET state='refused',state_version=state_version+1,"
                "updated_ms=?,outcome='OPERATOR_STOP_BEFORE_TOOL' "
                "WHERE job_id=? AND nonce=? AND state_version=? AND state='admitted'",
                (now, job_id, nonce, version))
            if changed.rowcount != 1:
                raise JobRefused('ACTION_FENCED')
            row = dict(db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone())
            self.store._event(db, row, 'operator_stop', actor, now, {'phase': 'before_tool'})
            return {'job': _summary(row)}

    def recheck(self, job_id, nonce, version, *, actor, note):
        """Delegate fresh native proof and attribution to recovery's own lock."""
        _validate(job_id, nonce, version, actor, note=note, require_note=True)
        row = recovery.recheck(self.store, job_id, nonce, version, actor=actor, note=note)
        return {'job': _summary(row)}

    def acknowledge(self, job_id, nonce, version, *, actor, note):
        """Append a private attributed note without changing any job field.

        Notes are not a receipt or recovery proof. They leave the generation,
        lifecycle, admission and stop-request state untouched, including cold
        history rows. A repeated valid note may append another audit event.
        """
        _validate(job_id, nonce, version, actor, note=note, require_note=True)
        with self.store._home(job_id=job_id), self.store._write() as db:
            row = _row(db, job_id, nonce, version)
            now = self.store._time(db)
            self.store._event(db, row, 'acknowledged', actor, now, {'note': note})
            return {'job': _summary(row)}
