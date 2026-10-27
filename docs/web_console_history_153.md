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

## Current implementation checkpoint

The three M3a surviving mutations now have direct witnesses and each fails
its new test: stale uncertain version on an eligible source row, complete
orphan-directory reuse, and root identity refusal before lock-file creation.
Direct runner-inspection tests also cover root access, a network location and
zero identity. The two files pass63/63 before archive integration.

The independent archive codec passes17 focused cases, including corruption,
changed sources, fsync failure, resource bounds and a real Windows junction.
Store/private-helper integration and ledger-first cleanup follow after the
helper's exact-head review and merge. This checkpoint is not a claim that the
complete #153 fix is ready or that production archival is enabled.

Cleanup verification has a separate missing-source mode for interrupted hot
removal after the archive pointer has committed. Missing members are then
acceptable; remaining members must match and new/unlisted files still refuse.
The codec does not supply or replace the caller's durable-pointer proof.
