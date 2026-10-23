# Series ambiguity report

`scripts/cbz_series_ambiguity_report.py` observes the configured SeriesIndex
directory population. It reads routing JSON and directory metadata, and emits
JSON to stdout. It does not read archives, open the operational database,
create report files, change routing, or move payloads.

## Run against an explicit scope

For a temporary test config whose index destinations are `comix` and `manga`:

```powershell
C:\Python311\python.exe -m scripts.cbz_series_ambiguity_report --routing C:\Temp\routing.test.json --scope comix manga
```

The routing config must use the same absolute destination paths required by
`cbz_routing.parse`. There is no default config or implicit scan target.
`--scope` must confirm every configured index destination exactly once.
Unknown, missing, extra or duplicate confirmations refuse before any root is
enumerated. Duplicate configured index destination labels also refuse. If
`series_index.destinations` is empty/omitted, the expected scope is every key
in `destinations`, matching SeriesIndex's existing fallback.

Confirmation order does not change destination priority. Priority and report
root order come from the routing config. This command cannot report a selected
subset as a clean measurement of the full configured index.

The command can also run directly from another working directory:

```powershell
C:\Python311\python.exe C:\git\ComicAutomation\scripts\cbz_series_ambiguity_report.py --routing C:\Temp\routing.test.json --scope comix manga
```

Use a separately authorized operator run for live library paths. The feature's
development and verification use temporary fixtures only.

## Exit codes and interpretation

| Exit | Meaning |
| --- | --- |
| 0 | JSON produced; the observed index has no collisions, empty keys or scan problems |
| 1 | JSON produced; unresolved diagnostics block the zero-ambiguity observation |
| 2 | Invalid/missing config or scope; no JSON report produced, reason on stderr |

A disabled index is blocked and does not enumerate any roots. Missing,
non-directory or unreadable roots have `status: failed`; their error is retained
and the diagnostics cannot be clear. Other roots can still contribute evidence.
A failed root's partial enumeration is discarded. `not_scanned` is distinct
from a successfully enumerated empty root.

The index policy remains in `cbz_routing`: all lexical paths contributing to a
canonical key, narrow Syncthing exclusions, empty-key reporting, configured
destination priority and lexical alias deduplication. Ordinary files under a
root are outside the series-directory population. Dot titles and UUID-looking
titles remain candidates.

## JSON and fingerprint contract (schema version 1)

- `config` records the absolute lexical config path and the SHA256 of the exact
  config bytes read, including formatting. It is read once.
- `priority` records configured index destination precedence.
- `scope` records each destination label, root spelling, enumeration status,
  directory paths supplied to SeriesIndex, and any enumeration error. Identical
  configured root spellings are enumerated once and reused for alias labels.
- `diagnostics` is the existing structured SeriesIndex result, with failed-root
  errors included in its scan problems. It retains all collision contributors,
  empty-key paths and excluded control paths.
- `counts.directory_observations` counts the directory entries under each
  destination label, including controls and empty keys. Alias labels can count
  one lexical path more than once. `canonical_keys` counts indexable keys;
  collision, empty-key and excluded-path counts come from the index's deduplicated
  diagnostics. These are different populations.
- `report_sha256` hashes the complete report **without** that field: JSON with
  keys sorted, `separators=(",", ":")`, `ensure_ascii=True`, `allow_nan=False`,
  encoded as UTF-8. The displayed indentation is not part of the hash.

There is no run timestamp in the hashed report. Unchanged inputs and observations
produce the same fingerprint, regardless of filesystem enumeration order or
scope-confirmation order. A changed non-colliding directory inventory changes
the fingerprint too. Archive content changes alone do not: no archive bytes
are read. The hash identifies this report, not an authenticated approval.
Revisions to the report semantics require revisiting its schema contract.

## Limits

This is an observation across roots, not an atomic filesystem snapshot or a
freshness guarantee. Directories can change during or after enumeration. It
does not resolve symlinks, junctions or SMB aliases into physical identity.
Host lexical path rules and the existing canonical series normalization apply.

A clear report evaluates only the zero-ambiguity observation. It does not
authenticate human decisions/exception manifests, establish correct placement,
validate every watcher startup invariant, or authorize index authority or
routing activation. The watcher remains off/shadow. Preserve a run's scope,
config digest and full report when using it as evidence for a later decision.

