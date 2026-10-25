"""Canonical-naming apply, recovery and undo (design step 7, §5.3-§5.4).

Tracks #111/#112. `docs/canonical_naming_and_version_supersession.md`
§5.1-§5.4 is the contract; its "Step 7 implementation notes" record every
choice the design left open.

Modules:

* `plan` -- the approved plan document, its digest and counts, and the
  step chains derived from it;
* `scope_lock` -- the OS-held, non-blocking scope lock per library root;
* `journal` -- the append-only, fsynced attempt journal;
* `markers` -- the ``rename_attempt_steps`` marker table and the one
  transaction that commits path updates together with the marker;
* `executor` -- the step protocol, family-unit phases, frontier recovery
  and undo;
* `cli` -- the operator entry point (no default paths).

Step 7 runs against tmp fixtures only; nothing here is pointed at a live
library or database.
"""
