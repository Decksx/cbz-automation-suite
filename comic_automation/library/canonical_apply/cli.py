"""Operator CLI for canonical-naming ``apply`` / ``recover`` / ``undo`` (§5.2-§5.4).

Nothing runs at import, and no path has a default: ``--library-root``,
``--database`` and ``--plan`` are always explicit, so this CLI can never
reach a live library or database by omission. Step 7 is exercised against
tmp fixtures only; a live run is step 8 and needs David's approval of a
digest.

Every subcommand that acts on a plan requires the approval triple
``--expect-digest``, ``--expect-renames`` and ``--expect-families`` and
refuses any mismatch before the scope lock is taken.

Quiescence (#158 B3, ``docs/library_writer_coordination.md``):

* Without ``--writer-roots`` nothing changes: the operator must attest
  that the watcher and every other mutator are stopped
  (``--attest-quiescent``), or every subcommand refuses.
* With ``--writer-roots`` the run also holds the registered root's writer
  gate exclusively, so participating writers defer, and a busy gate
  refuses at once. Until that root is ``enforced`` in the registry,
  ``--attest-quiescent`` is **still required**: the gate cannot exclude a
  writer that does not take it.
* On an ``enforced`` root the gate covers participating writers, and the
  operator attests only the non-participants (Syncthing, Komga, Explorer,
  Tower-local processes) with ``--attest-no-external-writers``.
  ``--attest-quiescent`` is also accepted there.

Crash injection (`executor.FAULT_POINTS`) is not reachable from here.

Exit codes::

    0  completed, or recovery found nothing it had to refuse
    1  refused (plan mismatch, lock held, not quiesced, precondition)
    2  stopped with work left: a unit blocked, an action in flight, or a
       recovery refusal that needs the operator
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from comic_automation.library.canonical_apply import executor
from comic_automation.library.canonical_apply.journal import JournalError
from comic_automation.library.canonical_apply.markers import MarkerError, install_schema
from comic_automation.library.canonical_apply.plan import PlanError, load_plan
from comic_automation.library.canonical_apply.scope_lock import ScopeLockError
from comic_automation.library import writer_gate as wg


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply, recover or undo an approved canonical-naming plan."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--library-root", required=True, type=Path)
        sub.add_argument("--database", required=True, type=Path)
        sub.add_argument("--plan", required=True, type=Path)
        sub.add_argument("--expect-digest", required=True)
        sub.add_argument("--expect-renames", required=True, type=int)
        sub.add_argument("--expect-families", required=True, type=int)
        sub.add_argument(
            "--attest-quiescent",
            action="store_true",
            help="the operator attests the watcher and every other mutator "
            "are stopped for this library root",
        )
        sub.add_argument(
            "--writer-roots", type=Path,
            help="library-writer-roots-v1 registry: hold the enclosing root's writer gate "
            "exclusively for the run",
        )
        sub.add_argument(
            "--attest-no-external-writers",
            action="store_true",
            help="on an enforced registered root: the operator attests that no writer "
            "outside the gate protocol (Syncthing, Komga, Explorer, Tower) is writing",
        )

    apply_cmd = commands.add_parser("apply", help="apply an approved plan")
    common(apply_cmd)
    apply_cmd.add_argument("--resume", metavar="ATTEMPT", help="resume this apply attempt")

    recover_cmd = commands.add_parser("recover", help="recover an attempt's frontier")
    common(recover_cmd)
    recover_cmd.add_argument("--attempt", required=True)

    undo_cmd = commands.add_parser("undo", help="undo confirmed actions of an apply attempt")
    common(undo_cmd)
    undo_cmd.add_argument("--forward-attempt", required=True)
    undo_cmd.add_argument("--action", action="append", dest="actions", metavar="ACTION_ID")
    undo_cmd.add_argument("--resume", metavar="ATTEMPT", help="resume this undo attempt")

    schema_cmd = commands.add_parser(
        "install-schema", help="create the rename_attempt_steps marker table"
    )
    schema_cmd.add_argument("--database", required=True, type=Path)
    return parser


def _attested(args: argparse.Namespace, gate: "wg.CoordinationRoot | None") -> executor.Quiescence:
    def probe(root: Path) -> str | None:
        if args.attest_quiescent:
            return None
        if gate is not None and gate.enforced:
            if args.attest_no_external_writers:
                return None
            return ("the root is enforced, so the gate excludes participating writers, but the "
                    "operator has not attested non-participants (--attest-no-external-writers)")
        # Not enforced (or no registry): an old watcher or Camelia may never take
        # the gate, so holding it proves nothing about them.
        return "the operator has not attested quiescence (--attest-quiescent)"

    return probe


def _gate(args: argparse.Namespace) -> "wg.CoordinationRoot | None":
    if args.writer_roots is None:
        return None
    root, _ = wg.coordination_root(wg.load_registry(args.writer_roots), args.library_root)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install-schema":
            install_schema(args.database)
            print(json.dumps({"installed": str(args.database)}))
            return 0
        plan = load_plan(
            args.plan,
            library_root=args.library_root,
            expect_digest=args.expect_digest,
            expect_renames=args.expect_renames,
            expect_families=args.expect_families,
        )
        gate = _gate(args)
        quiescence = _attested(args, gate)
        if args.command == "apply":
            report = executor.apply(
                plan,
                library_root=args.library_root,
                database=args.database,
                quiescence=quiescence,
                resume_attempt=args.resume,
                writer_gate=gate,
            )
        elif args.command == "recover":
            report = executor.recover(
                plan,
                library_root=args.library_root,
                database=args.database,
                attempt_id=args.attempt,
                quiescence=quiescence,
                writer_gate=gate,
            )
        else:
            report = executor.undo(
                plan,
                library_root=args.library_root,
                database=args.database,
                forward_attempt=args.forward_attempt,
                action_ids=args.actions,
                resume_attempt=args.resume,
                quiescence=quiescence,
                writer_gate=gate,
            )
    except (PlanError, executor.ExecutorError, ScopeLockError, JournalError, MarkerError,
            wg.GateError) as exc:
        print(json.dumps({"refused": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    incomplete = report.stopped is not None or any(
        unit["status"] == executor.UNIT_BLOCKED for unit in report.units
    ) or any(r["status"] == executor.RES_REFUSED for r in report.resolutions)
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
