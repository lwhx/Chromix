#!/usr/bin/env python3
"""Validate evidence bundles and select whole measured records without overrides.

Checksums establish integrity, not authenticity. A selected record is a preflight
result, not a Chromium backend configuration or a claim of physical equivalence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

SCOPES = ('window', 'iframe', 'worker', 'shared_worker', 'service_worker')
WIRE_PROTOCOLS = ('tls', 'http2', 'quic', 'dns', 'proxy', 'webrtc')
QUALIFICATION = {'wire':{name:'not_collected' for name in WIRE_PROTOCOLS},
                 'physical_backend_equivalence':'not_verified'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError(f'non-finite JSON number: {value}')
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique, parse_constant=invalid)


def finite(value, positive=False):
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def capability_errors(value):
    """Check probe v2 result payloads, not only their success labels."""
    errors = []
    execution = value.get('execution', {})
    if execution.get('wasm'):
        expected = {'scalar':42, 'simd':7 if execution.get('simd') else None,
                    'memoryGrowth':True, 'maximumEnforced':True,
                    'atomics':True if execution.get('sab') else None}
        runtime = execution.get('runtime', {})
        if runtime.get('status') != 'observed' or runtime.get('value') != expected:
            errors.append('execution self-test failed or missing')
        if type(execution.get('simd')) is not bool:
            errors.append('invalid SIMD capability')
        if execution.get('sab') and execution.get('sharedMemory') != {'status':'observed', 'value':True}:
            errors.append('shared Wasm memory self-test failed or missing')
    for key, absent in (('webgl', 'context'), ('webgpu', 'adapter')):
        item = value.get(key, {})
        payload = item.get('value')
        if item.get('status') != 'observed' or not isinstance(payload, dict):
            continue
        if payload == {absent:None}:
            continue
        expected = ({'shader':True, 'textureRGBA':[0,255,0,255], 'requestExtensions':True}
                    if key == 'webgl' else {'compute':42, 'textureRGBA':[0,255,0,255], 'mapRead':True})
        if payload.get('backend') != expected:
            errors.append(f'{key}: backend self-test failed or missing')
        if key == 'webgpu' and payload.get('requestDevice') is not True:
            errors.append('webgpu: requestDevice not verified')
    fonts = value.get('fonts', {})
    if fonts.get('status') == 'observed':
        samples = fonts.get('value', {}).get('samples', [])
        expected = {(family, text) for family in ('serif','sans-serif','monospace','system-ui')
                    for text in ('Aa09','\u4e2d\u6587','\U0001f600','\u2211','\u0378')}
        seen = set()
        for sample in samples:
            seen.add((sample.get('family'), sample.get('text')))
            dom, canvas = sample.get('domWidth'), sample.get('canvasWidth')
            if not finite(dom) or not finite(canvas) or abs(dom - canvas) > 1 or sample.get('loaded') is not True:
                errors.append('fonts: CSS/Canvas/loading mismatch')
        if seen != expected or len(samples) != len(expected):
            errors.append('fonts: incomplete sample matrix')
    return errors


def observation_errors(observation):
    errors = []
    if not isinstance(observation, dict):
        return ['observation must be an object']
    baseline = observation.get('window', {})
    for scope in SCOPES:
        value = observation.get(scope)
        if not isinstance(value, dict) or 'execution' not in value:
            errors.append(f'{scope}: missing execution observation')
            continue
        identity = value.get('identity', {})
        if identity.get('status') != 'observed' or not identity.get('value'):
            errors.append(f'{scope}: missing identity')
        elif identity != baseline.get('identity'):
            errors.append(f'{scope}: identity differs from window')
        execution = value['execution']
        if value.get('probeVersion', 1) >= 2:
            errors.extend(f'{scope}: {error}' for error in capability_errors(value))
        cores = execution.get('cores')
        if type(cores) is not int or cores < 1:
            errors.append(f'{scope}: invalid core count')
        memory = execution.get('deviceMemory')
        # Chromium 152 supports an expanded 16/32 GiB bucket range.
        if memory is not None and (type(memory) not in (int, float) or memory not in (0.25, 0.5, 1, 2, 4, 8, 16, 32)):
            errors.append(f'{scope}: invalid deviceMemory bucket')
        for key in ('wasm', 'sab', 'isolated'):
            if type(execution.get(key)) is not bool:
                errors.append(f'{scope}: invalid {key}')
        # Isolation belongs to each execution context, not to a hardware family.
        for key in ('cores', 'deviceMemory', 'wasm', 'simd'):
            if execution.get(key) != baseline.get('execution', {}).get(key):
                errors.append(f'{scope}: {key} differs from window')
        heap = execution.get('heapLimit')
        if heap is not None and not finite(heap, positive=True):
            errors.append(f'{scope}: invalid heap limit')
        baseline_heap = baseline.get('execution', {}).get('heapLimit')
        if heap is not None and baseline_heap is not None and heap != baseline_heap:
            errors.append(f'{scope}: reported heap limit differs from window')
        if execution.get('sab') and not execution.get('isolated'):
            errors.append(f'{scope}: SAB exposed outside isolated context')
        for key in ('webgl', 'webgpu', 'fonts', 'audio', 'media'):
            if scope not in ('window', 'iframe') and key in ('fonts', 'audio', 'media'):
                continue
            item = value.get(key, {})
            if item.get('status') not in ('observed', 'unavailable', 'error'):
                errors.append(f'{scope}.{key}: missing observation status')
            elif item.get('status') == 'observed' and not isinstance(item.get('value'), dict):
                errors.append(f'{scope}.{key}: missing observation value')
            elif item.get('status') == 'unavailable' and not item.get('reason'):
                errors.append(f'{scope}.{key}: unavailable without reason')
            elif item.get('status') == 'error':
                errors.append(f'{scope}.{key}: {item.get("reason")}')
            if key in ('webgl', 'webgpu'):
                native = baseline.get(key, {})
                if item.get('status') == native.get('status') == 'observed' and item != native:
                    errors.append(f'{scope}.{key}: identity or capabilities differ from window')
    display = baseline.get('display', {})
    screen, window = display.get('screen', {}), display.get('window', {})
    for key in ('width', 'height', 'availWidth', 'availHeight'):
        if not finite(screen.get(key), positive=True):
            errors.append(f'display.screen.{key}: invalid')
    for total, available in (('width', 'availWidth'), ('height', 'availHeight')):
        if finite(screen.get(total)) and finite(screen.get(available)) and screen[available] > screen[total]:
            errors.append(f'display: {available} exceeds {total}')
    for key in ('outerWidth', 'outerHeight', 'innerWidth', 'innerHeight'):
        if not finite(window.get(key), positive=True):
            errors.append(f'display.window.{key}: invalid')
    # Negative screen coordinates and windows spanning displays are valid.
    for key in ('x', 'y'):
        if type(window.get(key)) not in (int, float) or not math.isfinite(window[key]):
            errors.append(f'display.window.{key}: invalid')
    if not finite(display.get('dpr'), positive=True):
        errors.append('display: invalid DPR')
    for key in ('deviceWidth', 'deviceHeight', 'resolution'):
        if display.get('css', {}).get(key) is not True:
            errors.append(f'display.css.{key}: mismatch')
    viewport = display.get('viewport')
    if not isinstance(viewport, dict) or not all(finite(viewport.get(k), True) for k in ('width', 'height', 'scale')):
        errors.append('display: missing or invalid visualViewport')
    return errors


def stable_observation(observation):
    result = deepcopy(observation)
    for value in result.values():
        if not isinstance(value, dict):
            continue
        value.pop('network', None)
        value.pop('http', None)
        # IDs are origin/profile salted, but kinds and constraints stay together
        # with the device record. This does not prove capture capability.
        for device in value.get('media', {}).get('value', {}).get('devices', []):
            device.pop('deviceId', None)
            device.pop('groupId', None)
        audio = value.get('audio', {}).get('value', {})
        for key in ('state', 'baseLatency', 'outputLatency'):
            audio.pop(key, None)
    return result


def validate_record(record, root):
    if not isinstance(record, dict):
        raise ValueError('record must be an object')
    if type(record.get('schema_version')) is not int or record.get('schema_version') != 1 or record.get('kind') != 'measured':
        raise ValueError('only schema v1 measured records may enter the pool')
    if set(record) != {'schema_version', 'kind', 'provenance', 'device', 'evidence', 'qualification', 'record_id'}:
        raise ValueError('unknown or missing record fields')
    if record['qualification'] != QUALIFICATION:
        raise ValueError('v1 evidence cannot claim wire or physical-backend verification')
    payload = {k: v for k, v in record.items() if k != 'record_id'}
    if record.get('record_id') != digest(payload):
        raise ValueError('record digest mismatch')
    provenance = record.get('provenance', {})
    try:
        timestamp = datetime.fromisoformat(provenance['collected_at'])
        if timestamp.tzinfo is None:
            raise ValueError('timestamp requires timezone')
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid collection timestamp') from error
    sha = provenance.get('browser_sha256')
    if not provenance.get('collector') or not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('collector and executable hash are required')
    evidence = record.get('evidence', {})
    if set(evidence) != {'browser', 'host'}:
        raise ValueError('browser and host evidence are required')
    root = Path(root).resolve()
    loaded = {}
    for name, item in evidence.items():
        relative = Path(item['path'])
        path = (root / relative).resolve()
        if relative.is_absolute() or root not in path.parents:
            raise ValueError(f'{name}: evidence path escapes bundle')
        if file_hash(path) != item['sha256']:
            raise ValueError(f'{name}: evidence checksum mismatch')
        loaded[name] = load_json(path)
    browser = loaded['browser']
    if browser.get('binary', {}).get('sha256') != provenance['browser_sha256']:
        raise ValueError('browser provenance does not match evidence')
    observations = browser.get('observations', [])
    if len(observations) != 3:
        raise ValueError('initial, restart, and isolated-profile observations required')
    errors = [f'run {i}: {e}' for i, observation in enumerate(observations) for e in observation_errors(observation)]
    from ._device_headers import header_errors
    for i, observation in enumerate(observations):
        for scope in SCOPES:
            value = observation.get(scope, {})
            if value.get('probeVersion', 1) >= 2:
                errors.extend(f'run {i} {scope}: {error}' for error in header_errors(
                    value, require_hints=scope in ('window', 'iframe')))
    if errors:
        raise ValueError('; '.join(errors))
    stable = [stable_observation(o) for o in observations]
    if stable[0] != stable[1] or stable[0] != stable[2]:
        raise ValueError('stable device observations differ across restart/profiles')
    if browser.get('profile_isolation') is not True:
        raise ValueError('profile storage isolation was not verified')
    if browser.get('external_requests') != []:
        raise ValueError('unexpected or missing external-request evidence')
    if record.get('device') != {'host':loaded['host'], 'surfaces':stable[0]}:
        raise ValueError('device fields do not match whole evidence record')
    return record


def backend_gaps(record):
    host = record['device']['host']
    gaps = []
    if not all(host.get('os', {}).get(key) for key in ('system', 'release', 'version', 'architecture')):
        gaps.append('OS/build/architecture inventory missing')
    cores = host.get('cpu', {}).get('logical_cores')
    if type(cores) is not int or cores < 1:
        gaps.append('native CPU inventory missing')
    elif record['device']['surfaces']['window']['execution']['cores'] > cores:
        gaps.append('reported CPU count exceeds native inventory')
    for key in ('gpu', 'memory', 'fonts'):
        if host.get(key, {}).get('status') != 'observed':
            gaps.append(f'{key}: native inventory missing or incomplete')
    if not host.get('gpu', {}).get('value'):
        gaps.append('GPU driver inventory missing')
    if not host.get('fonts', {}).get('files'):
        gaps.append('font-file inventory missing')
    if not finite(host.get('memory', {}).get('bytes'), positive=True):
        gaps.append('physical memory inventory missing')
    return gaps


def select_record(records, host_record, seed):
    """Rendezvous selection among exact backend matches, never field-wise merging.

    Inputs must have passed validate_record. Unknown capabilities require native
    behavior; a preflight match does not authorize synthetic getter overrides.
    """
    if type(seed) is not int or not 0 < seed < 2**64:
        raise ValueError('seed must be a nonzero uint64')
    candidates, rejected = [], []
    seen = set()
    for record in records:
        rid = record['record_id']
        if rid in seen:
            raise ValueError('duplicate pool record')
        seen.add(rid)
        reasons = backend_gaps(record) + backend_gaps(host_record)
        if record['provenance']['browser_sha256'] != host_record['provenance']['browser_sha256']:
            reasons.append('browser executable differs')
        if record['device'] != host_record['device']:
            reasons.append('host/backend or correlated surface observations differ')
        if reasons:
            rejected.append({'record_id':rid, 'reasons':reasons})
        else:
            candidates.append(record)
    if not candidates:
        return {'status':'native', 'record':None, 'rejected':rejected, 'overrides':[]}
    selected = max(candidates, key=lambda r: digest({'seed':str(seed), 'record_id':r['record_id']}))
    return {'status':'compatible', 'record':deepcopy(selected), 'rejected':rejected,
            'overrides':[], 'qualification':'exact-observation preflight only'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    validate = sub.add_parser('validate')
    validate.add_argument('record', type=Path)
    select = sub.add_parser('select')
    select.add_argument('--host', type=Path, required=True)
    select.add_argument('--seed', type=lambda x: int(x, 0), required=True)
    select.add_argument('records', nargs='+', type=Path)
    args = parser.parse_args(argv)
    try:
        def read(path):
            return validate_record(load_json(path), path.parent)
        if args.command == 'validate':
            record = read(args.record)
            output = {'status':'valid', 'record_id':record['record_id']}
        else:
            output = select_record([read(p) for p in args.records], read(args.host), args.seed)
        print(json.dumps(output, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({'status':'error', 'message':str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
