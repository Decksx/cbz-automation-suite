"""Tests for review-case mechanics.

Case identity is the load-bearing part: it decides whether a retry recovers
the case already staged or duplicates 50 GB of payload beside it. The
semantics are asserted directly, one test per rule, because each is a
separate decision about what "the same case" means:

    same source + same bytes      -> same case
    same source + modified bytes  -> new case
    different source + same bytes -> different case
    renamed relative file         -> new case
    routing-config change only    -> same case

Nothing here touches a real library. The transfer-method branches are forced
independently of the machine, so the suite proves both paths regardless of
how this deployment's volumes happen to be arranged.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.cbz_classification_staging import (
    CASE_CONTRACT,
    RECOVERABLE_STATES,
    STATE_COPIED_VERIFIED,
    STATE_PENDING_REVIEW,
    STATE_PROMOTED,
    STATE_REJECTED,
    STATE_ROLLED_BACK,
    TERMINAL_STATES,
    VALID_STATES,
    STATUS_ABSENT,
    STATUS_ORPHANED_PENDING,
    STATUS_PARTIAL,
    STATUS_VALID,
    PayloadChangedError,
    SourceChangedError,
    CaseCollisionError,
    StagedPayloadMismatchError,
    PartialRecoveryRequiredError,
    InvalidExecutionPlanError,
    RECOVERY_NOTHING_STAGED,
    RECOVERY_RESUME_PUBLICATION,
    RECOVERY_RESTART_FROM_SOURCE,
    RECOVERY_PUBLISH_PARTIAL,
    RECOVERY_PARTIAL_UNUSABLE,
    RECOVERY_COMPLETE,
    RECOVERY_ORPHANED_PENDING,
    RECOVERY_PENDING_INCONSISTENT,
    RECOVERY_TERMINAL,
    RECOVERY_OPERATOR_REQUIRED,
    AUTOMATIC_RECOVERY_STATES,
    assess_recovery,
    recover_case,
    show_case,
    promote_case,
    reject_case,
    rollback_case,
    ACTION_PROMOTE,
    ACTION_REJECT,
    ACTION_ROLLBACK,
    inspect_case,
    execute_transfer,
    revalidate_source,
    METHOD_COPY_VERIFY,
    METHOD_RENAME,
    STATE_PLANNED,
    CaseManifest,
    FileEntry,
    PayloadInventory,
    StagingError,
    StagingLayout,
    build_inventory,
    case_identity,
    file_sha256,
    normalise_source_path,
    plan_case,
    same_volume,
    transfer_method,
    volume_identity,
)
from scripts.cbz_routing import series_key


def _payload(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


DEFAULT = {"ch01.cbz": b"one", "ch02.cbz": b"two", "extra/notes.txt": b"n"}


def _identity(root: Path, series="Some Series", source: Path | None = None):
    return case_identity(series_key(series), source or root, build_inventory(root))


# ── canonical inventory ──────────────────────────────────────────


def test_inventory_is_ordered_by_relative_path_not_walk_order(tmp_path):
    a = _payload(tmp_path / "a", {"z.cbz": b"z", "a.cbz": b"a", "m/x.cbz": b"x"})
    entries = build_inventory(a).entries
    assert [e.relative_path for e in entries] == ["a.cbz", "m/x.cbz", "z.cbz"]


def test_inventory_paths_are_separator_independent(tmp_path):
    a = _payload(tmp_path / "a", {"sub/deep/file.cbz": b"x"})
    assert build_inventory(a).entries[0].relative_path == "sub/deep/file.cbz"


def test_inventory_records_size_and_content_digest(tmp_path):
    a = _payload(tmp_path / "a", {"ch01.cbz": b"hello"})
    entry = build_inventory(a).entries[0]
    assert entry.size_bytes == 5
    assert entry.sha256 == file_sha256(a / "ch01.cbz")


def test_an_empty_directory_cannot_be_staged(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(StagingError, match="nothing to stage"):
        plan_case(tmp_path / "root", tmp_path / "empty", tmp_path / "empty",
                  "Empty", "empty", "src")


def test_a_missing_payload_root_is_an_error(tmp_path):
    with pytest.raises(StagingError, match="not a directory"):
        build_inventory(tmp_path / "nope")


def test_the_payload_digest_ignores_series_and_source(tmp_path):
    # Same bytes staged from two places share a payload digest, which is what
    # makes duplicate-content analysis across separate cases possible.
    a = _payload(tmp_path / "a", DEFAULT)
    b = _payload(tmp_path / "b", DEFAULT)
    assert build_inventory(a).digest == build_inventory(b).digest


def test_the_canonical_form_cannot_be_confused_by_a_hostile_filename(tmp_path):
    # A file whose name contains the field delimiter must not be able to
    # impersonate a different inventory.
    one = PayloadInventory((
        build_inventory(_payload(tmp_path / "a", {"x.cbz": b"x"})).entries[0],
    ))
    two = build_inventory(_payload(tmp_path / "b", {"x.cbz": b"x", "y.cbz": b"y"}))
    assert one.digest != two.digest


# ── case identity semantics ──────────────────────────────────────


def test_same_source_and_same_bytes_is_the_same_case(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    assert _identity(src) == _identity(src)


def test_modified_bytes_make_a_new_case(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    before = _identity(src)
    (src / "ch01.cbz").write_bytes(b"one modified")
    assert _identity(src) != before


def test_a_file_added_or_removed_makes_a_new_case(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    before = _identity(src)
    (src / "ch03.cbz").write_bytes(b"three")
    after_add = _identity(src)
    assert after_add != before
    (src / "ch03.cbz").unlink()
    assert _identity(src) == before


def test_a_renamed_relative_file_makes_a_new_case(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    before = _identity(src)
    (src / "ch01.cbz").rename(src / "chapter-one.cbz")
    assert _identity(src) != before


def test_the_same_bytes_from_a_different_source_is_a_different_case(tmp_path):
    a = _payload(tmp_path / "a", DEFAULT)
    b = _payload(tmp_path / "b", DEFAULT)
    assert _identity(a) != _identity(b)
    assert build_inventory(a).digest == build_inventory(b).digest


def test_a_different_series_is_a_different_case(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    assert _identity(src, series="Some Series") != _identity(src, series="Other")


def test_source_paths_differing_only_in_case_are_one_case(tmp_path):
    # Windows paths are case-insensitive, so the same directory reached by
    # differently-cased text must not mint two cases.
    src = _payload(tmp_path / "src", DEFAULT)
    inv = build_inventory(src)
    upper = Path(str(src).upper())
    assert case_identity("k", src, inv) == case_identity("k", upper, inv)


def test_normalisation_collapses_separators_and_traversal(tmp_path):
    a = normalise_source_path(Path(r"C:\a\b"))
    b = normalise_source_path(Path(r"C:\a\c\..\b"))
    assert a == b


def test_a_routing_config_change_alone_keeps_the_same_case(tmp_path):
    # A retry after a policy change must find the same physical case and
    # record a recomputed decision -- not duplicate 50 GB because policy moved.
    src = _payload(tmp_path / "src", DEFAULT)
    first = plan_case(tmp_path / "root", src, src, "S", series_key("S"), "src",
                      routing_config_digest="config-aaa")
    second = plan_case(tmp_path / "root", src, src, "S", series_key("S"), "src",
                       routing_config_digest="config-bbb")
    assert first.case_id == second.case_id
    assert first.manifest.routing_config_digest != \
        second.manifest.routing_config_digest


def test_the_decision_does_not_affect_case_identity(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)

    class _Decision:
        dest_key, confidence, authoritative = "manga", "unresolved", False
        evidence_strength, rule_name, reason = "none", None, "why"
        review_hints = ()

    plain = plan_case(tmp_path / "root", src, src, "S", series_key("S"), "src")
    decided = plan_case(tmp_path / "root", src, src, "S", series_key("S"), "src",
                        decision=_Decision())
    assert plain.case_id == decided.case_id
    assert decided.manifest.decision_dest_key == "manga"


def test_the_contract_version_is_part_of_the_identity(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    inv = build_inventory(src)
    assert CASE_CONTRACT.encode() in (
        CASE_CONTRACT.encode() + b"\0" + inv.canonical_bytes()
    )
    # Changing the contract string must change every case id.
    import scripts.cbz_classification_staging as staging
    before = case_identity("k", src, inv)
    original = staging.CASE_CONTRACT
    try:
        staging.CASE_CONTRACT = "classification-case-v2"
        assert case_identity("k", src, inv) != before
    finally:
        staging.CASE_CONTRACT = original


# ── transfer method ──────────────────────────────────────────────


def test_the_same_volume_selects_rename(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert same_volume(a, b)
    assert transfer_method(a, b) == METHOD_RENAME


def test_a_different_volume_selects_copy_verify(tmp_path, monkeypatch):
    # Forced, so both branches are proven regardless of how this machine's
    # volumes happen to be arranged.
    import scripts.cbz_classification_staging as staging
    monkeypatch.setattr(staging, "volume_identity",
                        lambda p: 1 if "left" in str(p) else 2)
    assert staging.transfer_method(tmp_path / "left", tmp_path / "right") \
        == METHOD_COPY_VERIFY


def test_volume_identity_walks_up_to_an_existing_ancestor(tmp_path):
    deep = tmp_path / "does" / "not" / "exist" / "yet"
    assert volume_identity(deep) == volume_identity(tmp_path)


def test_the_plan_records_the_chosen_method_and_volumes(tmp_path, monkeypatch):
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", DEFAULT)
    monkeypatch.setattr(staging, "volume_identity",
                        lambda p: 11 if "src" in str(p) else 22)
    plan = staging.plan_case(tmp_path / "root", src, src, "S", series_key("S"),
                             "src")
    assert plan.manifest.transfer_method == METHOD_COPY_VERIFY
    assert (plan.manifest.source_volume, plan.manifest.staging_volume) == (11, 22)
    assert plan.needs_space is True


# ── layout ───────────────────────────────────────────────────────


def test_the_layout_places_every_part_under_the_root(tmp_path):
    layout = StagingLayout(tmp_path / "_classification_review")
    case = "abc123"
    assert layout.partial_case(case).parent == layout.partial
    assert layout.payload(case).parent == layout.pending_case(case)
    assert layout.manifest(case) == layout.manifests / "abc123.json"
    for path in layout.all_dirs():
        assert path.parent == layout.root


def test_the_layout_creates_nothing(tmp_path):
    layout = StagingLayout(tmp_path / "review")
    layout.partial_case("x")
    layout.payload("x")
    layout.manifest("x")
    assert not layout.root.exists()


# ── manifest ─────────────────────────────────────────────────────


def test_a_manifest_round_trips(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    manifest = plan_case(tmp_path / "root", src, src, "S", series_key("S"),
                         "src").manifest
    restored = CaseManifest.from_json(manifest.to_json())
    assert restored == manifest


def _valid_manifest(tmp_path: Path) -> CaseManifest:
    src = _payload(tmp_path / "src", DEFAULT)
    return plan_case(tmp_path / "root", src, src, "S", series_key("S"),
                     "src").manifest


def _mutated(manifest: CaseManifest, **changes) -> str:
    raw = json.loads(manifest.to_json())
    for key, value in changes.items():
        if value is _DELETE:
            del raw[key]
        else:
            raw[key] = value
    return json.dumps(raw)


class _Delete:
    pass


_DELETE = _Delete()


def test_a_manifest_from_another_contract_is_refused(tmp_path):
    text = _mutated(_valid_manifest(tmp_path),
                    contract="classification-case-v99")
    with pytest.raises(StagingError, match="contract"):
        CaseManifest.from_json(text)


def test_an_unknown_manifest_field_is_refused(tmp_path):
    with pytest.raises(StagingError, match="unknown fields"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path), surprise=1))


def test_a_missing_manifest_field_is_refused(tmp_path):
    with pytest.raises(StagingError, match="missing fields"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        series_name=_DELETE))


@pytest.mark.parametrize("text", ["[]", "null", "3", '"a string"', "true"])
def test_a_manifest_root_must_be_an_object(text):
    with pytest.raises(StagingError, match="must be a JSON object"):
        CaseManifest.from_json(text)


@pytest.mark.parametrize("text", ["", "{", "{not json}", "{'single': 1}"])
def test_unparseable_manifest_json_is_a_staging_error(text):
    # Not a raw JSONDecodeError: callers handle StagingError.
    with pytest.raises(StagingError, match="not valid JSON"):
        CaseManifest.from_json(text)


@pytest.mark.parametrize("field, value", [
    ("state", 3),
    ("series_name", 7),
    ("files", "text"),
    ("file_count", "3"),
    ("total_bytes", None),
    ("decision_authoritative", 1),          # int must not satisfy a bool
    ("review_hints", {}),
])
def test_a_wrongly_typed_manifest_field_is_refused(tmp_path, field, value):
    with pytest.raises(StagingError, match=field):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        **{field: value}))


def test_a_boolean_must_not_satisfy_an_integer_field(tmp_path):
    with pytest.raises(StagingError, match="file_count"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        file_count=True))


def test_an_unknown_state_is_refused(tmp_path):
    with pytest.raises(StagingError, match="state"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        state="anything"))


@pytest.mark.parametrize("method", ["teleport", "", "RENAME", " rename"])
def test_an_unknown_transfer_method_is_refused(tmp_path, method):
    # Empty included: a persisted manifest must name the method that was
    # actually chosen, even though CaseManifest defaults to "" in memory.
    with pytest.raises(StagingError, match="transfer_method"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        transfer_method=method))


def test_an_in_memory_manifest_may_default_to_no_method(tmp_path):
    # The default is for construction convenience; it just cannot round-trip.
    blank = CaseManifest(case_id="x", state=STATE_PLANNED)
    assert blank.transfer_method == ""
    with pytest.raises(StagingError, match="transfer_method"):
        CaseManifest.from_json(blank.to_json())


@pytest.mark.parametrize("hints", [
    [1], [None], ["text"], [[]],
    [{"kind": 7, "value": "x"}],
    [{"kind": "x", "value": 7}],
    [{"kind": "x"}],
    [{"value": "x"}],
    [{"kind": "x", "value": "y", "extra": True}],
    [{"kind": "ok", "value": "ok"}, {"kind": None, "value": "y"}],
])
def test_a_malformed_review_hint_row_is_refused(tmp_path, hints):
    # Validating only the outer list let these reach whoever reviews the case.
    with pytest.raises(StagingError, match="review_hints"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        review_hints=hints))


def test_well_formed_review_hints_are_accepted(tmp_path):
    rows = [{"kind": "title_token", "value": "uncensored"},
            {"kind": "title_token", "value": "sex"}]
    restored = CaseManifest.from_json(
        _mutated(_valid_manifest(tmp_path), review_hints=rows))
    assert restored.review_hints == rows


def test_an_inconsistent_file_count_is_refused(tmp_path):
    with pytest.raises(StagingError, match="file_count"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        file_count=99))


def test_inconsistent_total_bytes_are_refused(tmp_path):
    with pytest.raises(StagingError, match="total_bytes"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        total_bytes=1))


def test_a_payload_digest_that_does_not_recompute_is_refused(tmp_path):
    with pytest.raises(StagingError, match="payload_inventory_digest"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        payload_inventory_digest="0" * 64))


def test_an_identity_digest_that_does_not_recompute_is_refused(tmp_path):
    manifest = _valid_manifest(tmp_path)
    with pytest.raises(StagingError, match="case_identity_digest"):
        CaseManifest.from_json(_mutated(manifest, series_key="other"))


def test_a_case_id_that_disagrees_with_its_digest_is_refused(tmp_path):
    with pytest.raises(StagingError, match="case_id"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        case_id="0" * 64))


def test_tampering_with_a_file_row_is_refused(tmp_path):
    manifest = _valid_manifest(tmp_path)
    files = json.loads(manifest.to_json())["files"]
    files[0]["sha256"] = "0" * 64
    with pytest.raises(StagingError, match="payload_inventory_digest"):
        CaseManifest.from_json(_mutated(manifest, files=files))


@pytest.mark.parametrize("relative", [
    "/etc/passwd", "C:/Windows/x.cbz", "C:\\Windows\\x.cbz",
    "../escape.cbz", "sub/../../escape.cbz", "./here.cbz",
    "back\\slash.cbz", "", " leading.cbz",
])
def test_an_unsafe_relative_path_is_refused(tmp_path, relative):
    # A manifest is read back to decide what to promote or roll back, so a
    # path in it must not be able to reach outside the case.
    manifest = _valid_manifest(tmp_path)
    files = [{"relative_path": relative, "size_bytes": 1, "sha256": "a" * 64}]
    with pytest.raises(StagingError, match="relative_path"):
        CaseManifest.from_json(_mutated(manifest, files=files))


def test_out_of_order_or_duplicated_file_rows_are_refused(tmp_path):
    manifest = _valid_manifest(tmp_path)
    rows = json.loads(manifest.to_json())["files"]
    with pytest.raises(StagingError, match="canonical relative-path order"):
        CaseManifest.from_json(_mutated(manifest, files=list(reversed(rows))))
    with pytest.raises(StagingError, match="duplicate"):
        CaseManifest.from_json(_mutated(manifest, files=[rows[0], rows[0]]))


def test_a_manifest_is_written_whole_and_leaves_no_temp(tmp_path):
    manifest = _valid_manifest(tmp_path)
    target = tmp_path / "manifests" / f"{manifest.case_id}.json"
    manifest.write(target)
    assert CaseManifest.from_json(target.read_text(encoding="utf-8")) == manifest
    assert list(target.parent.iterdir()) == [target]


def test_writing_replaces_an_existing_manifest_atomically(tmp_path):
    manifest = _valid_manifest(tmp_path)
    target = tmp_path / "m.json"
    manifest.write(target)
    manifest.state = STATE_PENDING_REVIEW
    manifest.write(target)
    assert CaseManifest.from_json(target.read_text(encoding="utf-8")).state \
        == STATE_PENDING_REVIEW


def test_created_at_survives_later_updates(tmp_path):
    manifest = CaseManifest(case_id="abc", state=STATE_PLANNED)
    manifest.touch()
    created = manifest.created_at
    manifest.touch()
    assert manifest.created_at == created


# ── planning is read-only ────────────────────────────────────────


def test_planning_creates_nothing(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    root = tmp_path / "review"
    plan = plan_case(root, src, src, "S", series_key("S"), "src")
    assert not root.exists()
    assert plan.manifest.state == STATE_PLANNED
    assert sorted(p.name for p in src.iterdir()) == ["ch01.cbz", "ch02.cbz",
                                                     "extra"]


def test_a_plan_reports_an_absent_case(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    plan = plan_case(tmp_path / "review", src, src, "S", series_key("S"), "src")
    assert plan.existing.status == STATUS_ABSENT
    assert plan.existing.manifest is None
    assert plan.existing.is_recoverable is False


def test_a_plan_recovers_a_validly_staged_case(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    root = tmp_path / "review"
    first = plan_case(root, src, src, "S", series_key("S"), "src")
    first.manifest.state = STATE_PENDING_REVIEW
    first.manifest.write(StagingLayout(root).manifest(first.case_id))

    again = plan_case(root, src, src, "S", series_key("S"), "src")
    assert again.case_id == first.case_id
    assert again.existing.status == STATUS_VALID
    assert again.existing.manifest.state == STATE_PENDING_REVIEW
    assert again.existing.is_recoverable is True


@pytest.mark.parametrize("content", ["{}", "", "not json", "[]",
                                     '{"contract": "classification-case-v9"}'])
def test_a_malformed_manifest_never_counts_as_an_existing_case(tmp_path, content):
    # The defect this replaced: any file at the path made existing_case True,
    # so a truncated manifest suppressed recovery of the real case.
    src = _payload(tmp_path / "src", DEFAULT)
    root = tmp_path / "review"
    case = plan_case(root, src, src, "S", series_key("S"), "src").case_id

    target = StagingLayout(root).manifest(case)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    with pytest.raises(StagingError):
        plan_case(root, src, src, "S", series_key("S"), "src")


def test_a_manifest_filed_under_the_wrong_case_id_is_refused(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    root = tmp_path / "review"
    manifest = plan_case(root, src, src, "S", series_key("S"), "src").manifest
    layout = StagingLayout(root)
    manifest.write(layout.manifest("f" * 64))          # filed under another id

    with pytest.raises(StagingError, match="filed as"):
        inspect_case(layout, "f" * 64)


def test_a_pending_directory_without_a_manifest_is_orphaned(tmp_path):
    # Nothing records what it was or whether its bytes are complete, so it
    # must not be mistaken for a recoverable case.
    src = _payload(tmp_path / "src", DEFAULT)
    root = tmp_path / "review"
    case = plan_case(root, src, src, "S", series_key("S"), "src").case_id
    StagingLayout(root).pending_case(case).mkdir(parents=True)

    again = plan_case(root, src, src, "S", series_key("S"), "src")
    assert again.existing.status == STATUS_ORPHANED_PENDING
    assert again.existing.is_recoverable is False


@pytest.mark.parametrize("state, recoverable", [
    (STATE_PLANNED, True),
    (STATE_COPIED_VERIFIED, True),
    (STATE_PENDING_REVIEW, True),
    (STATE_PROMOTED, False),
    (STATE_REJECTED, False),
    (STATE_ROLLED_BACK, False),
])
def test_recovery_is_state_aware(tmp_path, state, recoverable):
    # A valid manifest is not enough: reporting a promoted or rejected case
    # as resumable would let a retry reopen a decision already made.
    src = _payload(tmp_path / "src", DEFAULT)
    root = tmp_path / "review"
    plan = plan_case(root, src, src, "S", series_key("S"), "src")
    plan.manifest.state = state
    plan.manifest.write(StagingLayout(root).manifest(plan.case_id))

    existing = inspect_case(StagingLayout(root), plan.case_id)
    assert existing.status == STATUS_VALID
    assert existing.manifest.state == state
    assert existing.is_recoverable is recoverable


def test_every_terminal_state_is_non_recoverable():
    assert TERMINAL_STATES == {STATE_PROMOTED, STATE_REJECTED, STATE_ROLLED_BACK}
    assert not (TERMINAL_STATES & RECOVERABLE_STATES)
    assert TERMINAL_STATES | RECOVERABLE_STATES == VALID_STATES


def test_a_partial_directory_is_recoverable(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    root = tmp_path / "review"
    case = plan_case(root, src, src, "S", series_key("S"), "src").case_id
    StagingLayout(root).partial_case(case).mkdir(parents=True)

    again = plan_case(root, src, src, "S", series_key("S"), "src")
    assert again.existing.status == STATUS_PARTIAL
    assert again.existing.is_recoverable is True


# ── the inventory is a stable snapshot ───────────────────────────


def test_a_file_growing_during_hashing_is_rejected(tmp_path, monkeypatch):
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"ch01.cbz": b"start"})
    target = src / "ch01.cbz"
    real_open = staging.open if hasattr(staging, "open") else open

    def grow(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path) == target:
            with real_open(path, "ab") as extra:
                extra.write(b"more bytes arriving")
        return handle

    monkeypatch.setattr("builtins.open", grow)
    with pytest.raises(PayloadChangedError, match="retry after"):
        staging.build_inventory(src)


def test_a_file_replaced_during_hashing_is_rejected(tmp_path, monkeypatch):
    """Same size, different content -- caught by mtime, not by length.

    Driven by really rewriting the file mid-read rather than by counting
    os.stat calls: Path.is_file() stats too, and whether rglob serves that
    from a cached scandir entry varies by platform, so a call counter is not
    deterministic. mtime is bumped explicitly for the same reason -- a rewrite
    landing inside the filesystem's timestamp resolution would otherwise be
    invisible, which is a real limit of this check and is documented as one.
    """
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"ch01.cbz": b"aaaaa"})
    target = src / "ch01.cbz"
    real_open = open
    swapped = {"done": False}

    def replace_mid_read(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path) == target and not swapped["done"]:
            swapped["done"] = True
            target.write_bytes(b"bbbbb")            # identical length
            stat = staging.os.stat(target)
            staging.os.utime(
                target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        return handle

    monkeypatch.setattr("builtins.open", replace_mid_read)
    with pytest.raises(PayloadChangedError, match="changed while"):
        staging.build_inventory(src)
    assert swapped["done"], "the replacement never happened"


def test_a_file_appearing_between_enumerations_is_rejected(tmp_path, monkeypatch):
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"ch01.cbz": b"one"})
    real_members = staging._members
    seen = {"n": 0}

    def members(root):
        seen["n"] += 1
        if seen["n"] == 2:                     # after hashing, a new arrival
            (root / "ch02.cbz").write_bytes(b"two")
        return real_members(root)

    monkeypatch.setattr(staging, "_members", members)
    with pytest.raises(PayloadChangedError, match="membership changed"):
        staging.build_inventory(src)


def test_a_file_disappearing_between_enumerations_is_rejected(tmp_path, monkeypatch):
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"ch01.cbz": b"one", "ch02.cbz": b"two"})
    real_members = staging._members
    seen = {"n": 0}

    def members(root):
        seen["n"] += 1
        result = real_members(root)
        if seen["n"] == 1:
            return result
        (root / "ch02.cbz").unlink(missing_ok=True)
        return real_members(root)

    monkeypatch.setattr(staging, "_members", members)
    with pytest.raises(PayloadChangedError, match="membership changed"):
        staging.build_inventory(src)


def test_a_file_deleted_before_hashing_is_rejected(tmp_path, monkeypatch):
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"ch01.cbz": b"one"})
    real_hash = staging._hash_stable_file

    def vanish(path):
        Path(path).unlink()
        return real_hash(path)

    monkeypatch.setattr(staging, "_hash_stable_file", vanish)
    with pytest.raises(PayloadChangedError, match="disappeared"):
        staging.build_inventory(src)


def test_a_file_modified_after_its_own_hash_is_rejected(tmp_path, monkeypatch):
    """The whole tree must be stable at return, not each file during its read.

    Per-file checks accept this interleaving: hash a.cbz, start hashing
    b.cbz, rewrite a.cbz, finish b.cbz, membership unchanged. The returned
    inventory then carries a digest for an a.cbz that no longer exists.
    """
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"a.cbz": b"aaaaa", "b.cbz": b"bbbbb"})
    first, second = src / "a.cbz", src / "b.cbz"
    real_open = open
    done = {"n": False}

    def rewrite_first_while_hashing_second(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path) == second and not done["n"]:
            done["n"] = True
            first.write_bytes(b"ccccc")             # identical length
            stat = staging.os.stat(first)
            staging.os.utime(
                first, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        return handle

    monkeypatch.setattr("builtins.open", rewrite_first_while_hashing_second)
    with pytest.raises(PayloadChangedError, match="after it was hashed"):
        staging.build_inventory(src)
    assert done["n"], "the rewrite never happened"


def test_a_file_deleted_after_its_own_hash_is_rejected(tmp_path, monkeypatch):
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"a.cbz": b"aaaaa", "b.cbz": b"bbbbb"})
    real_members = staging._members
    seen = {"n": 0}

    def members(root):
        seen["n"] += 1
        result = real_members(root)
        if seen["n"] == 2:
            # Membership still reports both, but one is gone by the re-stat.
            (root / "a.cbz").unlink()
        return result

    monkeypatch.setattr(staging, "_members", members)
    with pytest.raises(PayloadChangedError, match="disappeared after"):
        staging.build_inventory(src)


def test_a_settled_payload_hashes_without_complaint(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    inventory = build_inventory(src)
    assert inventory.file_count == 3
    assert build_inventory(src) == inventory


# ── the source is revalidated before the source is released ─────


def test_revalidation_accepts_a_source_that_has_not_moved(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    expected = build_inventory(src)
    assert revalidate_source(src, expected) == expected


def test_a_source_that_grew_after_its_inventory_is_refused(tmp_path):
    """The case step 4 exists for, stated as the issue states it.

    Every check against the staged copy would pass here: the copy genuinely
    matches the inventory. It is the source that moved on, and the source is
    what step 6 deletes.
    """
    src = _payload(tmp_path / "src", DEFAULT)
    expected = build_inventory(src)
    (src / "ch03.cbz").write_bytes(b"a chapter that arrived late")

    with pytest.raises(SourceChangedError, match="no longer matches"):
        revalidate_source(src, expected)


def test_a_source_that_shrank_after_its_inventory_is_refused(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    expected = build_inventory(src)
    (src / "ch02.cbz").unlink()

    with pytest.raises(SourceChangedError, match="removed 1"):
        revalidate_source(src, expected)


def test_a_same_size_rewrite_is_caught_without_relying_on_mtime(tmp_path):
    """Content, not metadata -- this is the check `_hash_stable_file` cannot make.

    The rewrite keeps the length and the timestamp is deliberately restored,
    so a size-and-mtime guard would see nothing. Step 4 re-reads the bytes,
    which is why it narrows the tmp_replace_same_size window on both transfer
    paths rather than only on the copy path.
    """
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"ch01.cbz": b"aaaaa"})
    expected = build_inventory(src)

    target = src / "ch01.cbz"
    before = staging.os.stat(target)
    target.write_bytes(b"bbbbb")                     # identical length
    staging.os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    after = staging.os.stat(target)
    assert after.st_size == before.st_size, "the rewrite changed the length"
    assert after.st_mtime_ns == before.st_mtime_ns, "the mtime was not restored"

    with pytest.raises(SourceChangedError, match="modified 1"):
        revalidate_source(src, expected)


def test_a_renamed_file_reports_both_sides_of_the_rename(tmp_path):
    src = _payload(tmp_path / "src", {"ch01.cbz": b"one"})
    expected = build_inventory(src)
    (src / "ch01.cbz").rename(src / "ch01-v2.cbz")

    with pytest.raises(SourceChangedError) as caught:
        revalidate_source(src, expected)
    message = str(caught.value)
    assert "added 1" in message and "'ch01-v2.cbz'" in message
    assert "removed 1" in message and "'ch01.cbz'" in message


def test_a_refused_revalidation_leaves_the_source_alone(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    expected = build_inventory(src)
    (src / "ch03.cbz").write_bytes(b"late arrival")
    before = {p.relative_to(src).as_posix(): p.read_bytes()
              for p in src.rglob("*") if p.is_file()}

    with pytest.raises(SourceChangedError):
        revalidate_source(src, expected)

    after = {p.relative_to(src).as_posix(): p.read_bytes()
             for p in src.rglob("*") if p.is_file()}
    assert after == before, "revalidation is read-only and must delete nothing"


def test_a_changed_source_mints_a_new_case_id(tmp_path):
    """The retry contract: a changed payload is a different case, not a resume."""
    src = _payload(tmp_path / "src", DEFAULT)
    original = _identity(src)
    expected = build_inventory(src)

    (src / "ch03.cbz").write_bytes(b"a chapter that arrived late")
    with pytest.raises(SourceChangedError):
        revalidate_source(src, expected)

    assert _identity(src) != original


def test_a_source_still_moving_is_transient_not_a_changed_case(tmp_path,
                                                              monkeypatch):
    """PayloadChangedError, not SourceChangedError -- this case id may be resumed."""
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", {"ch01.cbz": b"start"})
    expected = build_inventory(src)
    target = src / "ch01.cbz"
    real_open = open
    grown = {"done": False}

    def grow(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path) == target and not grown["done"]:
            grown["done"] = True
            with real_open(path, "ab") as extra:
                extra.write(b" still arriving")
        return handle

    monkeypatch.setattr("builtins.open", grow)
    with pytest.raises(PayloadChangedError):
        staging.revalidate_source(src, expected)
    assert grown["done"], "the growth never happened"


def test_a_size_only_disagreement_is_still_reported_as_modified(tmp_path):
    """The report must never come back empty while the digests disagree.

    A manifest can carry a size_bytes inconsistent with its own sha256:
    from_json recomputes the manifest against itself, never against real
    files, so such a manifest validates. Comparing content alone would refuse
    it correctly and then tell the operator nothing had changed.
    """
    src = _payload(tmp_path / "src", {"ch01.cbz": b"one"})
    real = build_inventory(src)
    entry = real.entries[0]
    claimed = PayloadInventory((
        FileEntry(entry.relative_path, entry.size_bytes + 1, entry.sha256),
    ))
    assert claimed.digest != real.digest

    with pytest.raises(SourceChangedError, match="modified 1"):
        revalidate_source(src, claimed)


def test_the_difference_report_is_bounded(tmp_path):
    """Bounded listing, exact count -- this text goes into an operator's face."""
    src = _payload(tmp_path / "src", {"ch01.cbz": b"one"})
    expected = build_inventory(src)
    for n in range(12):
        (src / f"extra{n:02d}.cbz").write_bytes(b"x")

    with pytest.raises(SourceChangedError) as caught:
        revalidate_source(src, expected)
    message = str(caught.value)
    assert "added 12" in message
    assert "and 7 more" in message


