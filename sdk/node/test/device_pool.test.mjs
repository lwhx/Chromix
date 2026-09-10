import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runInNewContext } from 'node:vm';
import { bridge, launchMeasured, measuredOptions, probeExpression } from '../_device_pool.js';
import { buildContextOptions, buildLaunchOptions, launchContext, launchPersistentContext } from '../index.js';

test('default context uses native viewport without synthetic display selection', () => {
  assert.equal(buildContextOptions({ args:['--fingerprint=42'] }).viewport, null);
});

test('uint64 seed is lossless across Python bridge', () => {
  assert.equal(measuredOptions({devicePool:{seed:18446744073709551615n}}).pool.seed, '18446744073709551615');
  assert.throws(() => measuredOptions({devicePool:{seed:2**64}}), /Safe|BigInt/);
});

test('measured mode rejects field overrides', () => {
  for (const key of ['args', 'proxy', 'viewport', 'locale', 'launchOptions', 'contextOptions'])
    assert.throws(() => measuredOptions({devicePool:{seed:42}, [key]:null}), /rejects/);
  for (const devicePool of [null, [], 'record'])
    assert.throws(() => measuredOptions({devicePool}), /object/);
});

test('launch option types stay consistent across SDKs', () => {
  for (const headless of [null, 'true', 1])
    assert.throws(() => measuredOptions({devicePool:{}, headless}), /boolean/);
  for (const userDataDir of [null, '', 1])
    assert.throws(() => measuredOptions({devicePool:{}, userDataDir}), /nonempty/);
});

test('string probe expressions invoke functions and pass worker scope', async () => {
  assert.equal(await runInNewContext(probeExpression('async () => 42')), 42);
  const scope = 'worker"\\';
  assert.equal(await runInNewContext(probeExpression('async scope => scope', scope)), scope);
});

test('option builders cannot bypass runtime validation', async () => {
  assert.throws(() => buildContextOptions({devicePool:{}}), /runtime verification/);
  await assert.rejects(buildLaunchOptions({devicePool:{}}), /runtime verification/);
  await assert.rejects(launchContext({devicePool:{}, viewport:null}), /rejects/);
  await assert.rejects(launchPersistentContext({devicePool:{}}), /userDataDir/);
});

test('missing validator fails before browser launch', async () => {
  const python = 'chromix-nonexistent-python-for-test';
  await assert.rejects(bridge(python, 'prepare', {}), /ENOENT/);
  const chromium = {launch:() => assert.fail('must not launch')};
  await assert.rejects(launchMeasured(chromium, 'unused', {devicePool:{python, seed:42}}), /ENOENT/);
});
