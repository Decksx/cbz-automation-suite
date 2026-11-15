# Configuration evidence for the X: migration (#108)

`sync_config.bind_configuration(report, capture, expected=None)` binds a
sanitized description of supplied Syncthing exports to an existing filesystem
observation, preview or verification. It has no HTTP client and reads no library
files. The separate CLI reads named JSON files and prints one JSON artifact.
Neither API configures Syncthing, transfers data, or authorizes a cutover.

The original filesystem report remains intact under `filesystem_report`.
`scope_bound` means the supplied configuration and declared namespace mappings
fit the supported comparison policy. `complete` additionally requires the
original comparison to be complete/successful. `scoped_verified` applies only
to a successful fresh verification with matching configuration-bound baseline
evidence. **`sync_ready` is always false.**

## Capture input

Keep captures and reports outside Git and the compared roots. Input contract
`sync-config-capture-v1` has two fields:

- `mappings`: one row per selected folder ID, containing `folder_id`, `tower`
  and `target`. Each side has `syncthing_path` (the absolute path in that
  Syncthing instance's namespace) and `observed_path` (the exact absolute
  Windows root spelling from the filesystem report). This explicitly handles
  a container path such as `/media/Manga` versus a Windows UNC observation.
  The tool does not prove that a container mount maps to that share.
- `nodes`: exactly `tower` and `target`. Each has `before` and `after` snapshots.
  Each snapshot contains `status` (GET `/rest/system/status`), `config` (GET
  `/rest/config`) and `ignores` (folder ID to the complete GET
  `/rest/db/ignores?folder=...` response, including both `ignore` and `expanded`).

For example, the mapping portion might be:

```json
{
  "folder_id": "manga",
  "tower": {"syncthing_path": "/media/Manga", "observed_path": "\\\\Tower\\media\\Manga"},
  "target": {"syncthing_path": "X:\\Manga", "observed_path": "X:\\Manga"}
}
```

The caller assembles saved read-only exports at two boundaries. This release
does not collect them from live services. Only allowlisted selected-folder
paths, identities, roles, paused states, peer IDs/states, supported numeric
versioning parameters and ignore digests enter the output. GUI credentials,
API keys, encryption passwords, external commands and unrelated-folder paths
are never copied. Unselected folder IDs are listed as unmanaged. Refusal text
does not echo parser values or raw exports. Fingerprints cover sanitized scope;
credential rotation in unrelated fields does not alter them.

Before/after sanitized evidence must agree. Two matching snapshots are
non-atomic observations, not proof of present configuration, authenticated
capture, or an authoritative/current Syncthing index. Raw artifacts and unkeyed
digests are not signatures.

## Supported scope and fallback

The bounded ignore interpreter accepts only positive expanded globs exactly
representing the observer's current literal exclusions. For an unanchored
component `name`, the required effective forms are `name`, `name/**`,
`**/name`, `**/name/**`. A declared root-relative prefix requires `prefix`
and `prefix/**`; a leading `/` in the exported rooted form is also accepted.
On POSIX each pattern must have `(?i)`; Windows matching may be implicit.
`required_expanded_patterns(observation)` returns the component/temp forms;
add the prefix forms for each root's `exclude_prefixes`.

All observer control/history/protected-original names must be explicitly
represented, even where Syncthing reserves a name internally. The observer's
temporary-name rule also needs the literal `.syncthing.tmp` overlap case.
Expanded negations, deletion modifiers, general wildcards beyond the exact
required forms, missing rules and additional ignored areas are unsupported.
Includes are not read by this tool: only their supplied effective expansion can
be assessed. A missing/error expansion cannot establish scope.

Relative observed names must be ASCII for scope binding. Syncthing's Unicode
lowercase/glob rules are not assumed equivalent to the observer's NFC/casefold
rules. Counted files under `.syncthing.` or `~syncthing~` temporary prefixes
also prevent binding: Syncthing reserves either prefix even without `.tmp`,
whereas the observer has a narrower rule. The filesystem evidence is retained
in these cases, with `scope="filesystem-only"` and explanatory problems.

Only basic filesystems, the default marker, plaintext folder modes and the two
declared devices are supported. Simple/staggered/trashcan or disabled versioning
is described, with numeric parameters and default history placement. External
versioning, custom history paths, encrypted sharing and overlapping selected
remote roots are unsupported. Unknown or malformed structure is refused.

Roles, paused state and receiver versioning remain visible facts. The intended
future direction is Tower send-only to X receive-only; this tool neither fixes
the reported old direction nor treats a role flip as proof of index authority.
It establishes no backup, capacity, peer convergence or operational readiness.

## Artifact workflow

First collect filesystem evidence using the existing read-only CLI, then bind
its supplied configuration capture:

```powershell
python -m comic_automation.library.sync_config_cli --report preview.json --capture capture.json > preview.config.json
python -m comic_automation.library.sync_config_cli --report baseline.json --capture capture.json > baseline.config.json
```

Run the existing fresh verifier against `baseline.json`, then bind a newly
supplied capture and the original configuration-bound baseline:

```powershell
python -m comic_automation.library.sync_config_cli --report post.filesystem.json --capture post.capture.json --expected baseline.config.json > post.config.json
```

The expected artifact must bind the very same observation used by the fresh
verifier. Missing baseline configuration or changed folder/path/identity/peer/
ignore scope prevents scoped verification. Roles, pause and versioning changes
between baseline and post-sync capture are separately reported by
`state_changed_since_baseline`; they do not erase file differences or approve
operation. Within each capture boundary, any described state change fails closed.

Exit 0 means complete comparison with supported supplied evidence; exit 2 means
refusal, fallback or differences; exit 1 means output failure. Pin the observer,
binding code and exclusion policy versions. Old artifacts stay unchanged;
collect fresh evidence after a policy change.

Official references checked 2026-11-15:
[config endpoints](https://docs.syncthing.net/rest/config.html),
[status](https://docs.syncthing.net/rest/system-status-get.html),
[effective ignores](https://docs.syncthing.net/rest/db-ignores-get.html),
[ignore semantics](https://docs.syncthing.net/users/ignoring.html),
[versioning](https://docs.syncthing.net/users/versioning.html), and the primary
[ignore implementation](https://github.com/syncthing/syncthing/blob/main/lib/ignore/ignore.go)
and [temporary names](https://github.com/syncthing/syncthing/blob/main/lib/fs/tempname.go).
