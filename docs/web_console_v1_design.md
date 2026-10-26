# Web console v1 design (#36)

Status: **proposed design, for review.** This document has no implementation
code. It records decisions and open questions for the first web console. The
console replaces the Tk launcher (`apps/cbz_gui.py`) and adds a review screen
for canonical rename plans.

Revision 2 (2026-10-26) folds in codex-lead's CHANGES REQUESTED review of
`11ebf94` (D1–D5, preview sealing, bootstrap token) and David's answers to
Q1, Q3 and Q10, and records where the server is hosted. §15 lists what was
decided and what is still open.

Governing architecture: `docs/gui_architecture_implementation_roadmap.md`
(cited below as "roadmap §n"). Visual reference: David's Claude Design mockup,
https://claude.ai/code/artifact/03c76b76-f967-4f2b-9c06-c8011360086e
(Dashboard, the Library Maintenance tool runner, Series Review).

## 1. Scope

David approved this scope in chat on 2026-10-26.

In v1:

1. **Dashboard.** It must match the Tk GUI closely enough that the GUI can be
   retired: watcher start, stop and status, the live log, and settings. It
   adds a job list and a "Needs your decision" queue.
2. **Generic tool runner.** A declarative parameter schema for each tool drives
   it. The first two tools are the watcher and the maintenance workflow. The
   server builds the command from validated parameters, and the browser never
   sends a command string. Dry run is the default wherever a tool supports
   it, and the exact command is always shown.
3. **Plan review screen** for the `canonical-naming-plan-v1` document. It
   approves one exact `plan_digest`. The approval is append-only and tied to
   that version (roadmap §3.3).
4. **Localhost only.** The server binds 127.0.0.1, and §2.6 security applies
   anyway: a one-time bootstrap code exchanged for a session, Origin and Host checks, CSRF protection, a path
   allowlist, and validation of every parameter.

Not in v1: Series Review (it is next, as v1.1), phone or remote access, real
authentication, scheduling, presets shared between machines, applying a plan
from the browser (§8.5), editing the number-style registry, and the mockup
tiles that have no defined backend source yet (§7.3).

## 2. What the Tk GUI does today (parity inventory)

Measured on `master` at `f43174e`.

| Area | Current behaviour | v1 console |
|---|---|---|
| Tools | `TOOLS` lists 9 entries. They are: Series Workflow and Library Maintenance (`cbz_workflows.py`), Camelia Decensor, CBZ Watcher, Gap Checker, Apply Dry-Run Plan, Exclusions Log, Series Review (`propose-series`) and Stage Similar Series. | The watcher and maintenance schemas are in v1. The rest get schemas in M10, before the Tk GUI is retired. |
| Process model | Runs one subprocess at a time, as `python -m scripts.<module>` with `cwd=REPO_ROOT` and env `CBZ_PROGRESS=1`, `PYTHONIOENCODING=UTF-8` and `PYTHONUNBUFFERED=1`. stdout and stderr are merged into one pipe. | The same invocation form and env, launched through a detached runner (§5). |
| Watcher arguments | `--series-resolver` (on by default) with `--resolver-comix-root` (`Z:\comics\Comix`), `--resolver-manga-root` (`Z:\comics\Manga`), `--resolver-quarantine-root` (`Z:\comics\_resolver_quarantine`), `--resolver-threshold` (0.95/0.97/0.99), `--resolver-refresh` and `--resolver-normalize-apostrophes`. Also `--ai-decensor` with up to 4 ordered `--ai-decensor-model` stages, `--ai-decensor-archive-dir` (`F:\ai-decensor-originals`) and `--ai-decensor-reprocess`. | Everything here, plus the watcher flags the Tk GUI never exposed: `--readiness-hold-root`, `--readiness-stall-minutes`, `--number-style` and `--number-style-registry`. |
| Stop | `proc.terminate()`. Camelia Decensor uses `taskkill /T /F` because its model workers would otherwise survive. Stopping a workflow leaves its `subprocess.run` children running. | Every stop terminates the whole job object (§5.2). Stopping a writing run is enabled per tool after its interrupt gate (§5.5). |
| Settings | **Nothing is persisted.** Every launch resets to the defaults in `TOOLS`. The only file the GUI writes is `series_decisions.json`, next to the proposal. `ROUTING_JSON` is defined but never used. | Last-used validated parameters are saved per tool, and a Settings page shows the effective configuration (§7.4). |
| Log | An in-memory Tk text pane. Lines are tagged error, warn or success by keyword, and `CBZ_PROGRESS n/t p%` lines drive a progress bar. The log is lost when the window closes. | The log is a durable file for each job, streamed live. The same classification is used for display only, and progress is parsed on the server. |
| Duplicate watchers | Nothing detects a watcher already started from another GUI window, the CLI or `config/CBZWatcher_Task.xml`. | A single-instance lock in the watcher, plus a process scan (§6). |
| Series review | A Tk `Toplevel` with Yes/No/Skip that writes decisions and runs `apply-series`. | v1.1. |

The watcher's watch folder (`WATCH_FOLDER`) and its log file
(`Logs/cbz_watcher.log`, rotating 5 MB × 3) are hard-coded in
`scripts/cbz_watcher.py`. The watcher logs to stderr as well, so the runner
captures its log in the job output.

## 3. Backend choice: the standard library (WSGI)

**Decision: a stdlib WSGI application served by `wsgiref` with a threading
mixin. It adds no third-party dependencies.**

| Option | For | Against |
|---|---|---|
| **stdlib (`wsgiref` + `ThreadingMixIn`)** | No new dependencies, so `requirements.txt` and CI stay as they are. It runs under the `C:\Python311` interpreter the desktop shortcut already uses. The WSGI app is a plain callable that tests can drive in-process. It can move to a production WSGI server (for example waitress) later without code changes. | Routing, JSON bodies, cookies and SSE have to be hand-written (estimated at about 300 lines). `wsgiref` is not hardened for hostile networks. |
| Flask | Familiar from the Camelia app. Has a test client and Jinja2. | Adds five packages (Werkzeug, Jinja2, itsdangerous, click, blinker) and still needs hand-rolled CSRF, Origin and Host checks. |
| FastAPI | Roadmap §12's stated preference. Pydantic validation, OpenAPI. | Needs uvicorn (not installed on this machine) and httpx for tests. Async plus subprocess supervision adds complexity v1 does not need. `CLAUDE.md` explicitly defers a "FastAPI control plane". |

Reasons:

- Roadmap §16 lists "exact FastAPI endpoint names" as provisional. The frozen
  invariant is §7, the service layer before the HTTP layer. All behaviour
  lives in plain service modules (§10), and the HTTP layer is a thin adapter,
  so moving to FastAPI later is a transport change only.
- The threat model is one operator on loopback. The risks `wsgiref` leaves
  open (slowloris, hostile clients) are bounded by the 127.0.0.1 bind, a
  64 KiB body cap, socket timeouts and a cap on concurrent streams.
- The console stays in the existing CI matrix (`windows-latest`, Python 3.11)
  with no dependency review.

This lifts part of `CLAUDE.md`'s "Deferred until a concrete need" line: a
local web control plane now has a concrete need (David, 2026-10-26). M2 records
that in `docs/engineering_decisions.md` and updates the Scope line in
`CLAUDE.md`. FastAPI and an SPA framework stay deferred.

## 4. Runtime layout and configuration

### 4.1 Files in the repository

```text
apps/web_console.py                  launcher (desktop shortcut target)
comic_automation/web/
  __init__.py
  app.py            WSGI app: router, request/response, error mapping (thin)
  server.py         loopback-only threading WSGI server, startup and shutdown
  security.py       session token, cookie, Host/Origin, CSRF, headers
  config.py         console config load and effective-config report
  paths.py          path allowlist validation
  tools/
    schema.py       schema loader and meta-validation (fails closed)
    argv.py         parameters -> argv builder, preview seal
    registry.py     loaded schemas, saved defaults
  tool_schemas/
    watcher.json
    maintenance.json
    canonical_plan.json
  jobs/
    store.py        SQLite job store (state DB)
    runner.py       detached per-job supervisor process (python -m ...)
    supervisor.py   start, stop, reconcile, progress parsing
    logs.py         offset reads and SSE tailing
  watcher.py        watcher status: lock probe, process scan, start/stop
  plans.py          plan ingest, digest re-seal, paging, approvals
  decisions.py      "Needs your decision" queue
  static/           index.html, css/console.css, js/*.js, fonts/
config/web_console.example.toml
tests/web/          see §11
```

