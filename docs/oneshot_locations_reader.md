# Durable oneshot locations reader (#214, slice 2 foundation)

`comic_automation.library.oneshot_locations` reads the version-1
`_series_locations.json` defined by the merged
[shared contract](comix_oneshots_contract.md). This foundation does not enable
watcher routing, promotion, registry v2 or a relocation writer. Those integrations
require the reviewed shared relocation and Camelia APIs.

## Interfaces

- `parse_locations(document)` validates JSON text without filesystem access and
  returns an immutable `LocationsSnapshot`.
- `load_locations(root)` reads one published locations file under an already-bound
  library root. It returns the same snapshot type, creates no files and does not
  inspect or move any books.
- `LocationsError` means the identity source cannot be trusted. A caller must stop
  or defer the operation and report the error; it must not replace the file with
  an empty registry or infer identity from filenames.

The snapshot exposes `generation` and a tuple of `OneshotLocation` records sorted
by NFC/casefolded canonical title. Each record retains the canonical title,
relative path, original folder (or `None`), aliases, size, mtime, SHA-256 and last
operation ID. The tuple, records and alias tuples are immutable. The digest is
canonicalized to lowercase; it remains evidence from the recorded relocation,
not a claim that the current book was hashed by this reader.

Absence of the locations file in an available library is generation zero with
no records. A missing library root, unreadable or invalid file, directory or
reparse-point control file is an error. Replacement between the control-file
inspection and open is also an error so the caller can retry from a fresh read.

## Identity and schema rules

- Version 1 has exactly `version`, `generation` and `oneshots`. Each record has
  exactly the fields listed in the contract. Missing, extra or duplicate JSON
  members refuse; new schema fields need an explicit version/interface decision.
- Versions and nonnegative counters are integers, never booleans or coerced text.
- Canonical names are safe single Windows components. `_oneshots` cannot be a
  canonical series or an original series folder.
- Paths have exactly `_oneshots/<one filename>`, with portable forward slashes.
  Absolute, nested, traversal, device, alternate-stream and trailing-dot/space
  names refuse. File extensions are retained; format classification belongs to
  the planner, not this reader.
- Canonical titles and targets are unique under NFC/casefold comparison. Two
  records cannot silently replace the same identity or claim the same book.
- Alias overlap across distinct series is retained for the resolver to report
  as ambiguity. This reader does not choose an alias winner.
- Missing books remain in the snapshot. Staleness belongs in the resolver's
  result; a rebuild must not delete their durable identity records.

## Concurrency boundary

The caller must bind and validate its library root before calling the loader.
Writers must publish a complete file by atomic replacement under the contract's
global metadata lock and generation rules. Opening one file generation does not
establish consistency with a separate registry or database snapshot. Integrating
callers must perform the contract's generation checks and cache invalidation.
Before acting on a stored book path, re-bind it under the writer gate and the
relocation/fence protocol; schema validation alone does not authorize file I/O.

Tests use private directories. They cover valid and stale records, retained alias
ambiguity, unsafe/colliding paths, invalid schema and evidence, duplicate JSON,
unreadable data, reparse attributes and a real replacement before open. The
reparse-attribute unit fixture does not claim a native symlink privilege test.
