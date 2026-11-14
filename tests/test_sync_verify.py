"""Fresh private-tree verification, full baseline reads and scope/drift guards."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from comic_automation.library import sync_inventory as inv
from comic_automation.library import sync_observe_cli as cli
from comic_automation.library import sync_verify as verify
from test_sync_preview import trees, reseal, manifest  # noqa: F401


def baseline(pair):
    return inv.observe_pairs((pair,), hash_all_tower=True)


def test_full_tower_mode_reads_each_required_file_once_and_binds_its_policy(trees, monkeypatch):
    pair = trees({'new.cbz': b'unique', 'same.cbz': b'abc'}, {'same.cbz': b'abc', 'extra.cbz': b'extra-long'})
    reads = []
    real_hash = inv._hash
    def tracked(root, row, **kwargs):
        reads.append((str(root), row['relative_path']))
        return real_hash(root, row, **kwargs)
    monkeypatch.setattr(inv, '_hash', tracked)
    doc = baseline(pair)
    assert doc['complete'] and doc['hash_all_tower']
    assert len(reads) == len(set(reads)) == 3
    assert doc['hash_plan']['candidate_files'] == 3 and doc['hash_plan']['candidate_bytes'] == 12
    assert all(row['sha256'] for row in doc['files'] if row['side'] == 'tower')
    assert next(row for row in doc['files'] if row['relative_path']=='extra.cbz')['sha256'] is None


def test_full_tower_estimate_budgets_baseline_without_payload_reads(trees, monkeypatch, tmp_path, capsys):
    pair = trees({'new.cbz': b'abc'}, {})
    monkeypatch.setattr(inv, '_hash', lambda *a, **k: pytest.fail('estimate opened payload'))
    doc = inv.observe_pairs((pair,), hash_content=False, hash_all_tower=True)
    assert doc['hash_plan']['candidate_bytes'] == 3 and doc['files'][0]['sha256'] is None
    assert cli.main(['estimate','--all-tower','--quiet','--pairs',str(manifest(pair,tmp_path))]) == 0
    assert json.loads(capsys.readouterr().out)['hash_plan']['candidate_bytes'] == 3


@pytest.mark.parametrize('bad', [None, 0, 'yes'])
def test_full_tower_flag_is_boolean_only(trees, bad):
    pair = trees({}, {})
    with pytest.raises(ValueError, match='hash_all_tower'):
        inv.observe_pairs((pair,), hash_all_tower=bad)


def test_baseline_policy_is_digest_bound_even_when_candidates_are_identical(trees):
    pair = trees({'a.cbz': b'x'}, {'a.cbz': b'x'})
    default, full = inv.observe_pairs((pair,)), baseline(pair)
    assert default['files'] == full['files'] and default['hash_plan'] == full['hash_plan']
    assert default['observation_digest'] != full['observation_digest']
    assert verify.verify_pairs(default,(pair,))['verified']


def test_verification_reads_fresh_and_ignores_old_receiver_layout(trees, monkeypatch):
    pair = trees({'a.cbz': b'abc'}, {'old.cbz': b'longer'})
    expected = baseline(pair)
    original = copy.deepcopy(expected)
    (pair.target/'old.cbz').unlink()
    (pair.target/'a.cbz').write_bytes(b'abc')
    real_observe, calls = inv.observe_pairs, []
    def fresh(*args, **kwargs):
        calls.append(kwargs)
        return real_observe(*args, **kwargs)
    monkeypatch.setattr(inv, 'observe_pairs', fresh)
    result = verify.verify_pairs(expected, (pair,))
    assert len(calls)==1 and calls[0]['hash_all_tower'] is True
    assert result['verified'] and result['reconciled'] and not result['sync_ready']
    assert result['counts']['expected_tower']['matched'] == result['counts']['target']['matched'] == {'files':1,'bytes':3}
    assert result['counts']['current_tower']['unchanged']['files'] == 1
    assert expected == original and result['expected'] == expected
    assert result['actual']['observation_digest'] != expected['observation_digest']


@pytest.mark.parametrize('change', ['missing', 'same_size', 'different_size', 'extra', 'wrong_path', 'case_path'])
def test_target_path_and_content_differences_fail_even_with_equal_global_bytes(trees, change):
    pair = trees({'a.cbz': b'abc'}, {'a.cbz': b'abc'})
    expected = baseline(pair)
    path = pair.target/'a.cbz'
    if change=='missing': path.unlink()
    elif change=='same_size': path.write_bytes(b'xyz')
    elif change=='different_size': path.write_bytes(b'abcdef')
    elif change=='extra': (pair.target/'extra.cbz').write_bytes(b'x')
    else:
        path.rename(pair.target/'temporary')
        (pair.target/'temporary').rename(pair.target/('b.cbz' if change=='wrong_path' else 'A.cbz'))
    result = verify.verify_pairs(expected,(pair,))
    assert not result['verified'] and result['reconciled'] and not result['source_drift']
    if change in ('same_size','different_size'):
        assert len(result['mismatched'])==1 and not result['missing'] and not result['extra']
        assert result['counts']['target']['mismatched']['bytes'] == path.stat().st_size
    elif change in ('wrong_path','case_path'):
        assert len(result['missing'])==len(result['extra'])==1
    elif change=='missing': assert len(result['missing'])==1
    else: assert len(result['extra'])==1 and len(result['matched'])==1


@pytest.mark.parametrize('change', ['added','removed','same_size','different_size','renamed','case_path'])
def test_current_source_drift_prevents_a_stale_expected_pass(trees, change):
    pair = trees({'a.cbz': b'abc'}, {'a.cbz': b'abc'})
    expected = baseline(pair)
    path = pair.tower/'a.cbz'
    if change=='added': (pair.tower/'new.cbz').write_bytes(b'x')
    elif change=='removed': path.unlink()
    elif change=='same_size': path.write_bytes(b'xyz')
    elif change=='different_size': path.write_bytes(b'abcdef')
    else:
        path.rename(pair.tower/'temporary')
        (pair.tower/'temporary').rename(pair.tower/('b.cbz' if change=='renamed' else 'A.cbz'))
    result = verify.verify_pairs(expected,(pair,))
    assert not result['verified'] and result['reconciled']
    kinds = {row['kind'] for row in result['source_drift']}
    assert kinds == ({'changed'} if change in ('same_size','different_size') else
                     {'added','removed'} if change in ('renamed','case_path') else {change})
    if change in ('removed','different_size'):
        assert not result['mismatched'] and any(row['side']=='target' for row in result['unresolved'])


def test_equal_strong_bytes_with_metadata_changes_are_diagnostic(trees):
    pair = trees({'a.cbz': b'abc'}, {'a.cbz': b'abc'})
    expected = baseline(pair)
    os.utime(pair.tower/'a.cbz',ns=(1_000_000_000,1_000_000_000))
    os.utime(pair.target/'a.cbz',ns=(2_000_000_000,2_000_000_000))
    result = verify.verify_pairs(expected,(pair,))
    assert result['verified'] and not result['source_drift']
    assert result['source_metadata_differences'] and result['target_metadata_differences']


@pytest.mark.parametrize('change', ['prefix','target_identity','tower_identity'])
def test_scope_changes_are_unresolved_never_missing_or_extra_certainty(trees, change):
    pair = trees({'a.cbz': b'abc'}, {'a.cbz': b'abc'})
    expected = baseline(pair)
    if change=='prefix': pair=inv.RootPair(pair.folder_id,pair.tower,pair.target,('private',))
    else:
        root=pair.target if change=='target_identity' else pair.tower
        root.rename(root.with_name(root.name+'-old'))
        root.mkdir(); (root/'a.cbz').write_bytes(b'abc')
    result=verify.verify_pairs(expected,(pair,))
    assert not result['verified'] and not result['scope_matches']
    assert len(result['unresolved'])==3 and not result['missing'] and not result['extra'] and not result['source_drift']


@pytest.mark.parametrize('change',['no_hash','estimate','incomplete','tamper','bad_root','bad_policy','directory_spellings'])
def test_unusable_expected_baselines_refuse_before_fresh_payload_reads(trees, monkeypatch, change):
    pair=trees({'a.cbz':b'abc'}, {})
    expected=inv.observe_pairs((pair,)) if change=='no_hash' else baseline(pair)
    if change=='estimate': expected=inv.observe_pairs((pair,),hash_content=False)
    elif change=='incomplete': expected['complete']=False; reseal(expected)
    elif change=='tamper': expected['files'][0]['mtime_ns']+=1
    elif change=='bad_root': expected['roots'][0].pop('identity'); reseal(expected)
    elif change=='bad_policy': expected['exclusion_policy']={}; reseal(expected)
    elif change=='directory_spellings':
        row=expected['files'][0]; row['relative_path']='Series/a.cbz'
        extra=copy.deepcopy(row); extra['relative_path']='series/b.cbz'; extra['inode']+=1
        expected['files'].append(extra); expected['file_counts']['tower']=2
        expected['hash_plan']['candidate_files']=2; expected['hash_plan']['candidate_bytes']=6
        expected['hash_plan']['by_side']['tower']={'files':2,'bytes':6}
        reseal(expected)
    monkeypatch.setattr(inv,'observe_pairs',lambda *a,**k:pytest.fail('invalid baseline reached actual scan'))
    with pytest.raises(verify.VerificationRefused): verify.verify_pairs(expected,(pair,))


@pytest.mark.parametrize('change',['hash_failure','missing_root'])
def test_incomplete_actual_coverage_supplies_no_negative_facts(trees, monkeypatch, change):
    pair=trees({'a.cbz':b'abc'},{'a.cbz':b'abc'})
    expected=baseline(pair)
    if change=='hash_failure':
        def denied(*a,**k): raise PermissionError('fixture hash failure')
        monkeypatch.setattr(inv,'_hash',denied)
    else:
        (pair.target/'a.cbz').unlink(); pair.target.rmdir()
    result=verify.verify_pairs(expected,(pair,))
    assert not result['verified'] and not result['coverage']['actual'] and result['unresolved']
    assert not result['missing'] and not result['mismatched'] and not result['extra'] and not result['source_drift']


def test_excluded_originals_and_empty_directories_stay_outside_file_assertions(trees):
    pair=trees({'a.cbz':b'abc','ai-decensor-originals/x.cbz':b'original'},{'a.cbz':b'abc'})
    expected=baseline(pair)
    (pair.target/'ai-decensor-originals').mkdir(); (pair.target/'ai-decensor-originals/x.cbz').write_bytes(b'kept')
    (pair.target/'empty').mkdir()
    result=verify.verify_pairs(expected,(pair,))
    assert result['verified'] and not result['extra'] and result['excluded']['actual']
    assert (pair.target/'ai-decensor-originals/x.cbz').read_bytes()==b'kept'
    assert result['counts']['target']['observed']['files']==1


def test_result_digest_and_final_progress_are_independent_and_deterministic(trees):
    pair=trees({'a.cbz':b'abc'},{'a.cbz':b'abc'})
    expected=baseline(pair)
    events=[]
    def consumer(event):
        events.append(event['phase'])
        if event['phase']=='verification': event['counts'].clear()
    first=verify.verify_pairs(expected,(pair,),progress=consumer)
    second=verify.verify_pairs(expected,(pair,))
    assert first==second and events[-1]=='verification'
    assert first['verification_digest']==verify._digest(first,'verification_digest')
    assert first['counts']['target']['matched']['files']==1


def test_verification_progress_failure_propagates(trees):
    pair=trees({'a.cbz':b'x'},{'a.cbz':b'x'})
    expected=baseline(pair)
    def broken(event):
        if event['phase']=='verification': raise OSError('consumer failed')
    with pytest.raises(inv.ProgressError): verify.verify_pairs(expected,(pair,),progress=broken)


def test_real_cli_baseline_then_fresh_verify_is_read_only(trees,tmp_path):
    pair=trees({'new.cbz':b'abc'},{})
    path=manifest(pair,tmp_path)
    command=[sys.executable,'-m','comic_automation.library.sync_observe_cli']
    first=subprocess.run([*command,'baseline','--pairs',str(path),'--quiet'],capture_output=True,text=True,timeout=20)
    assert first.returncode==0 and json.loads(first.stdout)['files'][0]['sha256']
    expected_path=tmp_path/'baseline.json'; expected_path.write_text(first.stdout)
    (pair.target/'new.cbz').write_bytes(b'abc')
    before={str(p):p.read_bytes() for root in (pair.tower,pair.target) for p in root.rglob('*') if p.is_file()}
    checked=subprocess.run([*command,'verify','--pairs',str(path),'--expected',str(expected_path),'--quiet'],capture_output=True,text=True,timeout=20)
    assert checked.returncode==0 and json.loads(checked.stdout)['verified']
    assert before=={str(p):p.read_bytes() for root in (pair.tower,pair.target) for p in root.rglob('*') if p.is_file()}
    (pair.target/'new.cbz').write_bytes(b'xyz')
    failed=subprocess.run([*command,'verify','--pairs',str(path),'--expected',str(expected_path),'--quiet'],capture_output=True,text=True,timeout=20)
    assert failed.returncode==2 and json.loads(failed.stdout)['mismatched']


def test_cli_expected_argument_is_required_only_for_verify(trees,tmp_path):
    pair=trees({},{}); path=manifest(pair,tmp_path)
    for args in (['verify'],['observe','--expected','unused.json']):
        with pytest.raises(SystemExit) as error: cli.main([*args,'--pairs',str(path)])
        assert error.value.code==2


def test_a_changed_builtin_exclusion_policy_requires_a_new_baseline(trees):
    pair=trees({'a.cbz':b'x'},{'a.cbz':b'x'})
    expected=baseline(pair)
    expected['exclusion_policy']['component_names'].append('older-policy-name')
    result=verify.verify_pairs(reseal(expected),(pair,))
    assert not result['verified'] and not result['scope_matches'] and len(result['unresolved'])==3


def test_empty_desired_layout_still_checks_for_actual_extras(trees):
    pair=trees({}, {'old.cbz':b'x'})
    expected=baseline(pair)
    first=verify.verify_pairs(expected,(pair,))
    assert not first['verified'] and len(first['extra'])==1
    (pair.target/'old.cbz').unlink()
    assert verify.verify_pairs(expected,(pair,))['verified']


def test_missing_current_source_hash_is_unknown_not_a_claimed_byte_change(trees,monkeypatch):
    pair=trees({'a.cbz':b'abc'}, {})
    expected=baseline(pair)
    real_observe=inv.observe_pairs
    def without_full_hashes(*args,**kwargs):
        kwargs['hash_all_tower']=False
        return real_observe(*args,**kwargs)
    monkeypatch.setattr(inv,'observe_pairs',without_full_hashes)
    result=verify.verify_pairs(expected,(pair,))
    assert not result['verified'] and not result['source_drift']
    assert result['counts']['current_tower']['unresolved']['files']==1


def test_empty_but_unreadable_actual_scan_never_verifies(trees,monkeypatch):
    pair=trees({},{}); expected=baseline(pair)
    monkeypatch.setattr(inv,'_walk',lambda *a,**k:([],[],[{'path':'.','reason':'fixture denied'}]))
    result=verify.verify_pairs(expected,(pair,))
    assert not result['verified'] and not result['coverage']['actual'] and result['scope_matches']


def test_empty_trees_with_changed_scope_do_not_verify(trees):
    pair=trees({},{}); expected=baseline(pair)
    changed=inv.RootPair(pair.folder_id,pair.tower,pair.target,('private',))
    result=verify.verify_pairs(expected,(changed,))
    assert not result['verified'] and not result['scope_matches'] and result['coverage']['actual']


def test_corrupt_actual_producer_document_is_refused(trees,monkeypatch):
    pair=trees({'a.cbz':b'x'},{'a.cbz':b'x'}); expected=baseline(pair)
    real_observe=inv.observe_pairs
    def corrupt(*args,**kwargs):
        doc=real_observe(*args,**kwargs); doc['files'][0]['mtime_ns']+=1
        return doc
    monkeypatch.setattr(inv,'observe_pairs',corrupt)
    with pytest.raises(verify.VerificationRefused,match='actual observation'):
        verify.verify_pairs(expected,(pair,))


def test_cli_baseline_and_verify_statuses_follow_fresh_file_state(trees,tmp_path,capsys):
    pair=trees({'a.cbz':b'abc'},{}); path=manifest(pair,tmp_path)
    assert cli.main(['baseline','--pairs',str(path),'--quiet'])==0
    doc=json.loads(capsys.readouterr().out)
    assert doc['hash_all_tower'] and doc['files'][0]['sha256']
    expected=tmp_path/'expected.json'; expected.write_text(json.dumps(doc))
    assert cli.main(['verify','--pairs',str(path),'--expected',str(expected),'--quiet'])==2
    assert json.loads(capsys.readouterr().out)['missing']
    (pair.target/'a.cbz').write_bytes(b'abc')
    assert cli.main(['verify','--pairs',str(path),'--expected',str(expected),'--quiet'])==0
    assert json.loads(capsys.readouterr().out)['verified']
