# Lock topology: classify → move → index

Enumerated from the tree on 2026-08-05 at `cdc4288`, before any
synchronization was added, for issue #32. This is the read-only survey
the implementation is designed against; it records what exists, not what
is planned.

Nothing in this document changes behaviour. The per-series lock is not
wired into the watcher, routing v2 remains `off`/`shadow`, and legacy v1
is still authoritative for every move.

## Lock inventory

Every `threading` lock reachable from the watcher's processing path,
with the sites that acquire it.

| lock | defined | type | acquired at | keyed by |
|---|---|---|---|---|
| `_processing_dirs_lock` | `cbz_watcher.py:124` (module global) | `Lock` | 1249, 1254, 1278, 1631, 1717 | **arrival path** |
| `WatcherRouter._index_lock` | `cbz_watcher_router.py:213` (instance) | `RLock` | 276, 352 | — (one per router) |
| `DirectorySettleTracker._lock` | `cbz_watcher.py:1570` (instance) | `Lock` | 1580, 1595, 1606, 1619, 1646, 1654, 1663, 1693 | — |
| `ProgressReporter._lock` | `cbz_watcher.py:152` (instance) | `Lock` | 156 | — |

`_index_lock` is private and referenced **only** at its three sites —
its definition and the two `with` statements. No code outside
`WatcherRouter` can acquire it, which is what makes the ordering rule
enforceable at all.

Locks outside this chain, listed so future work does not have to
re-derive that they are irrelevant: `cbz_core.py:167` (translation
cache), `cbz_sanitizer.py:196,214`, `cbz_gap_checker.py:40`,
`cbz_library_maintenance.py:88,177`, `cbz_compilation_resolver.py:59`,
`comic_automation/service.py`, `comic_automation/jobs/worker.py`.

## Where the concurrency comes from

`threading.Timer` at `cbz_watcher.py:1590`, `1605`, and `1679`, all
owned by `DirectorySettleTracker`. Each fires `_on_settled`, which calls
`process_and_move_directory`. Several arrival directories therefore
settle and process on separate threads, sharing one `WatcherRouter` and
one `SeriesIndex`.

## The critical section

In `_process_and_move_directory_inner`, per `comic_dir`:

```text
1399  _resolve_series_dir_name(...)        -> series_name   [the lock key]
1409  _apply_fallback_naming(...)
1418  _shadow_route(...)  -> classify()    -> acquires _index_lock (276)
1420  _move_loose_files(...)               -> filesystem move
1421  _note_move(...)     -> note_move()   -> acquires _index_lock (352)
```

and the non-nested branch, identically:

```text
1424  _shadow_route(...)                   -> acquires _index_lock (276)
1426  _move_cbz_dir(...)                   -> filesystem move
1428  _note_move(...)                      -> acquires _index_lock (352)
```

So classify → move → index is lines 1418–1421 or 1424–1428, and the key
it must be serialized under is produced at 1399, before it begins.

## The gap #32 describes

`_processing_dirs_lock` is keyed by **arrival path** and only prevents
two threads working the same or a nested path. Two distinct arrival
paths that resolve to the *same series identity* are not serialized by
anything. Both can classify before either moves, both observe an index
miss, and both decide independently — which is the split #32 opens with.

`_index_lock` does not close it. It makes each index operation coherent;
it does not make the three-step sequence a transaction. That distinction
is stated in #32 and holds against the code.

## Finding: the lock key is computed from state the lock protects

`_resolve_series_dir_name` reads the destination filesystem. At
`cbz_watcher.py:1060`:

```python
if _find_existing_series_dir(bare_base, dest_folder) is not None:
    return bare_base, dir_number
```

The resolved identity therefore depends on which series directories
exist in the destination — which is exactly what another thread's move
changes. The key cannot simply be computed and then locked:

```text
thread A                        thread B
resolve -> "Berserk Ch. 4"      resolve -> "Berserk Ch. 4"
                                move creates X:\Manga\Berserk\
                                (destination state changed)
lock("Berserk Ch. 4")           lock("Berserk Ch. 4")
   ...but A would now resolve to "Berserk" if it looked again
```

A lock cannot be taken on an identity before that identity is computed,
and computing it races with the very operation the lock exists to
serialize.

The resolution this points at is the pattern already used for staging
transfers: resolve provisionally, take the lock on that key, **re-resolve
under the lock**, and if the identity changed, release and retry under
the new key. A bounded retry, not a loop that can spin: the identity can
only change when another thread completed a move, so progress is
monotonic.

This is recorded as a design consequence, not implemented here. It
should be settled before the critical section is wrapped.

## Ordering rule

```text
series-operation lock  MAY acquire  router index lock
router index lock      MUST NEVER acquire  series-operation lock
```

The forbidden edge does not exist today: `_index_lock` is acquired only
inside `WatcherRouter.classify` and `WatcherRouter.note_move`, neither of
which calls back into watcher code. The rule is therefore currently
satisfiable and the job is to keep it so as the lock is introduced —
which is why #32 requires the ordering be asserted by deterministic
tests rather than merely followed by the code as written.

## Why a tracker rather than a deadlock test

A test that proves ordering by letting two threads deadlock has to
decide how long to wait before calling it a deadlock, which makes it a
timing test wearing a correctness costume — it passes on a fast machine
and flakes on a loaded one. `docs/session_protocol.md` records three
tests that failed exactly that way.

The ordering is instead checked at acquisition against a thread-local
record of what that thread already holds. A violation raises
immediately, names both the held and the requested lock class, and needs
no second thread to demonstrate.