This follows the repository layout standard: the package goes in
`comic_automation/`, the GUI client in `apps/`, and checked-in defaults in
`config/`. The standard's `docs/design/` folder does not exist in this repo
yet, so this document sits in `docs/` (see Q12).

### 4.2 Runtime state on G:

David decided (PR comment, 2026-10-26) that runtime state lives in
`G:\ComicAutomation\web-console\`, with the other operational data (standard
rule 1.9). `CBZ_WEB_CONSOLE_HOME` can override it for tests only.

```text
console.toml        operator configuration (copied from the example on first run)
console.sqlite3     jobs, job events, saved parameters, plans index, approvals
locks\              admission.lock, console.lock, jobs\<job_id>.runner.lock
session.json        port, launcher secret and pid (ACL: the operator account only)
jobs\<job_id>\      command.json (the sealed preview, §8.1), log\seg-*.log, exit.json
plans\<digest>.json immutable copies of ingested plans
plans\incoming\     planner --output targets (§8.4)
backups\            daily SQLite backup of console.sqlite3, 7 kept
console.log         the server's own log (secrets redacted, §9)
```

**G: unavailable.** If G: or the state directory is missing, unwritable,
or has changed physical identity (`st_dev`/`st_ino` pinned in
`console.sqlite3` on first run) since the last start, the server refuses to
start. The launcher shows the reason in a message box. There is no fallback
location, because a second jobs database would split admission (§5.2).
`console.sqlite3` holds the approval ledger, so it goes on the operator's
G: backup checklist next to `comics.db`. The console never applies a plan,
so backup-before-apply stays with the CLI apply.

**Path rule.** The *state directory* must not lie inside any configured
library, quarantine, watch, readiness-hold or decensor-archive root, or
inside the repository. Path *parameters* are a separate matter: they may
point into those roots. That is what the allowlist in §4.3 is for.

### 4.3 `console.toml`

```toml
[server]
port = 8765                  # bound on 127.0.0.1 only; the host is not configurable

[paths]                      # path parameters must sit under a root of their role
library      = ['\\tower\media\comics', 'Z:\comics']
quarantine   = ['Z:\comics\_resolver_quarantine']
decensor_archive = ['F:\ai-decensor-originals']
readiness_hold = []          # empty: holds stay off (the watcher's own default)
database     = ['G:\ComicAutomation\database']   # planner --database
plan_import  = []            # the console's plans\ directory is always allowed

[naming]
# Passed explicitly to every tool that takes --number-style-registry, so the
# watcher and the planner read the same file. If unset, the effective value is
# CBZ_NUMBER_STYLE_REGISTRY, then config/number-styles.json (DEFAULT_REGISTRY).
number_style_registry = ''

