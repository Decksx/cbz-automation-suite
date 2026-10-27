# Console history and scoped privacy (#153)

This follow-up addresses job history making every store call and console
restart fail after roughly664 jobs. Claude owns the bounded state-home privacy
scan and public scoped helper; Codex owns store integration and archives.
Production submission remains unwired. No live archive operation is run here.

## Archive contract

The reviewed issue contract is
[6455644082](https://github.com/Decksx/cbz-automation-suite/issues/153#issuecomment-6455644082).
Archives live only in the pinned private home's `archive/` directory. No share,
second root or fallback is used. A missing or untrusted archive directory
refuses archiving; hot artifacts remain in place.

Each immutable `jobs-<batch ULID>.zip` holds many jobs, with a versioned
`manifest.json` recording job identity, terminal state, archive time, the
ledger's command digest and each member's path, length and SHA-256. Publication
is temp/write, flush/fsync, atomic replace, directory fsync where supported,
then full readback verification against the ledger and hot sources. A failed
or orphan batch is left as evidence and its name is never reused.

The store must append durable archive pointers/events in one transaction
before removing hot copies. A restart can finish that cleanup only after the
committed pointer and sealed batch verify again. Active, uncertain or held
runner jobs are never archived. The initial policy keeps200 hot jobs or7 days,
whichever is reached first. Audit rows/events are retained; cold archive bytes
still grow. Later roll-up can reduce batch counts without deleting history.

## Archive codec

`jobs.archives` implements the sealed file format and bounded streaming
verification. It neither changes the ledger nor removes a source. The caller
supplies terminal ledger evidence and already-scoped private source paths.
The codec rejects reparse/symlink sources, duplicate/unsafe members, changed
source content, wrong ledger digests, incomplete manifests and reused names.
Command reads verify the batch and read through the same open handle.

Limits are200 jobs,4,096 members,256 filesystem entries per job,512 MiB raw
batch payload and2 MiB manifest, with64 KiB streaming blocks. A command read
retains at most64 KiB. Exceeding a bound refuses rather than silently skipping
members. These are resource bounds, not evidence that a digest can be omitted.

On Windows the standard library cannot fsync a directory handle through the
CRT. File contents are fsynced, and the verified archive pointer gates every
later cleanup. Missing archives after a storage failure refuse cleanup rather
than deleting evidence. The archive is included with the state ledger in the
operator's existing backup responsibility.

## Store integration

The prerequisite helper merged as
`fecddf3694bd3744166a48c9134655d900a3602d` after exact-head review at63db21b.
Store operations now check only the selected job's directories, command,
receipt, stop path and runner lock. Locks and their parents are checked before
opening them, so inspection cannot discover an escaping lock path too late.
No get/event operation enumerates other jobs or archives.

Runner-lock creation and cleanup both hold admission exclusion. Cleanup checks
the opened file's identity again after close before unlinking its name. The
durable claim still holds admission. If cleanup fails while the body raises,
the body error propagates with a cleanup note. Closed failed-claim locks can
remain; archive maintenance removes them when handling the terminal job.

`jobs.history.JobHistory.initialize()` explicitly prepares the private archive
directory. `archive()` implements the200-job/7-day policy in bounded batches,
packing by bytes so large jobs do not make every batch exceed its limit. It
skips active or held-runner rows. The verified batch is published first; an
immutable pointer and an archived event commit together; only then does hot
removal begin. No job row or old event is removed. This additive schema keeps
version1 and adds archive pointers plus query indexes; old terminal claims are
still fenced and no existing column is reinterpreted.

`cleanup()` re-verifies committed batches and their remaining hot files before
resuming interrupted removal. Cleanup acknowledgments are append-only events,
so completed older batches cannot starve later recovery. No pointer means no
cleanup authority; orphan batches remain untouched. Recursive removal verifies
the resolved absolute job path remains in the pinned jobs directory.

`read_command()` verifies a hot command or the committed batch and ledger digest
through a single opened batch handle. When both copies exist, either digest
failure refuses the read; no fallback hides corruption. Cold log paging belongs
to M3b, using the same sealed-batch verification. M3d must schedule idle cleanup
and retention before M4 enables production submissions. This library does not
start background workers or run maintenance on the live state home.

## Verification checkpoints

The three M3a surviving mutations now have direct witnesses and each fails
its new test: stale uncertain version on an eligible source row, complete
orphan-directory reuse, and root identity refusal before lock-file creation.
Direct runner-inspection tests also cover root access, a network location and
zero identity. The two files pass63/63 before archive integration.

The independent archive codec passes17 focused cases, including corruption,
changed sources, fsync failure, resource bounds and a real Windows junction.
The integrated four-file focus passed104 cases before the added byte-packing
case, which also passed. Final full-suite/guard results are recorded in the
development log and PR handoff; CI and independent review still gate merge.

The1,001-job fixture creates more than2,000 job entries, successfully restarts,
archives to exactly200 hot jobs across five batches, and retains readable cold
commands. Its measured privacy-check count stays13 before/after growth; one
sample measured36.347 ms and58.503 ms. Timings vary; the structural check-count
bound is the stable assertion. No claim of constant SQL cost or total archive
byte retention is made. Cold audit history grows and future roll-up remains
separate. Production archival is not enabled by this library-only slice.

Cleanup verification has a separate missing-source mode for interrupted hot
removal after the archive pointer has committed. Missing members are then
acceptable; remaining members must match and new/unlisted files still refuse.
The codec does not supply or replace the caller's durable-pointer proof.
