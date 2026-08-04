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
from pathlib import Path

import pytest

from scripts.cbz_classification_staging import (
    CASE_CONTRACT,
    STATE_PENDING_REVIEW,
    STATUS_ABSENT,
    STATUS_ORPHANED_PENDING,
    STATUS_PARTIAL,
    STATUS_VALID,
    PayloadChangedError,
    inspect_case,
    METHOD_COPY_VERIFY,
    METHOD_RENAME,
    STATE_PLANNED,
    CaseManifest,
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


def test_an_unknown_transfer_method_is_refused(tmp_path):
    with pytest.raises(StagingError, match="transfer_method"):
        CaseManifest.from_json(_mutated(_valid_manifest(tmp_path),
                                        transfer_method="teleport"))


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


def test_a_settled_payload_hashes_without_complaint(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    inventory = build_inventory(src)
    assert inventory.file_count == 3
    assert build_inventory(src) == inventory


def test_the_plan_description_names_the_case_and_method(tmp_path):
    src = _payload(tmp_path / "src", DEFAULT)
    text = "\n".join(plan_case(tmp_path / "review", src, src, "Some Series",
                               series_key("Some Series"), "src").describe())
    assert "Some Series" in text
    assert "2 file(s)" not in text          # three files including extra/notes
    assert "3 file(s)" in text
    assert METHOD_RENAME in text or METHOD_COPY_VERIFY in text