[python]
executable = ''              # empty: python.exe next to the console's interpreter
```

The Settings page shows each effective value with its source (default,
`console.toml` or environment), whether it is editable, and whether a restart
is needed (roadmap §5.3). The console does not edit `console.toml` or the
number-style registry in v1. It shows the registry's path, SHA-256 and root
count, the same facts the planner records in its provenance.

## 5. Process and job model

### 5.1 Jobs and states

A job is one run of one tool with one sealed preview (§8.1). The `jobs`
table holds:

- **identity:** `job_id` (ULID), `admission_seq`, `nonce` (128-bit random);
- **request:** `tool_id`, `seal_json`, `seal_digest`, `dry_run`, `kind`
  (`task` or `service`), `effects`, `operator`;
- **runner:** `runner_pid`, `runner_created_at`;
- **tool:** `child_pid`, `child_created_at`, `job_object_name`;
- **lifecycle:** `state`, the timestamps, `exit_code`, `outcome`,
  `progress_json`, `stop_requested_at`.

`job_events` is append-only (a trigger refuses `UPDATE` and `DELETE`).

| State | Meaning | Holds admission? |
|---|---|---|
| `admitted` | The server committed the row and spawned a runner. No tool has started. | yes |
| `claimed` | The runner committed its claim (§5.2) and is starting the tool. | yes |
| `running` | The tool is running inside its job object. | yes |
| `succeeded` / `failed` / `stopped` | Exit record verified (§5.2 step 6). `stopped` carries "the library may be partly changed" for writing tools (§5.5). | no |
| `admission_expired` | Never claimed within 60 s, so no tool ever started (fenced). | no |
| `lost` | The runner died. The job object is verified gone, so nothing of the run is alive, but the outcome is unknown. | no |
| `uncertain` | Liveness or containment could not be verified. Kept until the operator resolves it with evidence. Never cleared automatically. | **yes** |
| `refused` | Validation or start failure; nothing was spawned. | no |

A schema can map exit codes to outcomes; for example, the planner's exit 1
is `succeeded_with_holds`.

### 5.2 Admission, claim and containment

The aim is that "one task at a time" and "never an uncontained tool" survive
a crash of the server, the runner, or both, at any point.

1. **Admission (server).** The server takes the OS lock `locks\admission.lock`
   (`msvcrt.locking`, non-blocking with a short retry), then runs
   `BEGIN IMMEDIATE`. Inside the transaction it checks the compatibility
   rules (§5.4) against every row holding admission, and inserts the job as
   `admitted` with a fresh nonce. It commits, releases the lock, and only
   then spawns `python -m comic_automation.web.jobs.runner <job_id> <nonce>`
   (detached: `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW`).
   Two concurrent requests serialise on the lock and the transaction, so the
   second one sees the first row and gets `409 JOB_ACTIVE`.
2. **Claim (runner).** The runner takes `locks\jobs\<job_id>.runner.lock`.
   If it cannot, a duplicate runner already holds it and this one exits.
   Then, under `BEGIN IMMEDIATE`, it runs
   `UPDATE jobs SET state='claimed', runner_pid=?, runner_created_at=? WHERE
   job_id=? AND nonce=? AND state='admitted'`. Unless exactly one row
   changes, it exits without starting anything. That fences runners that
   are late (the row has already expired), duplicated, or stale (wrong
   nonce). **A tool is never started before a committed claim.** So an
   `admitted` row older than the 60 s claim deadline can safely be fenced to
   `admission_expired`, by a transaction conditional on `state='admitted'`.
3. **Containment before any tool code runs.** The runner creates a job
   object named `Local\cbz-console-<job_id>` with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Breakaway is not allowed (no
   `BREAKAWAY_OK` or `SILENT_BREAKAWAY_OK`). The runner keeps the only handle,
   which is not inheritable. It then starts the tool **atomically inside the
   job**, using `CreateProcessW` (through ctypes) with a
   `PROC_THREAD_ATTRIBUTE_JOB_LIST` attribute and a
   `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` that lets the child inherit only the
   pipe's write end and a NUL stdin
   ([Raymond Chen, 2023-02-09](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812/)).
   Python's `subprocess` cannot set `JOB_LIST`, so M3 adds the ctypes call.
   If that call fails, the job fails closed (`refused`, nothing ran). The
   only fallback allowed is the suspended form of the agent-swarm relay's
   `popen_contained` (`C:\swarm-prod\agent-swarm\lead_relay_backends.py`):
   `CREATE_SUSPENDED`, assign, verify with `IsProcessInJob`, then resume.
   That is prior art with one deliberate difference: the relay runs a child
   *uncontained* when assignment fails, and here that case terminates the
   still-suspended child and refuses. In the suspended form the
   create-to-assign gap is recovered by identity. The runner commits
   `child_pid` and `child_created_at` before resuming. On reconciliation, a
   matching process that is not in the job is terminated by handle,
   because it has not executed any tool code yet.
4. **Running.** The runner commits `running` with `child_pid`,
   `child_created_at` and `job_object_name`, conditional on its own claim
   (`runner_pid`, `runner_created_at`, `nonce`). Every later write by the
   runner carries the same condition.
5. **Runner death kills the tree.** The runner holds the only handle to the
   job object, so if the runner dies the kernel closes that handle and
   `KILL_ON_JOB_CLOSE` ends every member. No tool outlives its supervisor,
   and nothing ever kills a PID tree from a snapshot.
6. **Finalisation only after verified descendant termination.** When the
   root process exits, the runner waits for pipe EOF (every holder of the
   write end has gone). It then queries
   `JobObjectBasicAccountingInformation`. If `ActiveProcesses` is above 0
   after a 10 s grace period, it calls `TerminateJobObject` and polls until
   the count is 0. Only then does it write `exit.json` atomically (temp,
   fsync, `os.replace`). That record holds `{job_id, nonce, runner_pid,
   runner_created_at, child_pid, child_created_at, exit_code,
   descendants_verified: true, stop_requested, ended_at}`. The runner also
   commits the terminal state under its claim condition. `exit.json` is the
   durable fallback if that commit fails (for example, a blip on G:).

**Reconciliation.** This runs at server start and every 30 s. It holds the
admission lock and handles jobs in `admission_seq` order:

1. **`exit.json` exists.** It is accepted only if `job_id`, `nonce` and both
   runner fields match the claim; the job is then finalised. A mismatch
   makes the job `uncertain`.
2. **`admitted`, past its deadline.** The job becomes `admission_expired`
   (the conditional update from step 2).
3. **`claimed` or `running`, runner alive** (the pid is live and its
   creation time equals `runner_created_at`). Nothing changes.
4. **Runner dead.** The server opens `Local\cbz-console-<job_id>`:
   - *not found:* the job becomes `lost`; there is no member left to stop;
   - *found:* it should not exist once its only holder has died, so the job
     becomes `uncertain` and stays that way;
   - *access denied or any other error:* `uncertain`.
5. **Pre-assignment gap** (suspended fallback only). This is handled by the
   identity rule in step 3.

An `uncertain` job blocks admission until the operator resolves it. The UI
shows the evidence (pids, creation times, job-object query result), and
resolving it appends an event. Nothing auto-clears it.

### 5.3 Log pump and streaming

The tool's stdout and stderr go to one anonymous pipe. A thread in the
**runner** drains it continuously, so the tool never blocks on a slow disk
and the server being down does not matter.

- **Logical offsets.** Each byte gets a monotonic logical offset for the
  whole job. Segments are named by their starting offset,
  `log\seg-<start:016x>.log`, and each holds at most 8 MiB. The runner
  closes a segment before it opens the next, so no open file is ever
  renamed (this avoids the `WinError 32` codex-lead reproduced). Readers
  open segments read-only with full sharing.
- **Bounded lines.** A partial line is capped at 64 KiB. A longer line is
  split, and the continuation is marked `[line continued]`.
- **Retention.** Service jobs keep 4 segments (32 MiB). Task jobs keep up to
  256 MiB. Beyond that the oldest segment is deleted and a `gap {from, to}`
  record is appended to `log\gaps.jsonl`. If a write fails (disk full, G:
  unavailable), the pump keeps draining, counts the dropped bytes, and
  writes a gap record when writing works again.
- **Cursors.** The SSE event id and the polling cursor are the logical
  offset at the end of each line. On reconnect with `Last-Event-ID`, if the
  offset is older than the oldest retained segment, the server sends a
  `gap` event first and resumes at the oldest retained byte. Offsets never
  alias across rotation.
- **Streaming.** `GET /api/jobs/{id}/stream` (SSE) checks for new data every
  250 ms and sends `line`, `gap`, `state` and `progress` events, plus a
  heartbeat comment every 15 s. Concurrent streams are capped at 8.
  `GET /api/jobs/{id}/log?cursor=&limit=` is the polling fallback and the
  paging API.

`CBZ_PROGRESS n/t p% label` lines are parsed on the server into
`progress_json`. Keyword tagging (error, warn, success) is presentation only,
in the browser.

### 5.4 Compatibility (what may run together)

The console has no shared exclusion protocol with the tools themselves. Its
admission rules are therefore conservative, and they include externally
started watchers (§6):

| Running | May start | Refused with `409 INCOMPATIBLE` |
|---|---|---|
| nothing | anything | |
| watcher (any owner) | read-only tasks: dry runs, the planner (§8.4), reports | maintenance normal run and other writing tasks |
| a writing task | nothing else | another task, watcher start |
| a read-only task | the watcher | another task |

This is stricter than the Tk GUI, which let a normal maintenance run start
beside a live watcher. These rules stay until a backend exclusion and
recovery protocol shared by the watcher and maintenance is proven, which is a
separate issue. The planner already refuses if the library inventory changes
while it runs, so running it beside the watcher can at worst fail cleanly.

### 5.5 Stop

David's decision (Q3, 2026-10-26) sets the **target**: the console may stop a
writing run mid-run by terminating its job object. The job ends `stopped`
with the warning "the library may be partly changed", and the log is kept.
Roadmap §5.9 still requires a proven cancellation contract per tool. So
stopping is enabled **per tool, through an acceptance gate**:

```json
"stop": { "mode": "none", "gate": "M4-maintenance-interrupt",
          "reason": "Stop is enabled once interruption/recovery tests pass for this tool" }
