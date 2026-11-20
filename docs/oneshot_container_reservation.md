# Reserving the oneshots container (#214, slice 2)

`_oneshots` is a shared book container, not an ordinary series directory. This
step protects that boundary while the v2 registry and relocation integrations
are still being built. It does not import, relocate or promote any book.

## Series lookup and cached data

`is_reserved_series_name(name)` compares a Windows-safe, casefolded series name
with `_oneshots`. The watcher checks this before routing, including when resolver
mode is disabled. A reserved query or registration raises `ValueError` instead
of becoming a suggested series directory or a new alias target. Titles such as
`Oneshots` and `_oneshots anthology` remain distinct permitted series names.

`build_registry` skips the container while preserving ordinary series folders.
Alias seeds cannot make the excluded container a canonical target.

`load_registry` removes old container series entries and their alias targets
from its returned v1 snapshot. It also removes container candidates from old
ambiguities and updates the counts. This cleanup is in memory: a read never
rewrites the operator's cache file or the durable locations file. A subsequent
normal registration uses the cleaned snapshot instead of republishing the old
container entry. Surviving ambiguity remains conservative until a rebuild.

`resolve_from_registry` also protects direct callers that bypass the loader:
reserved exact targets refuse, ambiguity candidates are filtered, and reserved
targets cannot win a fuzzy match. It does not mutate the supplied registry.

This stage supports v1 registries, including old snapshots without an explicit
version field. A different version refuses before cleanup or lookup. In
particular, a valid future v2 oneshot location must not be mistaken for a bad v1
container entry and silently removed. The next v2 integration must replace that
guard together with the proper location-aware reader and writer protocol.

## Repair and inventory are different operations

`normalize_apostrophe_names` skips the container and its contents. A direct call
with a root inside the container also refuses. The old repair function has no
Camelia relocation or location-record update, so renaming those books would
detach the durable identity being introduced by #214. Ordinary series repair
keeps its existing behavior. Container changes require the shared, journalled
relocation protocol instead.

This is not a generic directory exclusion. The common file inventory continues
to include books under `_oneshots`, and no Camelia crawl exclusion is added.
The change does not modify `_series_locations.json` or activate v2 registry
writes, first-book routing or second-book promotion.

The watcher is a mixed-line-ending file. Its import and route guard are spliced
with the existing local endings, and reversing those additions reproduces the
original bytes. No existing line endings are normalized.