# ── the transfer protocol ────────────────────────────────────────


def _forced_plan(tmp_path, monkeypatch, method, files=None):
    """A plan with *method* forced, independently of this machine's volumes.

    Both branches are exercised on every host: nothing here depends on how
    the running machine's drives happen to be arranged.
    """
    import scripts.cbz_classification_staging as staging
    src = _payload(tmp_path / "src", DEFAULT if files is None else files)
    if method == METHOD_COPY_VERIFY:
        monkeypatch.setattr(staging, "volume_identity",
                            lambda p: 11 if "src" in str(p) else 22)
    else:
        monkeypatch.setattr(staging, "volume_identity", lambda p: 7)
    plan = staging.plan_case(tmp_path / "review", src, src, "Some Series",
                             series_key("Some Series"), "src")
    assert plan.manifest.transfer_method == method, "method was not forced"
    return staging, src, plan


def _staged_files(layout, case_id, source_name) -> dict[str, bytes]:
    root = layout.payload(case_id) / source_name
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("method", [METHOD_COPY_VERIFY, METHOD_RENAME])
def test_a_transfer_publishes_the_payload_under_pending(tmp_path, monkeypatch,
                                                        method):
    """Both paths land the same published layout, byte for byte."""
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, method)
    manifest = execute_transfer(plan)

    assert manifest.state == STATE_PENDING_REVIEW
    assert _staged_files(plan.layout, plan.case_id, "src") == DEFAULT
    assert not plan.layout.partial_case(plan.case_id).exists()
    if method == METHOD_RENAME:
        assert not src.exists(), "the rename moves the original"
    else:
        assert src.exists(), "the copy path retains the original by default"


