import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import inspect_macos_sdk as inspect


class MacSdkInspectionTest(unittest.TestCase):
    def test_untracked_compiler_overrides_fail_before_hash_or_download(self):
        for name in ('C_INCLUDE_PATH', 'OBJC_INCLUDE_PATH', 'CCC_OVERRIDE_OPTIONS', 'CLANG_CONFIG_FILE'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                report = Path(temp) / 'sdk.json'
                with patch.dict(inspect.os.environ, {name: 'fixture'}, clear=True), \
                        patch.object(inspect.subprocess, 'run') as run, \
                        patch('sys.argv', ['inspect', '--report', str(report)]):
                    with self.assertRaisesRegex(SystemExit, 'Unsupported compiler environment'):
                        inspect.main()
                    run.assert_not_called()
                self.assertEqual(json.loads(report.read_text()), {'unsupported_environment': [name]})

    def test_explicit_sdkroot_is_verified_and_duplicate_aliases_scan_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sdk = root / 'sdk'
            sdk.mkdir()
            (sdk / 'header.h').write_text('header')
            broken = root / 'broken'
            broken.mkdir()
            (broken / 'link').symlink_to('missing')
            alias = root / 'alias'
            alias.symlink_to(sdk, target_is_directory=True)
            for configured, failure in ((broken, True), (alias, False)):
                report = root / 'report.json'
                with patch.dict(inspect.os.environ, {'SDKROOT': str(configured)}, clear=True), \
                        patch.object(inspect.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, str(sdk) + '\n')), \
                        patch.object(inspect, 'sdk_content_identity', wraps=inspect.sdk_content_identity) as scan, \
                        patch('sys.argv', ['inspect', '--report', str(report)]):
                    if failure:
                        with self.assertRaisesRegex(SystemExit, 'SDK content verification failed'):
                            inspect.main()
                    else:
                        self.assertEqual(inspect.main(), 0)
                    self.assertEqual(scan.call_count, 2 if failure else 1)
                self.assertEqual(len(json.loads(report.read_text())['sdks']), 2 if failure else 1)

    def test_complete_and_incomplete_scans_write_diagnostics(self):
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as temp:
                report = Path(temp) / 'sdk.json'
                identity = {'complete': complete}
                with patch.object(inspect.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '/sdk\n')) as run, \
                        patch.object(inspect, 'sdk_content_identity', return_value=identity), \
                        patch.object(inspect, 'validated_sdk_content', return_value=complete), \
                        patch('sys.argv', ['inspect', '--report', str(report)]):
                    if complete:
                        self.assertEqual(inspect.main(), 0)
                    else:
                        with self.assertRaisesRegex(SystemExit, 'SDK content verification failed'):
                            inspect.main()
                data = json.loads(report.read_text())
                self.assertEqual(data['content'], identity)
                self.assertEqual(data['sdk_path'], '/sdk')
                self.assertGreaterEqual(data['seconds'], 0)
                self.assertEqual(run.call_args.kwargs['timeout'], 30)


if __name__ == '__main__':
    unittest.main()
