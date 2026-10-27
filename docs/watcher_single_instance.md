# Watcher single-instance ownership (web console M1)

Every new watcher session acquires `Global\CBZWatcher-<st_dev:x>-<st_ino:x>`
before loading routing, cleaning stale rewrites, discovering arrivals or
starting its observer. The key is the watch directory's physical identity,
not its supplied spelling or checkout. Drive-letter, UNC (Universal Naming
Convention), mapped-drive and junction aliases therefore share ownership.
This implements section 6 of `web_console_v1_design.md` independently of the
future web server. The Tk GUI, command line and scheduled task use the same
watcher entry point.

## Acquisition and refusal

The existing hard-coded `WATCH_FOLDER` remains unchanged. Argument parsing
and help happen first. Integration settings and media-policy save/restore
happen only after ownership is acquired, so a refused embedded call cannot
reconfigure an active session. The watcher retains its existing creation of a missing
watch directory, then requires a successful directory stat with positive
`st_dev` and `st_ino`. Symlinks and junctions are resolved and their target
identity is checked against the supplied path. Identity is checked again
after mutex acquisition. A failed or zero identity, inaccessible directory,
identity change, or any uncertain kernel result refuses startup.

The helper creates/opens a noninheritable Win32 mutex with no initial owner
and calls `WaitForSingleObject(handle, 0)`. Only `WAIT_OBJECT_0` or
`WAIT_ABANDONED` grants ownership. A busy mutex, access denied or another
error refuses. Refusal is logged and `main()` and the command-line entry
point return **exit code 3**, before import activity. There is no per-session,
per-user, path-hash or file-lock fallback. Embedded recursive acquisition in
the same owning thread is also refused, since a native mutex alone permits it.

The acquired log records the key, supplied and resolved roots, and whether
ownership was abandoned. An abandoned mutex says that its previous owning
thread died; it makes no claim that the previous import completed. Existing
startup recovery still runs only after ownership is acquired.

The helper is Windows-only and refuses on another platform. The Global
namespace spans Windows sessions, but access is subject to the kernel
object's security descriptor. Cross-account operation hasn't been measured
on this host. If a supported account can't create/open the mutex, keep the
refusal and ask the operator to settle the account/access contract before
rollout. Don't select a different backend.

## Ownership lifetime and shutdown

The acquiring thread owns and releases the mutex. Handle close is attempted on
every exit, including initialization failure and abandoned-owner recovery.
Cleanup failure is distinct from startup refusal: main returns **exit code 4**
and logs "Watcher shutdown failed" if release or handle close is the only
failure. If acquisition or the session already raised, that original exception
and its exit classification remain primary; cleanup is logged and attached as
an exception note. Close is attempted even after a release exception. A root
whose release or close was uncertain stays blocked to new embedded sessions
for that process's lifetime. Closing a handle alone is not proof of release;
there is no automatic in-process retry or forced ownership takeover.

No child inherits it. Process/thread death gives Win32 its normal abandoned
mutex semantics; no stale lock file or PID (process identifier) takeover is used.

Ownership also encloses observer shutdown and timer work. The observer is
stopped and joined on normal interruption or an exception. The settle tracker
then closes admission to new callbacks, cancels pending settle/readiness
timers, and waits for callbacks that already entered. Late callbacks from a
cancel race do nothing. Only after active callback work ends can the mutex
be released. Repeated Ctrl+C during callback draining logs a wait message;
it doesn't admit another watcher while an import callback is still running.

This is ownership cleanup, not M1b's graceful stop-file API (application
programming interface), a Job Object supervisor, or a proof that arbitrary
mid-write cancellation is safe. Those later milestones retain their separate
interrupt/recovery gates. Direct import helpers used outside a watcher
session aren't cross-process mutator exclusion.

The watch root must remain the same physical directory for a session's
lifetime. Startup revalidation detects changes during acquisition; M1 does
not pin the root or all ancestor junctions against later operator retargeting.
Stop the watcher before moving, replacing or retargeting its intake root.
Separate physical roots have separate locks even if their output libraries
overlap. This lock doesn't serialize all writers to a destination library.

## Tests and host measurement

`tests/test_watcher_instance.py` uses only temporary intake directories and
bounded child processes. It covers:

- Real duplicate refusal through `main()` and the script's exit path, before
  the locked work body is entered; normal release allows the next session.
- Two-process local/localhost UNC, temporary `subst`, junction and separate
  source-copy aliases; different physical roots can run concurrently.
- A killed holder while another non-owning handle keeps the object alive.
  The replacement must observe `WAIT_ABANDONED`, acquire it, and exclude a
  third contender. Without that extra handle this would only test recreation.
- The exact Global identity key, native noninheritance, recursive refusal,
  zero/missing/non-directory/denied identity and post-acquisition drift.
- Injected creation/wait/release errors and handle cleanup, plus tracker
  draining, late-timer fencing and observer cleanup while ownership is held.

On OfficePC on 2026-10-26 the executing `david` token was medium integrity,
with Administrators deny-only and no `SeCreateGlobalPrivilege` in its token
privileges (`whoami.exe /groups` and `/priv`). The real mutex and alias tests
passed under that token. This measures the present account, not every Windows
account or every future host. UNC/mapped-drive tests explicitly skip if their
host facility is unavailable; the local OfficePC run had neither skip.

The public Win32 contracts are [CreateMutexW](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createmutexw),
[WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)
and [kernel object namespaces](https://learn.microsoft.com/en-us/windows/win32/termserv/kernel-object-namespaces).
Exact-head suite and guard-bypass receipts belong on the implementation pull
request; this document doesn't substitute for them.

## Deliberate rollout

This PR doesn't change the running watcher, its configuration or its launchers.
A pre-M1 watcher holds no such mutex. Before the first live M1 start, the
rollout owner must stop the old watcher through the supported controls and
verify that it and its workers have ended. Then deploy the reviewed exact
head and start one watcher deliberately. Simply updating a checkout doesn't
add a mutex to an already running interpreter. The future console's legacy
process scan is additional detection, not part of this watcher-only milestone.
