"""Synthetic fixtures test validators, never count as measured-device evidence."""
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect_device as collector
import device_pool as pool
import device_wire_evidence as wire


def observation():
    common = {'identity':{'status':'observed', 'value':{'ua':'fixture'}},
              'execution':{'cores':8, 'deviceMemory':16, 'wasm':True, 'sab':False,
                           'isolated':False, 'simd':True, 'heapLimit':None},
              'webgl':{'status':'observed', 'value':{'vendor':'fixture', 'limits':{'size':128}}}}
    result = {scope:deepcopy(common) for scope in pool.SCOPES}
    for value in result.values():
        for key in ('webgpu', 'fonts', 'audio', 'media'):
            value[key] = {'status':'unavailable', 'reason':'unit-test fixture'}
    result['window']['display'] = {
        'screen':{'width':1920, 'height':1080, 'availWidth':1920, 'availHeight':1040},
        'window':{'x':-100, 'y':0, 'outerWidth':1000, 'outerHeight':700, 'innerWidth':980, 'innerHeight':610},
        'viewport':{'width':980, 'height':610, 'scale':1}, 'dpr':1.25,
        'css':{'deviceWidth':True, 'deviceHeight':True, 'resolution':True}}
    return result


def seal(record):
    record['record_id'] = pool.digest({k:v for k, v in record.items() if k != 'record_id'})
    return record


def bundle(root):
    root.mkdir(parents=True, exist_ok=True)
    host = {'os':dict.fromkeys(('system', 'release', 'version', 'architecture'), 'fixture'),
            'cpu':{'logical_cores':8}, 'gpu':{'status':'observed', 'value':[{'driver':'fixture'}]},
            'memory':{'status':'observed', 'bytes':32 * 1024**3},
            'fonts':{'status':'observed', 'files':[{'sha256':'a' * 64}]}}
    browser = {'binary':{'sha256':'b' * 64}, 'observations':[observation() for _ in range(3)],
               'profile_isolation':True, 'external_requests':[]}
    for name, data in [('host', host), ('browser', browser)]:
        (root / f'{name}.json').write_text(json.dumps(data), encoding='utf-8')
    return seal({'schema_version':1, 'kind':'measured', 'provenance':{
        'collector':'unit-test-fixture-not-hardware', 'browser_sha256':'b' * 64,
        'collected_at':'2026-09-10T00:00:00+00:00'},
        'device':{'host':host, 'surfaces':observation()},
        'qualification':deepcopy(pool.QUALIFICATION),
        'evidence':{name:{'path':name + '.json', 'sha256':pool.file_hash(root / f'{name}.json')}
                    for name in ('host', 'browser')}})


def test_bundle_validates_and_selects_whole_copy(tmp_path):
    record = pool.validate_record(bundle(tmp_path), tmp_path)
    result = pool.select_record([record], record, 2**64 - 1)
    assert result['status'] == 'compatible'
    assert result['record'] == record
    assert result['record'] is not record
    assert result['overrides'] == []


@pytest.mark.parametrize('seed', [0, -1, 2**64, True, '1', 1.5])
def test_invalid_seed(seed):
    with pytest.raises(ValueError, match='uint64'):
        pool.select_record([], {}, seed)


def test_order_independent_and_duplicate_rejected(tmp_path):
    one = bundle(tmp_path)
    two = deepcopy(one)
    two['provenance']['collected_at'] = '2026-09-11T00:00:00+00:00'
    seal(two)
    a = pool.select_record([one, two], one, 123)
    b = pool.select_record([two, one], one, 123)
    assert a == b
    with pytest.raises(ValueError, match='duplicate'):
        pool.select_record([one, one], one, 123)


@pytest.mark.parametrize('field', ['gpu', 'fonts', 'memory', 'os'])
def test_no_cross_device_merging(tmp_path, field):
    record = bundle(tmp_path)
    different = deepcopy(record)
    different['device']['host'][field] = {'status':'observed', 'value':'another device'}
    seal(different)
    result = pool.select_record([different], record, 42)
    assert result['status'] == 'native'
    assert result['record'] is None
    assert result['rejected']


