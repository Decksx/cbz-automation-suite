# Graceful watcher stop file (web console M1b)

The watcher accepts an optional absolute --stop-file path. Without it the
existing launcher behaviour stays unchanged. The caller owns the path and
must give each session a fresh one, for example jobs/<job_id>/stop.request.
The parent directory must exist and be a directory when the session starts;
a relative path or unavailable parent refuses with an argparse diagnostic and
exit 2 before creating the intake or acquiring its mutex.

Presence is the request; contents are ignored. The future runner writes a
temporary file and renames it to stop.request. The watcher never creates,
removes or consumes the request. Once observed, stopping stays latched even
if the caller removes the file. A stale request therefore cannot silently
restart imports. An existing request at startup exits successfully before
routing, rewrite recovery, directory discovery or observer startup.

## Directory boundaries and ownership

Idle polling is once per second. Startup discovery also polls before each
directory, and admission checks poll at timer entry, notify, readiness retry,
minimum-age rearm and the directory readiness gate. Polling and admission
share the tracker lock. Seeing a request closes admission and cancels pending
settle/readiness timers; late timer callbacks cannot start new work.

An admitted directory unit includes its readiness check, any explicitly
configured readiness-hold operation, reprobe and import/move. Already-admitted
units finish. They are never interrupted halfway through an archive rewrite,
move, external tool, or directory operation. Other units admitted concurrently
may also finish. A request is not an assertion that only one import was active.
Pending arrivals remain in intake for a later session.

The observer stops and joins, then the tracker drains active callbacks and
directory units while the M1 mutex remains held. Only after draining may root
ownership be released. Requested shutdown has process exit 0; cleanup errors
retain the separate #147 classification. The log records exactly one first-seen
line with the number of admitted directory units still finishing, then an exit
line after draining:

    Watcher stop request seen: <path>; N admitted directories still finishing.
    CBZ Watcher stopped by stop-file request: <path>

Detection can take one idle interval plus scheduling/filesystem delay. Startup
configuration/recovery and an active unit can take longer. There is no maximum
graceful shutdown duration: a blocked external tool or filesystem operation can
keep its directory active. This milestone provides no forced termination or
proof of arbitrary mid-write cancellation. The M6 interruption/recovery gate
must pass before the console enables its terminate fallback.

## Storage errors and limits

An OSError while polling logs a warning once per session and keeps the watcher
running; it never latches a request from an uncertain result. A missing request
with an existing parent is normal. A parent that disappears during the session
is a polling error. Later successful polls can still observe a request. This
follows Claude's approved integration contract in hive #3204: a temporary G:
state-drive failure must not silently stop imports. The future console remains
responsible for truthful stop/timeout status and its gated fallback.

An existing filesystem entry at the selected request path is conservatively
considered a request; no content, extension, timestamp or token is required.
The caller must protect its job directory. This is a local cooperation signal,
not an authentication or destination-wide writer-exclusion mechanism. Root and
ancestor stability and cross-account limitations remain in
watcher_single_instance.md. No launcher, live configuration, running watcher,
web server or runner is changed by this implementation.

## Evidence

Tests use temporary intake roots, valid ZIP fixtures, explicit thread events
and bounded child processes. They cover invalid paths, pre-existing and latched
requests, warning-only storage errors and recovery, all timer entry paths,
startup directory boundaries, cancellation and late callback fencing, direct
admission draining, M1 exclusion through the final active operation, and an
actual watchdog observer process stopped by an atomic request. Exact-head full
suite and named guard-bypass results are recorded on the implementation PR.
