# M3c slice 1: native ownership primitives

This implements the native boundary of the [agreed M3c contract](web_console_m3c_contract.md)
from merged master f09fcfcb9bfa276ecc99416aa796a743c7dbfc02. It does not implement
the claimed runner, ledger finalization, recovery scheduling, HTTP routes,
production tool adapters or a rollout. Those remain subsequent slices.

## Internal API and ownership

win32.launch(job_id, executable, arguments, cwd=..., environment=...) takes a
canonical job ID, absolute executable and existing absolute cwd, a sequence of
Windows arguments, and an explicit case-unique environment. It uses
subprocess.list2cmdline quoting, an explicit lpApplicationName and Unicode
environment. This is an internal primitive; the M4 registry/runner must check
command seals, source/interpreter hashes and policy before calling it.

A new operator/SYSTEM/Administrators-only Local Job Object gets
KILL_ON_JOB_CLOSE and neither breakaway flag. STARTUPINFOEX supplies JOB_LIST
and an explicit HANDLE_LIST (merged stdout/stderr writer and NUL stdin).
Creation uses CREATE_NO_WINDOW, EXTENDED_STARTUPINFO_PRESENT and
CREATE_UNICODE_ENVIRONMENT. The job, process/thread handles and pipe reader
are noninheritable. There is no suspended/assign-later or uncontained fallback.

The returned ContainedProcess owns its job handle, root process handle and
unbuffered CRT read stream. The creator closes the initial thread, pipe writer
and NUL handles immediately, and frees the attribute list/security descriptor.
The read handle transfers to a noninheritable CRT descriptor exactly once.
Both attribute arrays and backing memory live until native creation returns.

- identity holds integer PID, exact integer FILETIME creation ticks and session.
- wait_root(timeout) waits for a signalled process before reading its DWORD
  exit code, so a real exit259 is not mistaken for STILL_ACTIVE. Bounds0-30s.
- active_processes() queries fresh accounting. Root exit is independent of
  pipe EOF and descendant completion. A closed owner refuses before a NULL
  handle could accidentally query the caller's own job.
- terminate(code) requests whole-job termination; it neither enforces stop
  policy nor proves completion. The caller must independently verify the
  process/job conditions and obey the accepted writing interruption gate.
- close() attempts all owned closes, job first. It is destructive containment
  cleanup, not a policy-approved stop. A failed close raises, never proves
  completion. Callers must cancel/join pipe users before final close.
  Read cancellation continues to use existing log_io.cancel_thread_io.

NativeLaunchError carries stage/code and child_created. Failure after the OS
populates PROCESS_INFORMATION, even if a Python wrapper raises before normal
return processing, recovers and closes both handles and reports possible
birth. Such a failure is uncertain; it cannot become a no-tool refusal.
Factory failure closes the job first, killing a contained child if one was
born. That fatal birth/setup cleanup is distinct from a later log failure in a
running writing tool, which must not invent a new forced-stop permission.

JobCollision covers an existing job, an inaccessible name and a name occupied
by a different kernel object type. Existing limits are never changed.
All mean uncertain admission for the later runner, even with no child created.

## Observations are evidence, not lifecycle decisions

current_identity observes the current process through a real query handle.
observe_process(expected) returns alive/gone/unknown with exact identity:
access denied or failed identity/query is unknown; a different creation time
proves the old identity gone, not the new process dead. PID disappearance's
native invalid-parameter result is treated as gone only after canonical
nonzero identity validation. No observation kills or adopts a PID.

observe_job(job_id, expected_session) refuses to infer absence in another
session. Same-session FILE_NOT_FOUND is absent; access denied/wrong object
type is unknown. An existing empty object is present, not absent.
Query handles have only JOB_OBJECT_QUERY, are noninheritable and are closed
immediately, so polling cannot retain the last kill-on-close reference.
M3d still owns row/version rechecks, uncertain acknowledgement and recovery.

A named job is not a sandbox. An authorized same-user process can deliberately
open another job handle and delay last-close semantics. Supported ordinary
tool trees must not retain such handles or broker work outside containment;
a present/denied object remains uncertainty after runner death.

## Disposable acceptance boundaries

Native tests check exact named-job membership at the earliest fixture action,
Windows argv round trips, NUL stdin, explicit environment, exclusion of an
otherwise inheritable sentinel file, private DACL and job noninheritance.
They verify collision refusal before limit modification and native ABI sizes.

Root/leaf helpers have explicit ready/release handshakes and self-expiry for
failed mutants. Killing their owning fixture process proves both exact Python
identities gone and the named job absent. This is measured both after the
factory returns and while it is paused after CreateProcessW but before child
identity is returned to its caller. No real job ledger or claim is used yet.
A separate root-exits/leaf-retains-pipe case checks that root waits do not wait
for EOF; termination and zero/process-exit observations remain separate.

Failure injection covers security conversion, pipe/handle/attribute setup,
both CRT transfers, native create and both post-birth wrapper/identity
failures. Tests check native handles directly, including handles returned
before the wrapper throws, rather than only tracking Python owner objects.

OfficePC probe: Python3.11.3 x64; Windows build26200; session1; TokenIsElevated=0,
TokenElevationType=3 (filtered token). The witnessed two-Python tree also held
two conhost.exe processes. Tests therefore require the witnessed identities
and eventual whole-job zero/absence, not a guessed fixed process count.
No in-kernel interruption of CreateProcessW is claimed by these handshakes.
Windows CI is additionally required at the delivered exact head.

Primary references checked2026-11-02:
[process attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute),
[assignment before initial thread runs](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812),
[CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw),
[job lifetime](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-createjobobjectw),
[job limits layout](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information),
[accounting layout](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information).
