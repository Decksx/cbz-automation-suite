# Web console v1 design (#36)

Status: **proposed design, for review.** This document has no implementation
code. It records decisions and open questions for the first web console. The
console replaces the Tk launcher (`apps/cbz_gui.py`) and adds a review screen
for canonical rename plans.

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
   anyway: a session token, Origin and Host checks, CSRF protection, a path
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
| Stop | `proc.terminate()`. Camelia Decensor uses `taskkill /T /F` because its model workers would otherwise survive. Stopping a workflow leaves its `subprocess.run` children running. | Every stop is a process-tree stop through a Job Object (§5.4). |
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
    argv.py         parameters -> argv builder, params digest
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

### 4.2 Runtime state lives outside the repository and the library

Standard rule 1.9 says data never lives in a checkout. The state directory is
`%LOCALAPPDATA%\ComicAutomation\web-console\`, which `CBZ_WEB_CONSOLE_HOME`
can override (see Q1):

```text
console.toml        operator configuration (copied from the example on first run)
console.sqlite3     jobs, job events, saved parameters, plans index, approvals
console.lock        single-instance lock for the console server
session.json        port, token and pid for the launcher (current user only)
jobs/<job_id>/      command.json, output.log[.1..3], exit.json, stop.request
plans/<digest>.json immutable copies of ingested plans
console.log         the server's own log
```

The directory is created with the default per-user ACL. It is never under
`X:\`, `Z:\`, `\\tower\` or `G:\ComicAutomation`. Paths in `console.toml` must
not point to any of those roots either; the server refuses a config that does.

### 4.3 `console.toml`

```toml
[server]
port = 8765                  # bound on 127.0.0.1 only; the host is not configurable

[paths]                      # path parameters must sit under a root of their role
library      = ['\\tower\media\comics', 'Z:\comics']
quarantine   = ['Z:\comics\_resolver_quarantine']
decensor_archive = ['F:\ai-decensor-originals']
readiness_hold = []          # empty: holds stay off (the watcher's own default)
plan_output  = []            # the console's plans/ directory is always allowed
plan_import  = []

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

### 5.1 Jobs

A job is one run of one tool with one set of validated parameters. The
`jobs` table holds: `job_id` (ULID), `tool_id`, `tool_schema_sha256`,
`params_json`, `params_digest`, `argv_json`, `dry_run`, `kind` (`task` or
`service`), `state`, `created_at`, `started_at`, `ended_at`, `exit_code`,
`outcome`, `runner_pid`, `runner_created_at`, `child_pid`, `operator`,
`progress_json` and `stop_requested_at`.

`job_events` is an append-only table: created, started, progress, stop
requested, ended, reconciled. A trigger refuses `UPDATE` and `DELETE` on it,
which follows roadmap §2.5 in spirit.

States: `starting → running → succeeded | failed | stopped | lost`, plus
`refused`, which is a validation or start failure that never spawned anything.
`lost` means the runner vanished without writing `exit.json`, so the outcome is
unknown. That is shown honestly and never guessed. A schema can map exit codes
to outcomes. For example, the planner's exit 1 is `succeeded_with_holds`.

Concurrency in v1 matches the Tk GUI: **one task job at a time**, plus the
watcher service. A second task start returns `409 JOB_ACTIVE`. This is a UX
guard only. It is not the authoritative exclusion; roadmap §2.4 keeps locking
in the backend, such as the canonical-apply scope lock and the watcher's
series locks.

### 5.2 The detached runner (survives browser and server loss)

The server never holds a tool's pipe. For each job it starts
`python -m comic_automation.web.jobs.runner <job_dir>` with
`CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | DETACHED_PROCESS`. The runner:

1. reads `command.json` (argv, cwd = repository root, env additions);
2. creates a Windows **Job Object** with `KILL_ON_JOB_CLOSE`, starts the tool
   with stdout and stderr redirected to `output.log` (a file handle, not a
   pipe, so the tool never blocks or dies on a broken pipe), and assigns it to
   the Job Object so grandchildren (workflow stages, Camelia workers) are
   inside it;
3. waits for the tool, polling for `stop.request` once a second;
4. writes `exit.json` atomically (temporary file, fsync, `os.replace`) with the
   exit code, the end time, and whether a stop was requested.

Closing the browser changes nothing. If the **server** exits, jobs keep
running. On startup the server reconciles every `starting` or `running` job:
it checks whether the runner pid is alive *and* whether its creation time
matches `runner_created_at`, which guards against pid reuse. A job with
`exit.json` is finalized, a job whose runner is alive stays running, and any
other job becomes `lost`. This answers roadmap §2.7: the backend can rebuild
the state, and an unknown outcome is shown as unknown.