@pytest.mark.parametrize("method", [METHOD_COPY_VERIFY, METHOD_RENAME])
def test_a_published_manifest_round_trips(tmp_path, monkeypatch, method):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, method)
    execute_transfer(plan)
    written = plan.layout.manifest(plan.case_id).read_text(encoding="utf-8")
    assert CaseManifest.from_json(written).state == STATE_PENDING_REVIEW


def test_the_copy_path_retains_the_source_by_default(tmp_path, monkeypatch):
    """Quarantine, don't delete. The destructive step must be asked for."""
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan)
    assert src.exists()
    assert _staged_files(plan.layout, plan.case_id, "src") == DEFAULT


def test_the_copy_path_deletes_the_source_only_when_asked(tmp_path, monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan, delete_copied_source=True)
    assert not src.exists()
    assert _staged_files(plan.layout, plan.case_id, "src") == DEFAULT


def test_an_opted_in_deletion_still_waits_for_publication(tmp_path, monkeypatch):
    """The flag authorises deletion after publication, never instead of it."""
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    real_verify = staging._verify_staged_payload

    def verify_then_grow(staged_root, expected):
        real_verify(staged_root, expected)
        (src / "ch03.cbz").write_bytes(b"a chapter that arrived late")

    monkeypatch.setattr(staging, "_verify_staged_payload", verify_then_grow)

    with pytest.raises(SourceChangedError):
        execute_transfer(plan, delete_copied_source=True)
    assert src.exists(), "the source was deleted despite publication failing"
    assert (src / "ch01.cbz").read_bytes() == DEFAULT["ch01.cbz"]