```

- `mode` is `none`, `terminate_job` or `graceful_then_terminate`. If it is
  `none`, the Stop button is disabled and shows `reason`.
- **Read-only runs** may be stopped from day one: dry runs, the planner and
  reports. A stopped planner's partial `--output` file is discarded.
- **A writing tool's gate** works like this. On temporary fixture libraries,
  the tool's process tree is killed at several points (counted output
  lines, and between file operations). The tool is then re-run, and the
  test asserts that the library is recoverable:
  - every archive's original bytes, by SHA-256, are still present in the
    library or its quarantine;
  - no `*.tmp.cbz` or `*.bak.cbz` is left that the tool's next run does not
    clean up;
  - a following dry run reports a consistent state.

  The named tests, and a bypass that makes them fail, are recorded on the
  PR. Only then does the schema switch to `terminate_job`.
- **Watcher:** `graceful_then_terminate`. M1b adds a stop file that the
  watcher polls, and it exits between directories. The terminate fallback
  is enabled only after the watcher's own interrupt gate passes (kill during
  rewrite or move, then restart cleanup via `_clean_up_stale_rewrite_files`
  and readiness sidecars). This gate is part of M6's acceptance.
- **Mechanism.** The server writes `stop.request`. The runner calls
  `TerminateJobObject` and then finalises as in §5.2 step 6. If the runner is
  dead, the kernel has already ended the tree (§5.2 step 5).

## 6. Watcher supervision: never two watchers

There are several ways to start a watcher: the Tk GUI, the CLI, the
`CBZWatcher` scheduled task in `config/CBZWatcher_Task.xml` (not registered on
OfficePC as of 2026-10-26), and now the console. The console is only one of
them, so it cannot be the place that enforces a single instance.

**The M1 contract (codex-lead has offered to implement it, with claude-lead
reviewing):**

1. **The key is physical identity.** Before its startup discovery the watcher
   calls `os.stat` on the watch root and requires a directory with nonzero
   `st_dev` and `st_ino`, the same rule as `_identity` in
   `comic_automation/library/number_style.py`. A failed stat, a zero value,
   or a reparse-point root refuses startup. The key is
   `<st_dev:x>-<st_ino:x>`. A path string is never part of the key, so
   `C:\…`, `\\localhost\C$\…`, a mapped drive or a junction to the same
   directory all give the same key. (codex-lead reproduced the path-hash
   failure.)
2. **The lock namespace is shared across Windows accounts.** The watcher
   creates the named mutex `Global\CBZWatcher-<key>` and waits on it with a
   timeout of 0.
   - *Acquired, or `WAIT_ABANDONED`* (a previous holder died): it owns the
     lock.
   - *Held, or `ACCESS_DENIED`* (another account owns it): it refuses and
     exits with code 3, logging what it can about the owner.

   The kernel releases the mutex when the process dies, so no stale-lock
   takeover is needed. M1's first test measures whether a standard user can
   create a `Global\` mutex on OfficePC (`CLAUDE.md`: measure, don't infer).
   If not, the fallback is an `msvcrt` byte-range lock in
   `G:\ComicAutomation\web-console\locks\watcher-<key>.lock`, with an ACL
   granting the operator accounts modify access. The measurement and the
   choice go on the M1 PR.
3. **Required witnesses.** Each pair is two real processes on a temporary
   watch root:
   - local path vs `\\localhost\C$` UNC;
   - drive letter vs a `subst` or `net use` mapped drive;
   - real path vs a junction (`mklink /J`, which needs no privilege);
   - the same root from two different checkouts.

   In every pair the second process exits with code 3. Two different roots
   do not conflict. A killed holder releases the lock. A zero or failed
   identity refuses startup.

**What the console does.** It probes the same mutex. It also scans processes
(`Get-CimInstance Win32_Process`, cached for 5 s) for command lines with
`scripts.cbz_watcher` or `cbz_watcher.py`, which covers watchers started
before M1. Status is one of:

- `running (console)`;
- `running (external: pid, command line)`;
- `held by an unverified owner`: the mutex is held but no visible process
  matches, as with another account;
- `stopped`;
- `unknown`.

Every state except `stopped` refuses Start (`409 WATCHER_RUNNING`), and
counts as "watcher running" for §5.4. The console stops only its own watcher
job; it never stops an external one (Q4, accepted in review).

## 7. Screens

### 7.1 Dashboard

- **Watcher card.** State, pid, uptime and the effective arguments, shown as
  the exact command. It shows resolver roots and threshold, decensor stages,
  number style and registry (path and short SHA), and readiness-hold root and
  minutes. Buttons: Start (opens the watcher form, §8), Stop, and View live
  log.
- **Job queue.** The running task job with its progress, then recent jobs:
  status, tool, mode (DRY RUN / WRITES CHANGES), target, start time,
  duration, exit code, and the exact command with a Copy button.
- **Needs your decision** (`GET /api/decisions`). The v1 sources are all
  read-only and all have a defined backend:
  - plans awaiting approval (ingested and not approved, or approval revoked);
  - readiness holds: `*.held.json` sidecars under the configured
    `readiness_hold` roots, showing original name, reason and elapsed time,
    as described in `docs/watcher_readiness_holding.md`;
  - `failed` or `lost` jobs not yet acknowledged (acknowledging appends an
    event);
  - a series proposal newer than its decisions file. It is shown as a count
    and links to "Series Review, coming in v1.1".
- **All tools.** The mockup's domain catalog of scripts. Tools that have a
  schema open the runner. The others show "CLI only" with their module name
  and cannot be launched.

### 7.2 Tool runner and job pages

The mockup's `Main.dc.html` layout, generated from the schema. It has a
target field, a run-mode toggle (dry run first), grouped parameters, a run
summary, a **live command preview** (each change calls `POST .../preview`,
debounced by 250 ms), Run and Stop buttons, output with All / Changes /
Warnings / Errors filters, and recent runs of this tool. The Jobs list and the
job detail page reuse the output component and work the same for running and
finished jobs.

### 7.3 Mockup elements deferred

These need a backend source that does not exist yet. They are left out rather
than approximated:

- "Imported today" and "In staging" counters;
- "Classification cases" and "Resolver quarantine" counts;
- perceptual coverage;
- Save as preset and Schedule.

"Pinned" is a preference kept in each browser's `localStorage`. Losing it is
harmless because it is not operational truth.

### 7.4 Settings

The effective configuration (§4.3) with its sources; saved default parameters
for each tool (reset is available); the state directory; the server version
and git SHA; and the operator identity. There is a Quit server button. It
does not stop running jobs, because their runners are detached (§5.2).

## 8. Tool schema format

### 8.1 Format

Schemas are JSON files in `comic_automation/web/tool_schemas/`. The registry
validates them at startup against a fixed meta-schema, written in Python, and
refuses to start if any schema is invalid. Each parameter has a `type`,
validation constraints and one `emit` rule. The argv builder is the only code
that turns parameters into arguments, and it emits a list, never a string.

| `type` | Validation | Emit styles |
|---|---|---|
| `bool` | true/false | `flag` (present when true), `flag_false` (present when false) |
| `choice` | value in `choices` | `option` (`--x v`), `equals` (`--x=v`) |
| `multi_choice` | subset of `choices`; `ordered`, `max`, `unique` | `csv` (`--x=a,b`), `repeat` (`--x a --x b`) |
| `int` / `number` | `min`, `max`, finite | `option` |
| `path` | `role` (§9), `kind` dir/file, `must_exist`, `create_new` | `option`, `positional` |
| `text` | `max_len`, `pattern` (anchored), no control characters | `option` |

Other fields: `when` (emit only when another parameter has a given value),
`default`, `label`, `help`, `group`, and the Tk `OPTION_GUIDANCE` triple
(`description`, `example`, `expected_result`). At the tool level: `id`,
`label`, `domain`, `module`, `subcommand`, `kind` (`task` or `service`),
`effects` (`read_only`, `writes_files` or `service`), `dry_run`
(`{"param": ..., "default": true}` or `"unsupported"`), `stop` (§5.5),
`exit_codes`, and `parser`, the dotted path of the tool's own
`argparse` builder, used for the parity test in §12.

**Preview sealing.** Preview returns a **seal** and its `seal_digest`, the
SHA-256 of the seal's canonical JSON. The seal contains everything that
decides what will run:

- `tool_id` and `tool_schema_sha256`;
- the params, with defaults filled in;
- the **expanded argv**;
- the interpreter's absolute path and `sys.version`;
- the `cwd`;
- the env additions;
- the repository HEAD SHA, plus a dirty flag;
- every `fixed` value with its source. For the number-style registry that
  means the path *and its content SHA-256*, the fingerprint the planner
  records.

A run request echoes `seal_digest`. The server rebuilds the seal from current
state, and if anything differs (for example, the registry bytes changed after
the preview) it returns `409 PREVIEW_STALE` with the fields that differ. The
stored seal goes into `command.json` and the job row, so the job record shows
exactly what ran. This is the tool-level version of roadmap §3.3.

### 8.2 Worked example: maintenance (abridged)

```json
{
  "id": "maintenance", "label": "Library Maintenance", "domain": "workflows",
  "module": "scripts.cbz_workflows", "subcommand": "maintenance",
  "parser": "scripts.cbz_workflows:build_parser",
  "kind": "task", "effects": "writes_files",
  "dry_run": { "param": "dry_run", "default": true },
  "stop": { "mode": "none", "read_only_runs": "terminate_job",
            "gate": "M4-maintenance-interrupt",
            "reason": "Stopping a writing run is enabled after the interrupt/recovery gate (5.5)" },
  "params": [
    { "key": "root", "type": "path", "role": "library", "kind": "dir",
      "must_exist": true, "emit": { "style": "positional" } },
    { "key": "dry_run", "type": "bool", "default": true,
      "emit": { "style": "flag", "flag": "--dry-run" } },
    { "key": "stages", "type": "multi_choice", "ordered": true, "min": 1,
      "choices": ["sanitize", "archive", "organize", "metadata", "names"],
      "default": ["sanitize", "archive", "organize", "metadata", "names"],
      "emit": { "style": "csv", "flag": "--stages" } },
    { "key": "sort", "type": "choice", "group": "sanitizer",
      "choices": ["newest", "oldest", "alpha", "alpha-reverse"], "default": "newest",
      "emit": { "style": "option", "flag": "--sort" } },
    { "key": "full_rescan", "type": "bool", "default": false,
      "emit": { "style": "flag", "flag": "--full" } },
    { "key": "restart", "type": "bool", "default": false,
      "emit": { "style": "flag", "flag": "--restart" } },
    { "key": "rules", "type": "multi_choice", "default": [],
      "choices": ["brackets", "comicinfo", "leading_nums", "non_latin", "normalize_stem",
                  "number_tokens", "scan_groups", "trailing_junk", "translate", "url"],
      "emit": { "style": "csv", "flag": "--rules", "omit_empty": true } },
    { "key": "metadata_dedupe", "type": "bool", "default": true,
      "emit": { "style": "flag_false", "flag": "--no-metadata-dedupe" } },
    { "key": "names_only", "type": "bool", "default": false,
      "emit": { "style": "flag", "flag": "--names-only" } },
    { "key": "workers", "type": "choice", "choices": ["1", "2", "4", "8", "12", "20"],
      "default": "8", "emit": { "style": "option", "flag": "--workers" } }
  ]
}
```

With the defaults and the root `\\tower\media\comics\Comix`, the preview shows
`python.exe -m scripts.cbz_workflows maintenance \\tower\media\comics\Comix
--dry-run --stages=sanitize,archive,organize,metadata,names --sort newest
--workers 8`. The preview uses `subprocess.list2cmdline`, as the Tk GUI does,
and the executed argv is the list itself.

### 8.3 Worked example: watcher (abridged)

```json
{
  "id": "watcher", "label": "CBZ Watcher", "domain": "watcher_routing",
  "module": "scripts.cbz_watcher", "parser": "scripts.cbz_watcher:build_argument_parser",
  "kind": "service", "effects": "service", "dry_run": "unsupported",
  "stop": { "mode": "graceful_then_terminate", "graceful": "stop_file (M1b)",
            "gate": "M6-watcher-interrupt",
            "reason": "Terminate fallback enabled after the watcher interrupt gate (5.5)" },
  "exit_codes": { "3": "already_running" },
  "params": [
    { "key": "series_resolver", "type": "bool", "default": true,
      "emit": { "style": "flag", "flag": "--series-resolver" } },
    { "key": "resolver_comix_root", "type": "path", "role": "library", "kind": "dir",
      "must_exist": true, "default": "Z:\\comics\\Comix",
      "when": { "series_resolver": true },
      "emit": { "style": "option", "flag": "--resolver-comix-root" } },
    { "key": "resolver_manga_root", "...": "as above, Z:\\comics\\Manga" },
    { "key": "resolver_quarantine_root", "type": "path", "role": "quarantine",
      "kind": "dir", "must_exist": true, "when": { "series_resolver": true },
      "emit": { "style": "option", "flag": "--resolver-quarantine-root" } },
    { "key": "resolver_threshold", "type": "choice", "choices": ["0.95", "0.97", "0.99"],
      "default": "0.95", "when": { "series_resolver": true },
      "emit": { "style": "option", "flag": "--resolver-threshold" } },
    { "key": "resolver_refresh", "type": "bool", "default": false,
      "when": { "series_resolver": true },
      "emit": { "style": "flag", "flag": "--resolver-refresh" } },
    { "key": "resolver_normalize_apostrophes", "...": "bool flag, when series_resolver" },
    { "key": "ai_decensor", "type": "bool", "default": false,
      "emit": { "style": "flag", "flag": "--ai-decensor" } },
    { "key": "ai_decensor_models", "type": "multi_choice", "ordered": true, "unique": true,
      "min": 1, "max": 4, "default": ["black_bars"],
      "choices": ["black_bars", "transparent_black", "white_bars", "mosaic"],
      "when": { "ai_decensor": true },
      "emit": { "style": "repeat", "flag": "--ai-decensor-model" } },
    { "key": "ai_decensor_archive_dir", "type": "path", "role": "decensor_archive",
      "kind": "dir", "when": { "ai_decensor": true },
      "emit": { "style": "option", "flag": "--ai-decensor-archive-dir" } },
    { "key": "ai_decensor_reprocess", "...": "bool flag, when ai_decensor" },
    { "key": "readiness_hold_root", "type": "path", "role": "readiness_hold", "kind": "dir",
      "must_exist": true, "optional": true,
      "emit": { "style": "option", "flag": "--readiness-hold-root" } },
    { "key": "readiness_stall_minutes", "type": "number", "min": 0, "max": 1440,
      "default": 30, "emit": { "style": "option", "flag": "--readiness-stall-minutes" } },
    { "key": "number_style", "type": "choice", "choices": ["auto", "issue", "chapter"],
      "default": "auto", "emit": { "style": "option", "flag": "--number-style" } },
    { "key": "number_style_registry", "type": "fixed", "source": "naming.number_style_registry",
      "emit": { "style": "option", "flag": "--number-style-registry" } }
  ]
}
```

`fixed` parameters come from the effective configuration. The browser shows
them but cannot change them, so the watcher and the planner always receive
the same registry file. The browser's four "Camelia stage" selects become one
ordered list, which matches the watcher's own `action="append"` contract and
removes the Tk GUI's "None stops the chain" special case. `--camelia-root`,
`--camelia-python` and `--ai-decensor-backup-dir` keep their environment-driven
defaults and are not exposed, which also matches the Tk GUI.

The watcher's validation of the hold root (exists, writable, same volume,
outside the watch tree, no reparse points) stays in the watcher. If it
refuses, the job fails at startup with the watcher's own message. The console
does not reimplement that check (roadmap §2.1).

### 8.4 `canonical_plan.json`

This schema is read-only and runs `scripts.cbz_canonical_plan`. Its
parameters are `--library-root` (role `library`), `--database` (role
`database`, allowlisted separately), `--quarantine-date` (a date, defaulting
to today), `--number-style`, the fixed registry, `--series-folder-root`,
`--max-held-bytes` and `--temp-dir`. `--output` is always generated by the
server as `<state>/plans/incoming/<job_id>.json`, a new file outside the
library, so the browser never names it. When the job ends with exit 0 or 1,
the server ingests the file (§8.5).

David decided (Q10, 2026-10-26) that the console runs the planner itself, as
well as importing plans produced on the CLI. The planner reads every
archive's bytes, which is expensive over SMB, so it runs as a long read-only
job:

- **Stop.** It may be stopped at any time (`stop.mode: terminate_job`).
  Cancelling is safe because it writes nothing to the library, and a
  partial output file is discarded.
- **Progress.** The planner emits no `CBZ_PROGRESS` lines today, so the
  console shows an indeterminate progress bar and the live log. M8 adds
  `CBZ_PROGRESS` lines to the planner's per-folder loop; that is a small,
  separately reviewed change that does not alter its output or digest.
- **Concurrency.** It is a read-only task under §5.4, so it may run beside
  the watcher.

### 8.5 Plan review and approval

- **Ingest.** The source is either a planner job or `POST /api/plans/import`
  with a path under the `plan_import` role. The server reads the file once,
  re-computes the digest with `canonical_apply.plan.compute_digest` and the
  counts with the executor's own count rules (no second implementation), and
  refuses any mismatch with `422 PLAN_DIGEST_MISMATCH`. It then stores the
  bytes immutably as `plans/<digest>.json` and indexes `contract`,
  `planner_version`, `library_root`, `database_target`, counts, diagnostics
  and `provenance.number_style`.
- **Review screen.** The header shows the digest (full, with a copy button),
  contract and planner version, library root, quarantine root, database
  target, and number-style provenance (style, source, scope, registry path
  and SHA-256, matched root). It also shows `series_folder_root`,
  `accept_comicinfo_drop`, the snapshot fields and the excluded paths with
  reasons. The tabs are:
  - **Renames:** before and after file names relative to the root, with the
    changed tokens highlighted, the action id, archive id and SHA-256, and
    evidence that can be expanded.
  - **Families:** the keeper and its `rename_to`, and each dependant with its
    relation, outcome and `quarantine_to`.
  - **Held for review:** the path or members, with every reason listed.
  - **Unchanged:** includes `unverified_version_label` diagnostics.

  The counts row shows renames, families, quarantines, keeper renames, review,
  and unverified version labels. Lists are paged on the server (200 rows per
  page) and can be filtered by folder and text.
- **Approve.** `POST /api/plans/{digest}/approvals` with
  `{expect_digest, expect_renames, expect_families, note}`. These are the
  three values the executor's `--expect-*` flags require. The server checks
  them against the stored document and appends a row to `plan_approvals`
  (`approval_id`, `plan_digest`, `counts_json`, `decision` approved or
  revoked, `operator`, `session_id`, `decided_at`, `note`,
  `supersedes_approval_id`). Triggers forbid `UPDATE` and `DELETE`. Revoking
  appends a new row. The plan's state is derived from its newest decision.
  An approval names one digest, so a regenerated plan is a new document and
  needs a new approval (roadmap §3.3; `docs/canonical_naming_plan.md`,
  "Consistency and approval").
- **What approval means.** Approval records that the operator reviewed this
  exact document. It is not a freshness claim. The console cannot check
  freshness cheaply, because a plan pins the bytes of the whole library. The
  executor revalidates every action at apply time (roadmap §4.1), and the
  screen says so next to the plan's age.

**Apply stays CLI-only and operator-gated in v1.** The screen shows the exact
`python -m comic_automation.library.canonical_apply.cli apply --library-root …
--database … --plan <state>/plans/<digest>.json --expect-digest …
--expect-renames N --expect-families M --attest-quiescent` command, filled in
from the approval, for the operator to copy. It also offers the approval
record as a JSON receipt to post on the PR or in the hive. The reasons apply
stays out of the browser:

1. The executor requires `--attest-quiescent`, and there is no cross-process
   quiescence mechanism yet. A browser button would turn that attestation
   into one click, while the console cannot prove that the watcher and other
   mutators are stopped.
2. A live apply is step 8. It needs David's approval of the digest, a
   verified backup and the executor's own operational checks
   (`docs/media_aware_naming.md`). Those are human gates, not parameters.
3. Roadmap §2.6 and Phase 5 require authenticated identity and authoritative
   cross-process exclusion before destructive GUI execution. A localhost
   token proves "the person at this desktop", not an operator.

Because the approval record exists, a later Phase 5 PR could add an apply
endpoint without changing the review model.

## 9. Security (roadmap §2.6, applied on localhost)

- **Bind.** `127.0.0.1` only, and the bind host is not configurable. The
  server refuses to start if `console.lock` is held, so only one console runs.
- **Three distinct secrets.** Each is 256 bits from `secrets`, compared in
  constant time, and regenerated every time the server starts.
  1. The *launcher secret* is kept in `session.json`, whose ACL allows only
     the operator account. The launcher sends it in an `X-Launcher-Secret`
     header to `POST /api/bootstrap`. This is the only endpoint exempt from
     the Origin check; the Host check still applies.
  2. That call returns a *bootstrap code*. It is single-use, expires after
     60 s, and grants nothing except the exchange below. The launcher opens
     `http://127.0.0.1:<port>/login?code=…`.
  3. The server consumes the code, creates a *session* with its own random
     id, sets it as a `HttpOnly; SameSite=Strict; Path=/` cookie, and
     redirects (303) to `/`.

  A login URL left in browser history is therefore inert: the code was used
  once and has expired. The redirect clears the address bar but is not
  relied on to erase history. Every route except `/login` and static assets
  requires the session cookie; failures return 401.
