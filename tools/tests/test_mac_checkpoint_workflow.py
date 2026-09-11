"""Mac cross-run checkpoints must migrate before normal build verification."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[2]


def workflow(name):
    return yaml.safe_load((REPO / '.github/workflows' / name).read_text())


class MacCheckpointWorkflowTest(unittest.TestCase):
    def test_only_mac_entries_expose_checkpoint_selection(self):
        for platform in ('macos', 'linux'):
            for arch in ('x64', 'arm64'):
                entry = workflow(f'build-{platform}-{arch}.yml')
                events = entry.get('on', entry.get(True))
                inputs = events['workflow_dispatch']['inputs']
                for field, default in (('resume_run_id', ''), ('resume_tree_stage', '7'), ('resume_attempt', '1'), ('resume_artifact_ids', '')):
                    if platform == 'macos':
                        self.assertEqual(inputs[field]['type'], 'string')
                        self.assertEqual(inputs[field]['default'], default)
                        self.assertIn(f'inputs.{field}', entry['jobs']['build']['with'][field])
                    else:
                        self.assertNotIn(field, inputs)
                        self.assertNotIn(field, entry['jobs']['build']['with'])

    def test_donor_validation_precedes_exact_checkout_download_and_migration(self):
        stages = workflow('build-posix-github.yml')['jobs']
        steps = stages['posix-1']['steps']
        names = {step.get('name'): step for step in steps}
        ordered = ['Validate selected Mac checkpoint', 'Check out checkpoint patch definitions',
                   'Download selected Mac checkpoint', 'Restore and migrate selected Mac checkpoint', 'Run stage 1']
        self.assertEqual(sorted(ordered, key=lambda name: steps.index(names[name])), ordered)
        for name in ordered[:-1]:
            self.assertEqual(names[name]['if'], "inputs.resume_run_id != ''")
        checkout = names[ordered[1]]['with']
        self.assertEqual(checkout['ref'], '${{ steps.resume.outputs.head_sha }}')
        self.assertFalse(checkout['persist-credentials'])
        download = names[ordered[2]]['with']
        self.assertEqual(download['pattern'], '${{ steps.resume.outputs.pattern }}')
        self.assertEqual(download['run-id'], '${{ inputs.resume_run_id }}')
        self.assertTrue(download['merge-multiple'])
        validate = names[ordered[0]]
        self.assertEqual(validate['env']['SNAPSHOT_ATTEMPT'], '${{ inputs.resume_attempt }}')
        self.assertIn('test "$BUILD_PLATFORM" = macos', validate['run'])
        self.assertIn('test "$CACHE_REQUIRED" = true', validate['run'])
        migrate = names[ordered[3]]['run']
        self.assertIn('set -euo pipefail', migrate)
        self.assertLess(migrate.index('zstd -d'), migrate.index('migrate_restored_snapshot.py'))
        self.assertNotIn('.chromix-previous-repo/tools/', migrate)
        for number in range(2, 9):
            self.assertNotIn('migrate_restored_snapshot.py', str(stages[f'posix-{number}']))
        self.assertNotIn('needs', stages['posix-1'])

    def test_failed_terminal_stage_can_upload_only_verified_checkpoint(self):
        for number in range(1, 9):
            stage = workflow('build-posix-github.yml')['jobs'][f'posix-{number}']
            steps = stage['steps']
            build = next(step for step in steps if step.get('id') == 'stage')
            self.assertNotIn('continue-on-error', build)
            verify = next(step for step in steps if step.get('id') == 'checkpoint')
            self.assertIn('!cancelled()', verify['if'])
            self.assertIn("steps.stage.outputs.upload_snapshot == 'true'", verify['if'])
            self.assertNotIn('success()', verify['if'])
            uploads = [step for step in steps if step.get('name', '').startswith('Upload tree part')]
            self.assertEqual(len(uploads), 4)
            for upload in uploads:
                self.assertIn('!cancelled()', upload['if'])
                self.assertIn("steps.checkpoint.outcome == 'success'", upload['if'])
                self.assertNotIn('success()', upload['if'])
            final = next(step for step in steps if step.get('name') == 'Upload final bundle')
            self.assertEqual(final['if'], "steps.stage.outputs.finished == 'true'")

    def test_sdk_preflight_precedes_large_downloads_on_every_mac_stage(self):
        for number in range(1, 9):
            steps = workflow('build-posix-github.yml')['jobs'][f'posix-{number}']['steps']
            inspect = next(step for step in steps if step.get('name') == 'Verify complete Mac SDK contents')
            self.assertEqual(inspect['if'], "runner.os == 'macOS' && inputs.use_upstream_cache")
            self.assertIn('inspect_macos_sdk.py', inspect['run'])
            for step in steps:
                if step.get('uses') == 'actions/download-artifact@v4':
                    self.assertLess(steps.index(inspect), steps.index(step))

    def test_resume_shell_parses_under_bash32(self):
        shell = Path.home() / '.local/bash-3.2-for-ci/bash'
        if not shell.exists():
            self.skipTest('bash 3.2 unavailable')
        steps = workflow('build-posix-github.yml')['jobs']['posix-1']['steps']
        with tempfile.TemporaryDirectory() as temp:
            for index, step in enumerate(steps):
                if 'run' not in step or 'checkpoint' not in step.get('name', '').lower():
                    continue
                script = Path(temp) / f'{index}.sh'
                script.write_text(step['run'])
                result = subprocess.run([str(shell), '-n', str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