def test_an_interrupted_rename_is_never_discarded(tmp_path, monkeypatch):
    """The crash window where .partial holds the only copy.

    On the copy path .partial is disposable debris because the source
    survives. On the rename path, between the move and the publication, the
    source is gone and .partial is everything. Cleaning it to restart -- safe
    on the copy path -- would destroy the payload.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_RENAME)
    real_replace = staging.os.replace
    published = plan.layout.pending_case(plan.case_id)

    def replace_but_never_publish(a, b, *args, **kwargs):
        if Path(b) == published:
            raise RuntimeError("crash between the move and the publication")
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(staging.os, "replace", replace_but_never_publish)
    with pytest.raises(RuntimeError, match="crash between"):
        execute_transfer(plan)

    staged = plan.layout.partial_payload(plan.case_id) / "src"
    assert not src.exists(), "precondition: the source really was moved"
    assert not published.exists()
    stranded = {p.relative_to(staged).as_posix(): p.read_bytes()
                for p in staged.rglob("*") if p.is_file()}
    assert stranded == DEFAULT, "precondition: .partial holds every byte"

    with pytest.raises(PartialRecoveryRequiredError, match="only copy"):
        execute_transfer(plan)

    assert {p.relative_to(staged).as_posix(): p.read_bytes()
            for p in staged.rglob("*") if p.is_file()} == DEFAULT


def test_a_case_that_turns_terminal_after_planning_is_refused(tmp_path,
                                                              monkeypatch):
    """Collisions are judged at the destructive boundary, not from the plan.

    plan.existing is a snapshot taken during planning. A case can be
    published, promoted, or rejected in between, and trusting the snapshot
    would reopen a decision an operator had already made.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    assert plan.existing.status == STATUS_ABSENT, "the snapshot says absent"

    terminal = CaseManifest.from_json(plan.manifest.to_json())
    terminal.state = STATE_PROMOTED
    terminal.write(plan.layout.manifest(plan.case_id))

    with pytest.raises(CaseCollisionError, match="refusing to reopen"):
        execute_transfer(plan)

    still = CaseManifest.from_json(
        plan.layout.manifest(plan.case_id).read_text(encoding="utf-8"))
    assert still.state == STATE_PROMOTED, "the terminal case was rewritten"
    assert src.exists()
    assert not plan.layout.pending_case(plan.case_id).exists()


