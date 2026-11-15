"""Configuration captures are sanitized evidence, never control authority."""
import copy
import json
from pathlib import Path

import pytest

from comic_automation.library import sync_inventory as inv
from comic_automation.library.sync_preview import build_preview, _digest
from comic_automation.library.sync_verify import verify_pairs
from comic_automation.library import sync_config as cfg
from comic_automation.library import sync_config_cli as cli
from test_sync_preview import trees  # noqa: F401


def capture_for(obs):
    """API-shaped private fixture with an explicit remote-to-observed mapping."""
    result = dict(contract=cfg.CAPTURE, nodes={}, mappings=[])
    for side, device, remote, separator in [('tower', 'TOWER-ID', '/media/manga', '/'),
                                           ('target', 'OFFICE-ID', 'X:\\Manga', '\\')]:
        base_remote = remote
        folders, ignores = [], {}
        for root in obs['roots']:
            if root['side'] != side:
                continue
            key = root['folder_id']
            remote = base_remote if key == 'manga' else ('/media/' + key if side == 'tower' else 'X:\\' + key)
            patterns = cfg.required_expanded_patterns(obs)
            for prefix in root['exclude_prefixes']:
                patterns.update((prefix.casefold(), prefix.casefold() + '/**'))
            raw = [*obs['exclusion_policy']['component_names'], '.syncthing.*.tmp', '.syncthing.tmp']
            raw += ['/' + p for p in root['exclude_prefixes']]
            ignores[key] = dict(ignore=['(?i)' + p for p in raw], expanded=['(?i)' + p for p in sorted(patterns)])
            folders.append(dict(id=key, path=remote, type='sendonly' if side == 'tower' else 'receiveonly',
                paused=True, filesystemType='basic', versioning=dict(type='staggered', params={'maxAge': '0'}),
                devices=[dict(deviceID='TOWER-ID', encryptionPassword=''), dict(deviceID='OFFICE-ID', encryptionPassword='')]))
            mapping = next((r for r in result['mappings'] if r['folder_id'] == key), None)
            if mapping is None:
                mapping = dict(folder_id=key); result['mappings'].append(mapping)
            mapping[side] = dict(syncthing_path=remote, observed_path=root['path'])
        phase = dict(status=dict(myID=device, pathSeparator=separator), config=dict(folders=folders,
                     devices=[dict(deviceID='TOWER-ID', paused=False), dict(deviceID='OFFICE-ID', paused=False)]), ignores=ignores)
        result['nodes'][side] = dict(before=copy.deepcopy(phase), after=copy.deepcopy(phase))
    return result


@pytest.fixture
def evidence(trees):
    pair = trees({'Book/a.cbz': b'book'}, {'Book/a.cbz': b'book'})
    obs = inv.observe_pairs((pair,), hash_all_tower=True)
    return pair, obs, capture_for(obs)


def mutate_both(capture, side, function):
    for phase in ('before', 'after'):
        function(capture['nodes'][side][phase])


def test_bound_preview_is_deterministic_read_only_and_not_ready(evidence):
    pair, obs, capture = evidence
    report = build_preview(obs)
    before = copy.deepcopy((report, capture))
    result = cfg.bind_configuration(report, capture)
    assert result == cfg.bind_configuration(report, capture)
    assert result['scope_bound'] and result['complete'] and not result['sync_ready']
    assert not result['scoped_verified']
    assert result['filesystem_report'] == report and (report, capture) == before
    assert result['binding_digest'] == _digest(result, 'binding_digest')
    assert (pair.target/'Book/a.cbz').read_bytes() == b'book'