def test_browser_and_capability_mismatch(tmp_path):
    record = bundle(tmp_path)
    different = deepcopy(record)
    different['provenance']['browser_sha256'] = 'c' * 64
    seal(different)
    assert pool.select_record([different], record, 42)['status'] == 'native'
    different = deepcopy(record)
    different['device']['surfaces']['worker']['execution']['simd'] = False
    seal(different)
    assert pool.select_record([different], record, 42)['status'] == 'native'


def test_incomplete_native_inventory_not_compatible(tmp_path):
    record = bundle(tmp_path)
    record['device']['host']['fonts']['status'] = 'incomplete'
    seal(record)
    assert pool.select_record([record], record, 42)['status'] == 'native'


@pytest.mark.parametrize('kind', ['synthetic', 'test-only', None])
def test_synthetic_cannot_enter_measured_pool(tmp_path, kind):
    record = bundle(tmp_path)
    record['kind'] = kind
    seal(record)
    with pytest.raises(ValueError, match='measured'):
        pool.validate_record(record, tmp_path)


def test_tampering_and_field_splicing(tmp_path):
    record = bundle(tmp_path)
    record['device']['host']['extra'] = True
    with pytest.raises(ValueError, match='digest mismatch'):
        pool.validate_record(record, tmp_path)
    seal(record)
    with pytest.raises(ValueError, match='whole evidence'):
        pool.validate_record(record, tmp_path)
    record = bundle(tmp_path)
    (tmp_path / 'host.json').write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        pool.validate_record(record, tmp_path)


@pytest.mark.parametrize('path', ['../escape.json', str(Path.cwd() / 'absolute.json')])
def test_evidence_path_escape(tmp_path, path):
    record = bundle(tmp_path)
    record['evidence']['host']['path'] = path
    seal(record)
    with pytest.raises(ValueError, match='escapes'):
        pool.validate_record(record, tmp_path)


def test_strict_json(tmp_path):
    for value in ('{"seed":1,"seed":2}', '{"seed":NaN}', '{"seed":Infinity}'):
        path = tmp_path / 'bad.json'
        path.write_text(value)
        with pytest.raises(ValueError):
            pool.load_json(path)


@pytest.mark.parametrize('scope', pool.SCOPES)
def test_missing_or_inconsistent_context(scope):
    sample = observation()
    del sample[scope]
    assert pool.observation_errors(sample)
    sample = observation()
    sample[scope]['execution']['cores'] = 9
    assert pool.observation_errors(sample)


@pytest.mark.parametrize('memory', [0.25, 0.5, 1, 2, 4, 8, 16, 32, None])
def test_native_memory_buckets(memory):
    sample = observation()
    for value in sample.values():
        value['execution']['deviceMemory'] = memory
    assert not pool.observation_errors(sample)


@pytest.mark.parametrize('memory', [True, 0, -1, 12, '8', float('nan')])
def test_invalid_memory_buckets(memory):
    sample = observation()
    sample['worker']['execution']['deviceMemory'] = memory
    assert pool.observation_errors(sample)


def test_geometry_and_capability_errors():
    sample = observation()
    assert not pool.observation_errors(sample)  # Negative screen X is valid.
    sample['window']['display']['screen']['availWidth'] = 2000
    assert any('availWidth' in e for e in pool.observation_errors(sample))
    sample = observation()
    sample['window']['display']['css']['resolution'] = False
    assert any('resolution' in e for e in pool.observation_errors(sample))
    sample['worker']['webgl'] = {'status':'error', 'reason':'driver failed'}
    assert any('driver failed' in e for e in pool.observation_errors(sample))


@pytest.mark.parametrize('scope', ['worker', 'shared_worker', 'service_worker', 'iframe'])
def test_gpu_cross_context_mismatch(scope):
    sample = observation()
    sample[scope]['webgl']['value']['limits']['size'] = 256
    assert any(f'{scope}.webgl: identity or capabilities' in e for e in pool.observation_errors(sample))


