# Holding stalled incoming archives

Implements comic #136 and Claude's approved design in issue comment
6420991540. Automatic holds are **off by default**. This does not change
the pure ZIP readiness probe's `READY` / `RETRY_LATER` contract.

For a later, separately coordinated Windows watcher rollout:

```powershell
python -m scripts.cbz_watcher --readiness-stall-minutes 30 `
  --readiness-hold-root C:\test-holding
```

The explicit holding directory must already exist, be writable, outside
the watched tree, on the same volume and free of reparse components. Its
physical identity is pinned at startup. Startup validates actual write
access before starting discovery or observers. No directory is selected
automatically; an omitted root or zero minutes disables holds. Minutes
must be finite, nonnegative and representable as finite seconds. This PR
does not change the GUI launcher, restart a watcher or enable live holds.
Claude owns the later configuration/rollout receipt.

Each failing archive gets its own monotonic inactivity budget, default
30 minutes. An observed change to device/file identity, size or mtime
restarts that budget. Repeated events/retries never restart it. Budgets
are in-memory: watcher restart starts fresh. Missing, fully ready and
empty directories clear their stale budgets. These are observations,
not exclusive proof that no writer exists or an atomic filesystem snapshot.

Only `incomplete_central_directory` is allowlisted for automatic holding.
Unknown/new reasons, sharing violations, stat failures and transient I/O
expire visibly but cannot authorize movement. The originating case was
extracting only part 1 of a seven-part RAR (Lookism, 2026-10-25): the ZIP
prefix stopped growing without acquiring a central directory/EOCD. The
fixture models that stable truncated byte stream; it does not use real
library data or invoke a RAR extractor.

At the deadline, the watcher claims the existing directory-processing
exclusion, then re-probes the archive. The final generation check is
serialized with notifications through the rename. It revalidates source
identity/size/mtime and root identities, including after receipt I/O.
Reparse or unidentified sources/targets refuse. Normal processing also
claims exclusion atomically, so it cannot enter during a holding operation.

Holding performs only a same-volume Windows no-replace rename into a
new exclusive `readiness-*` attempt directory, retaining the archive's
original name and bytes. No overwrite, copy fallback, deletion, metadata
normalization, decensor processing or library routing occurs. Other hosts
cannot enable automatic holds until an equivalent no-replace primitive
is implemented and verified. Move failures retain the source and log the
failure; retries reuse their identified attempt directory.

Before rename, `<original-name>.held.json` is exclusively created and
fsynced in the attempt directory. It records the source/destination,
observed elapsed seconds, reason, source fingerprint and the loaded
watcher source's SHA256 as `watcher_version`. `written_before_rename=true`
means this is an **intent**, not a completion receipt: a failed move or
crash can leave JSON with no archive beside it. A successful move has the
archive and sidecar together, plus a `Readiness held` log entry. The
sidecar is for David to read; nothing automatically replays it. Receipt
failure prevents movement. Paths and elapsed time refer to the first
attempt; later retries do not rewrite that history.

The attempt directory is tracked before receipt work, so persistent open,
write or fsync failures reuse one attempt per unchanged archive instead of
creating an orphan every five seconds. Partial receipts remain unready.
A retry opens only its own identified receipt without truncation, checks
the open handle's identity, then rewrites/truncates and fsyncs it before
marking it ready. A replaced or unidentified file is never adopted or
written. A completed receipt must still exist with the same identity on a
later move retry. Recovery retains the first-attempt source/time fields.

Ready siblings still wait for the directory gate. After a successful
hold, a fresh recursive inventory/readiness probe decides whether the
remaining siblings can proceed. The old result is never reused. Early
sibling release during the inactivity budget is separately tracked in
comic #137: the current importer rescans, renames and moves whole trees,
so safely processing a subset requires a broader selected-input boundary.

This feature does not repair malformed archives, attest quiescence,
resume an external download, or authorize a canonical naming plan/apply.
