"""Offline fixtures validate the audit, not a browser or physical device."""
import base64
from copy import deepcopy
from io import BytesIO
import importlib.util
from pathlib import Path

import pytest

Image = pytest.importorskip('PIL.Image')
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('canvas_chain_audit', ROOT / 'tools/canvas_chain_audit.py')
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def encoded(pixels, mime='image/png', size=(32, 24)):
    image = Image.frombytes('RGBA', size, bytes(pixels))
    if mime == 'image/jpeg':
        backdrop = Image.new('RGB', size)
        backdrop.paste(image, mask=image.getchannel('A'))
        image = backdrop
    buffer = BytesIO()
    # The passing validator fixture avoids chroma subsampling; live probes use
    # the browser's native encoder, whose quality bounds are reported separately.
    image.save(buffer, format=audit.FORMATS[mime], quality=92, subsampling=0, lossless=True)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


def row(kind, alpha):
    ref = audit.input_pixels(alpha)
    exports = []
    for mime in audit.FORMATS:
        payload = encoded(ref, mime)
        decoded = audit.decode(payload, mime)['rgba']
        exports.append({'type':mime, 'bytes':payload, 'repeat':True,
                        'urlMatches':True if kind == 'html' else None,
                        'decoded':decoded, 'decodedSrgb':decoded,
                        'decodedNoPremultiply':decoded})
    return {'id':f'{kind}/srgb/{str(alpha).lower()}', 'kind':kind, 'alpha':alpha,
            'colorSpace':'srgb', 'width':32, 'height':24,
            'attributes':{'alpha':alpha, 'colorSpace':'srgb'}, 'input':audit.input_pixels(),
            'reference':ref, 'srgb':ref, 'direct':ref, 'noConversion':ref,
            'premultiply':{mode:ref for mode in ('none', 'premultiply', 'default')},
            'float16':{'status':'observed', 'typed':True, 'colorSpace':'srgb',
                       'pixels':[v / 255 for v in ref], 'inputReadback':ref},
            'crop':audit.region(ref, 3, 2, 7, 6), 'padded':audit.region(ref, -2, -2, 36, 28),
            'cropMatches':True, 'paddingMatches':True, 'sourceStable':True, 'finalRead':ref,
            'invalidRead':'IndexSizeError', 'fallback':'image/png', 'exports':exports,
            'transfer':{'pixels':ref, 'cleared':[0, 0, 0, 0 if alpha else 255] * (32 * 24)}
                       if kind == 'offscreen' else None}


@pytest.fixture(scope='module')
def template():
    result = {}
    for scope in audit.launch.pool.SCOPES:
        kinds = ('html', 'offscreen') if scope in ('window', 'iframe') else ('offscreen',)
        result[scope] = {'version':1, 'errors':[], 'rows':[row(k, a) for k in kinds for a in (True, False)],
                         'unavailable':[{'id':f'{k}/display-p3/{str(a).lower()}',
                                         'reason':'offline fixture: P3 unavailable'} for k in kinds for a in (True, False)],
                         'zeroBlob':'IndexSizeError'}
        result[scope]['edges'] = [{'id':f'{k}/{h}/{str(e).lower()}',
                                  'pixels':audit.region([48, 96, 160, 255] * 768, -2, -2, 36, 28)}
                                 for k in kinds for h in ('fresh', 'full', 'crop') for e in (False, True)]
        if scope in ('window', 'iframe'):
            result[scope].update(zeroURL='data:,', zeroCallback=True,
                                taint=[{'kind':k, 'read':'SecurityError', 'blob':'SecurityError',
                                        'url':'SecurityError' if k == 'html' else None} for k in kinds])
    return result


def test_valid_fixture_is_not_full_coverage(template):
    data = deepcopy(template)
    result = audit.evaluate(data)
    assert data == template
    assert result['errors'] == []
    assert len(result['skipped']) == 14
    assert result['comparisons']


@pytest.mark.parametrize('field,value', [
    ('reference', [0] * 3072), ('input', [0] * 3072), ('direct', [0] * 3072), ('finalRead', []),
    ('crop', []), ('padded', []), ('sourceStable', False), ('cropMatches', False),
    ('paddingMatches', False), ('fallback', 'image/jpeg'), ('invalidRead', None),
    ('attributes', {'alpha':False, 'colorSpace':'srgb'}), ('premultiply', {}),
    ('float16', {'status':'observed', 'typed':True, 'colorSpace':'srgb', 'pixels':[float('nan')] * 3072}),
    ('float16', {}), ('width', 31), ('alpha', 1), ('exports', []),
])
def test_mutated_row_rejected(template, field, value):
    data = deepcopy(template)
    data['window']['rows'][0][field] = value
    assert audit.evaluate(data)['errors']


