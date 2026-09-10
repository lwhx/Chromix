"""Measured native launch: validate bundles, then verify the actual context.

There is no synthetic fallback and no per-field injection. Pool mode permits
only exact host records; live mismatch closes the context before returning it.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

from . import device_pool as pool
from ._device_host import host_inventory
from ._device_headers import header_errors

PROBE = Path(__file__).with_name('device_probe.js')
DEFAULT_TIMEOUT = 30000
NATIVE_ARGS = ['--fingerprint=off', '--uxr-webgl-real', '--uxr-disable-fingerprint-noise',
               '--no-first-run', '--no-default-browser-check', '--disable-background-networking']


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError('device_pool must be an object')
    unknown = set(options) - {'host', 'records', 'seed', 'max_age_hours'}
    if unknown or not options.get('host') or not isinstance(options.get('records'), list):
        raise ValueError('device_pool requires host, records, seed; unknown keys are rejected')
    seed = options.get('seed')
    if isinstance(seed, str):
        try:
            seed = int(seed, 0) if seed.startswith('0x') else int(seed)
        except ValueError as error:
            raise ValueError('invalid device pool seed') from error
    if type(seed) is not int or not 0 < seed < 2**64:
        raise ValueError('device pool seed must be nonzero uint64')
    age = options.get('max_age_hours', 24)
    if not pool.finite(age, True) or age > 168:
        raise ValueError('max_age_hours must be in (0, 168]')
    return {**options, 'seed':seed, 'max_age_hours':age}


def prepare(options, binary, headless, persistent=None):
    options = validate_options(options)
    if type(headless) is not bool:
        raise ValueError('headless must be boolean')
    if persistent is not None and not os.fspath(persistent):
        raise ValueError('user_data_dir must not be empty')
    def read(path):
        path = Path(path)
        return pool.validate_record(pool.load_json(path), path.parent)
    host = read(options['host'])
    collected = datetime.fromisoformat(host['provenance']['collected_at'])
    hours = (datetime.now(timezone.utc) - collected).total_seconds() / 3600
    if not 0 <= hours <= options['max_age_hours']:
        raise ValueError('host evidence is expired or future-dated; collect it again')
    if pool.file_hash(binary) != host['provenance']['browser_sha256']:
        raise ValueError('launch executable does not match host evidence')
    browser_evidence = pool.load_json(Path(options['host']).parent / host['evidence']['browser']['path'])
    if browser_evidence.get('headless') is not headless:
        raise ValueError('headed/headless mode differs from host evidence')
    if host_inventory() != host['device']['host']:
        raise ValueError('native host inventory changed; collect host evidence again')
    result = pool.select_record([read(p) for p in options['records']], host, options['seed'])
    # Native fallback still uses and verifies the current host, never a candidate.
    expected = result['record'] if result['record'] is not None else host
    manifest = {'schema_version':1, 'record_id':expected['record_id'],
                'device_sha256':pool.digest(expected['device']),
                'browser_sha256':host['provenance']['browser_sha256'],
                'seed':str(options['seed']), 'status':result['status']}
    if persistent is not None:
        bind_profile(Path(persistent), manifest)
    return {'expected':expected, 'manifest':manifest, 'rejected':result['rejected'],
            'binary':str(Path(binary).resolve()), 'headless':headless,
            'args':list(NATIVE_ARGS)}


def bind_profile(directory, manifest):
    """Atomic binding shared by Node and Python; never rotate on pool changes."""
    import tempfile
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / '.chromix-device-profile.json'
    data = pool.canonical(manifest) + b'\n'
    fd, temporary = tempfile.mkstemp(prefix='.chromix-device-profile.', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        if pool.load_json(path) != manifest:
            raise ValueError('persistent profile already bound to a different record/seed; use its original pool or a new profile')
    finally:
        os.unlink(temporary)


def runtime_projection(value):
    value = pool.stable_observation({'window':value})['window']
    # Device family is independent of the user-resized window. Display/DPR and
    # CSS consistency are checked separately; no fixed 85px toolbar assumption.
    display = value.get('display', {})
    display.pop('window', None)
    display.pop('viewport', None)
    return value


def verify_observation(observation, prepared):
    errors = pool.observation_errors(observation)
    expected = prepared['expected']['device']['surfaces']
    for scope in pool.SCOPES:
        if observation.get(scope, {}).get('probeVersion', 1) >= 2:
            errors.extend(f'{scope}: {error}' for error in header_errors(observation[scope],
                          require_hints=scope in ('window', 'iframe')))
        if runtime_projection(observation.get(scope, {})) != runtime_projection(expected[scope]):
            errors.append(f'{scope}: live native capabilities differ from selected record')
    if pool.file_hash(prepared['binary']) != prepared['manifest']['browser_sha256']:
        errors.append('browser executable changed during launch')
    if errors:
        raise ValueError('; '.join(errors))
    return {**prepared['manifest'], 'runtime_verified':True,
            'rejected':prepared['rejected']}


WORKER_BASE = "importScripts('/probe.js'); const send=p=>chromixDeviceProbe().then(value=>p.postMessage({value}),e=>p.postMessage({error:String(e)}));\n"
ASSETS = {
    '/atomic.js': "onmessage=e=>{const a=new Int32Array(e.data);Atomics.add(a,0,7);postMessage(Atomics.load(a,0));};",
    '/worker.js': WORKER_BASE + 'send(self);',
    '/shared.js': WORKER_BASE + 'onconnect=e=>send(e.ports[0]);',
    '/service.js': WORKER_BASE + "addEventListener('install',e=>e.waitUntil(skipWaiting()));addEventListener('activate',e=>e.waitUntil(clients.claim()));addEventListener('message',e=>e.waitUntil(send(e.ports[0])));",
}
WORKER_EVAL = """async kind => {
  let worker,port,registration,channel,timer;
  try {
    if(kind==='worker'){worker=new Worker('/worker.js');port=worker;}
    else if(kind==='shared_worker'){worker=new SharedWorker('/shared.js');port=worker.port;}
    else {registration=await navigator.serviceWorker.register('/service.js');
      await navigator.serviceWorker.ready;channel=new MessageChannel();port=channel.port1;
      registration.active.postMessage('probe',[channel.port2]);}
    return await new Promise((resolve,reject)=>{
      timer=setTimeout(()=>reject(Error(kind+' timeout')),15000);
      port.onmessage=e=>e.data.error?reject(Error(e.data.error)):resolve(e.data.value);
      port.onmessageerror=()=>reject(Error('message error'));
      if(worker)worker.onerror=e=>reject(Error(e.message||'worker error'));
      port.start?.();
    });
  }finally{clearTimeout(timer);if(kind==='worker')worker?.terminate();else port?.close();
    channel?.port2.close();if(registration)await registration.unregister();}
}"""


def bounded(script):
    return """async argument => {
      let timer;
      try { return await Promise.race([(%s)(argument),new Promise((_,reject)=>{
        timer=setTimeout(()=>reject(Error('device probe timeout')),30000);
      })]); } finally { clearTimeout(timer); }
    }""" % script


PROBE_EVAL = bounded('() => chromixDeviceProbe()')
WORKER_EVAL = bounded(WORKER_EVAL)


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
            self.send_error(403)
            return
        if self.path == '/headers':
            content = json.dumps({'headers':{key.lower():value for key, value in self.headers.items()}})
            mime = 'application/json'
        elif self.path in ('/', '/frame'):
            content = '<!doctype html><script src="/probe.js"></script>'
            if self.path == '/':
                content += '<iframe src="/frame"></iframe>'
            mime = 'text/html'
        elif self.path == '/probe.js':
            content, mime = PROBE.read_text(encoding='utf-8'), 'text/javascript'
        elif self.path in ASSETS:
            content, mime = ASSETS[self.path], 'text/javascript'
        else:
            self.send_error(404)
            return
        data = content.encode()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Accept-CH', 'Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform, '
                         'Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Platform-Version, '
                         'Sec-CH-UA-Full-Version-List, Sec-CH-UA-Model, Sec-CH-UA-WoW64')
        if getattr(self.server, 'isolated', False):
            self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
            self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


@contextmanager
def probe_server(isolated=False):
    server = ThreadingHTTPServer(('127.0.0.1', 0), ProbeHandler)
    server.isolated = isolated
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def collect_live(context, origin):
    page = context.new_page()
    try:
        page.goto(origin, wait_until='load', timeout=DEFAULT_TIMEOUT)
        observation = {'window':page.evaluate(PROBE_EVAL)}
        observation['iframe'] = page.frame(url=origin + '/frame').evaluate(PROBE_EVAL)
        for scope in pool.SCOPES[2:]:
            observation[scope] = page.evaluate(WORKER_EVAL, scope)
        return observation
    finally:
        page.close()


async def collect_live_async(context, origin):
    page = await context.new_page()
    try:
        await page.goto(origin, wait_until='load', timeout=DEFAULT_TIMEOUT)
        observation = {'window':await page.evaluate(PROBE_EVAL)}
        observation['iframe'] = await page.frame(url=origin + '/frame').evaluate(PROBE_EVAL)
        for scope in pool.SCOPES[2:]:
            observation[scope] = await page.evaluate(WORKER_EVAL, scope)
        return observation
    finally:
        await page.close()


def reject_overrides(kwargs):
    # Measured mode deliberately has one configuration owner. Supporting a
    # new option requires proving it does not invalidate the selected record.
    unknown = set(kwargs) - {'headless', 'browser_version', 'release_channel', 'user_data_dir'}
    if unknown:
        raise ValueError('device_pool mode rejects launch/context overrides: ' + ', '.join(sorted(unknown)))
    if 'user_data_dir' in kwargs and not kwargs['user_data_dir']:
        raise ValueError('user_data_dir must not be empty')


def launch_measured(options, *, asynchronous=False, **kwargs):
    reject_overrides(kwargs)
    if asynchronous:
        return _launch_async(options, kwargs)
    from .api import ensure_binary
    from playwright.sync_api import sync_playwright
    binary = ensure_binary(browser_version=kwargs.get('browser_version'), release_channel=kwargs.get('release_channel'))
    headless = kwargs.get('headless', True)
    directory = kwargs.get('user_data_dir')
    prepared = prepare(options, binary, headless, directory)
    pw = sync_playwright().start()
    browser = context = None
    try:
        launch = dict(executable_path=str(binary), headless=headless, args=prepared['args'], chromium_sandbox=True)
        if directory is None:
            browser = pw.chromium.launch(**launch)
            context = browser.new_context(no_viewport=True, service_workers='allow')
        else:
            context = pw.chromium.launch_persistent_context(str(directory), no_viewport=True, service_workers='allow', **launch)
        context.set_default_timeout(DEFAULT_TIMEOUT)
        with probe_server() as origin:
            verified = verify_observation(collect_live(context, origin), prepared)
        context._chromix_device_profile = verified
    except BaseException:
        try:
            try:
                if context is not None:
                    context.close()
            finally:
                if browser is not None:
                    browser.close()
        finally:
            pw.stop()
        raise
    original = context.close
    def close(*args, **kw):
        try:
            original(*args, **kw)
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                pw.stop()
    context.close = close
    return context


async def _launch_async(options, kwargs):
    from .api import ensure_binary
    from playwright.async_api import async_playwright
    loop = asyncio.get_running_loop()
    binary = await loop.run_in_executor(None, partial(ensure_binary, browser_version=kwargs.get('browser_version'), release_channel=kwargs.get('release_channel')))
    headless = kwargs.get('headless', True)
    directory = kwargs.get('user_data_dir')
    prepared = await loop.run_in_executor(None, prepare, options, binary, headless, directory)
    pw = await async_playwright().start()
    browser = context = None
    try:
        launch = dict(executable_path=str(binary), headless=headless, args=prepared['args'], chromium_sandbox=True)
        if directory is None:
            browser = await pw.chromium.launch(**launch)
            context = await browser.new_context(no_viewport=True, service_workers='allow')
        else:
            context = await pw.chromium.launch_persistent_context(str(directory), no_viewport=True, service_workers='allow', **launch)
        context.set_default_timeout(DEFAULT_TIMEOUT)
        with probe_server() as origin:
            verified = verify_observation(await collect_live_async(context, origin), prepared)
        context._chromix_device_profile = verified
    except BaseException:
        try:
            try:
                if context is not None:
                    await context.close()
            finally:
                if browser is not None:
                    await browser.close()
        finally:
            await pw.stop()
        raise
    original = context.close
    async def close(*args, **kw):
        try:
            await original(*args, **kw)
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                await pw.stop()
    context.close = close
    return context


def main():
    """Node bridge uses framed JSON over pipes; no shell or repository imports."""
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'verify', 'serve'])
    args = parser.parse_args()
    try:
        if args.command == 'serve':
            with probe_server() as origin:
                print(json.dumps({'origin':origin, 'worker_eval':WORKER_EVAL, 'probe_eval':PROBE_EVAL}), flush=True)
                sys.stdin.readline()
        else:
            request = json.loads(sys.stdin.read())
            if args.command == 'prepare':
                result = prepare(request['options'], request['binary'], request['headless'], request.get('persistent'))
            else:
                result = verify_observation(request['observation'], request['prepared'])
            print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except Exception as error:
        print(json.dumps({'error':str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
