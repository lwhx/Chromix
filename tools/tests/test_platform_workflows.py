"""Platform entrypoints must dispatch and fail independently."""
from pathlib import Path
import unittest

import yaml

REPO = Path(__file__).resolve().parents[2]
DIRECTORY = REPO / '.github/workflows'
PLATFORMS = {
    'build-linux-x64': ('linux', 'x64', 'ubuntu-22.04', 'chromix-linux-x64'),
    'build-linux-arm64': ('linux', 'arm64', None, 'chromix-linux-arm64'),
    'build-macos-x64': ('macos', 'x64', 'macos-15-intel', 'chromix-mac-x64'),
    'build-macos-arm64': ('macos', 'arm64', 'macos-15', 'chromix-mac-arm64'),
}


def load(name):
    return yaml.safe_load((DIRECTORY / (name + '.yml')).read_text())


def events(workflow):
    return workflow.get('on', workflow.get(True))


class PlatformWorkflowTest(unittest.TestCase):
    def test_exactly_five_build_entrypoints(self):
        names = {load(path.stem)['name'] for path in DIRECTORY.glob('build-*.yml')
                 if 'workflow_dispatch' in events(load(path.stem))}
        self.assertEqual(names, set(PLATFORMS) | {'build-win-x64-github'})
        self.assertFalse((DIRECTORY / 'build-cross-platform.yml').exists())
        self.assertEqual(set(events(load('build-posix-github'))), {'workflow_call'})

    def test_posix_entries_keep_platform_inputs_and_isolation(self):
        groups = set()
        for name, (platform, arch, runner, artifact) in PLATFORMS.items():
            with self.subTest(name=name):
                workflow = load(name)
                self.assertEqual(workflow['name'], name)
                self.assertEqual(set(events(workflow)), {'push', 'workflow_dispatch'})
                self.assertEqual(events(workflow)['push']['branches'], ['main'])
                self.assertTrue(events(workflow)['workflow_dispatch']['inputs']['use_upstream_cache']['default'])
                self.assertFalse(workflow['concurrency']['cancel-in-progress'])
                groups.add(workflow['concurrency']['group'])
                self.assertEqual(set(workflow['jobs']), {'build'})
                job = workflow['jobs']['build']
                self.assertNotIn('needs', job)
                self.assertNotIn('strategy', job)
                self.assertEqual(job['uses'], './.github/workflows/build-posix-github.yml')
                self.assertEqual(job['secrets'], 'inherit')
                inputs = job['with']
                self.assertEqual((inputs['platform'], inputs['arch'], inputs['artifact']),
                                 (platform, arch, artifact))
                self.assertEqual(inputs['max-stages'], 8)
                self.assertIn("github.event_name != 'workflow_dispatch' || inputs.use_upstream_cache",
                              inputs['use_upstream_cache'])
                if runner:
                    self.assertEqual(inputs['runner'], runner)
                paths = events(workflow)['push']['paths']
                self.assertIn(f'.github/workflows/{name}.yml', paths)
                self.assertIn('build/*', paths)
                self.assertNotIn('build/**', paths)
                self.assertIn(f'build/{platform}/**', paths)
                self.assertNotIn('build/windows/**', paths)
        self.assertEqual(len(groups), 4)
        self.assertNotIn(load('build-win-x64-github')['concurrency']['group'], groups)

    def test_windows_push_requires_cache_and_keeps_manual_resume(self):
        workflow = load('build-win-x64-github')
        self.assertIn('push', events(workflow))
        self.assertEqual(events(workflow)['push']['branches'], ['main'])
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertIn("github.event_name == 'push' || inputs.use_upstream_cache",
                      workflow['env']['CHROMIX_USE_UPSTREAM_CACHE'])
        stage = next(step for step in workflow['jobs']['build-1']['steps'] if step.get('id') == 'stage')
        self.assertIn("github.event_name == 'push' || inputs.use_upstream_cache",
                      stage['env']['USE_UPSTREAM_CACHE'])
        self.assertIn('resume_run_id', events(workflow)['workflow_dispatch']['inputs'])
        paths = events(workflow)['push']['paths']
        self.assertIn('build/windows/**', paths)
        self.assertNotIn('build/posix/**', paths)
        self.assertNotIn('build/**', paths)
        self.assertIn('build-12', workflow['jobs'])


if __name__ == '__main__':
    unittest.main()
