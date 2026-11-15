"""Local maintenance state never becomes migration payload or sync evidence."""
import os

import pytest

from comic_automation.library import sync_config as config
from comic_automation.library import sync_inventory as inventory
from comic_automation.library.sync_preview import build_preview
from comic_automation.library.sync_verify import verify_pairs
from test_sync_config import capture_for
from test_sync_inventory import put
from test_sync_preview import trees  # noqa: F401


NAMES = (".cbz-canonical-naming", ".cbz-canonical-quarantine")


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("parent", ("", "Book/"))
@pytest.mark.parametrize("uppercase", (False, True))
def test_maintenance_boundaries_are_not_walked_or_hashed(trees, monkeypatch, name, parent, uppercase):
    pair = trees({"Book/1.cbz": b"book"}, {"Book/1.cbz": b"book"})
    relative = parent + (name.upper() if uppercase else name)
    for root in (pair.tower, pair.target):
        put(root, relative + "/run/old.cbz", b"private original")
        # Similar names are ordinary content; the exclusion is a whole component.
        put(root, relative + "-extra/visible.cbz", b"visible")
    real_scan = os.scandir
    real_hash = inventory._hash
    reads = []

    def scan(path):
        assert not any(part.casefold() in NAMES for part in path.parts)
        return real_scan(path)

    def hashed(root, row, **kwargs):
        reads.append(row["relative_path"])
        return real_hash(root, row, **kwargs)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(inventory, "_hash", hashed)
    observed = inventory.observe_pairs((pair,), hash_all_tower=True)
    assert observed["complete"] and not observed["problems"]
    assert {row["relative_path"] for row in observed["files"]} == {
        "Book/1.cbz", relative + "-extra/visible.cbz"}
    assert len(reads) == 4 and all("/run/" not in path for path in reads)
    assert {(row["side"], row["path"]) for row in observed["excluded"]} == {
        ("tower", relative), ("target", relative)}


@pytest.mark.parametrize("name", NAMES)
def test_local_maintenance_changes_do_not_become_preview_or_verifier_differences(trees, name):
    pair = trees({"Book/1.cbz": b"book"}, {"Book/1.cbz": b"book"})
    expected = inventory.observe_pairs((pair,), hash_all_tower=True)
    put(pair.tower, "Book/" + name + "/source-only.cbz", b"source private")
    put(pair.target, name + "/target-only.cbz", b"target private")
    actual = inventory.observe_pairs((pair,), hash_all_tower=True)
    preview = build_preview(actual)
    assert preview["complete"] and not preview["to_write"] and not preview["to_remove"]
    result = verify_pairs(expected, (pair,))
    assert result["verified"] and not result["source_drift"] and not result["extra"]
    assert not result["sync_ready"]


@pytest.mark.parametrize("name", NAMES)
def test_configuration_evidence_requires_maintenance_ignore_rules(trees, name):
    pair = trees({"Book/1.cbz": b"book"}, {"Book/1.cbz": b"book"})
    observed = inventory.observe_pairs((pair,), hash_all_tower=True)
    assert name in observed["exclusion_policy"]["component_names"]
    required = config.required_expanded_patterns(observed)
    assert {name, name + "/**", "**/" + name, "**/" + name + "/**"} <= required
    capture = capture_for(observed)
    assert config.bind_configuration(observed, capture)["scope_bound"]
    for phase in capture["nodes"]["tower"].values():
        phase["ignores"]["manga"]["expanded"] = [
            pattern for pattern in phase["ignores"]["manga"]["expanded"] if name not in pattern]
    result = config.bind_configuration(observed, capture)
    assert not result["scope_bound"] and result["scope"] == "filesystem-only"


def test_old_policy_baseline_cannot_verify_under_the_new_policy(trees, monkeypatch):
    pair = trees({"Book/1.cbz": b"book"}, {"Book/1.cbz": b"book"})
    current = inventory._EXCLUDED
    with monkeypatch.context() as old:
        old.setattr(inventory, "_EXCLUDED", current - set(NAMES))
        expected = inventory.observe_pairs((pair,), hash_all_tower=True)
    actual = inventory.observe_pairs((pair,), hash_all_tower=True)
    assert expected["files"] == actual["files"]
    assert expected["observation_digest"] != actual["observation_digest"]
    result = verify_pairs(expected, (pair,))
    assert not result["scope_matches"] and not result["verified"]
    assert result["unresolved"] and not result["missing"] and not result["extra"]
