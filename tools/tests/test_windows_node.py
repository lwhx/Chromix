"""Windows build-only WASM compatibility regression checks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / 'build/windows/configure-node.ps1'
NODE = os.environ.get('CHROMIX_TEST_NODE') or shutil.which('node')


class NodeIntegrationTest(unittest.TestCase):
    def test_both_entrypoints_configure_bundled_node_before_compilation(self):
        for name in ('ci-stage.ps1', 'build.ps1'):
            source = (HELPER.parent / name).read_text()
            self.assertIn('configure-node.ps1', source)
            self.assertIn("-NodePath (Join-Path $Src 'third_party\\node\\win\\node.exe')", source)
            compile_offset = (source.index('$rc = Invoke-Tracked -File $Ninja')
                              if name == 'ci-stage.ps1' else source.index('& $Ninja -C'))
            self.assertLess(source.index('configure-node.ps1'), compile_offset)


@unittest.skipUnless(os.name == 'nt' and NODE, 'Windows and Node required')
class WindowsNodeExecutionTest(unittest.TestCase):
    def run_helper(self, shell, options='', node=NODE):
        env = {**os.environ, 'TEST_NODE': str(node), 'TEST_HELPER': str(HELPER),
               'NODE_OPTIONS': options}
        script = r'''
$ErrorActionPreference = 'Stop'
$before = $env:NODE_OPTIONS
try {
  & $env:TEST_HELPER -NodePath $env:TEST_NODE
  & $env:TEST_HELPER -NodePath $env:TEST_NODE
  if ($env:NODE_OPTIONS -notmatch '--disable-wasm-trap-handler$') { throw 'flag missing' }
  & $env:TEST_NODE -e 'const m = new WebAssembly.Memory({initial:1}); if(m.buffer.byteLength !== 65536) process.exit(8);'
  if ($LASTEXITCODE -ne 0) { throw 'child did not inherit valid options' }
  @{options=$env:NODE_OPTIONS; before=$before} | ConvertTo-Json -Compress
} catch {
  @{options=$env:NODE_OPTIONS; before=$before; error=$_.Exception.Message} | ConvertTo-Json -Compress
  exit 1
}
'''
        result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script],
                                env=env, capture_output=True, text=True, timeout=30)
        return result, json.loads(result.stdout.strip().splitlines()[-1])

    def test_real_node_accepts_flag_and_preserves_options_without_duplicates(self):
        for shell in ('powershell', 'pwsh'):
            if not shutil.which(shell):
                continue
            for options in ('', '--max-old-space-size=512', '--disable-wasm-trap-handler'):
                with self.subTest(shell=shell, options=options):
                    result, data = self.run_helper(shell, options)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(data['options'].count('--disable-wasm-trap-handler'), 1)
                    self.assertTrue(data['options'].startswith(options))

    def test_unsupported_options_fail_and_restore_original_environment(self):
        options = '--chromix-invalid-node-option'
        result, data = self.run_helper('powershell', options)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(data['options'], options)
        self.assertIn('compatibility probe failed', data['error'])

    def test_missing_bundled_node_does_not_fall_back_to_path(self):
        result, data = self.run_helper('powershell', '--max-old-space-size=512',
                                       REPO / '.missing-node.exe')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Node is missing', data['error'])
        self.assertEqual(data['options'], data['before'])