- **Redaction.** The access log and `console.log` never record query
  strings on `/login`, any `Cookie`, `X-Launcher-Secret` or `X-CSRF-Token`
  header, or the contents of `session.json`. A test scans the log output for
  each secret's value.
- **Host check.** Every request must have `Host` equal to `127.0.0.1:<port>`
  or `localhost:<port>`. Anything else gets 421. This blocks DNS rebinding.
- **Origin and CSRF.** State-changing methods (POST, PUT, DELETE) must have
  an `Origin` equal to the server's own origin (a missing Origin is refused),
  `Content-Type: application/json` (which rules out form-based CSRF), and an
  `X-CSRF-Token` header matching the per-session token from `/api/session`.
  If `Sec-Fetch-Site` is present, it must be `same-origin`.
- **Headers.** `Content-Security-Policy: default-src 'self'; frame-ancestors
  'none'; base-uri 'none'; form-action 'self'` (no inline scripts, no
  `eval`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  and `Cache-Control: no-store` on every API response.
- **Input.** JSON bodies are capped at 64 KiB. Unknown fields are rejected
  with 422. A body containing a command field (`argv`, `cmd`, `command`) is
  rejected with 422 `COMMAND_NOT_ACCEPTED`. Every parameter is validated
  against its schema.
- **Paths.** A `path` parameter must be absolute after `os.path.normpath`, with
  no `..` segments, NUL bytes, `\\?\` or `\\.\` device prefixes, or
  drive-relative forms (`C:foo`), and no trailing dot or space in any
  component. It must sit under an allowlisted root of its role, checked both
  lexically and on `os.path.realpath`, with `normcase` and `commonpath`. This
  follows the planner's own lexical-and-resolved containment idiom. A
  directory picker (`GET /api/fs/dirs`) lists only directories, only under
  allowlisted roots, one level at a time, and is capped at 500 entries. The
  console never serves file contents from library roots.
- **Output.** The browser renders log lines and plan rows with
  `textContent`, never `innerHTML`.
- **Attribution.** The operator is the Windows account running the server
  (`getpass.getuser()`), recorded with the session id on jobs and approvals.
  codex-lead accepted this for the localhost threat model (Q5).
- **Hosting.** The server runs on OfficePC, beside the tools (David,
  2026-10-26). Future phone access will go through the existing Cloudflare
  tunnel and NPM to OfficePC. That needs a non-loopback bind, so it comes
  only with a separate authentication milestone (real login plus roadmap
  §2.6 authorization). Nothing in v1 listens beyond 127.0.0.1.

## 10. API

All API responses are JSON. Errors have the form `{"error": "CODE", "message":
"…", "details": {…}}` with status 400, 401, 403, 404, 409, 413, 415, 421 or
422, using structured conflict codes as in roadmap §4.1.

| Method and path | Request | Response |
|---|---|---|
| `POST /api/bootstrap` | header `X-Launcher-Secret` | `{code, expires_in: 60}` |
| `GET /login?code=` | | 303 to `/`, sets the session cookie; the code is consumed |
| `GET /api/session` | | `{operator, csrf_token, server_version, git_sha, started_at}` |
| `GET /api/health` | header `X-Launcher-Secret` or the session cookie | `{ok, jobs_running, watcher_state}` |
| `GET /api/config` | | `{values: {key: {value, source, editable, restart_required}}}` |
| `GET /api/tools` | | `[{id, label, domain, effects, kind, dry_run_supported, has_schema}]`, including CLI-only catalog entries |
| `GET /api/tools/{id}` | | `{schema, saved_params, last_job}` |
| `PUT /api/tools/{id}/defaults` | `{params}` | `{saved_params}` (validated first) |
| `POST /api/tools/{id}/preview` | `{params}` | `{seal, seal_digest, display, dry_run, effects, stop, errors: [{key, code, message}]}` |
| `POST /api/tools/{id}/runs` | `{params, seal_digest, confirm_normal_run?}` | 202 `{job_id}`, or 409 `JOB_ACTIVE`, `INCOMPATIBLE`, `JOB_UNCERTAIN`, `PREVIEW_STALE` or `WATCHER_RUNNING` |
| `GET /api/jobs` | `?state&tool&limit&before` | `{jobs: [summary], next}` |
| `GET /api/jobs/{id}` | | `{job, events, progress, display_command}` |
| `GET /api/jobs/{id}/log` | `?cursor&limit` | `{cursor, next_cursor, lines, gaps, eof, state}` (logical offsets, §5.3) |
| `GET /api/jobs/{id}/stream` | `Last-Event-ID` | SSE `line`, `gap`, `state` and `progress` events |
| `POST /api/jobs/{id}/stop` | `{confirm?}` | 202 `{state}`, or 409 `NOT_STOPPABLE` with the gate `reason` |
| `POST /api/jobs/{id}/acknowledge` | `{note?}` | `{acknowledged_at}` |
| `POST /api/jobs/{id}/resolve-uncertain` | `{resolution: lost\|stopped, evidence_note}` | `{state}`; only for `uncertain` jobs, appends an event |
| `GET /api/watcher` | | `{state, owner: console\|external\|unverified\|none, pid, job_id, display_command, identity_key, scan_ok}` |
| `POST /api/watcher/start` | `{params, seal_digest}` | 202 `{job_id}` |
| `POST /api/watcher/stop` | `{confirm: true}` | 202, or 409 `EXTERNAL_WATCHER` |
| `GET /api/decisions` | | `{items: [{kind, id, title, detail, link, since}]}` |
| `GET /api/plans` | | `[{digest, state, counts, library_root, ingested_at, latest_decision}]` |
| `POST /api/plans/import` | `{path}` | `{digest}`, or 422 `PLAN_DIGEST_MISMATCH` or `PATH_NOT_ALLOWED` |
| `GET /api/plans/{digest}` | | `{header, provenance, counts, diagnostics, approvals}` |
| `GET /api/plans/{digest}/{renames\|families\|review\|unchanged}` | `?offset&limit&q&folder` | `{rows, total, next}` |
| `POST /api/plans/{digest}/approvals` | `{expect_digest, expect_renames, expect_families, note}` | 201 `{approval}`, or 409 `APPROVAL_MISMATCH` |
| `POST /api/plans/{digest}/approvals/{id}/revoke` | `{note}` | 201 `{approval}` |
| `GET /api/plans/{digest}/apply-command` | | `{argv, display, requires: ["backup", "quiescence", "David digest approval"]}` |
| `GET /api/fs/roots` | | `{roles: {role: [roots]}}` |
| `GET /api/fs/dirs` | `?role&path` | `{path, dirs: [name], truncated}` |

`/api/watcher/start` is the watcher's runner path, with the single-instance
checks from §6 added. There is no generic job-cancel or lease endpoint
(roadmap §14).

## 11. Frontend

**No build step.** The frontend is static HTML, one CSS file and native ES
modules served from `comic_automation/web/static/`. There is no CDN; the CSP
is `'self'`.

- **Why not htmx.** htmx favours HTML fragments rendered on the server, and
  the stdlib has no autoescaping template engine. Hand-escaping on the server
  is where XSS comes from. Keeping the server JSON-only means every screen is
  an API client (roadmap §2.1), and escaping happens in one place, a small
  `h(tag, attrs, ...children)` helper that only ever sets `textContent` and
  attributes.
- **Why not Alpine.** Its standard build needs `unsafe-eval`, which the CSP
  forbids.
- **Why not an SPA framework.** A framework brings a build chain, and
  `CLAUDE.md` and roadmap §12 defer it until a measured need.

The JavaScript is an estimated 1,000–1,500 lines across `api.js` (fetch
wrapper adding the CSRF header and decoding errors), `form.js` (renders a
schema as a form), `stream.js` (SSE with fallback to polling), and one module
per page.

**Mockup mapping.** CSS custom properties on `:root` take the mockup's tokens:
background `#0e1016`, panels `#12141b`/`#161922`, borders `#232733`, text
`#e6e8ee`/`#9aa1b2`, accent `#ff5c72`, OK `#3fd18f`, warn `#f0a63a`, info
`#5b9cff`, and the effect badge colours (read-only `#12261f`/`#7fe0b5`, writes
files `#3a2410`/`#ffc27a`). Fonts are IBM Plex Sans and Mono, vendored as
WOFF2 with the OFL licence (Q6). The layout follows the mockup: a left nav
(Dashboard, Jobs, All tools, Pinned, Needs review with counts, Domains), a
top bar with tool search and a watcher status pill, and the cards and tables
from `Dashboard.dc.html` and `Main.dc.html`. v1 is dark only; the tokens
leave room for a light theme later.

**Accessibility.**

- Landmarks (`nav`, `main`) and a skip link.
- Native controls: `button`, and `label`/`for`; groups in `fieldset` with
  `legend`; toggles are checkboxes, with `role="switch"` where the mockup
  shows a switch.
- Status is never shown by colour alone. Pills carry text such as "DRY RUN",
  "Running" or "Failed (exit 2)".
- A visible accent `:focus-visible` ring; the whole UI is usable by keyboard,
  including the stage chips and the plan tabs.
- State changes are announced in one polite `aria-live` region. The log
  itself is `aria-live="off"` because it is too chatty, but it is a focusable
  `role="log"` region.
- Respects `prefers-reduced-motion`; works at 200% zoom.
- Text contrast is at least WCAG AA 4.5:1, checked by a test over the token
  pairs (§12).

## 12. Testing

The existing suite runs on `windows-latest` with Python 3.11. The console
adds no runtime dependencies. The one real-browser smoke test adds
Playwright as a dev-only dependency in `.github/requirements-ci.txt`. It
drives the Edge that the runner already has (`channel="msedge"`), so no
browser is downloaded.

- **Unit.**
  - Schema meta-validation: an unknown type, a duplicate key, a `when` that
    points at a missing parameter, or a bad emit rule each refuse startup.
  - The argv builder for every type and emit style, including `when` and
    `omit_empty`.
  - Seal stability. Each sealed field (argv, interpreter, cwd, HEAD, the
    registry's SHA-256) changes the digest, and a changed registry makes a
    run return `PREVIEW_STALE`.
  - Progress-line parsing.
  - Config precedence (default, `console.toml`, environment).
  - The token-pair contrast check.
- **Argparse parity** (the property that matters, after
  `tests/test_gui_tool_commands.py`). For each schema, generated parameter
  sets (defaults, every choice, every bool flipped) produce argv that the
  tool's own parser accepts, and the resulting namespace has the expected
  values. The parser is called directly (`build_argument_parser()`,
  `build_parser()`), never the tool's `main()`. `cbz_canonical_plan` builds
  its parser inside `main`, so M4 extracts a `build_parser()` from it. Until
  the Tk GUI is retired, the maintenance and watcher argv must also equal the
  Tk `_build_command` output for equivalent inputs, ignoring order.
- **API.** An in-process WSGI test client (about 40 lines of stdlib) drives
  every endpoint. One socket-level smoke test starts the real server on
  port 0.
- **Real browser (one test).** Playwright drives the real server on port 0.
  It covers the bootstrap code, the session cookie, a CSRF-protected POST
  (rejected without the header), a cross-origin `fetch` refused by the
  browser, an SSE stream that resumes after `page.reload()`, and a reused
  bootstrap code being refused.
- **Security.**
  - No cookie gives 401. A wrong, reused or expired bootstrap code gives
    401. The launcher secret is not accepted as a session.
  - Secrets never appear in logs (redaction scan).
  - A foreign Host gives 421; a POST with a missing or foreign Origin, 403.
  - A missing or wrong CSRF header gives 403; a form content type, 415; an
    oversized body, 413.
  - Unknown fields and command fields give 422.
  - Path traversal is refused: `..`, a junction pointing outside the root
    (created without privilege, as the existing planner tests do), case and
    UNC variants, `\\?\`, `C:foo`, trailing dots, NUL.
  - A non-loopback host in the config refuses startup.
  - The CSP and other headers are present.
  - A static check fails if any JS module uses `innerHTML`,
    `insertAdjacentHTML` or `eval`.
- **Job lifecycle with fake tools** (`tests/web/fake_tools/`: print lines,
  exit with code N, sleep, spawn a grandchild that starts at once, a
  grandchild that outlives its parent, a tool that requests breakaway,
  ignore termination, flood output, a 1 MiB line, progress lines, bytes that
  are not UTF-8).
  - *Containment:*
    - a grandchild started as the tool's first instruction is inside the
      job;
    - a breakaway request fails;
    - a failed `JOB_LIST` or assignment refuses the job, and nothing runs;
    - the job handle is not inheritable;
    - a job is finalised only once `ActiveProcesses == 0`, so a surviving
      grandchild delays `exit.json` until it is terminated.
  - *Admission and adoption (D3).* The server or the runner is killed at
    each boundary (after admit, before spawn; after spawn, before claim;
    after claim, before tool start; while running; after root exit, before
    `exit.json`; after `exit.json`, before the commit), and reconciliation
    gives the specified state each time. Also tested:
    - a duplicate runner and a late runner after `admission_expired` are
      both fenced;
    - an `exit.json` with a foreign nonce or runner identity makes the job
      `uncertain`;
    - `uncertain` blocks admission until it is resolved;
    - 20 concurrent run requests admit exactly one;
    - "one task at a time" holds across a server restart.
  - *Log pump (D4):*
    - output over the cap produces `gap` events, and the tool never blocks;
    - rotation during a disconnected client, then a reconnect with an old
      `Last-Event-ID`, gives a gap followed by exact resumption, with no
      aliasing;
    - the server is down while the tool writes 50 MiB; the log is complete
      when the server returns;
    - a 1 MiB line is split into bounded parts;
    - a write failure records the dropped-byte count.
  - *Stop:*
    - read-only runs stop and are marked `stopped`;
    - a writing tool with `mode: none` returns `NOT_STOPPABLE` with its
      reason;
    - the compatibility matrix in §5.4 is enforced, including against an
      external watcher.
  - *Per-tool interrupt gates* (§5.5) for maintenance (M4) and the watcher
    (M6). These are acceptance gates, not optional tests.
- **Watcher single instance (M1).** The two-process witnesses listed in §6:
  local vs UNC, mapped drive, junction, and two checkouts. Also: different
  roots, release after a kill, refusal on zero or failed identity, the
  `Global\` creation measurement, and the process scan detecting a fake
  process named like the watcher. Tests use temporary watch roots and never
  run the watcher against real paths.
- **Plans.** Fixtures are built with `canonical_apply.plan.build_plan` in a
  temporary directory.
  - A tampered byte gives `PLAN_DIGEST_MISMATCH`.
  - Mismatched counts give `APPROVAL_MISMATCH`.
  - An approval of digest A is not shown as approving B.
  - The append-only triggers refuse UPDATE and DELETE.
  - The apply command contains exactly the approved triple.
- **Guards proven load-bearing** (`CLAUDE.md`). Each security and validation
  guard is bypassed once and the named tests that should fail are recorded on
  the PR.
- **Manual.** A checklist per frontend PR: keyboard-only pass, 200% zoom,
  screen-reader spot check with NVDA.

## 13. Desktop shortcut and rollout

1. **Coexistence.** A new desktop shortcut, "CBZ Console", targets
   `C:\Python311\pythonw.exe C:\git\ComicAutomation\apps\web_console.py`
   with working directory `C:\git\ComicAutomation`. The operator creates it
   with a PowerShell snippet documented in M11; no code creates shortcuts.
   The launcher reads `session.json`. If a live console answers
   `/api/health`, it opens the browser to the login URL. Otherwise it starts
   the server detached under `pythonw` and then opens the browser. The
   existing "CBZ Automation" Tk shortcut stays as it is. Once M1 is deployed,
   both front ends can safely be pointed at the watcher. The server runs on
   OfficePC (§9, Hosting).
2. **Order of trust.** The console goes read-only first (dashboard, jobs,
   plan review). Then maintenance in dry run, then normal runs, then watcher
   control. Plan approval comes after review has been used on at least one
   real read-only plan.
3. **Retirement criteria.** Every Tk tool either has a schema (M10) or David
   has dropped it explicitly. The console has run the production watcher for
   two weeks with no duplicate-watcher or lost-job incident. David signs off.
4. **Retirement (M12).** The "CBZ Automation" shortcut is repointed to the
   console launcher, keeping its name, so the dependency recorded in layout
   standard rule 8 stays one shortcut. `apps/cbz_gui.py` moves to `archive/`.
   `tests/test_gui_tool_commands.py` is rewritten against the schemas, and
   the docs are updated.

## 14. Milestones (one concern per PR, one file per commit)

| # | PR | Notes |
|---|---|---|
| M0 | This design | Reviewed by codex-lead |
| M1 | Watcher single-instance lock by physical identity (§6) and exit code 3 | codex-lead implements (offered in review) and claude-lead reviews, now that the contract is settled. Watcher-only change. `cbz_watcher.py` has mixed line endings, so edits must be byte-exact. Rollout is deliberate. |
| M1b | Graceful watcher stop file | Watcher-only change |
| M2 | `comic_automation/web` skeleton: config, G: state directory and refusal, WSGI app, security (bootstrap, session, redaction), static shell, launcher | Security tests plus the real-browser smoke test; engineering-decision and `CLAUDE.md` scope updates |
| M3 | Job store, admission and claim, `JOB_LIST` containment, runner log pump, reconciliation, stop mechanism, log and SSE API | D2–D4 tests with fake tools |
| M4 | Schema format, registry, argv builder, seal, path allowlist, compatibility matrix, preview and run API, `maintenance.json` | Argparse and Tk parity tests; extract the `cbz_canonical_plan` parser. **Gate:** maintenance interrupt/recovery tests before its stop is enabled |
| M5 | Tool runner page, Jobs list and job page | Manual accessibility checklist |
| M6 | `watcher.json`, watcher status (mutex probe and process scan), start and stop, dashboard watcher card | **Gate:** the watcher interrupt test before its terminate fallback is enabled |
| M7 | Dashboard, Needs-your-decision queue, Settings | |
| M8 | `canonical_plan.json` (cancellable), planner `CBZ_PROGRESS` lines, plan ingest, read-only review screen | |
| M9 | Append-only approvals, revocation, apply-command and receipt export | |
| M10 | Schemas for the remaining Tk tools: Series Workflow, Camelia Decensor, Gap Checker, Apply Dry-Run Plan, Exclusions Log | Needed before retirement |
| M11 | Shortcut snippet, operator docs, coexistence notes | |
| M12 | Retire the Tk GUI | After the §13 criteria |
| v1.1 | Series Review (proposal, Yes/No/Skip, append-only decisions, `apply-series` through a plan) | Mockup `SeriesReview.dc.html` |

## 15. Decisions recorded and open questions

**Answered** (2026-10-26):

| Q | Answer | By |
|---|---|---|
| Q1 state directory | `G:\ComicAutomation\web-console\`; refuse to start if it is unavailable (§4.2) | David |
| Q2 M1 ownership | codex-lead implements, claude-lead reviews; the key is physical identity (§6) | codex-lead review |
| Q3 stopping a writing run | Allowed mid-run, recorded as `stopped` with "the library may be partly changed"; enabled per tool after its interrupt gate (§5.5) | David; gate from codex-lead review |
| Q4 external watchers | The console does not stop them | accepted in codex-lead review |
| Q5 operator identity | Windows account plus session id, for localhost | accepted in codex-lead review |
| Q8 approval receipts | Export only, with no network calls | accepted in codex-lead review |
| Q9 framework | stdlib WSGI; no objection | codex-lead review |
| Q10 planner in the console | Yes, as a long read-only, cancellable job with progress (§8.4) | David |
| Q11 browser tests | One real-browser smoke test (§12) | codex-lead review |
| Q12 document location | Stays at `docs/web_console_v1_design.md` | codex-lead review |
| Hosting | OfficePC; phone access later via Cloudflare tunnel and NPM, with real auth (§9) | David |

**Still open:**

- **Q6. Fonts.** Vendor IBM Plex as WOFF2 under the OFL (about 300 KB of
  binaries in the repo), or use a system font stack?
- **Q7. Port.** A fixed 8765 (proposed; the launcher finds it through
  `session.json`) or an ephemeral port?
- **Q13. Watch folder.** `WATCH_FOLDER` is hard-coded in the watcher. Should
  it become a CLI flag, so the console can show and set it? That would be a
  watcher change, outside v1.