def test_raw_credentials_and_unselected_paths_never_enter_output_or_fingerprints(evidence):
    _, obs, capture = evidence
    initial = cfg.bind_configuration(obs, capture)
    for node in capture['nodes'].values():
        for phase in node.values():
            phase['config']['gui'] = {'apiKey': 'SECRET-API', 'password': 'SECRET-PASSWORD'}
            phase['status']['connections'] = {'url': 'SECRET-TOKEN'}
    result = cfg.bind_configuration(obs, capture)
    assert result == initial
    for node in capture['nodes'].values():
        for phase in node.values():
            phase['config']['folders'].append(dict(id='unmanaged', path='SECRET-PATH', gui='SECRET-EXTRA'))
    result = cfg.bind_configuration(obs, capture)
    assert result['scope_bound'] and 'unmanaged' in result['configuration']['nodes']['tower']['unmanaged_folder_ids']
    assert 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('change', ['negation', 'wildcard', 'include', 'deletable', 'missing', 'extra', 'case_sensitive'])
def test_unsupported_or_differing_effective_ignores_fall_back(evidence, change):
    _, obs, capture = evidence
    def change_ignore(phase):
        patterns = phase['ignores']['manga']['expanded']
        if change == 'missing': patterns.pop()
        elif change == 'case_sensitive': patterns[:] = [p.removeprefix('(?i)') for p in patterns]
        else: patterns.append({'negation': '!keep', 'wildcard': '(?i)*', 'include': '#include secret',
                               'deletable': '(?d)secret', 'extra': '(?i)private'}[change])
    mutate_both(capture, 'tower', change_ignore)
    result = cfg.bind_configuration(obs, capture)
    assert not result['scope_bound'] and not result['complete']
    assert result['scope'] == 'filesystem-only' and result['problems']
    assert 'secret' not in json.dumps(result)


def test_windows_implicit_case_insensitivity_and_literal_prefixes(evidence):
    pair, _, _ = evidence
    pair = inv.RootPair(pair.folder_id, pair.tower, pair.target, ('private',))
    obs = inv.observe_pairs((pair,), hash_all_tower=True); capture = capture_for(obs)
    mutate_both(capture, 'target', lambda p: p['ignores']['manga'].update(
        expanded=[s.removeprefix('(?i)').upper() for s in p['ignores']['manga']['expanded']]))
    for side in cfg.SIDES:
        for phase in capture['nodes'][side].values():
            phase['ignores']['manga']['expanded'] = [
                p.replace('private', '/private') if p.endswith(('private', 'private/**')) else p
                for p in phase['ignores']['manga']['expanded']]
    assert cfg.bind_configuration(obs, capture)['scope_bound']


@pytest.mark.parametrize('name', ['漫画.cbz', 'ſystem volume information/a.cbz'])
def test_unicode_policy_equivalence_is_not_claimed(trees, name):
    pair = trees({name: b'book'}, {name: b'book'})
    obs = inv.observe_pairs((pair,), hash_all_tower=True)
    result = cfg.bind_configuration(obs, capture_for(obs))
    assert not result['scope_bound'] and not result['complete']


def test_local_device_can_be_implicit_in_global_device_list(evidence):
    _, obs, capture = evidence
    for side, node in capture['nodes'].items():
        for phase in node.values():
            own = phase['status']['myID']
            phase['config']['devices'] = [d for d in phase['config']['devices'] if d['deviceID'] != own]
    assert cfg.bind_configuration(obs, capture)['scope_bound']


@pytest.mark.parametrize('field', ['observed_path', 'syncthing_path'])
def test_declared_windows_path_spelling_is_not_guessed(evidence, field):
    _, obs, capture = evidence
    capture['mappings'][0]['target'][field] = capture['mappings'][0]['target'][field].lower()
    assert not cfg.bind_configuration(obs, capture)['scope_bound']


def test_selected_remote_namespace_overlap_is_unresolved(evidence, tmp_path):
    pair, _, _ = evidence
    other = inv.RootPair('comix', tmp_path/'extraTower', tmp_path/'extraTarget')
    other.tower.mkdir(); other.target.mkdir()
    obs = inv.observe_pairs((pair, other), hash_all_tower=True)
    capture = capture_for(obs)
    assert cfg.bind_configuration(obs, capture)['scope_bound']
    mapping = next(m for m in capture['mappings'] if m['folder_id'] == 'comix')
    mapping['tower']['syncthing_path'] = '/media/manga/nested'
    def overlap(phase):
        next(f for f in phase['config']['folders'] if f['id'] == 'comix')['path'] = '/media/manga/nested'
    mutate_both(capture, 'tower', overlap)
    assert not cfg.bind_configuration(obs, capture)['scope_bound']


