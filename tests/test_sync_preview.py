"""Logical file accounting from real temporary observations (#108)."""
import copy
import hashlib
import json
from pathlib import Path
import os

import pytest

from comic_automation.library import sync_inventory as inv
from comic_automation.library import sync_observe_cli as cli
from comic_automation.library.sync_preview import PreviewRefused, build_preview


@pytest.fixture
def trees(tmp_path):
    def create(tower, target):
        roots = [tmp_path / 'tower', tmp_path / 'target']
        for root, files in zip(roots, (tower, target)):
            root.mkdir()
            for name, payload in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
        return inv.RootPair('manga', *roots)
    return create


def reseal(doc):
    doc['observation_digest'] = hashlib.sha256(json.dumps(
        {k: v for k, v in doc.items() if k != 'observation_digest'}, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return doc


def test_equal_bytes_at_the_same_path_are_keep_with_metadata_separate(trees):
    pair = trees({'Book/a.cbz': b'abc'}, {'Book/a.cbz': b'abc'})
    os.utime(pair.target / 'Book/a.cbz', ns=(1_000_000_000, 1_000_000_000))
    observation = inv.observe_pairs((pair,))
    untouched = copy.deepcopy(observation)
    preview = build_preview(observation)
    assert preview['complete'] and preview['reconciled'] and not preview['sync_ready']
    assert len(preview['keep_in_place']) == 1
    assert not preview['to_write'] and not preview['to_remove'] and not preview['unresolved']
    assert preview['metadata_differences'][0]['fields'] == ['mtime_ns']
    assert preview['counts']['tower']['keep'] == {'files': 1, 'bytes': 3}
    assert preview['counts']['target']['keep'] == {'files': 1, 'bytes': 3}
    assert observation == untouched


def test_new_and_obsolete_unique_sizes_need_no_payload_reads(trees, monkeypatch):
    pair = trees({'new.cbz': b'new'}, {'old.cbz': b'old-longer'})
    monkeypatch.setattr(inv, '_hash', lambda *a, **k: pytest.fail('unshared size payload read'))
    report = build_preview(inv.observe_pairs((pair,)))
    assert report['complete'] and len(report['to_write']) == len(report['to_remove']) == 1
    assert report['to_write'][0]['kind'] == 'new_path' and report['to_write'][0]['old_target'] is None
    assert report['counts']['tower']['write'] == {'files': 1, 'bytes': 3}
    assert report['counts']['target']['remove'] == {'files': 1, 'bytes': 10}
    assert report['exact_matches_elsewhere'] == []


@pytest.mark.parametrize('target', [b'longer', b'xyz'])
def test_replacements_are_not_also_removals(trees, target):
    pair = trees({'a.cbz': b'abc'}, {'a.cbz': target})
    report = build_preview(inv.observe_pairs((pair,)))
    assert report['complete'] and not report['keep_in_place'] and not report['to_remove']
    assert report['to_write'][0]['kind'] == 'replacement'
    assert report['to_write'][0]['old_target']['size'] == len(target)
    assert report['counts']['target']['replaced'] == {'files': 1, 'bytes': len(target)}
    assert report['counts']['tower']['write'] == {'files': 1, 'bytes': 3}


def test_many_to_many_relocated_bytes_are_advisory_not_same_path_keeps(trees):
    pair = trees({'a.cbz': b'x', 'b.cbz': b'x'}, {'c.cbz': b'x', 'd.cbz': b'x'})
    report = build_preview(inv.observe_pairs((pair,)))
    assert report['complete'] and not report['keep_in_place']
    assert len(report['to_write']) == len(report['to_remove']) == 2
    assert report['counts']['tower']['write']['files'] == report['counts']['target']['remove']['files'] == 2
    assert len(report['exact_matches_elsewhere']) == 1
    group = report['exact_matches_elsewhere'][0]
    assert [r['relative_path'] for r in group['tower']] == ['a.cbz', 'b.cbz']
    assert [r['relative_path'] for r in group['target']] == ['c.cbz', 'd.cbz']


def test_exact_group_can_cross_folder_ids_without_changing_path_counts(tmp_path):
    pairs = []
    for name in ('manga', 'comix'):
        tower, target = tmp_path / (name+'-tower'), tmp_path / (name+'-target')
        tower.mkdir(); target.mkdir()
        pairs.append(inv.RootPair(name, tower, target))
    (pairs[0].tower / 'a.cbz').write_bytes(b'x')
    (pairs[1].target / 'a.cbz').write_bytes(b'x')
    report = build_preview(inv.observe_pairs(tuple(pairs)))
    assert report['complete'] and not report['keep_in_place']
    assert report['to_write'][0]['tower']['folder_id'] == 'manga'
    assert report['to_remove'][0]['target']['folder_id'] == 'comix'
    assert len(report['exact_matches_elsewhere']) == 1


@pytest.mark.parametrize('source,target', [('Book.cbz', 'book.cbz'), ('é.cbz', 'e\u0301.cbz')])
def test_cross_side_windows_spelling_conflicts_are_unresolved(trees, source, target):
    pair = trees({source: b'x'}, {target: b'x'})
    report = build_preview(inv.observe_pairs((pair,)))
    assert not report['complete'] and report['reconciled']
    assert len(report['unresolved']) == 2
    assert not report['keep_in_place'] and not report['to_write'] and not report['to_remove']
    assert report['counts']['tower']['unresolved']['files'] == report['counts']['target']['unresolved']['files'] == 1


def test_incomplete_hash_coverage_never_becomes_path_certainty(trees, monkeypatch):
    pair = trees({'a.cbz': b'x'}, {'b.cbz': b'x'})
    def denied(*a, **k):
        raise PermissionError('fixture payload refusal')
    monkeypatch.setattr(inv, '_hash', denied)
    report = build_preview(inv.observe_pairs((pair,)))
    assert not report['complete'] and report['reconciled'] and len(report['unresolved']) == 2
    assert not report['to_write'] and not report['to_remove'] and not report['exact_matches_elsewhere']


def test_missing_root_is_unresolved_even_when_no_files_were_observed(trees):
    pair = trees({}, {})
    pair.target.rmdir()
    report = build_preview(inv.observe_pairs((pair,)))
    assert not report['complete'] and report['observation']['problems']
    assert report['counts']['tower']['observed']['files'] == 0


def test_equal_size_without_hash_evidence_is_not_a_keep(trees):
    pair = trees({'a.cbz': b'x'}, {'a.cbz': b'x'})
    doc = inv.observe_pairs((pair,))
    doc['files'][0]['sha256'] = None
    report = build_preview(reseal(doc))
    assert not report['complete'] and len(report['unresolved']) == 2
    assert not report['keep_in_place'] and not report['to_write']


@pytest.mark.parametrize('flag,problems', [(False, []), (True, [{'reason': 'reported gap'}])])
def test_coverage_flag_and_reported_gaps_each_prevent_certainty(trees, flag, problems):
    pair = trees({'a.cbz': b'x'}, {'a.cbz': b'x'})
    doc = inv.observe_pairs((pair,))
    doc['complete'], doc['problems'] = flag, problems
    report = build_preview(reseal(doc))
    assert not report['complete'] and len(report['unresolved']) == 2


def test_estimates_and_tampered_artifacts_are_refused(trees):
    pair = trees({'a.cbz': b'x'}, {'a.cbz': b'x'})
    with pytest.raises(PreviewRefused, match='content-mode'):
        build_preview(inv.observe_pairs((pair,), hash_content=False))
    doc = inv.observe_pairs((pair,))
    doc['files'][0]['size'] += 1
    with pytest.raises(PreviewRefused, match='digest'):
        build_preview(doc)


@pytest.mark.parametrize('bad', ['negative', 'boolean', 'absolute', 'parent', 'backslash', 'duplicate',
                                'unbound', 'short_hash', 'file_type', 'root_type', 'collection', 'scope',
                                'read_count', 'read_boolean', 'counts', 'duplicate_root', 'unpaired_root', 'missing_hash'])
def test_resealed_malformed_documents_cannot_supply_assertions(trees, bad):
    pair = trees({'a.cbz': b'x'}, {'a.cbz': b'x'})
    doc = inv.observe_pairs((pair,))
    if bad == 'negative': doc['files'][0]['size'] = -1
    elif bad == 'boolean': doc['files'][0]['size'] = True
    elif bad == 'absolute': doc['files'][0]['relative_path'] = '/a.cbz'
    elif bad == 'parent': doc['files'][0]['relative_path'] = '../a.cbz'
    elif bad == 'backslash': doc['files'][0]['relative_path'] = 'a\\b.cbz'
    elif bad == 'duplicate': doc['files'].append(copy.deepcopy(doc['files'][0]))
    elif bad == 'unbound': doc['files'][0]['folder_id'] = 'other'
    elif bad == 'short_hash': doc['files'][0]['sha256'] = '1234'
    elif bad == 'file_type': doc['files'][0] = None
    elif bad == 'root_type': doc['roots'][0] = None
    elif bad == 'collection': doc['excluded'] = None
    elif bad == 'read_count': doc['files'][0]['bytes_read'] = 2
    elif bad == 'read_boolean': doc['files'][0]['bytes_read'] = True
    elif bad == 'missing_hash': doc['files'][0].pop('sha256')
    elif bad == 'counts': doc['file_counts']['tower'] = 9
    elif bad == 'duplicate_root': doc['roots'].append(copy.deepcopy(doc['roots'][0]))
    elif bad == 'unpaired_root':
        doc['roots'] = [r for r in doc['roots'] if r['side'] == 'tower']
        doc['files'] = [r for r in doc['files'] if r['side'] == 'tower']
        doc['file_counts'] = {'tower': 1}
    else: doc['scope'] = 'syncthing'
    with pytest.raises(PreviewRefused): build_preview(reseal(doc))


def test_output_binds_evidence_and_is_deterministic_without_mutating_input(trees):
    pair = trees({'a.cbz': b'x'}, {'a.cbz': b'x'})
    doc = inv.observe_pairs((pair,))
    doc['files'].reverse()
    reseal(doc)
    original = copy.deepcopy(doc)
    first = build_preview(doc)
    assert first == build_preview(doc) and doc == original
    assert first['observation'] == doc
    assert first['preview_digest'] == hashlib.sha256(json.dumps(
        {k:v for k,v in first.items() if k != 'preview_digest'}, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    doc['files'][0]['mtime_ns'] += 1
    assert first['preview_digest'] != build_preview(reseal(doc))['preview_digest']


def test_protected_originals_are_explicitly_excluded_and_preserved(trees):
    pair = trees({'ai-decensor-originals/a.cbz': b'original'}, {'old.cbz': b'x'})
    report = build_preview(inv.observe_pairs((pair,)))
    assert report['complete'] and report['counts']['tower']['observed']['files'] == 0
    assert report['observation']['excluded']
    assert (pair.tower/'ai-decensor-originals/a.cbz').read_bytes() == b'original'


def manifest(pair, tmp_path):
    path = tmp_path/'pairs.json'
    path.write_text(json.dumps({'contract':'sync-root-pairs-v1','pairs':[
        {'folder_id':'manga','tower':str(pair.tower),'target':str(pair.target)}]}))
    return path


def test_cli_preview_emits_a_complete_filesystem_delta_not_sync_readiness(trees, tmp_path, capsys):
    pair = trees({'new.cbz': b'x'}, {'old.cbz': b'x'})
    path = manifest(pair, tmp_path)
    assert cli.main(['preview', '--pairs', str(path), '--quiet']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['contract'] == 'sync-filesystem-preview-v1'
    assert report['complete'] and not report['sync_ready'] and not report['keep_in_place']


def test_cli_incomplete_preview_is_failure(trees, tmp_path, capsys):
    pair = trees({'Book.cbz': b'x'}, {'book.cbz': b'x'})
    assert cli.main(['preview', '--pairs', str(manifest(pair, tmp_path)), '--quiet']) == 2
    assert not json.loads(capsys.readouterr().out)['complete']


@pytest.mark.parametrize('change', ['pair_secret', 'root_relative', 'forward_root_relative'])
def test_manifest_followups_refuse_unknown_pair_fields_and_ambiguous_roots(trees, tmp_path, capsys, change):
    pair = trees({}, {})
    path = manifest(pair, tmp_path)
    doc = json.loads(path.read_text())
    if change == 'pair_secret': doc['pairs'][0]['api_key'] = 'NEVER_ECHO_PAIR_SECRET'
    else: doc['pairs'][0]['tower'] = '\\comics' if change == 'root_relative' else '/comics'
    path.write_text(json.dumps(doc))
    assert cli.main(['preview','--pairs',str(path),'--quiet']) == 2
    captured = capsys.readouterr()
    assert not captured.out and 'NEVER_ECHO_PAIR_SECRET' not in captured.err
