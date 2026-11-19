# D3a: durable advisory progress

`jobs.observations.JobObservations` implements the persistence and bounded
catch-up part of the accepted M3d contract. It is not yet wired to startup,
HTTP, SSE, a background scheduler or the browser. Nothing launches a tool.

Call `initialize()` explicitly under the console's existing ownership. It adds
`job_observations` to the schema-v1 jobs database, keyed by job ID and nonce.
Each published snapshot has its own consecutive generation. Initialization and
observation writes leave `jobs`, `job_events`, `console_meta`, lifecycle version
fences and the unused `jobs.progress_json` column unchanged.

`snapshot(job_id, nonce)` is read-only. `consume_once(job_id, nonce, limit=1000)`
is for the service consumer, not a GET handler. One call reads at most one
existing bounded `JobLogs.read_page` page. The consumer releases its initial
admission lock and database connection before reading the page, then reacquires
admission ownership for a transactional commit. The log reader owns its usual
short admission lock while reading; no handle or transaction spans both phases.
The commit checks the job's nonce again and compares the observation generation.
A competing consumer refuses with `STALE_OBSERVATION`; it cannot rewind a newer
cursor. Retry from the durable snapshot on a later tick. A failed write leaves
the old cursor available for retry.

For the D3b shared producer, `consume_page(job_id, nonce, limit=1000)` returns an
`ObservedPage` envelope containing that same bounded page and its committed
observation. The existing `consume_once` delegates to it and still returns only
the snapshot. There is no second source read. A batch is returned only after
commit succeeds and admission/database/source ownership is released; read,
write and generation failures never return a usable batch. The durable cursor
still chooses the next page; this API does not accept arbitrary replay cursors.

The envelope contains server-owned JSON objects, not a wire DTO or an iterator
over open files. Its frozen fields do not freeze nested records. A shared
publisher must encode immutable bytes once before fan-out and deduplicate
unchanged observation generations/empty pages. Subscriber queues, slots,
cadence, replay scheduling and HTTP ownership remain separate implementation
work; this API only supplies the producer handoff without duplicate I/O.

The snapshot contains:

| Field | Meaning |
| --- | --- |
| `generation` | Observation version; unchanged polls do not increment it. |
| `cursor` | Decimal source-byte offset through which records were examined. |
| `proven_offset` | Latest observed index coverage, also a decimal string. |
| `progress` | Last complete record accepted by `logs.parse_progress`, or null. Its cursor is the end of that progress record, which can precede the parse cursor. |
| `gap_count`, `last_gap` | Number of gap spans encountered by catch-up, and the latest span. These are not a count of lost progress events. |
| `log_status`, `eof` | Latest log observation, never the job's lifecycle outcome. |

The log reader leaves incomplete short live lines for another poll. The
consumer does not assemble large continuation fragments or treat a line after
a gap as a known beginning. Ordinary output advances the parse cursor without
clearing the last progress record. Restart resumes from the durable cursor;
retention and dropped-byte gaps are recorded without guessing progress counts.
Cold reads retain the existing member-verification boundary.

A refused selected log read raises `LogRefused` or `ArchiveRefused`, and a
successful observation commit records `log_status='unavailable'` and `eof=false`, preserving
the last progress and cursor. Repeated identical failures do not increment the
generation. A later successful read restores the observed status. Unknown tails
remain `unknown_tail`, not EOF. The log adapter converts `OSError` only around
the selected archive-member read or hot-file open/read/close, preserving the
original exception as its cause. Private-path checks and admission/home/database
operations stay outside those catches. Private-home, database and commit failures
still propagate; this table is observational evidence, not a replacement for either
the log source or the runner's fresh native/receipt proof.
The pinned-member codec also normalizes invalid DEFLATE payloads (`zlib.error`)
and truncated-stream `EOFError` to `ArchiveRefused`; these decoder failures
cannot bypass this boundary.

## Compatibility and validation

The compatibility witness starts with a schema-v1 runner claim, a disposable
contained child and its completed log pump, before creating the observation
table. With the claim and running ledger row still owned, initialization and
catch-up leave the full lifecycle/audit/clock rows byte-for-byte equal. The
existing runner then publishes its receipt and finalizes with the original
expected version. The observation survives finalization. This proves the
additive/version-fence boundary; it is not a production rollout receipt.

The runner's store and receipt modules are unchanged from pre-D3
`ee13ad91cae834163930845da1d722efcf3bacbc`:

| Module | Git blob |
| --- | --- |
| `jobs/store.py` | `0a6d6426515ac934d4e84bb00168fe7930a98c10` |
| `jobs/receipts.py` | `269a0b1527b01c6e39dbc134f8e2e4d112045fd3` |

The review fix changes only `read_page` in `jobs/log_service.py` to classify
source-file I/O failures. Its `create_writer` method is unchanged, as are the
runner, log-disk/pump, native-I/O and recovery modules. The compatibility fixture
still exercises that unchanged writer/receipt/lifecycle path.

The focused tests also cover read-only snapshots, bounded pages, exact 63-bit
cursors, retained/cold logs, partial and fragmented records, missing evidence
after EOF, failed commits, nonce changes, generation races and exhaustion.
The development log records mutation and suite results for the candidate.

## Remaining D3 work

The stream manager must own and share the per-job reader/consumer rather than
create a consumer for each subscriber. Its integration should use consume_page
to avoid a second independent source poll merely to publish progress. The remaining acceptance
gates include eight global stream slots with spare JSON request capacity,
bounded subscriber queues, session revalidation, owned iterable closure,
disconnect/shutdown cleanup, hot/cold cursor resumption and measured reader
cadence. D4 supplies the browser controls and authenticated fetch stream.