@pytest.mark.parametrize('field', ['role', 'paused', 'versioning', 'ignore', 'device', 'unmanaged'])
def test_capture_drift_is_never_bound(evidence, field):
    _, obs, capture = evidence
    after = capture['nodes']['tower']['after']
    folder = after['config']['folders'][0]
    if field == 'role': folder['type'] = 'sendreceive'
    elif field == 'paused': folder['paused'] = False
    elif field == 'versioning': folder['versioning']['params']['maxAge'] = '1'
    elif field == 'ignore': after['ignores']['manga']['ignore'].append('// change')
    elif field == 'device': after['status']['myID'] = 'OTHER-ID'
    else: after['config']['folders'].append(dict(id='other'))
    result = cfg.bind_configuration(obs, capture)
    assert not result['scope_bound'] and not result['configuration']['capture_stable']


@pytest.mark.parametrize('change', ['remote_path', 'observed_path', 'encrypted', 'role', 'fs', 'marker', 'version_path', 'version_command', 'peers'])
def test_known_but_unsupported_configuration_is_not_scoped(evidence, change):
    _, obs, capture = evidence
    if change in ('remote_path', 'observed_path'):
        key = 'syncthing_path' if change == 'remote_path' else 'observed_path'
        capture['mappings'][0]['tower'][key] = '/different' if change == 'remote_path' else 'C:\\different'
    else:
        def alter(phase):
            folder = phase['config']['folders'][0]
            if change == 'encrypted': folder['devices'][0]['encryptionPassword'] = 'SECRET'
            elif change == 'role': folder['type'] = 'receiveencrypted'
            elif change == 'fs': folder['filesystemType'] = 'fake'
            elif change == 'marker': folder['markerName'] = 'other'
            elif change == 'version_path': folder['versioning']['fsPath'] = 'SECRET'
            elif change == 'version_command': folder['versioning'] = dict(type='external', params={'command': 'SECRET'})
            else:
                folder['devices'].append(dict(deviceID='THIRD-ID'))
                phase['config']['devices'].append(dict(deviceID='THIRD-ID', paused=False))
        mutate_both(capture, 'tower', alter)
    result = cfg.bind_configuration(obs, capture)
    assert not result['scope_bound'] and 'SECRET' not in json.dumps(result)


@pytest.mark.parametrize('change', ['folder_missing', 'duplicate_folder', 'duplicate_device', 'paused_type', 'mapping_missing', 'mapping_duplicate', 'same_device', 'relative_path', 'version_number', 'missing_expanded'])
def test_malformed_exports_refuse_without_raw_values(evidence, change):
    _, obs, capture = evidence
    node = capture['nodes']['tower']['after']
    if change == 'folder_missing': node['config']['folders'] = []
    elif change == 'duplicate_folder': node['config']['folders'] *= 2
    elif change == 'duplicate_device': node['config']['devices'] *= 2
    elif change == 'paused_type': node['config']['folders'][0]['paused'] = 'SECRET'
    elif change == 'mapping_missing': capture['mappings'] = []
    elif change == 'mapping_duplicate': capture['mappings'] *= 2
    elif change == 'same_device':
        for phase in capture['nodes']['target'].values(): phase['status']['myID'] = 'TOWER-ID'
    elif change == 'relative_path': node['config']['folders'][0]['path'] = 'SECRET'
    elif change == 'version_number': node['config']['folders'][0]['versioning']['params']['maxAge'] = 'SECRET'
    else: del node['ignores']['manga']['expanded']
    with pytest.raises(cfg.ConfigurationRefused) as error: cfg.bind_configuration(obs, capture)
    assert 'SECRET' not in str(error.value)