Service jobs (the watcher) rotate `output.log` at 10 MB and keep 3 files.
Task logs are not rotated, but are capped at 200 MB; past the cap, writes go
to a truncation notice.

### 5.3 Log streaming

`GET /api/jobs/{id}/stream` is a Server-Sent Events stream. The SSE event id
is the byte offset in `output.log`, so a browser that reconnects with
`Last-Event-ID` resumes exactly where it stopped, and a reload replays from 0
or from the tail. The server tails the file every 250 ms and sends whole
UTF-8 lines (decoded with `errors="replace"`). It also sends `state` events
for job transitions and a heartbeat comment every 15 s. Concurrent streams
are capped at 8. `GET /api/jobs/{id}/log?offset=&limit=` is the polling
fallback and the paging API.

`CBZ_PROGRESS n/t p% label` lines are parsed on the server into
`progress_json`, so the dashboard shows progress without opening a log. The
error/warn/success keyword tagging stays in the browser, as presentation only,
and is never used for state.

### 5.4 Stop

The roadmap says not to offer a generic cancel until a job type has a
proven-safe cancellation contract (roadmap §5.9). Each schema declares one:

```json
"stop": { "mode": "terminate_tree", "confirm": "normal_run",
          "contract": "Tk parity: abrupt tree stop; tool tolerates restart (see note)" }
```

`mode` is `terminate_tree` or `none`. `confirm` is `never`, `normal_run` or
`always`. On stop, the server writes `stop.request`. The runner terminates the
Job Object, which kills the whole tree; this matches what the Tk GUI already
does, now including grandchildren. The job becomes `stopped` with the exit
code recorded. If the runner has died, the server terminates the recorded
child tree directly, after the same pid-plus-creation-time check. A graceful
stop for the watcher (a stop file it polls) would need a watcher change and is
a follow-up, not v1 (Q3).

## 6. Watcher supervision: never two watchers

There are several ways to start a watcher: the Tk GUI, the CLI, the
`CBZWatcher` scheduled task in `config/CBZWatcher_Task.xml` (not registered on
OfficePC as of 2026-10-26), and now the console. The console is only one of
them, so it cannot be the place that enforces a single instance.

1. **Authoritative: a single-instance lock in the watcher (M1).** Before its
   startup discovery, `cbz_watcher` takes a non-blocking `msvcrt.locking`
   byte-range lock, the same idiom as `canonical_apply/scope_lock.py`, on
   `%LOCALAPPDATA%\ComicAutomation\locks\watcher-<h>.lock`. `<h>` is the first
   16 hex digits of the SHA-256 of the normalized real path of the watch
   root. Keying the lock to the watch root rather than the checkout means a
   second checkout or worktree is excluded too. Windows releases the lock
   when the process exits or is killed, so no stale-lock takeover logic is
   needed. If the lock is held, the watcher logs the owner (from an
   informational `.owner.json`: pid, start time, argv, source SHA) and exits
   with code 3. Every launcher, the Tk GUI included, gets this protection
   automatically.
2. **Defence in depth: a process scan.** For watchers started before M1 is
   deployed, the console lists processes whose command line contains
   `scripts.cbz_watcher` or `cbz_watcher.py` (through `Get-CimInstance
   Win32_Process`, cached for 5 s). It compares them with its own job's
   `child_pid`.
3. **Status** is one of: `running (console)`, `running (external: pid, command
   line, lock owner)`, `stopped`, `unknown` (the scan failed). **Start** is
   refused with `409 WATCHER_RUNNING` whenever the lock probe or the scan
   finds a watcher. A probe that takes the lock and releases it at once is
   only a pre-check; the watcher's own lock is the real exclusion, so if two
   starts race, one watcher exits with code 3 and its job ends `failed` with
   reason `already_running`. **Stop** applies only to the console's own
   watcher job. An external watcher is shown with its pid and command line,
   but the console does not stop it in v1 (Q4).

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
does not stop running jobs, because their runners are detached.

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
(`{"param": ..., "default": true}` or `"unsupported"`), `stop` (§5.4),
`exit_codes`, and `parser`, the dotted path of the tool's own
`argparse` builder, used for the parity test in §12.

The **params digest** is the SHA-256 of the canonical JSON of
`{tool_id, tool_schema_sha256, params}` with defaults filled in. Preview
returns it. A run must echo it back, which proves the operator was shown this
exact command. A mismatch returns `409 PREVIEW_STALE`. This is the tool-level
version of roadmap §3.3.

### 8.2 Worked example: maintenance (abridged)

