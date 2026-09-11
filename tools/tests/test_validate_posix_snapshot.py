import copy
import unittest

from tools import validate_posix_snapshot as snapshot


REPO = 'owner/Chromix'
SHA = 'a' * 40


class Client:
    def __init__(self):
        self.run = {'id': 123, 'name': 'build-macos-arm64',
                    'path': '.github/workflows/build-macos-arm64.yml',
                    'head_branch': 'main', 'event': 'workflow_dispatch',
                    'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO},
                    'status': 'completed', 'head_sha': SHA, 'run_attempt': 2}
        self.jobs = [{'id': 456, 'name': 'build / macos-arm64 stage 7 (resume compile)',
                      'status': 'completed', 'conclusion': 'success', 'steps': [
                          {'name': 'Verify handoff snapshot', 'conclusion': 'success'},
                          *[{'name': f'Upload tree part {n}', 'conclusion': 'success'} for n in range(1, 5)]]}]
        self.artifacts = [{'id': 100 + n, 'name': f'chromix-mac-arm64-tree-s7-attempt-1-part{n}',
                           'size_in_bytes': 1024, 'expired': False,
                           'workflow_run': {'id': 123, 'head_sha': SHA}} for n in (1, 2)]
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return self.run

    def items(self, path, key):
        self.calls.append(path)
        return self.jobs if key == 'jobs' else self.artifacts


class SnapshotValidationTest(unittest.TestCase):
    def validate(self, client):
        return snapshot.validate(client, REPO, 123, 7, 1, 'arm64', [101, 102])

    def test_exact_original_attempt_from_terminal_retry(self):
        client = Client()
        client.artifacts.append(dict(client.artifacts[0], id=900, name='chromix-mac-arm64-tree-s7-attempt-2-part1'))
        report = self.validate(client)
        self.assertEqual(report['head_sha'], SHA)
        self.assertEqual(report['pattern'], 'chromix-mac-arm64-tree-s7-attempt-1-part*')
        self.assertEqual([item['id'] for item in report['artifacts']], [101, 102])
        self.assertIn('/actions/runs/123/attempts/1/jobs', client.calls)

    def test_wrong_origin_platform_or_unfinished_run_rejected(self):
        for key, value in {'id': 456, 'name': 'build-macos-x64', 'head_branch': 'feature',
                           'path': '.github/workflows/other.yml', 'event': 'pull_request',
                           'head_sha': 'bad\nsha', 'run_attempt': 0, 'status': 'in_progress',
                           'repository': {'full_name': 'foreign/Chromix'},
                           'head_repository': {'full_name': 'foreign/Chromix'}}.items():
            with self.subTest(key=key):
                client = Client()
                client.run[key] = value
                with self.assertRaises(ValueError):
                    self.validate(client)

    def test_missing_expired_duplicate_noncontiguous_parts_rejected(self):
        cases = [[], [Client().artifacts[1]], [Client().artifacts[0]], [Client().artifacts[0]] * 2]
        for field, value in [('expired', True), ('size_in_bytes', 0),
                             ('name', 'chromix-mac-arm64-tree-s7-attempt-1-part5'),
                             ('workflow_run', {'id': 999, 'head_sha': SHA})]:
            artifacts = copy.deepcopy(Client().artifacts)
            artifacts[0][field] = value
            cases.append(artifacts)
        for artifacts in cases:
            with self.subTest(artifacts=artifacts):
                client = Client()
                client.artifacts = artifacts
                with self.assertRaises(ValueError):
                    self.validate(client)

    def test_unverified_or_failed_upload_rejected(self):
        for index in range(5):
            client = Client()
            client.jobs[0]['steps'][index]['conclusion'] = 'failure'
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.validate(client)

    def test_invalid_numeric_inputs(self):
        for value in ('', '0', '-1', '1\nfoo=bar', '1.0', 'true', '01'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                snapshot.positive(value, 'input')
        self.assertEqual(snapshot.positive('123', 'input'), 123)

    def test_pagination_is_bounded_and_complete(self):
        client = snapshot.Client(REPO, 'test')
        responses = iter([{'items': list(range(100)), 'total_count': 101},
                          {'items': [100], 'total_count': 101}])
        client.get = lambda path: next(responses)
        self.assertEqual(client.items('/items', 'items'), list(range(101)))
        client.get = lambda path: {'items': [], 'total_count': 1}
        with self.assertRaises(ValueError):
            client.items('/items', 'items')


if __name__ == '__main__':
    unittest.main()