def test_configuration_bound_baseline_and_fresh_verification(evidence):
    pair, obs, capture = evidence
    expected = cfg.bind_configuration(obs, capture)
    report = verify_pairs(obs, (pair,))
    result = cfg.bind_configuration(report, capture, expected=expected)
    assert result['scoped_verified'] and result['complete'] and result['state_changed_since_baseline'] is False
    assert not cfg.bind_configuration(report, capture)['scope_bound']
    changed = copy.deepcopy(capture)
    mutate_both(changed, 'tower', lambda p: p['config']['folders'][0].update(paused=False))
    result = cfg.bind_configuration(report, changed, expected=expected)
    assert result['scoped_verified'] and result['state_changed_since_baseline'] is True
    changed['mappings'][0]['tower']['syncthing_path'] = '/elsewhere'
    mutate_both(changed, 'tower', lambda p: p['config']['folders'][0].update(path='/elsewhere'))
    assert not cfg.bind_configuration(report, changed, expected=expected)['scoped_verified']


@pytest.mark.parametrize('change', ['digest', 'baseline_digest', 'baseline_flag', 'baseline_observation', 'scope_fingerprint'])
def test_digest_and_baseline_bindings_are_checked(evidence, change):
    pair, obs, capture = evidence
    expected = cfg.bind_configuration(obs, capture); report = verify_pairs(obs, (pair,))
    if change == 'digest': report['verified'] = False
    elif change == 'baseline_digest': expected['complete'] = False
    elif change == 'baseline_flag': expected['scope_bound'] = False; expected['binding_digest'] = _digest(expected, 'binding_digest')
    elif change == 'baseline_observation':
        expected['filesystem_report']['observation_digest'] = '0'*64
        expected['binding_digest'] = _digest(expected, 'binding_digest')
    else:
        expected['scope_fingerprint'] = '0'*64; expected['binding_digest'] = _digest(expected, 'binding_digest')
    with pytest.raises(cfg.ConfigurationRefused): cfg.bind_configuration(report, capture, expected=expected)


def test_incomplete_filesystem_evidence_never_becomes_complete(evidence):
    _, obs, capture = evidence
    obs['complete'] = False; obs['problems'].append(dict(reason='fixture'))
    obs['observation_digest'] = _digest(obs, 'observation_digest')
    result = cfg.bind_configuration(obs, capture)
    assert not result['complete'] and not result['scoped_verified'] and not result['sync_ready']


def test_file_differences_are_not_erased_by_valid_configuration(evidence):
    pair, obs, capture = evidence
    expected = cfg.bind_configuration(obs, capture)
    (pair.target/'extra.cbz').write_bytes(b'extra')
    report = verify_pairs(obs, (pair,))
    result = cfg.bind_configuration(report, capture, expected=expected)
    assert result['scope_bound'] and not result['complete'] and not result['scoped_verified']
    assert result['filesystem_report']['extra'] == report['extra']


def test_raw_report_or_bound_baseline_tampering_refuses(evidence):
    _, obs, capture = evidence
    obs['files'][0]['size'] += 1
    with pytest.raises(cfg.ConfigurationRefused): cfg.bind_configuration(obs, capture)


def test_estimate_is_not_upgraded_to_complete_evidence(evidence):
    pair, _, capture = evidence
    (pair.target/'Book/a.cbz').unlink()
    estimate = inv.observe_pairs((pair,), hash_content=False)
    with pytest.raises(cfg.ConfigurationRefused): cfg.bind_configuration(estimate, capture)


def test_cli_round_trip_and_secret_safe_errors(evidence, tmp_path, capsys):
    pair, obs, capture = evidence
    report_path, capture_path, expected_path = (tmp_path/name for name in ('report.json','capture.json','expected.json'))
    report_path.write_text(json.dumps(obs)); capture_path.write_text(json.dumps(capture))
    args = ['--report', str(report_path), '--capture', str(capture_path)]
    assert cli.main(args) == 0
    expected_path.write_text(capsys.readouterr().out)
    report_path.write_text(json.dumps(verify_pairs(obs, (pair,))))
    assert cli.main([*args, '--expected', str(expected_path)]) == 0
    assert json.loads(capsys.readouterr().out)['scoped_verified']
    capture_path.write_text('{"apikey":"SECRET", broken')
    assert cli.main(args) == 2
    assert 'SECRET' not in str(capsys.readouterr())
