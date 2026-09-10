#!/usr/bin/env python3
"""Canvas API/codec integration audit. Requires Playwright and Pillow with LittleCMS."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from chromix import _device_launch as launch

PROBE = Path(__file__).with_name('canvas_chain_probe.js')
FORMATS = {'image/png':'PNG', 'image/jpeg':'JPEG', 'image/webp':'WEBP'}
WIDTH, HEIGHT = 32, 24
COLORS = ((48, 96, 160, 255), (160, 80, 48, 128),
          (40, 200, 100, 64), (200, 30, 160, 0))


def input_pixels(alpha=True):
    return [channel for _ in range(HEIGHT) for x in range(WIDTH)
            for channel in (*COLORS[x // 8][:3], COLORS[x // 8][3] if alpha else 255)]


def region(reference, x, y, width, height):
    result = []
    for row in range(y, y + height):
        for col in range(x, x + width):
            offset = (row * WIDTH + col) * 4
            result.extend(reference[offset:offset + 4] if 0 <= row < HEIGHT and 0 <= col < WIDTH else [0] * 4)
    return result


class Handler(launch.ProbeHandler):
    def send_header(self, name, value):
        if name.lower() == 'content-security-policy':
            value = "default-src 'self'; img-src 'self' " + self.server.taint_origin
        super().send_header(name, value)

    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            self.send_error(403)
            return
        if self.path == '/probe.js':
            body = (PROBE.read_text(encoding='utf-8') + '\n' +
                    'globalThis.chromixDeviceProbe = canvasChainProbe;\n' +
                    'globalThis.canvasTaintURL = ' + json.dumps(self.server.taint_origin + '/test.png') + ';').encode()
            mime = 'text/javascript'
        elif self.path == '/test.png':
            body, mime = self.server.png, 'image/png'
        else:
            super().do_GET()
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def server(taint_origin=''):
    from PIL import Image
    data = BytesIO()
    Image.new('RGBA', (2, 2), (32, 64, 128, 255)).save(data, format='PNG')
    instance = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    instance.daemon_threads = True
    instance.taint_origin, instance.png = taint_origin, data.getvalue()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{instance.server_port}'
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def pixels(value, *, floating=False):
    import math
    return (isinstance(value, list) and len(value) == WIDTH * HEIGHT * 4 and
            all(type(v) in (int, float) and math.isfinite(v) if floating
                else type(v) is int and 0 <= v <= 255 for v in value))


def compare(actual, expected, *, lossy=False, jpeg=False):
    if not pixels(actual) or not pixels(expected):
        raise ValueError('invalid RGBA pixel array')
    differences = []
    alpha_error = 0
    for i in range(0, len(actual), 4):
        a, b = actual[i + 3] / 255, expected[i + 3] / 255
        alpha_error = max(alpha_error, abs(actual[i + 3] - (255 if jpeg else expected[i + 3])))
        # Compare visible premultiplied color, not undefined RGB under alpha=0.
        differences.extend(abs(actual[i + k] * a - expected[i + k] * b) for k in range(3))
    maximum, mean = max(differences), sum(differences) / len(differences)
    bound, average = (36, 7) if lossy else (2, 0.6)
    return {'pass':maximum <= bound and mean <= average and alpha_error <= 1,
            'max':maximum, 'mean':mean, 'alpha_max':alpha_error,
            'max_bound':bound, 'mean_bound':average}


def decode(encoded, mime):
    from PIL import Image, ImageCms
    if not isinstance(encoded, str) or not encoded or len(encoded) > 1400000:
        raise ValueError('invalid encoded image text')
    raw = base64.b64decode(encoded, validate=True)
    if not raw or len(raw) > 1024 * 1024:
        raise ValueError('invalid encoded image size')
    with Image.open(BytesIO(raw)) as image:
        if image.format != FORMATS[mime] or image.size != (WIDTH, HEIGHT):
            raise ValueError('encoded format or dimensions mismatch')
        image.load()
        rgba = image.convert('RGBA')
        icc = image.info.get('icc_profile')
        if icc:
            rgb = ImageCms.profileToProfile(image.convert('RGB'), ImageCms.ImageCmsProfile(BytesIO(icc)),
                                           ImageCms.createProfile('sRGB'), outputMode='RGB')
            converted = rgb.convert('RGBA')
            converted.putalpha(rgba.getchannel('A'))
        else:
            converted = rgba
        return {'rgba':list(rgba.tobytes()), 'srgb':list(converted.tobytes()),
                'icc':bool(icc), 'sha256':hashlib.sha256(raw).hexdigest()}


def evaluate(observation):
    errors, comparisons, skipped = [], [], []
    if not isinstance(observation, dict):
        return {'errors':['observation must be an object'], 'comparisons':[], 'skipped':[]}
    for scope in launch.pool.SCOPES:
        value = observation.get(scope, {})
        try:
            if value.get('version') != 1 or value.get('errors') != []:
                raise ValueError('probe failed: ' + str(value.get('errors')))
            kinds = ('html', 'offscreen') if scope in ('window', 'iframe') else ('offscreen',)
            expected = {f'{kind}/{space}/{str(alpha).lower()}' for kind in kinds
                        for space in ('srgb', 'display-p3') for alpha in (True, False)}
            seen = set()
            for absent in value.get('unavailable', []):
                if absent.get('id') not in expected or '/display-p3/' not in absent['id'] or not absent.get('reason'):
                    raise ValueError('required capability unavailable')
                if absent['id'] in seen:
                    raise ValueError('duplicate unavailable row')
                seen.add(absent['id'])
                skipped.append({'scope':scope, **absent})
            for row in value.get('rows', []):
                key = row['id']
                if key in seen or key != f"{row['kind']}/{row['colorSpace']}/{str(row['alpha']).lower()}":
                    raise ValueError('duplicate or inconsistent row')
                seen.add(key)
                label = scope + '/' + key
                if row.get('width') != WIDTH or row.get('height') != HEIGHT or type(row.get('alpha')) is not bool:
                    raise ValueError(label + ': dimensions/alpha mismatch')
                reference = row['reference']
                if not pixels(reference) or not pixels(row['srgb']):
                    raise ValueError(label + ': invalid reference')
                if row.get('input') != input_pixels():
                    raise ValueError(label + ': input pattern mismatch')
                attrs = row.get('attributes', {})
                if attrs.get('alpha') is not row['alpha'] or attrs.get('colorSpace') != row['colorSpace']:
                    raise ValueError(label + ': actual context attributes mismatch')
                for field in ('cropMatches', 'paddingMatches', 'sourceStable'):
                    if row.get(field) is not True:
                        errors.append(label + ': ' + field)
                for field, rect in (('crop', (3, 2, 7, 6)), ('padded', (-2, -2, WIDTH + 4, HEIGHT + 4))):
                    if row.get(field) != region(reference, *rect):
                        errors.append(label + ': ' + field + ' evidence mismatch')
                if row.get('finalRead') != reference:
                    errors.append(label + ': source changed after export')
                if row.get('invalidRead') != 'IndexSizeError' or row.get('fallback') != 'image/png':
                    errors.append(label + ': exception/MIME fallback mismatch')
                alphas = [reference[(x * 4) + 3] for x in (0, 8, 16, 24)]
                if alphas != ([255, 128, 64, 0] if row['alpha'] else [255] * 4):
                    errors.append(label + ': alpha contract mismatch')
                def check(name, actual, wanted, **options):
                    result = compare(actual, wanted, **options)
                    category = 'codec-quality' if options.get('lossy') else 'pixel-consistency'
                    comparisons.append({'path':label + '/' + name, 'category':category, **result})
                    if not result['pass']:
                        errors.append(label + '/' + name + ': ' + category + ' mismatch')
                check('input-readback', reference, input_pixels(row['alpha']))
                if row['colorSpace'] == 'srgb':
                    check('same-space-read', row['srgb'], reference)
                check('direct-bitmap', row['direct'], reference)
                check('no-color-conversion', row['noConversion'], reference)
                if set(row['premultiply']) != {'none', 'premultiply', 'default'}:
                    raise ValueError('missing premultiply cases')
                for mode, actual in row['premultiply'].items():
                    check('premultiply/' + mode, actual, reference)
                floating = row['float16']
                if floating.get('status') == 'observed':
                    check('float16-input', floating.get('inputReadback'), input_pixels(row['alpha']))
                    if floating.get('typed') is not True or floating.get('colorSpace') != row['colorSpace'] or not pixels(floating.get('pixels'), floating=True):
                        raise ValueError('float16 metadata/values invalid')
                    if any(v < 0 or v > 1 for v in floating['pixels']):
                        errors.append(label + ': out-of-range float16 for bounded input')
                    elif not compare([round(v * 255) for v in floating['pixels']], reference)['pass']:
                        errors.append(label + ': float16/uint8 mismatch')
                elif floating.get('status') == 'unavailable' and floating.get('reason'):
                    skipped.append({'scope':label, 'reason':floating['reason']})
                else:
                    raise ValueError('missing float16 probe')
                encoded = row['exports']
                if [item.get('type') for item in encoded] != list(FORMATS):
                    raise ValueError('missing codec matrix')
                for item in encoded:
                    mime = item['type']
                    if item.get('repeat') is not True or (row['kind'] == 'html' and item.get('urlMatches') is not True):
                        errors.append(label + '/' + mime + ': unstable or mismatched exports')
                    jpeg = mime == 'image/jpeg'
                    check(mime + '/browser', item['decodedSrgb'] if jpeg else item['decoded'],
                          row['srgb'] if jpeg else reference, lossy=mime != 'image/png', jpeg=jpeg)
                    check(mime + '/premultiply-decode', item['decodedNoPremultiply'], item['decoded'])
                    independent = decode(item['bytes'], mime)
                    check(mime + '/independent-srgb', independent['srgb'], item['decodedSrgb'],
                          lossy=mime != 'image/png')
                    if mime == 'image/png':
                        check(mime + '/independent-raw', independent['rgba'], reference)
                    comparisons.append({'path':label + '/' + mime + '/independent-decode', 'pass':True,
                                        **{k:v for k,v in independent.items() if k not in ('rgba','srgb')}})
                if row['kind'] == 'offscreen':
                    check('transfer', row['transfer']['pixels'], reference)
                    clear = row['transfer']['cleared']
                    expected_clear = [0,0,0,0 if row['alpha'] else 255] * (WIDTH * HEIGHT)
                    if clear != expected_clear:
                        errors.append(label + ': transfer did not reset backing store')
            if seen != expected:
                raise ValueError('incomplete row matrix')
            edge_ids = {f'{k}/{h}/{str(e).lower()}' for k in kinds
                        for h in ('fresh', 'full', 'crop') for e in (False, True)}
            edges = value.get('edges')
            if not isinstance(edges, list) or len(edges) != len(edge_ids) or {e['id'] for e in edges} != edge_ids:
                raise ValueError('incomplete options/history edge matrix')
            edge_pixels = region([48, 96, 160, 255] * (WIDTH * HEIGHT), -2, -2, WIDTH + 4, HEIGHT + 4)
            for edge in edges:
                if edge.get('pixels') != edge_pixels:
                    errors.append(scope + '/edge/' + edge['id'] + ': out-of-bounds read mismatch')
            if value.get('zeroBlob') != 'IndexSizeError':
                raise ValueError('zero OffscreenCanvas exception mismatch')
            if scope in ('window', 'iframe'):
                if value.get('zeroURL') != 'data:,' or value.get('zeroCallback') is not True:
                    raise ValueError('zero HTMLCanvas behavior mismatch')
                taint = value.get('taint')
                if not isinstance(taint, list) or [r.get('kind') for r in taint] != ['html','offscreen']:
                    raise ValueError('missing taint tests')
                for row in taint:
                    if row.get('read') != 'SecurityError' or row.get('blob') != 'SecurityError' or (
                            row['kind'] == 'html' and row.get('url') != 'SecurityError'):
                        raise ValueError('tainted canvas did not reject read/export')
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            errors.append(scope + ': ' + str(error))
    errors.extend(cross_context_errors(observation))
    return {'errors':errors, 'comparisons':comparisons, 'skipped':skipped}


def cross_context_errors(observation):
    errors = []
    baseline = observation.get('window', {})
    if not isinstance(baseline, dict):
        return errors
    rows = baseline.get('rows', [])
    if not isinstance(rows, list):
        return errors
    reference = {r['id']:r for r in rows if isinstance(r, dict) and isinstance(r.get('id'), str)}
    for scope in launch.pool.SCOPES:
        if scope == 'window' or not isinstance(observation.get(scope), dict):
            continue
        rows = observation[scope].get('rows', [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                continue
            key = row['id']
            if key not in reference:
                errors.append(scope + '/' + key + ': capability differs from window')
                continue
            # Compare the same canvas kind and options, including encoded bytes.
            for field in ('reference', 'srgb', 'direct', 'premultiply', 'noConversion', 'float16', 'exports', 'transfer'):
                if row.get(field) != reference[key].get(field):
                    errors.append(scope + '/' + key + '/' + field + ': cross-context mismatch')
        absent = observation[scope].get('unavailable', [])
        if isinstance(absent, list):
            for row in absent:
                if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] in reference:
                    errors.append(scope + '/' + row['id'] + ': capability differs from window')
    return errors


def signature(observation):
    # All operations are fixed inputs; salted identities and elapsed timings are absent.
    return launch.pool.digest(observation)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--headed', action='store_true')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('output must be a new file')
    report = {'schema_version':1, 'collected_at':datetime.now(timezone.utc).isoformat(),
              'runs':[], 'errors':[], 'qualification':'capability audit, not matching-patch build attestation'}
    try:
        from PIL import features, __version__ as pillow_version
        from playwright.sync_api import sync_playwright
        if not features.check('littlecms2') or not features.check('webp'):
            raise ValueError('Pillow LittleCMS and WebP decoders are required')
        report['browser_sha256'] = launch.pool.file_hash(args.browser)
        report['probe_sha256'] = launch.pool.file_hash(PROBE)
        report['decoders'] = {'pillow':pillow_version, 'littlecms2':features.version('littlecms2'),
                              'webp':features.version('webp')}
        report['launch_args'] = launch.NATIVE_ARGS
        with tempfile.TemporaryDirectory(prefix='chromix-canvas-') as profiles, server() as other, server(other) as origin, sync_playwright() as pw:
            for profile in ('a','a','b'):
                context = pw.chromium.launch_persistent_context(str(Path(profiles) / profile),
                    executable_path=str(args.browser.resolve()), headless=not args.headed,
                    no_viewport=True, chromium_sandbox=True, args=launch.NATIVE_ARGS)
                try:
                    report['browser_version'] = context.browser.version
                    observation = launch.collect_live(context, origin)
                    result = evaluate(observation)
                    report['runs'].append({'profile':profile, 'observation':observation, **result})
                    report['errors'].extend(result['errors'])
                finally:
                    context.close()
            signatures = [signature(run['observation']) for run in report['runs']]
            if len(set(signatures)) != 1:
                report['errors'].append('canvas results changed across restart/profiles')
            report['signatures'] = signatures
        if launch.pool.file_hash(args.browser) != report['browser_sha256']:
            report['errors'].append('browser executable changed')
    except Exception as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    report['skipped_count'] = sum(len(run['skipped']) for run in report['runs'])
    report['status'] = 'failed' if report['errors'] else 'incomplete' if report['skipped_count'] else 'passed'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=True)
        stream.write('\n')
    print(json.dumps({'status':report['status'], 'error_count':len(report['errors']),
                      'first_errors':report['errors'][:12], 'output':str(args.output)}))
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
