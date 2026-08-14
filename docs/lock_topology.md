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

## Lock key normalization

The key is `cbz_lock_order.lock_key`, built on `cbz_routing.series_key` —
the identity routing itself files under — and **not** the display
directory name. The directory name is not guaranteed unique across the
operation domain, and two arrivals whose folders differ only in
punctuation resolve to one series.

`series_key` lowercases, strips `uncensored`/`decensored` markers,
replaces every non-word non-space character with a space, and collapses
whitespace. Verified over a sample, these already key the same:

```text
case          "BERSERK"          == "Berserk"
punctuation   "Berserk!!"        == "Berserk"
hyphen        "Attack-on-Titan"  == "Attack on Titan"
marker        "X (Uncensored)"   == "X"
outer space   "Berserk "         == "Berserk"
```

### Measured: composition splits identities [RESOLVED 2026-08-13]

**Resolved by #44's Unicode strand.** `series_key` now normalizes to NFC
before anything else, so composition forms converge everywhere rather
than only inside the lock domain, and `lock_key` no longer normalizes at
all — it delegates entirely. The finding below is the original
measurement and is kept as written; see *Resolution* at the end of this
section for what changed.

`series_key` applies no Unicode normalization. Measured on this
checkout, a title carrying a combining diaeresis:

```text
NFC form  ->  'kantai'  (with the precomposed vowel, a word character)
NFD form  ->  'ka ntai' (the combining mark is not \w, so it becomes a space)
```

Two forms of one title, two keys, therefore two locks and no
serialization at all. Content arriving from a macOS-side share is
routinely NFD, so this is reachable rather than theoretical. `lock_key`
normalizes to NFC before and after `series_key`, which closes it.

This makes the lock key deliberately **coarser** than routing's own: two
names routing treats as distinct series can share one lock.
Over-serializing costs a little concurrency; under-serializing costs the
split this issue exists to prevent. The asymmetry is taken on purpose.

#### Resolution 2026-08-13

NFC moved to the front of `series_key`, before marker removal,
lowercasing, and the punctuation rule. The ordering is load-bearing: the
punctuation rule is what destroys a combining mark, so normalizing after
it would compose nothing.

```text
NFC  'Kantai'-with-umlaut      ->  'kantai'-with-umlaut
NFD  'Ka' + U+0308 + 'ntai'    ->  'kantai'-with-umlaut   (was 'ka ntai')
```

`series_key` normalizes at **both** ends, and the two calls do different
jobs. The first is load-bearing: composition must precede the
punctuation rule, which destroys combining marks. The last is a
postcondition — the returned identity is NFC whatever the transforms in
between do.

Both halves of `lock_key`'s wrapper were therefore removed: the inner
call because `series_key` normalizes its own input, the outer because
`series_key` now guarantees its own output.

An earlier draft removed that outer call on the strength of an
exhaustive scan instead — every codepoint in four embeddings, plus every
cased character crossed with every combining mark in three orders:
**14,800,248 probes, zero non-NFC outputs**. That measurement stands and
is kept here as evidence, but it described the Unicode database and the
`str.lower()` behaviour of the day it was taken. `.lower()` can emit
combining sequences, so a revision to one case mapping could have
invalidated it silently. The postcondition carries the invariant;
`test_series_key_output_is_always_nfc` corroborates it.

**The lock key is therefore no longer coarser than routing's own.** The
asymmetry described above was a compensation for an index that split
composition forms; the index no longer splits them, so the two rules
agree exactly.

The change also *separates* identities in one direction, which is easy
to miss: the old rule deleted an NFD accent rather than failing to
compose it, so `'Cafe' + U+0301` keyed to `cafe` and collided with the
plain ASCII `Cafe`. It now keys to `café`. An index built under the old
rule must be rebuilt rather than topped up.

### Two gaps documented rather than closed [BOTH RESOLVED 2026-08-13]

Both were closed by #44, in separate PRs, after this section was
written. The findings are kept as recorded; the resolutions are noted
inline.

Both would change `series_key` itself, and therefore change SeriesIndex
keys, which is not something an additions-only chunk may do:

- **Separator handling is inconsistent.** `_` is a word character and
  survives; `-` does not. So `Attack_on_Titan` and `Attack on Titan` key
  differently while `Attack-on-Titan` and `Attack on Titan` do not.
  — **[RESOLVED 2026-08-13, PR #51.](https://github.com/Decksx/cbz-automation-suite/pull/51)**
  `_` is now normalized as a separator. 14 inputs changed identity.
- **There are two implementations.** `cbz_routing.series_key:92` and
  `cbz_watcher._series_key:983` each define their own copies of the same
  three regexes. They agree today — verified across a sample — but
  nothing enforces that they keep agreeing, and a divergence would mean
  the watcher comparing series by one rule while the lock and the index
  use another.
  — **[RESOLVED 2026-08-13, PR #49.](https://github.com/Decksx/cbz-automation-suite/pull/49)**
  Consolidated onto `cbz_routing.series_key`; the old names survive as
  aliases, and a test pins the set of modules that import it.

Neither affects correctness of the lock as specified, because `lock_key`
is the single definition the registry uses. Both are worth their own
issue.

## Provisional resolve, lock, re-resolve

Because the identity is derived from destination state, a single
pre-lock resolution serializes operations that already agreed without
guaranteeing the identity is authoritative once the section begins.
`stable_series_lock` implements the bounded algorithm:

```text
1. resolve provisionally           -> K1
2. acquire the series lock for K1
3. re-resolve while holding K1
4. still K1  -> body runs, holding K1
5. now K2    -> release K1 having mutated nothing, acquire K2
6. re-resolve under K2; still K2 -> body runs
7. changed again -> UnstableSeriesIdentityError
```

Two keyed acquisitions, never more. The second re-resolution covers
another operation completing between releasing K1 and acquiring K2. A
third distinct identity means the topology is still moving; looping
would starve the operation and make the outcome depend on scheduling
rather than on the library, so it refuses with every identity it
observed.

Releasing K1 is safe precisely because nothing has been mutated under
it. The resolver passed in must therefore be free of irreversible work:

```text
permitted    read routing inputs
             inspect destination directories
             re-resolve identity

forbidden    move or rename a payload
             create authoritative staging state
             update index or repository state
             emit a completed routing decision
```

Nothing in the primitive can enforce that, so it is stated as the
contract it is.

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
