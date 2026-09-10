"""Measured launch gates using explicit synthetic fixtures, not hardware evidence."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'sdk/python'))
from chromix import _device_launch as launch
from test_device_pool import bundle, observation, seal


@pytest.fixture
def measured(tmp_path, monkeypatch):
    record = bundle(tmp_path)
    binary = tmp_path / 'browser.exe'
    binary.write_bytes(b'fixture executable')
    browser_path = tmp_path / 'browser.json'
    browser = json.loads(browser_path.read_text())
    browser['headless'] = True
    browser['binary']['sha256'] = launch.pool.file_hash(binary)
    browser_path.write_text(json.dumps(browser))
    record['evidence']['browser']['sha256'] = launch.pool.file_hash(browser_path)
    record['provenance'].update(browser_sha256=launch.pool.file_hash(binary),
                                collected_at=datetime.now(timezone.utc).isoformat())
    path = tmp_path / 'record.json'
    def save():
        path.write_text(json.dumps(seal(record)))
    save()
    monkeypatch.setattr(launch, 'host_inventory', lambda: deepcopy(record['device']['host']))
    return SimpleNamespace(record=record, save=save, binary=binary,
                           options={'host':str(path), 'records':[str(path)], 'seed':42})


@pytest.mark.parametrize('change', [{'seed':0}, {'seed':True}, {'seed':2**64},
    {'seed':'bad'}, {'records':{}}, {'unknown':1}, {'max_age_hours':0},
    {'max_age_hours':169}, {'max_age_hours':float('nan')}])
def test_invalid_config(measured, change):
    with pytest.raises(ValueError):
        launch.prepare({**measured.options, **change}, measured.binary, True)


@pytest.mark.parametrize('age', [-1, 25])
def test_evidence_age(measured, age):
    measured.record['provenance']['collected_at'] = (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()
    measured.save()
    with pytest.raises(ValueError, match='expired or future'):
        launch.prepare(measured.options, measured.binary, True)


@pytest.mark.parametrize('fault', ['binary', 'headless', 'host'])
def test_host_mismatch(measured, monkeypatch, fault):
    if fault == 'binary':
        measured.binary.write_bytes(b'changed')
    if fault == 'host':
        monkeypatch.setattr(launch, 'host_inventory', lambda: {})
    with pytest.raises(ValueError, match='does not match|differs|inventory changed'):
        launch.prepare(measured.options, measured.binary, fault != 'headless')


def test_native_fallback_and_whole_record(measured):
    prepared = launch.prepare(measured.options, measured.binary, True)
    assert prepared['manifest']['status'] == 'compatible'
    assert prepared['expected'] == measured.record
    assert '--fingerprint=off' in prepared['args']
    prepared = launch.prepare({**measured.options, 'records':[]}, measured.binary, True)
    assert prepared['manifest']['status'] == 'native'
    assert prepared['expected'] == measured.record
    assert launch.verify_observation(observation(), prepared)['runtime_verified']


def test_runtime_mismatch_and_executable_change(measured):
    prepared = launch.prepare(measured.options, measured.binary, True)
    changed = observation()
    changed['worker']['execution']['cores'] = 16
    with pytest.raises(ValueError, match='worker'):
        launch.verify_observation(changed, prepared)
    measured.binary.write_bytes(b'changed')
    with pytest.raises(ValueError, match='executable changed'):
        launch.verify_observation(observation(), prepared)


def test_profile_atomic_binding_and_no_rotation(tmp_path):
    manifest = {'seed':'42', 'record_id':'fixture'}
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: launch.bind_profile(tmp_path, manifest), range(16)))
    before = (tmp_path / '.chromix-device-profile.json').read_bytes()
    with pytest.raises(ValueError, match='already bound'):
        launch.bind_profile(tmp_path, {**manifest, 'seed':'43'})
    assert (tmp_path / '.chromix-device-profile.json').read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('key', ['args', 'proxy', 'viewport', 'locale', 'user_agent', 'stealth_args', 'fonts_dir'])
def test_overrides_rejected_before_launch(key):
    with pytest.raises(ValueError, match='rejects'):
        launch.launch_measured({}, **{key:None})


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('close_fails', [False, True])
@pytest.mark.parametrize('cancelled', [False, True])
def test_live_failure_closes_browser_and_driver(monkeypatch, asynchronous, close_fails, cancelled):
    from chromix import api
    calls = []
    def close_context():
        calls.append('context')
        if close_fails:
            raise RuntimeError('close failure')
    context = SimpleNamespace(close=close_context, set_default_timeout=lambda _: None)
    browser = SimpleNamespace(new_context=lambda **_: context, close=lambda: calls.append('browser'))
    pw = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_:browser), stop=lambda: calls.append('driver'))
    monkeypatch.setattr(api, 'ensure_binary', lambda **_: 'fixture')
    monkeypatch.setattr(launch, 'prepare', lambda *_: {'args':[]})
    def fail(*_):
        if cancelled:
            raise asyncio.CancelledError('live mismatch')
        raise ValueError('live mismatch')
    monkeypatch.setattr(launch, 'collect_live', fail)
    if asynchronous:
        async def context_close():
            close_context()
        async def browser_close():
            calls.append('browser')
        async def stop():
            calls.append('driver')
        async def new_context(**_):
            return context
        async def start_browser(**_):
            return browser
        async def start():
            return pw
        async def collect(*_):
            fail()
        context.close, browser.close, browser.new_context = context_close, browser_close, new_context
        pw.stop, pw.chromium.launch = stop, start_browser
        monkeypatch.setattr(launch, 'collect_live_async', collect)
        monkeypatch.setitem(sys.modules, 'playwright.async_api', SimpleNamespace(
            async_playwright=lambda: SimpleNamespace(start=start)))
    else:
        monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(
            sync_playwright=lambda: SimpleNamespace(start=lambda:pw)))
    with pytest.raises((ValueError, RuntimeError, asyncio.CancelledError), match='live mismatch|close failure'):
        result = launch.launch_measured({}, asynchronous=asynchronous)
        if asynchronous:
            asyncio.run(result)
    assert calls == ['context', 'browser', 'driver']