```json
{
  "id": "maintenance", "label": "Library Maintenance", "domain": "workflows",
  "module": "scripts.cbz_workflows", "subcommand": "maintenance",
  "parser": "scripts.cbz_workflows:build_parser",
  "kind": "task", "effects": "writes_files",
  "dry_run": { "param": "dry_run", "default": true },
  "stop": { "mode": "terminate_tree", "confirm": "normal_run",
            "contract": "Tk parity; see Q3" },
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
  "stop": { "mode": "terminate_tree", "confirm": "always",
            "contract": "Tk parity; startup cleans stale rewrite files (_clean_up_stale_rewrite_files)" },
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
the server ingests the file (§8.5). The planner reads every archive's bytes,
which is expensive over SMB, so the form shows that warning (Q10).

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
- **Session token.** At startup the server generates a 256-bit token
  (`secrets.token_urlsafe(32)`) and writes it to `session.json`. The launcher
  opens `http://127.0.0.1:<port>/login?token=…`. The server compares the
  token in constant time, sets a `HttpOnly; SameSite=Strict; Path=/` session
  cookie, and redirects to `/`, which removes the token from the address bar
  and history. The token is regenerated every time the server starts. Every
  route except `/login` and static assets requires the cookie, and failures
  return 401.
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
  That is the v1 identity model (Q5). Real authentication arrives with phone
  access, before any non-loopback bind.

## 10. API

All API responses are JSON. Errors have the form `{"error": "CODE", "message":
"…", "details": {…}}` with status 400, 401, 403, 404, 409, 413, 415, 421 or
422, using structured conflict codes as in roadmap §4.1.

| Method and path | Request | Response |
|---|---|---|
| `GET /login?token=` | | 303 to `/`, sets the cookie |
| `GET /api/session` | | `{operator, csrf_token, server_version, git_sha, started_at}` |
| `GET /api/health` | | `{ok, jobs_running, watcher_state}` (the launcher uses it with the cookie or token) |
| `GET /api/config` | | `{values: {key: {value, source, editable, restart_required}}}` |
| `GET /api/tools` | | `[{id, label, domain, effects, kind, dry_run_supported, has_schema}]`, including CLI-only catalog entries |
| `GET /api/tools/{id}` | | `{schema, saved_params, last_job}` |
| `PUT /api/tools/{id}/defaults` | `{params}` | `{saved_params}` (validated first) |
| `POST /api/tools/{id}/preview` | `{params}` | `{argv, display, params_digest, dry_run, effects, errors: [{key, code, message}]}` |
| `POST /api/tools/{id}/runs` | `{params, params_digest, confirm_normal_run?}` | 202 `{job_id}`, or 409 `JOB_ACTIVE`, `PREVIEW_STALE` or `WATCHER_RUNNING` |
| `GET /api/jobs` | `?state&tool&limit&before` | `{jobs: [summary], next}` |
| `GET /api/jobs/{id}` | | `{job, events, progress, display_command}` |
| `GET /api/jobs/{id}/log` | `?offset&limit` | `{offset, next_offset, lines, eof, state}` |
| `GET /api/jobs/{id}/stream` | `Last-Event-ID` | SSE `line`, `state` and `progress` events |
| `POST /api/jobs/{id}/stop` | `{confirm?}` | 202 `{state}`, or 409 `NOT_STOPPABLE` |
| `POST /api/jobs/{id}/acknowledge` | `{note?}` | `{acknowledged_at}` |
| `GET /api/watcher` | | `{state, owner: console\|external\|none, pid, job_id, argv, display_command, lock_owner, scan_ok}` |
| `POST /api/watcher/start` | `{params, params_digest}` | 202 `{job_id}` |
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
adds no dependencies, so CI does not change.

- **Unit.**
  - Schema meta-validation: an unknown type, a duplicate key, a `when` that
    points at a missing parameter, or a bad emit rule each refuse startup.
  - The argv builder for every type and emit style, including `when` and
    `omit_empty`.
  - Params-digest stability.
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
- **Security.**
  - No cookie gives 401; a wrong token, 401.
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
  exit with code N, sleep, spawn a grandchild, ignore termination, emit
  progress lines, write bytes that are not UTF-8):
  - success, failure and exit-code mapping;
  - stop kills the grandchild;
  - the runner keeps running after the server object is dropped;
  - reconciliation on restart finalizes, keeps running, or marks `lost`;
  - a runner pid reused by another process (creation time differs) is
    detected;
  - log offsets resume exactly across reconnects;
  - service log rotation;
  - one task job at a time.
