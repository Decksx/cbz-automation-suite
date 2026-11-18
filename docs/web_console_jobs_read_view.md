# Read-only Jobs view (M3d D2a)

This slice adds a Jobs panel to the existing authenticated console shell. It
does not enable tool submission, stop, acknowledge or recovery actions. The
M3d contract's remaining D2 mutations and D3 streaming/progress are separate.

## Data boundary

`Supervisor.read_jobs` and `read_job` retain the console operation guard for
one bounded read. `JobViews` retains the pinned home/admission exclusion and
uses one deferred, query-only snapshot. A changed WAL data_version rejects the
response. No lock, DB transaction, command handle or archive reader survives
the returned DTO. Cold jobs retain their ledger summaries/events; no command
or archive payload needs opening to display them.

Summaries explicitly select job ID, admission sequence, nonce/version, safe
tool identifier, kind, read-only/dry-run flags, state, request/update timestamps,
exit code and a fixed outcome vocabulary. Free-form refusal reasons, raw
commands, seals, process identity, actor and event details remain private.
Unknown tool labels/events become `unknown`/`other`. This is deliberate minimal
history, not a diagnostic evidence viewer. State `uncertain` remains uncertain;
reading never reconciles, stops or releases it.

## Protected routes

| Route | Query | Response |
| --- | --- | --- |
| GET/HEAD `/api/jobs` | `before`: positive admission cursor; `limit`: 1–100, default 50 | `{jobs, next}` newest first |
| GET/HEAD `/api/jobs/{id}` | canonical ULID; `after`: event cursor >=0; `limit`: 1–1000, default 100 | `{job, events, next}` events oldest first |

Cursors are canonical decimal strings in JSON, preserving SQLite's 64-bit
sequences in JavaScript. An absent `next` is represented as null. Unknown,
duplicate, blank, signed, noncanonical or out-of-range query values refuse with
400. Missing jobs return 404; unavailable/private-state failures return a
generic 503, retaining the actual diagnostic in the private server log.

Dynamic dispatch runs after existing Host, session and state-change guards.
HEAD has the GET headers and no body. POST to these read views is 405 only
after Origin/CSRF checks; unimplemented action paths remain 404. All JSON uses
the existing no-store and security headers. There is no raw-file route.

## Browser behavior and limits

The Jobs panel uses the existing authenticated API helper and text-only DOM
builder. Refresh returns the newest page; Older replaces the current page.
Details show a bounded event page, with Later events replacing it. Busy state
has explicit text, prevents overlapping requests and disables controls.
Failures keep the displayed snapshot, show a retryable error and restore
controls. Opening details moves keyboard focus to their heading. The panel
and configuration layout fit a narrow viewport; only tables scroll internally.

There is no automatic polling, live log, progress persistence or run button.
Those follow the reviewed D2/D3/D4/M4 contracts. The console is not deployed
as a Tk replacement by this PR; fixture screenshots/tests demonstrate the view
without reading a live library or launching tools.
