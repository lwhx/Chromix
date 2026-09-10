"""Resource policy and workflow wiring tests; no browser builds are launched."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('build_resources', ROOT / 'tools/build_resources.py')
resources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resources)


@pytest.mark.parametrize('cpus,gib,expected', [(4, 15, 4), (32, 64, 24), (32, 128, 32),
                                            (4, 7, 2), (32, 4, 1), (1, 0, 1)])
def test_auto_limits(cpus, gib, expected):
    assert resources.select_jobs('auto', cpus, gib * resources.GIB) == expected


def test_unavailable_memory_is_conservative():
    assert resources.select_jobs('auto', 32, None) == 4
    assert resources.select_jobs('auto', 2, None) == 2


@pytest.mark.parametrize('value', ['0', '-1', '1.5', ' 4', '4 ', '01', '1025', 'AUTO', '4\n', 'auto\n'])
def test_invalid_override(value):
    with pytest.raises(ValueError):
        resources.select_jobs(value, 32, 128 * resources.GIB)


@pytest.mark.parametrize('value', ['1', '8', '64', '1024'])
def test_explicit_override(value):
    assert resources.select_jobs(value, 4, 8 * resources.GIB) == int(value)


def test_github_output(tmp_path):
    output, summary = tmp_path / 'env', tmp_path / 'summary'
    run = subprocess.run([sys.executable, str(ROOT / 'tools/build_resources.py'), '--jobs', '8', '--github-env'],
                         env={**os.environ, 'GITHUB_ENV':str(output), 'GITHUB_STEP_SUMMARY':str(summary)},
                         capture_output=True, text=True, check=True, timeout=20)
    assert json.loads(run.stdout)['ninja_jobs'] == 8
    assert output.read_text().strip() == 'CHROMIX_JOBS=8'
    assert '"ninja_jobs": 8' in summary.read_text()


def test_linux_memory_parser(monkeypatch):
    monkeypatch.setattr(resources.sys, 'platform', 'linux')
    monkeypatch.setattr(resources.Path, 'read_text', lambda _: 'MemTotal: 32000 kB\nMemAvailable: 12000 kB\n')
    assert resources.available_memory() == 12000 * 1024


def test_macos_memory_parser(monkeypatch):
    monkeypatch.setattr(resources.sys, 'platform', 'darwin')
    monkeypatch.setattr(resources.subprocess, 'check_output', lambda *a, **k:
                        'Mach Virtual Memory Statistics: (page size of 16384 bytes)\n'
                        'Pages free: 100.\nPages inactive: 200.\nPages speculative: 50.\nPages wired down: 900.\n')
    assert resources.available_memory() == 350 * 16384


def load(name):
    return yaml.safe_load((ROOT / '.github/workflows' / (name + '.yml')).read_text())


def test_five_platforms_expose_jobs_and_trigger_on_policy_changes():
    for name in ('build-linux-x64', 'build-linux-arm64', 'build-macos-x64', 'build-macos-arm64', 'build-win-x64-github'):
        workflow = load(name)
        events = workflow.get('on', workflow.get(True))
        assert events['workflow_dispatch']['inputs']['compile_jobs']['default'] == 'auto'
        assert 'tools/build_resources.py' in events['push']['paths']
        if name != 'build-win-x64-github':
            assert workflow['jobs']['build']['with']['compile_jobs'] == "${{ inputs.compile_jobs || 'auto' }}"


def test_every_compile_stage_selects_jobs_before_compiling():
    for name, prefix, count in [('build-posix-github', 'posix-', 8), ('build-win-x64-github', 'build-', 12)]:
        workflow = load(name)
        assert 'inputs.compile_jobs' in workflow['env']['CHROMIX_JOBS']
        for stage in range(1, count + 1):
            steps = workflow['jobs'][prefix + str(stage)]['steps']
            selection = next(i for i, s in enumerate(steps) if s.get('name') == 'Select compile parallelism')
            compile_step = next(i for i, s in enumerate(steps) if s.get('id') == 'stage')
            assert selection < compile_step
            assert 'tools/build_resources.py --github-env' in steps[selection]['run']


def test_windows_snapshot_attempt_is_exact():
    workflow = load('build-win-x64-github')
    for stage in range(2, 13):
        steps = workflow['jobs'][f'build-{stage}']['steps']
        download = next(s for s in steps if s.get('name') == 'Download tree from previous run')
        assert '-attempt-${{ inputs.resume_attempt }}-part*' in download['with']['pattern']
        assert download['with']['run-id'] == '${{ inputs.resume_run_id }}'


def test_gn_merge_preserves_mtime_and_header_on_in_place_reruns(tmp_path):
    first, output = tmp_path / 'first.gn', tmp_path / 'args.gn'
    first.write_text('symbol_level = 0\n')
    command = [sys.executable, str(ROOT / 'tools/merge_gn_args.py'), str(output), str(first)]
    subprocess.run(command, check=True, capture_output=True)
    stamp = 1700000000000000000
    os.utime(output, ns=(stamp, stamp))
    for _ in range(3):
        subprocess.run(command[:-1] + [str(output)], check=True, capture_output=True)
        assert output.stat().st_mtime_ns == stamp
        assert output.read_text().count('# Generated.') == 1
    first.write_text('symbol_level = 1\n')
    subprocess.run(command, check=True, capture_output=True)
    assert 'symbol_level = 1' in output.read_text()
    assert output.stat().st_mtime_ns != stamp


def test_windows_keeps_native_profile_by_default():
    workflow = load('build-win-x64-github')
    inputs = workflow.get('on', workflow.get(True))['workflow_dispatch']['inputs']
    assert inputs['build_profile']['default'] == 'native'
    assert inputs['build_profile']['options'] == ['native', 'fast', 'release']
    script = (ROOT / 'build/windows/ci-stage.ps1').read_text()
    assert '-j $CompileJobs chrome' in script
    assert '$mergeArgs += @("--build-profile", $BuildProfile)' in script


@pytest.mark.parametrize('jobs,valid', [('8', True), ('1', True), ('0', False), ('auto', False)])
def test_windows_ninja_receives_validated_jobs(jobs, valid):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if not shell:
        pytest.skip('PowerShell is unavailable')
    source = (ROOT / 'build/windows/ci-stage.ps1').read_text()
    start = source.index('$CompileJobs = 4')
    end = source.index('\nif ($RestoredUpstream)', start)
    script = '''$ErrorActionPreference = "Stop"
$OutDir = "fixture out"
$Src = "fixture source"
$Ninja = "fixture ninja"
$ninjaBudget = 20
function Invoke-Tracked {
  param($File, $ArgList, $Cwd, $TimeoutSec)
  if ($ArgList -ne "-C `"$OutDir`" -j $env:CHROMIX_JOBS chrome") { throw "wrong jobs" }
  Write-Host "NINJA_VERIFIED"
}
''' + source[start:end]
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script],
                            env={**os.environ, 'CHROMIX_JOBS': jobs},
                            capture_output=True, text=True, timeout=20)
    assert (result.returncode == 0) == valid, result.stdout + result.stderr
    assert ('NINJA_VERIFIED' in result.stdout) == valid


@pytest.mark.parametrize('profile,value', [('fast', 'false'), ('release', 'true')])
def test_windows_profile_cli_argument_order(tmp_path, profile, value):
    source, output = tmp_path / 'input.gn', tmp_path / 'args.gn'
    source.write_text('is_official_build = true\n')
    subprocess.run([sys.executable, str(ROOT / 'tools/merge_gn_args.py'), str(output),
                    '--build-profile', profile, str(source)],
                   check=True, capture_output=True, timeout=20)
    assert f'thin_lto_enable_optimizations = {value}' in output.read_text()