def _mutate_case_id(manifest, plan):
    manifest.case_id = "0" * 64


def _mutate_source_path(manifest, plan):
    manifest.staging_source_path = str(Path(manifest.staging_source_path).parent
                                       / "somewhere-else")


def _mutate_files(manifest, plan):
    manifest.files = manifest.files[:-1]


def _mutate_digest(manifest, plan):
    manifest.payload_inventory_digest = "f" * 64


def _mutate_method(manifest, plan):
    manifest.transfer_method = (METHOD_RENAME
                                if manifest.transfer_method == METHOD_COPY_VERIFY
                                else METHOD_COPY_VERIFY)


@pytest.mark.parametrize("mutate", [
    _mutate_case_id, _mutate_source_path, _mutate_files, _mutate_digest,
    _mutate_method,
], ids=["case_id", "source_path", "files", "digest", "method"])
def test_a_mutated_plan_is_refused_before_anything_is_touched(tmp_path,
                                                              monkeypatch,
                                                              mutate):
    """StagingPlan is frozen; its manifest is not.

    These fields decide which directory is created, which manifest is
    overwritten, and which directory is deleted. Validation runs before any
    of them is used, so pre-existing debris must survive untouched.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    debris = plan.layout.partial_payload(plan.case_id) / "src"
    debris.mkdir(parents=True)
    (debris / "leftover.cbz").write_bytes(b"from an interrupted attempt")

    mutate(plan.manifest, plan)

    with pytest.raises(InvalidExecutionPlanError):
        execute_transfer(plan)

    assert (debris / "leftover.cbz").read_bytes() == b"from an interrupted attempt"
    assert not plan.layout.pending.exists()
    assert src.exists()


def test_a_staged_copy_that_does_not_match_the_manifest_is_refused(tmp_path,
                                                                   monkeypatch):
    """Gate 1. The copy is wrong; the source is untouched and still authoritative."""
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    real_copytree = staging.shutil.copytree

    def copy_then_corrupt(source, destination, *args, **kwargs):
        result = real_copytree(source, destination, *args, **kwargs)
        (Path(destination) / "ch01.cbz").write_bytes(b"corrupted in transit")
        return result

    monkeypatch.setattr(staging.shutil, "copytree", copy_then_corrupt)

    with pytest.raises(StagedPayloadMismatchError, match="does not match"):
        execute_transfer(plan)

    assert {p.relative_to(src).as_posix(): p.read_bytes()
            for p in src.rglob("*") if p.is_file()} == DEFAULT
    assert not plan.layout.pending_case(plan.case_id).exists()


def test_a_source_that_grows_after_the_copy_aborts_and_deletes_nothing(
        tmp_path, monkeypatch):
    """Gate 2, on the copy path -- the case #35 is built around.

    Growth is injected between the destination check and the source check, so
    gate 1 genuinely passes and only gate 2 can catch it. That is the whole
    claim: verifying the copy says nothing about the source.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    real_verify = staging._verify_staged_payload
    grown = {"done": False}

    def verify_then_grow(staged_root, expected):
        real_verify(staged_root, expected)               # gate 1 really passes
        if not grown["done"]:
            grown["done"] = True
            (src / "ch03.cbz").write_bytes(b"a chapter that arrived late")

    monkeypatch.setattr(staging, "_verify_staged_payload", verify_then_grow)

    with pytest.raises(SourceChangedError, match="no longer matches"):
        execute_transfer(plan)

    assert grown["done"], "the growth never happened"
    assert (src / "ch03.cbz").exists(), "the late chapter was destroyed"
    assert {p.relative_to(src).as_posix(): p.read_bytes()
            for p in src.rglob("*") if p.is_file()} == {
        **DEFAULT, "ch03.cbz": b"a chapter that arrived late"}
    assert not plan.layout.pending_case(plan.case_id).exists()


def test_the_rename_path_revalidates_before_it_moves_anything(tmp_path,
                                                              monkeypatch):
    """Gate 2, on the path that has no copied bytes to check.

    The rename skips gates 2 and 3 of the copy path but not this one, and it
    must run before the move -- afterwards there is nothing left to check.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_RENAME)
    (src / "ch03.cbz").write_bytes(b"a chapter that arrived late")

    with pytest.raises(SourceChangedError, match="no longer matches"):
        execute_transfer(plan)

    assert src.exists(), "the source was moved despite failing revalidation"
    assert (src / "ch03.cbz").exists()
    assert not plan.layout.pending_case(plan.case_id).exists()


@pytest.mark.parametrize("method, gate", [
    # Which gate fires depends on when the growth lands, and both are correct.
    # Growing before the copy means the staged bytes already disagree with the
    # manifest, so gate 1 catches it and gate 2 is never reached. The rename
    # path has no copied bytes, so gate 2 is the only thing standing there.
    (METHOD_COPY_VERIFY, StagedPayloadMismatchError),
    (METHOD_RENAME, SourceChangedError),
])
def test_a_retry_after_an_abort_mints_a_new_case_id(tmp_path, monkeypatch,
                                                    method, gate):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, method)
    (src / "ch03.cbz").write_bytes(b"a chapter that arrived late")
    with pytest.raises(gate):
        execute_transfer(plan)

    assert not plan.layout.pending_case(plan.case_id).exists()
    retry = staging.plan_case(tmp_path / "review", src, src, "Some Series",
                              series_key("Some Series"), "src")
    assert retry.case_id != plan.case_id


def test_a_published_case_refuses_a_second_transfer(tmp_path, monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY,
                                      files=DEFAULT)
    execute_transfer(plan)

    again = staging.plan_case(tmp_path / "review", src, src, "Some Series",
                              series_key("Some Series"), "src")
    assert again.case_id == plan.case_id, "same bytes must resolve to one case"
    with pytest.raises(CaseCollisionError, match="already exists"):
        execute_transfer(again)


def test_a_terminal_case_is_never_reopened(tmp_path, monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    plan.manifest.state = STATE_PROMOTED
    plan.manifest.write(plan.layout.manifest(plan.case_id))

    reloaded = staging.plan_case(tmp_path / "review", src, src, "Some Series",
                                 series_key("Some Series"), "src")
    with pytest.raises(CaseCollisionError, match="refusing to reopen"):
        execute_transfer(reloaded)
    assert src.exists()


def test_insufficient_space_refuses_before_copying(tmp_path, monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    starved = staging.StagingPlan(
        manifest=plan.manifest, layout=plan.layout, inventory=plan.inventory,
        existing=plan.existing, free_bytes=0)

    with pytest.raises(StagingError, match="insufficient space"):
        execute_transfer(starved)
    assert src.exists()
    assert not plan.layout.pending_case(plan.case_id).exists()


def test_stale_partial_debris_is_cleaned_rather_than_merged(tmp_path,
                                                            monkeypatch):
    """.partial is never authoritative, so debris is discarded, not resumed."""
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    debris = plan.layout.partial_payload(plan.case_id) / "src"
    debris.mkdir(parents=True)
    (debris / "leftover.cbz").write_bytes(b"from an interrupted attempt")

    execute_transfer(plan)
    assert _staged_files(plan.layout, plan.case_id, "src") == DEFAULT


# ── recovery assessment: the seven-state matrix ──────────────────


def _interrupt_before_publication(tmp_path, monkeypatch, method):
    """Drive a real transfer and stop it in the window before publication.

    Injected at the publication rename specifically, so everything before it
    genuinely happened: on the copy path the bytes are copied and verified, on
    the rename path the source has really been moved.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, method)
    real_replace = staging.os.replace
    published = plan.layout.pending_case(plan.case_id)

    def never_publish(a, b, *args, **kwargs):
        if Path(b) == published:
            raise RuntimeError("interrupted before publication")
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(staging.os, "replace", never_publish)
    with pytest.raises(RuntimeError, match="interrupted"):
        execute_transfer(plan)
    monkeypatch.setattr(staging.os, "replace", real_replace)
    assert not published.exists(), "precondition: publication did not happen"
    return staging, src, plan