@pytest.mark.parametrize('field,value', [('bytes', '!'), ('repeat', False), ('urlMatches', False),
                                        ('decoded', []), ('decodedSrgb', [0] * 3072),
                                        ('decodedNoPremultiply', [0] * 3072)])
def test_bad_export_rejected(template, field, value):
    data = deepcopy(template)
    data['window']['rows'][0]['exports'][0][field] = value
    assert audit.evaluate(data)['errors']


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'taint', 'zero', 'required-absent',
                                     'unknown-absent', 'duplicate-absent', 'probe-error', 'malformed'])
def test_incomplete_evidence_rejected(template, mutation):
    data = deepcopy(template)
    scope = data['window']
    if mutation == 'missing':
        scope['rows'].pop()
    elif mutation == 'duplicate':
        scope['rows'].append(deepcopy(scope['rows'][0]))
    elif mutation == 'taint':
        scope['taint'][0]['read'] = None
    elif mutation == 'zero':
        scope['zeroBlob'] = None
    elif mutation == 'required-absent':
        scope['unavailable'].append({'id':scope['rows'].pop()['id'], 'reason':'missing'})
    elif mutation == 'unknown-absent':
        scope['unavailable'].append({'id':'unknown/display-p3/true', 'reason':'missing'})
    elif mutation == 'duplicate-absent':
        scope['unavailable'].append(deepcopy(scope['unavailable'][0]))
    elif mutation == 'probe-error':
        scope['errors'].append({'name':'Error'})
    else:
        data['window'] = None
    assert audit.evaluate(data)['errors']


def test_cross_context_and_restart_signature(template):
    data = deepcopy(template)
    assert audit.cross_context_errors(data) == []
    before = audit.signature(data)
    data['iframe']['rows'][0]['reference'][0] += 1
    assert audit.cross_context_errors(data)
    assert before != audit.signature(data)


@pytest.mark.parametrize('missing', [True, False])
def test_options_history_edges(template, missing):
    data = deepcopy(template)
    if missing:
        data['window']['edges'].pop()
    else:
        data['window']['edges'][0]['pixels'][0] = 255
    assert audit.evaluate(data)['errors']


@pytest.mark.parametrize('mime', list(audit.FORMATS))
def test_independent_codec_validation(mime):
    data = audit.input_pixels()
    payload = encoded(data, mime)
    result = audit.decode(payload, mime)
    assert len(result['sha256']) == 64
    assert audit.compare(result['rgba'], data, lossy=mime != 'image/png', jpeg=mime == 'image/jpeg')['pass']
    wrong = 'image/jpeg' if mime == 'image/png' else 'image/png'
    with pytest.raises(ValueError):
        audit.decode(payload, wrong)


@pytest.mark.parametrize('value', ['', '!!!', 'A' * 1400001, None], ids=['empty', 'malformed', 'oversize', 'null'])
def test_bad_base64(value):
    with pytest.raises(ValueError):
        audit.decode(value, 'image/png')


def test_bad_dimensions():
    with pytest.raises(ValueError, match='dimensions'):
        audit.decode(encoded([0] * 16, size=(2, 2)), 'image/png')


def test_compare_premultiplication_and_limits():
    assert audit.compare([255, 20, 40, 0] * 768, [0] * 3072)['pass']
    assert audit.compare([100, 50, 20, 128] * 768, [50, 25, 10, 255] * 768)['pass'] is False
    assert audit.compare([100, 50, 20, 255] * 768, [100, 50, 20, 255] * 768)['pass']
    assert not audit.compare([100, 50, 20, 255] * 768, [140, 50, 20, 255] * 768, lossy=True)['pass']
    with pytest.raises(ValueError):
        audit.compare([True] * 3072, [0] * 3072)


def test_bridge_gate_precedes_endpoint_and_connection():
    patch = next((ROOT / 'patches').glob('0069-*')).read_text(encoding='utf-8')
    start = patch.index('+CanvasBridgeClient* CanvasBridgeClient::Get()')
    get = patch[start:patch.index('+CanvasBridgeClient::CanvasBridgeClient(', start)]
    assert get.index('config.Get("uxr-synthetic-device-tests") != "true"') < get.index('ParseEndpoint(')
    assert get.index('return nullptr;') < get.index('new CanvasBridgeClient(') < get.index('WaitUntilReady()')


def test_native_smoke_does_not_enable_synthetic_paths():
    from test_fingerprint_smoke import smoke, scenario
    for mode in ('native', 'on', 'off'):
        args = smoke.browser_args(scenario(mode), 'http://127.0.0.1:9876', False)
        assert ('--uxr-synthetic-device-tests=true' in args) == (mode != 'native')
