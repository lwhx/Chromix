"""Exercise real staged restore, including corrupt streams and publication errors."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'tools'))
import restore_posix_snapshot as restore
import test_posix_stages as stages


@unittest.skipUnless(shutil.which('zstd'), 'zstd required')
class TransactionalMetadataTest(stages.PosixSnapshotRoundTripTest):
    """Reuse all real tar/Ninja round trips, but through the production helper."""

    def restore(self, parts_dir, dest, extra_env=None):
        incoming = self.root / 'incoming'
        shutil.copytree(parts_dir, incoming)
        result = subprocess.run(
            ['bash', str(REPO / 'build/posix/restore-snapshot.sh'), str(incoming), str(dest)],
            env={**self.env, **(extra_env or {})}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(incoming.exists())
        self.assertFalse(list(dest.parent.glob(f'.{dest.name}.restore-*')))


@unittest.skipUnless(shutil.which('zstd'), 'zstd required')
class RestoreFailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='restore spaces\n')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'snapshot'
        self.source.mkdir()
        self.dest = self.root / 'work'
        (self.dest / 'download_cache').mkdir(parents=True)
        (self.dest / 'download_cache/pinned').write_bytes(b'cache')
        (self.dest / 'old-marker').write_bytes(b'original')
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo('src/payload')
            member.size = 131072
            archive.addfile(member, io.BytesIO(os.urandom(member.size)))
        self.raw = stream.getvalue()
        self.compressed = self.compress(self.raw)

    def compress(self, raw):
        return subprocess.run(['zstd', '-c'], input=raw, check=True,
                              capture_output=True, timeout=15).stdout

    def write_volumes(self, data=None, count=1, flat=False):
        data = self.compressed if data is None else data
        width = (len(data) + count - 1) // count
        result = []
        for index in range(count):
            directory = self.source if flat else self.source / f'p{index % 4 + 1}'
            directory.mkdir(exist_ok=True)
            path = directory / f'tree.tar.zst.{index + 1:03d}'
            path.write_bytes(data[index * width:(index + 1) * width])
            result.append(path)
        return result

    def run_restore(self, shell='bash'):
        return subprocess.run([str(shell), str(REPO / 'build/posix/restore-snapshot.sh'),
                               str(self.source), str(self.dest)],
                              capture_output=True, text=True, timeout=30)

    def assert_untouched(self):
        self.assertEqual((self.dest / 'old-marker').read_bytes(), b'original')
        self.assertEqual((self.dest / 'download_cache/pinned').read_bytes(), b'cache')
        self.assertFalse((self.dest / 'src').exists())
        self.assertTrue(self.source.exists())
        self.assertFalse(list(self.root.glob('.work.restore-*')))

    def test_five_to_eight_round_robin_and_flattened_volumes(self):
        for count in range(5, 9):
            for flat in (False, True):
                with self.subTest(count=count, flat=flat):
                    self.source.mkdir(exist_ok=True)
                    self.write_volumes(count=count, flat=flat)
                    result = self.run_restore()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((self.dest / 'download_cache/pinned').read_bytes(), b'cache')
                    self.assertEqual((self.dest / 'src/payload').stat().st_size, 131072)
                    self.assertFalse(self.source.exists())

    def test_bad_numbering_and_file_types_fail_before_extraction(self):
        for bad in ('missing', 'duplicate', 'empty', 'symlink', 'directory', 'malformed', 'zero'):
            with self.subTest(bad=bad):
                shutil.rmtree(self.source)
                self.source.mkdir()
                paths = self.write_volumes(count=5)
                path = paths[1]
                if bad == 'duplicate':
                    shutil.copy2(path, self.source / path.name)
                else:
                    path.unlink()
                    if bad == 'empty':
                        path.touch()
                    elif bad == 'symlink':
                        path.symlink_to(paths[0])
                    elif bad == 'directory':
                        path.mkdir()
                    elif bad in ('malformed', 'zero'):
                        (path.parent / ('tree.tar.zst.BAD' if bad == 'malformed'
                                        else 'tree.tar.zst.000')).write_bytes(b'bad')
                result = self.run_restore()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('snapshot volume error', result.stderr)
                self.assert_untouched()

    def test_truncated_zstd_or_tar_never_publishes_partial_tree(self):
        for data in (self.compressed[:-11], self.compress(self.raw[:64000])):
            with self.subTest(size=len(data)):
                self.write_volumes(data)
                result = self.run_restore()
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn('snapshot extraction failed', result.stderr)
                self.assert_untouched()

    def test_corrupt_stream_leaves_absent_destination_absent(self):
        shutil.rmtree(self.dest)
        self.write_volumes(self.compressed[:-11])
        self.assertNotEqual(self.run_restore().returncode, 0)
        self.assertFalse(self.dest.exists())
        self.assertTrue(self.source.exists())

    def test_real_upload_gate_rejects_corrupt_archive(self):
        import yaml
        workflow = yaml.safe_load((REPO / '.github/workflows/build-posix-github.yml').read_text())
        steps = workflow['jobs']['posix-1']['steps']
        script = next(step['run'] for step in steps if step.get('id') == 'checkpoint')
        handoff = self.root / 'chromix-build/.snapshot-stage-1'
        for data in (self.compressed, self.compressed[:-11], self.compress(self.raw[:64000])):
            with self.subTest(size=len(data)):
                self.write_volumes(data, count=8)
                if handoff.exists():
                    shutil.rmtree(handoff)
                shutil.copytree(self.source, handoff)
                result = subprocess.run(['bash', '-c', script], cwd=REPO,
                                        env={**os.environ, 'RUNNER_TEMP': str(self.root)},
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode == 0, data == self.compressed, result.stderr)

    def test_publication_failure_rolls_back_cache_and_tree(self):
        self.write_volumes()
        actual_publish = restore.publish
        def fail_final(source, destination):
            if source.name == 'tree':
                raise OSError('injected publication failure')
            return actual_publish(source, destination)
        with mock.patch.object(restore, 'publish', side_effect=fail_final):
            with self.assertRaisesRegex(OSError, 'injected'):
                restore.restore(self.source, self.dest)
        self.assert_untouched()

    def test_archive_cannot_replace_separately_cached_downloads(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            member = tarfile.TarInfo('download_cache')
            member.type = tarfile.SYMTYPE
            member.linkname = 'src'
            archive.addfile(member)
        self.write_volumes(self.compress(stream.getvalue()))
        result = self.run_restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must exclude the root download_cache', result.stderr)
        self.assert_untouched()

    def test_interrupt_during_extraction_keeps_original_and_volumes(self):
        self.write_volumes()
        def interrupted_extract(volumes, destination):
            (destination / 'partial').write_bytes(b'partial data')
            raise KeyboardInterrupt('injected interruption')
        with mock.patch.object(restore, 'extract', side_effect=interrupted_extract):
            with self.assertRaises(KeyboardInterrupt):
                restore.restore(self.source, self.dest)
        self.assert_untouched()

    def test_overlaps_and_symlinks_are_rejected(self):
        self.write_volumes()
        for destination in (self.source, self.source / 'child', self.root):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, 'overlap'):
                    restore.restore(self.source, destination)
        alias = self.root / 'alias'
        alias.symlink_to(self.dest)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            restore.restore(self.source, alias)
        self.assert_untouched()

    def test_failed_rollback_retains_original_data(self):
        self.write_volumes()
        actual_publish = restore.publish
        def fail_publish_and_rollback(source, destination):
            if source.name in ('tree', 'old'):
                raise OSError('injected rename failure')
            return actual_publish(source, destination)
        with mock.patch.object(restore, 'publish', side_effect=fail_publish_and_rollback):
            with self.assertRaisesRegex(OSError, 'injected'):
                restore.restore(self.source, self.dest)
        saved = next(self.root.glob('.work.restore-*')) / 'old'
        self.assertEqual((saved / 'download_cache/pinned').read_bytes(), b'cache')
        self.assertEqual((saved / 'old-marker').read_bytes(), b'original')
        self.assertTrue(self.source.exists())

    @unittest.skipUnless(stages.BASH32.is_file(), 'bash 3.2 required')
    def test_real_bash32_wrapper(self):
        self.write_volumes(count=8)
        result = self.run_restore(stages.BASH32)
        self.assertEqual(result.returncode, 0, result.stderr)