def _rebuilt_from_disk(review_root: Path):
    """Layout and case id recovered from the filesystem, with no plan object.

    This is the whole claim recovery has to support: after a process dies,
    nothing survives but what was written down.
    """
    layout = StagingLayout(review_root)
    ids = sorted(p.stem for p in layout.manifests.glob("*.json"))
    assert len(ids) == 1, f"expected one manifest on disk, found {ids}"
    return layout, ids[0]


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_nothing_staged_is_reported_not_invented(tmp_path):
    layout = StagingLayout(tmp_path / "review")
    found = assess_recovery(layout, "0" * 64)
    assert found.state == RECOVERY_NOTHING_STAGED
    assert found.manifest is None
    assert not layout.root.exists(), "assessment created the staging root"


def test_matrix_1_copy_with_a_verified_partial_resumes(tmp_path, monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_RESUME_PUBLICATION
    assert found.automatic is True
    assert src.exists(), "the copy path keeps its source"


def test_matrix_2_copy_with_a_bad_partial_restarts_from_source(tmp_path,
                                                               monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    (layout.partial_payload(case_id) / "src" / "ch01.cbz").write_bytes(b"wrong")

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_RESTART_FROM_SOURCE
    assert found.automatic is True
    assert "authoritative" in found.detail


def test_matrix_3_rename_with_a_verified_partial_publishes(tmp_path,
                                                           monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_RENAME)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")

    assert not src.exists(), "precondition: the rename gave up the source"
    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_PUBLISH_PARTIAL
    assert found.automatic is True
    assert "only copy" in found.detail


def test_matrix_4_rename_with_a_bad_partial_needs_an_operator(tmp_path,
                                                              monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_RENAME)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    (layout.partial_payload(case_id) / "src" / "ch01.cbz").write_bytes(b"wrong")

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_PARTIAL_UNUSABLE
    assert found.automatic is False
    assert "must not be deleted" in found.detail


def test_matrix_5_a_published_case_is_idempotently_complete(tmp_path,
                                                            monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_COMPLETE
    assert found.automatic is True


def test_matrix_6_a_pending_case_without_a_manifest_is_orphaned(tmp_path,
                                                                monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan)
    case_id = plan.case_id
    layout = StagingLayout(tmp_path / "review")
    layout.manifest(case_id).unlink()

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_ORPHANED_PENDING
    assert found.automatic is False
    assert found.manifest is None
    assert "none of that can be recovered" in found.detail


def test_matrix_7_a_manifest_claiming_pending_with_no_payload(tmp_path,
                                                              monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    shutil.rmtree(layout.pending_case(case_id))

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_PENDING_INCONSISTENT
    assert found.automatic is False
    assert "contradiction" in found.detail


def test_a_pending_payload_that_does_not_verify_is_inconsistent(tmp_path,
                                                                monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    (layout.payload(case_id) / "src" / "ch01.cbz").write_bytes(b"tampered")

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_PENDING_INCONSISTENT
    assert found.automatic is False
    assert "republish" in found.detail


def test_a_terminal_case_is_not_an_interrupted_transfer(tmp_path, monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    promoted = CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8"))
    promoted.state = STATE_PROMOTED
    promoted.write(layout.manifest(case_id))

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_TERMINAL
    assert found.automatic is False


@pytest.mark.parametrize("content", ["", "{", "not json", "[]", "{}"])
def test_a_manifest_that_will_not_validate_is_reported_not_raised(
        tmp_path, monkeypatch, content):
    """Unlike inspect_case, which gates creation and refuses loudly.

    This function exists to tell an operator what is on disk, and "the
    manifest is corrupt" is the most important thing it could have to say.
    """
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    layout.manifest(case_id).write_text(content, encoding="utf-8")

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_OPERATOR_REQUIRED
    assert found.automatic is False
    assert "does not validate" in found.detail


def test_a_partial_with_no_manifest_is_not_attributed_to_a_case(tmp_path):
    layout = StagingLayout(tmp_path / "review")
    case_id = "a" * 64
    (layout.partial_payload(case_id) / "src").mkdir(parents=True)
    (layout.partial_payload(case_id) / "src" / "x.cbz").write_bytes(b"x")

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_OPERATOR_REQUIRED
    assert "must not be assumed to belong" in found.detail


def test_a_payload_in_neither_location_is_reported(tmp_path, monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_RENAME)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    shutil.rmtree(layout.partial_case(case_id))

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_OPERATOR_REQUIRED
    assert "neither location" in found.detail


def test_a_copy_transfer_whose_source_vanished_is_unexplained(tmp_path,
                                                              monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    shutil.rmtree(src)

    found = assess_recovery(layout, case_id)
    assert found.state == RECOVERY_OPERATOR_REQUIRED
    assert found.automatic is False
    assert "unexplained" in found.detail


@pytest.mark.parametrize("method", [METHOD_COPY_VERIFY, METHOD_RENAME])
def test_assessment_changes_nothing_on_disk(tmp_path, monkeypatch, method):
    staging, src, plan = _interrupt_before_publication(tmp_path, monkeypatch,
                                                       method)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    before_review, before_source = _snapshot(layout.root), _snapshot(src)

    assess_recovery(layout, case_id)

    assert _snapshot(layout.root) == before_review
    assert _snapshot(src) == before_source


def test_the_automatic_set_is_exactly_the_actionable_states():
    """A state is automatic only if a machine can act without asking."""
    assert AUTOMATIC_RECOVERY_STATES == {
        RECOVERY_RESUME_PUBLICATION, RECOVERY_RESTART_FROM_SOURCE,
        RECOVERY_PUBLISH_PARTIAL, RECOVERY_COMPLETE,
    }
    for state in (RECOVERY_PARTIAL_UNUSABLE, RECOVERY_ORPHANED_PENDING,
                  RECOVERY_PENDING_INCONSISTENT, RECOVERY_TERMINAL,
                  RECOVERY_OPERATOR_REQUIRED, RECOVERY_NOTHING_STAGED):
        assert state not in AUTOMATIC_RECOVERY_STATES


# ── recovery actions ─────────────────────────────────────────────


def test_recovery_resumes_a_verified_copy_without_copying_again(tmp_path,
                                                                monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")

    copied = {"n": 0}
    real_copytree = staging.shutil.copytree

    def counted(*a, **kw):
        copied["n"] += 1
        return real_copytree(*a, **kw)

    monkeypatch.setattr(staging.shutil, "copytree", counted)
    outcome = recover_case(layout, case_id)

    assert outcome.acted is True
    assert outcome.state == RECOVERY_RESUME_PUBLICATION
    assert copied["n"] == 0, "the payload was copied again"
    assert _staged_files(layout, case_id, "src") == DEFAULT
    assert src.exists(), "the source is retained by default"
    assert not layout.partial_case(case_id).exists()


def test_recovery_recopies_when_the_staged_copy_is_unusable(tmp_path,
                                                            monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    (layout.partial_payload(case_id) / "src" / "ch01.cbz").write_bytes(b"wrong")

    outcome = recover_case(layout, case_id)

    assert outcome.acted is True
    assert outcome.state == RECOVERY_RESTART_FROM_SOURCE
    assert _staged_files(layout, case_id, "src") == DEFAULT
    assert src.exists()


def test_recovery_publishes_an_authoritative_partial_without_the_source(
        tmp_path, monkeypatch):
    """The rename crash window: .partial is everything, and it is enough."""
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_RENAME)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    assert not src.exists(), "precondition: the source is gone"

    outcome = recover_case(layout, case_id)

    assert outcome.acted is True
    assert outcome.state == RECOVERY_PUBLISH_PARTIAL
    assert _staged_files(layout, case_id, "src") == DEFAULT
    assert not src.exists(), "recovery must not require or recreate the source"


def test_recovery_reconciles_a_published_case_idempotently(tmp_path,
                                                           monkeypatch):
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    execute_transfer(plan)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")

    drifted = CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8"))
    drifted.state = STATE_COPIED_VERIFIED
    drifted.write(layout.manifest(case_id))

    first = recover_case(layout, case_id)
    assert first.acted is True
    assert first.manifest.state == STATE_PENDING_REVIEW

    second = recover_case(layout, case_id)
    assert second.acted is False, "reconciliation is not idempotent"
    assert second.state == RECOVERY_COMPLETE


@pytest.mark.parametrize("wreck, expected_state", [
    ("rename_partial", RECOVERY_PARTIAL_UNUSABLE),
    ("orphan_pending", RECOVERY_ORPHANED_PENDING),
    ("drop_pending", RECOVERY_PENDING_INCONSISTENT),
    ("corrupt_manifest", RECOVERY_OPERATOR_REQUIRED),
])
def test_recovery_refuses_without_mutating_anything(tmp_path, monkeypatch,
                                                    wreck, expected_state):
    """Structured refusal, and the filesystem is byte for byte untouched."""
    if wreck == "rename_partial":
        staging, src, plan = _interrupt_before_publication(
            tmp_path, monkeypatch, METHOD_RENAME)
        layout, case_id = _rebuilt_from_disk(tmp_path / "review")
        (layout.partial_payload(case_id) / "src" / "ch01.cbz").write_bytes(b"x")
    elif wreck == "corrupt_manifest":
        staging, src, plan = _interrupt_before_publication(
            tmp_path, monkeypatch, METHOD_COPY_VERIFY)
        layout, case_id = _rebuilt_from_disk(tmp_path / "review")
        layout.manifest(case_id).write_text("{not json}", encoding="utf-8")
    else:
        staging, src, plan = _forced_plan(tmp_path, monkeypatch,
                                          METHOD_COPY_VERIFY)
        execute_transfer(plan)
        layout, case_id = StagingLayout(tmp_path / "review"), plan.case_id
        if wreck == "orphan_pending":
            layout.manifest(case_id).unlink()
        else:
            shutil.rmtree(layout.pending_case(case_id))

    before_review, before_source = _snapshot(layout.root), _snapshot(src)
    outcome = recover_case(layout, case_id)

    assert outcome.acted is False
    assert outcome.state == expected_state
    assert "no automatic action" in outcome.detail
    assert _snapshot(layout.root) == before_review, "recovery mutated the root"
    assert _snapshot(src) == before_source, "recovery mutated the source"


def test_recovery_never_discards_a_rename_partial_that_holds_a_payload(
        tmp_path, monkeypatch):
    """Source and rename-path payload both present is unexplained, so refuse."""
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_RENAME)
    layout = plan.layout
    plan.manifest.state = STATE_PLANNED
    plan.manifest.touch()
    plan.manifest.write(layout.manifest(plan.case_id))
    staged = layout.partial_payload(plan.case_id) / "src"
    staged.mkdir(parents=True)
    (staged / "ch01.cbz").write_bytes(b"one")

    before = _snapshot(layout.root)
    outcome = recover_case(layout, plan.case_id)

    assert outcome.acted is False
    assert outcome.state == RECOVERY_OPERATOR_REQUIRED
    assert _snapshot(layout.root) == before
    assert src.exists()


def test_recovery_aborts_when_the_source_changed_underneath_it(tmp_path,
                                                               monkeypatch):
    """Propagated, not returned: the payload is now a different case."""
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    (src / "ch03.cbz").write_bytes(b"a chapter that arrived late")

    with pytest.raises(SourceChangedError):
        recover_case(layout, case_id)

    assert not layout.pending_case(case_id).exists()
    assert (src / "ch03.cbz").exists()


def test_recovery_takes_no_plan_and_survives_a_rebuilt_layout(tmp_path,
                                                              monkeypatch):
    """Everything recovery needs is on disk, or it is not recovery."""
    import inspect as _inspect
    params = _inspect.signature(recover_case).parameters
    assert "plan" not in params
    assert [p for p in params if params[p].kind is _inspect.Parameter.POSITIONAL_OR_KEYWORD] \
        == ["layout", "case_id"]

    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_RENAME)
    del plan
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    assert recover_case(layout, case_id).acted is True


def test_recovery_can_delete_the_copied_source_when_asked(tmp_path,
                                                          monkeypatch):
    staging, src, plan = _interrupt_before_publication(
        tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")

    outcome = recover_case(layout, case_id, delete_copied_source=True)
    assert outcome.acted is True
    assert not src.exists()
    assert _staged_files(layout, case_id, "src") == DEFAULT


def test_recovery_refuses_if_a_rename_payload_appears_after_assessment(
        tmp_path, monkeypatch):
    """The window between classifying and acting, on the irreplaceable path.

    Assessment only reports RESTART_FROM_SOURCE for a rename when .partial
    holds no payload. If one appears in the gap, clearing .partial would
    delete something that may be the only copy -- so it is checked again
    rather than trusted, and this test is what makes that check reachable.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_RENAME)
    layout, case_id = plan.layout, plan.case_id
    plan.manifest.state = STATE_PLANNED
    plan.manifest.touch()
    plan.manifest.write(layout.manifest(case_id))
    layout.partial_payload(case_id).mkdir(parents=True)      # scaffolding only

    assert assess_recovery(layout, case_id).state == RECOVERY_RESTART_FROM_SOURCE

    real_assess = staging.assess_recovery

    def assess_then_plant(lay, cid):
        found = real_assess(lay, cid)
        staged = lay.partial_payload(cid) / "src"
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "ch01.cbz").write_bytes(b"appeared in the gap")
        return found

    monkeypatch.setattr(staging, "assess_recovery", assess_then_plant)
    outcome = staging.recover_case(layout, case_id)

    assert outcome.acted is False
    assert "appeared between assessment and recovery" in outcome.detail
    assert (layout.partial_payload(case_id) / "src" / "ch01.cbz").read_bytes() \
        == b"appeared in the gap", "the planted payload was destroyed"
    assert src.exists()


@pytest.mark.parametrize("method, state", [
    (METHOD_COPY_VERIFY, RECOVERY_RESUME_PUBLICATION),
    (METHOD_RENAME, RECOVERY_PUBLISH_PARTIAL),
])
def test_recovery_refuses_if_the_staged_payload_stops_verifying(
        tmp_path, monkeypatch, method, state):
    """Re-verified immediately before publication, on both paths.

    The assessment describes a moment that has already passed. Publishing on
    the strength of it would stage bytes nobody checked.
    """
    staging, src, plan = _interrupt_before_publication(tmp_path, monkeypatch,
                                                       method)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    assert assess_recovery(layout, case_id).state == state

    real_assess = staging.assess_recovery

    def assess_then_corrupt(lay, cid):
        found = real_assess(lay, cid)
        (lay.partial_payload(cid) / "src" / "ch01.cbz").write_bytes(b"corrupt")
        return found

    monkeypatch.setattr(staging, "assess_recovery", assess_then_corrupt)
    outcome = staging.recover_case(layout, case_id)

    assert outcome.acted is False
    assert "stopped verifying" in outcome.detail
    assert not layout.pending_case(case_id).exists()


def test_a_failed_destination_check_leaves_a_recoverable_case(tmp_path,
                                                              monkeypatch):
    """The four properties together, pinning the #35 criterion as a contract.

    Previously only the source-intact half was asserted; the .partial
    recoverable half was verified by hand and never regression-pinned.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_COPY_VERIFY)
    real_copytree = staging.shutil.copytree

    def copy_then_corrupt(source, destination, *args, **kwargs):
        result = real_copytree(source, destination, *args, **kwargs)
        (Path(destination) / "ch01.cbz").write_bytes(b"corrupted in transit")
        return result

    monkeypatch.setattr(staging.shutil, "copytree", copy_then_corrupt)
    with pytest.raises(StagedPayloadMismatchError):
        execute_transfer(plan)

    case_id = plan.case_id
    assert src.exists(), "the source must survive a destination check failure"
    assert not plan.layout.pending_case(case_id).exists()
    assert plan.layout.partial_case(case_id).exists()
    found = inspect_case(plan.layout, case_id)
    assert (found.status, found.manifest.state) == (STATUS_VALID, STATE_PLANNED)
    assert found.is_recoverable is True


# ── operator commands ────────────────────────────────────────────


def _published(tmp_path, monkeypatch, method=METHOD_RENAME):
    """A case staged and published, ready for an operator decision."""
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, method)
    execute_transfer(plan)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    return staging, src, layout, case_id


def test_a_report_carries_the_digest_the_acting_commands_demand(tmp_path,
                                                                monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    report = show_case(layout, case_id)

    assert report.payload_verified is True
    assert report.record_digest == report.manifest.record_digest
    assert report.record_digest in "\n".join(report.describe())

    outcome = promote_case(layout, case_id,
                           expected_record_digest=report.record_digest,
                           destination_root=tmp_path / "library")
    assert outcome.acted is True


def test_a_report_verifies_the_payload_rather_than_believing_the_manifest(
        tmp_path, monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    (layout.payload(case_id) / "src" / "ch01.cbz").write_bytes(b"tampered")

    report = show_case(layout, case_id)
    assert report.payload_verified is False
    assert "FAILED" in "\n".join(report.describe())


def test_showing_a_case_changes_nothing(tmp_path, monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    before = _snapshot(layout.root)
    show_case(layout, case_id)
    assert _snapshot(layout.root) == before


def test_the_record_digest_moves_when_anything_about_the_case_does(tmp_path,
                                                                   monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    first = show_case(layout, case_id).record_digest

    manifest = CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8"))
    manifest.decision_reason = "an operator note added after the report"
    manifest.write(layout.manifest(case_id))

    assert show_case(layout, case_id).record_digest != first


def test_promote_moves_the_payload_and_records_the_decision(tmp_path,
                                                            monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    library = tmp_path / "library"
    digest = show_case(layout, case_id).record_digest

    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=library)

    assert (outcome.acted, outcome.action) == (True, ACTION_PROMOTE)
    assert {p.relative_to(library / "src").as_posix(): p.read_bytes()
            for p in (library / "src").rglob("*") if p.is_file()} == DEFAULT
    assert CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8")).state \
        == STATE_PROMOTED


@pytest.mark.parametrize("action", [ACTION_PROMOTE, ACTION_REJECT,
                                    ACTION_ROLLBACK])
def test_a_stale_record_digest_refuses_every_acting_command(tmp_path,
                                                            monkeypatch,
                                                            action):
    """The digest is what makes the decision about the case that was reviewed."""
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    stale = show_case(layout, case_id).record_digest

    moved = CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8"))
    moved.decision_reason = "changed after the operator looked"
    moved.write(layout.manifest(case_id))

    before = _snapshot(layout.root)
    if action == ACTION_PROMOTE:
        outcome = promote_case(layout, case_id, expected_record_digest=stale,
                               destination_root=tmp_path / "library")
    elif action == ACTION_REJECT:
        outcome = reject_case(layout, case_id, expected_record_digest=stale)
    else:
        outcome = rollback_case(layout, case_id, expected_record_digest=stale)

    assert outcome.acted is False
    assert "has changed since it was reported" in outcome.detail
    assert _snapshot(layout.root) == before


def test_promote_refuses_an_occupied_destination(tmp_path, monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    library = tmp_path / "library"
    (library / "src").mkdir(parents=True)
    (library / "src" / "something_else.cbz").write_bytes(b"not this case")
    digest = show_case(layout, case_id).record_digest

    before = _snapshot(layout.root)
    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=library)

    assert outcome.acted is False
    assert "already exists" in outcome.detail
    assert (library / "src" / "something_else.cbz").exists()
    assert _snapshot(layout.root) == before


def test_promote_refuses_staged_content_that_does_not_match(tmp_path,
                                                            monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    digest = show_case(layout, case_id).record_digest
    (layout.payload(case_id) / "src" / "ch01.cbz").write_bytes(b"tampered")

    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=tmp_path / "library")

    assert outcome.acted is False
    assert "does not match its manifest" in outcome.detail
    assert not (tmp_path / "library").exists()


def test_reject_quarantines_rather_than_deletes(tmp_path, monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    digest = show_case(layout, case_id).record_digest

    outcome = reject_case(layout, case_id, expected_record_digest=digest)

    assert (outcome.acted, outcome.action) == (True, ACTION_REJECT)
    rejected = layout.rejected / case_id / "payload" / "src"
    assert {p.relative_to(rejected).as_posix(): p.read_bytes()
            for p in rejected.rglob("*") if p.is_file()} == DEFAULT
    assert not layout.pending_case(case_id).exists()
    assert CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8")).state \
        == STATE_REJECTED


def test_rollback_returns_the_payload_to_where_it_was_staged_from(tmp_path,
                                                                  monkeypatch):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch,
                                               METHOD_RENAME)
    assert not src.exists(), "precondition: the rename gave up the source"
    digest = show_case(layout, case_id).record_digest

    outcome = rollback_case(layout, case_id, expected_record_digest=digest)

    assert (outcome.acted, outcome.action) == (True, ACTION_ROLLBACK)
    assert {p.relative_to(src).as_posix(): p.read_bytes()
            for p in src.rglob("*") if p.is_file()} == DEFAULT
    assert CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8")).state \
        == STATE_ROLLED_BACK


def test_rollback_refuses_when_the_original_path_is_occupied(tmp_path,
                                                             monkeypatch):
    """#35's criterion. Something else being there means the world moved on."""
    staging, src, layout, case_id = _published(tmp_path, monkeypatch,
                                               METHOD_RENAME)
    src.mkdir(parents=True)
    (src / "a_different_download.cbz").write_bytes(b"arrived while in review")
    digest = show_case(layout, case_id).record_digest

    before_review, before_source = _snapshot(layout.root), _snapshot(src)
    outcome = rollback_case(layout, case_id, expected_record_digest=digest)

    assert outcome.acted is False
    assert "is occupied" in outcome.detail
    assert _snapshot(src) == before_source, "the occupant was overwritten"
    assert _snapshot(layout.root) == before_review


@pytest.mark.parametrize("action", [ACTION_PROMOTE, ACTION_REJECT,
                                    ACTION_ROLLBACK])
def test_a_terminal_case_refuses_every_acting_command(tmp_path, monkeypatch,
                                                      action):
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    manifest = CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8"))
    manifest.state = STATE_PROMOTED
    manifest.write(layout.manifest(case_id))
    digest = show_case(layout, case_id).record_digest

    if action == ACTION_PROMOTE:
        outcome = promote_case(layout, case_id, expected_record_digest=digest,
                               destination_root=tmp_path / "library")
    elif action == ACTION_REJECT:
        outcome = reject_case(layout, case_id, expected_record_digest=digest)
    else:
        outcome = rollback_case(layout, case_id, expected_record_digest=digest)

    assert outcome.acted is False
    assert f"the case is {STATE_PROMOTED}" in outcome.detail


def test_rollback_reports_cross_volume_unavailability_explicitly(tmp_path,
                                                                  monkeypatch):
    """The real gap: a copy case whose source was deliberately released.

    Structured rather than generic, because this is a missing capability
    rather than a transient obstacle, and an operator needs to see all three
    facts at once instead of inferring them.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch,
                                      METHOD_COPY_VERIFY)
    execute_transfer(plan, delete_copied_source=True)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")
    assert not src.exists(), "precondition: the source was released"
    digest = show_case(layout, case_id).record_digest

    before = _snapshot(layout.root)
    outcome = rollback_case(layout, case_id, expected_record_digest=digest)

    assert outcome.acted is False
    for line in ("rollback unavailable:",
                 "original source absent",
                 "destination is cross-volume",
                 "no verified cross-volume rollback protocol exists"):
        assert line in outcome.detail

    assert _snapshot(layout.root) == before, "the staged payload was disturbed"
    assert layout.pending_case(case_id).exists()
    assert not src.exists(), "rollback invented a destination"


@pytest.mark.parametrize("where", ["review_root", "staged_path", "source_path"])
def test_promote_refuses_a_destination_the_manifest_rules_out(tmp_path,
                                                              monkeypatch,
                                                              where):
    """Validates the caller's destination; never resolves one of its own."""
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    manifest = show_case(layout, case_id).manifest
    digest = manifest.record_digest

    roots = {
        "review_root": layout.pending,
        "staged_path": Path(manifest.staged_path),
        "source_path": Path(manifest.staging_source_path).parent,
    }
    before = _snapshot(layout.root)
    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=roots[where])

    assert outcome.acted is False
    assert _snapshot(layout.root) == before
    assert CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8")).state \
        == STATE_PENDING_REVIEW


def test_promote_does_not_resolve_the_decision_destination(tmp_path,
                                                           monkeypatch):
    """decision_dest_key is provenance, not an execution directive here.

    Resolving it would couple promotion to routing configuration before #31
    and v2 activation are ready. The field staying unused is the boundary
    holding, so this pins it.
    """
    staging, src, plan = _forced_plan(tmp_path, monkeypatch, METHOD_RENAME)
    plan.manifest.decision_dest_key = "manga"
    plan.manifest.touch()
    execute_transfer(plan)
    layout, case_id = _rebuilt_from_disk(tmp_path / "review")

    library = tmp_path / "explicitly_chosen_by_the_caller"
    digest = show_case(layout, case_id).record_digest
    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=library)

    assert outcome.acted is True
    assert (library / "src").is_dir(), "the caller's destination was not used"
    assert not (tmp_path / "manga").exists(), "dest_key was resolved to a path"
    assert CaseManifest.from_json(
        layout.manifest(case_id).read_text(encoding="utf-8")
    ).decision_dest_key == "manga", "provenance was not preserved"


def test_a_cross_volume_promotion_is_refused_rather_than_faked(tmp_path,
                                                               monkeypatch):
    """A cross-volume move is a copy plus a delete, with no safe moment."""
    staging, src, layout, case_id = _published(tmp_path, monkeypatch)
    digest = show_case(layout, case_id).record_digest
    monkeypatch.setattr(staging, "same_volume", lambda a, b: False)

    outcome = promote_case(layout, case_id, expected_record_digest=digest,
                           destination_root=tmp_path / "library")

    assert outcome.acted is False
    assert "different volume" in outcome.detail
    assert layout.pending_case(case_id).exists(), "the payload was given up"


def test_the_plan_description_names_the_case_and_method(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    text = "\n".join(plan_case(tmp_path / "review", src, src, "Some Series",
                               series_key("Some Series"), "src").describe())
    assert "Some Series" in text
    assert "2 file(s)" not in text          # three files including extra/notes
    assert "3 file(s)" in text
    assert METHOD_RENAME in text or METHOD_COPY_VERIFY in text