- **Watcher single instance (M1).** Two processes on the same watch root: the
  second exits with code 3 and names the owner. Different roots do not
  conflict. The lock is released after a kill. The process scan detects a
  fake process named like the watcher. Tests use temporary watch roots and
  never import the watcher's `main` against real paths.
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
  screen-reader spot check with NVDA. Browser automation is deferred (Q11).

## 13. Desktop shortcut and rollout

1. **Coexistence.** A new desktop shortcut, "CBZ Console", targets
   `C:\Python311\pythonw.exe C:\git\ComicAutomation\apps\web_console.py`
   with working directory `C:\git\ComicAutomation`. The operator creates it
   with a PowerShell snippet documented in M11; no code creates shortcuts.
   The launcher reads `session.json`. If a live console answers
   `/api/health`, it opens the browser to the login URL. Otherwise it starts
   the server detached under `pythonw` and then opens the browser. The
   existing "CBZ Automation" Tk shortcut stays as it is. Once M1 is deployed,
   both front ends can safely be pointed at the watcher.
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
| M1 | Watcher single-instance lock and exit code 3 | Watcher-only change. `cbz_watcher.py` has mixed line endings, so edits must be byte-exact. Rollout is deliberate. |
| M2 | `comic_automation/web` skeleton: config, state directory, WSGI app, security, `/login`, `/api/session`, `/api/health`, static shell, launcher | Security tests; engineering-decision and `CLAUDE.md` scope updates |
| M3 | Job store, detached runner, Job Object, reconciliation, stop, log and SSE API | Fake-tool lifecycle tests |
| M4 | Schema format, registry, argv builder, path allowlist, preview and run API, `maintenance.json` | Argparse and Tk parity tests; extract the `cbz_canonical_plan` parser |
| M5 | Tool runner page, Jobs list and job page | Manual accessibility checklist |
| M6 | `watcher.json`, watcher status (lock probe and process scan), start and stop, dashboard watcher card | |
| M7 | Dashboard, Needs-your-decision queue, Settings | |
| M8 | `canonical_plan.json`, plan ingest, read-only review screen | |
| M9 | Append-only approvals, revocation, apply-command and receipt export | |
| M10 | Schemas for the remaining Tk tools: Series Workflow, Camelia Decensor, Gap Checker, Apply Dry-Run Plan, Exclusions Log | Needed before retirement |
| M11 | Shortcut snippet, operator docs, coexistence notes | |
| M12 | Retire the Tk GUI | After the §13 criteria |
| v1.1 | Series Review (proposal, Yes/No/Skip, append-only decisions, `apply-series` through a plan) | Mockup `SeriesReview.dc.html` |

## 15. Open questions

- **Q1. State directory.** `%LOCALAPPDATA%\ComicAutomation\web-console`
  (proposed) or `G:\ComicAutomation\web-console`, which would sit with the
  other operational data under layout rule 1.9 but on the production drive?
- **Q2. Watcher lock ownership.** M1 changes the watcher. Who implements it,
  who reviews it, and is keying the lock to the watch root's real path
  (§6, item 1) acceptable?
- **Q3. Stopping a maintenance normal run.** The proposal is Tk parity (an
  abrupt tree stop after a confirmation). The alternative is to forbid it
  until each stage has a written cancellation contract (roadmap §5.9). The
  same question applies to a graceful watcher stop file.
- **Q4. External watchers.** Should the console be able to stop a watcher it
  did not start? The proposal is no, in v1.
- **Q5. Operator identity.** Is the Windows account plus a session id enough
  attribution for localhost approvals until real authentication arrives?
- **Q6. Fonts.** Vendor IBM Plex as WOFF2 under the OFL (about 300 KB of
  binaries in the repo), or use a system font stack?
- **Q7. Port.** A fixed 8765 (proposed; the launcher finds it through
  `session.json`) or an ephemeral port?
- **Q8. Plan approval and the hive.** Should an approval also be posted
  somewhere outside the console (a PR comment, the hive)? The proposal is to
  export only, with no network calls.
- **Q9. Framework.** Is the stdlib choice (§3) acceptable to the reviewer,
  given that roadmap §12 names FastAPI as its preference?
- **Q10. Running the planner from the console.** Should v1 run the planner,
  which reads every archive in full and is expensive over SMB, or only
  import plan files the operator produces from the CLI?
- **Q11. Browser tests.** Is a manual checklist enough for v1, or should a
  Playwright smoke test (a new dev dependency) be added?
- **Q12. Location of this document.** `docs/web_console_v1_design.md` as
  requested, or the layout standard's `docs/design/web_console_v1.md`?
- **Q13. Watch folder.** `WATCH_FOLDER` is hard-coded in the watcher. Should
  it become a CLI flag, so the console can show and set it? That would be a
  watcher change, outside v1.