def test_dynamic_observations_not_stable_identity():
    a, b = observation(), observation()
    a['window']['network'] = {'rtt':100}
    b['window']['network'] = {'rtt':20}
    a['window']['media'] = {'status':'observed', 'value':{'devices':[{'kind':'audioinput', 'deviceId':'origin-one'}]}}
    b['window']['media'] = {'status':'observed', 'value':{'devices':[{'kind':'audioinput', 'deviceId':'origin-two'}]}}
    assert pool.stable_observation(a) == pool.stable_observation(b)
    assert 'network' in a['window']


@pytest.mark.parametrize('mode', ['missing_restart', 'restart_changed', 'isolation', 'external'])
def test_invalid_browser_evidence(tmp_path, mode):
    record = bundle(tmp_path)
    path = tmp_path / 'browser.json'
    data = pool.load_json(path)
    if mode == 'missing_restart':
        data['observations'].pop()
    elif mode == 'restart_changed':
        for value in data['observations'][1].values():
            value['execution']['cores'] = 16
    elif mode == 'isolation':
        data['profile_isolation'] = False
    else:
        data['external_requests'] = ['https://example.invalid']
    path.write_text(json.dumps(data))
    record['evidence']['browser']['sha256'] = pool.file_hash(path)
    seal(record)
    with pytest.raises(ValueError):
        pool.validate_record(record, tmp_path)


def test_worker_scripts_only_served_at_local_origin():
    with collector.server_context() as server:
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        for path in (*collector.SCRIPTS, '/device-probe.js'):
            connection.request('GET', path)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read()
        connection.request('GET', '/device-probe.js', headers={'Host':'other.invalid'})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()


def test_wire_presence_not_verification():
    packet = {'_source':{'layers':{'frame':{'frame.number':'1'},
              'tls':{'tls.handshake':{'tls.handshake.type':'1'}}, 'http2':{}, 'quic':{},
              'dns':{}, 'stun':{}, 'http':{'http.request.method':'CONNECT'}}}}
    report = wire.summarize([packet])
    assert all(p['status'] == 'observed' and not p['verified'] for p in report['protocols'].values())
    empty = wire.summarize([])
    assert all(p['status'] == 'not_observed' for p in empty['protocols'].values())
    packet['_source']['layers']['tls']['tls.handshake']['tls.handshake.type'] = '2'
    assert wire.summarize([packet])['protocols']['tls']['status'] == 'not_observed'


def test_wire_malformed_input_rejected():
    for data in ({}, [{}], [{'_source':{'layers':{}}}]):
        with pytest.raises((ValueError, KeyError)):
            wire.summarize(data)


def test_cannot_claim_unperformed_verification(tmp_path):
    record = bundle(tmp_path)
    record['qualification']['wire']['tls'] = 'verified'
    seal(record)
    with pytest.raises(ValueError, match='cannot claim'):
        pool.validate_record(record, tmp_path)


def test_record_cli_and_existing_output_protection(tmp_path, capsys):
    record = bundle(tmp_path)
    path = tmp_path / 'record.json'
    path.write_text(json.dumps(record))
    assert pool.main(['validate', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'valid'
    assert pool.main(['select', '--host', str(path), '--seed', '0x100000001', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'compatible'
    assert collector.main(['--browser', 'missing.exe', '--output', str(tmp_path)]) == 1
    assert json.loads(path.read_text()) == record


def test_wire_cli_missing_tool_is_not_a_pass(tmp_path, capsys):
    pcap = tmp_path / 'test.pcap'
    pcap.write_bytes(b'unit test, not a real capture')
    output = tmp_path / 'wire.json'
    assert wire.main(['--pcap', str(pcap), '--output', str(output),
                      '--tshark', str(tmp_path / 'missing-tshark.exe')]) == 1
    assert not output.exists()
    assert json.loads(capsys.readouterr().out)['status'] == 'error'
