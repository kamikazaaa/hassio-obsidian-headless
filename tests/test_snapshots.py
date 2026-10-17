import hashlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hardened import snapshots
from hardened.snapshots import publish


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / 'source'
        self.src.mkdir()
        self.pub = self.root / 'publication'
        (self.src / 'note.md').write_text('Original content', encoding='utf-8')

    def run_publish(self):
        return publish(self.src, self.pub, source={'kind': 'test', 'id': 'fixture', 'revision': 'one'})

    def current(self):
        pointer = json.loads((self.pub / 'current.json').read_text())
        return self.pub / 'generations' / pointer['generation']

    def test_publish_complete_manifest_and_content(self):
        self.run_publish()
        self.assertTrue((self.pub / 'current.json').exists(), 'completed publication must expose current pointer')
        gen = self.current()
        manifest = json.loads((gen / 'manifest.json').read_text())
        self.assertEqual((gen / 'files/note.md').read_text(), 'Original content')
        self.assertEqual(manifest['files']['note.md']['sha256'], hashlib.sha256(b'Original content').hexdigest())
        self.assertEqual(manifest['source']['revision'], 'one')
        self.assertEqual(manifest['generation'], gen.name)

    def test_successive_publications_preserve_previous_generation(self):
        self.run_publish()
        self.assertTrue((self.pub / 'current.json').exists())
        old = self.current()
        (self.src / 'note.md').write_text('New content')
        self.run_publish()
        self.assertNotEqual(self.current(), old)
        self.assertEqual((old / 'files/note.md').read_text(), 'Original content')
        self.assertEqual((self.current() / 'files/note.md').read_text(), 'New content')

    def test_hidden_configuration_and_credentials_not_published(self):
        (self.src / '.obsidian').mkdir()
        (self.src / '.obsidian/private.md').write_text('secret')
        (self.src / '.env').write_text('secret')
        (self.src / 'key.pem').write_text('secret')
        self.run_publish()
        self.assertTrue((self.pub / 'current.json').exists())
        manifest = json.loads((self.current() / 'manifest.json').read_text())
        self.assertEqual(set(manifest['files']), {'note.md'})
        self.assertFalse((self.current() / 'files/.obsidian').exists())

    def test_symlink_source_rejected(self):
        outside = self.root / 'private.md'
        outside.write_text('secret')
        (self.src / 'escape.md').symlink_to(outside)
        with self.assertRaises((ValueError, OSError)):
            self.run_publish()
        self.assertFalse((self.pub / 'current.json').exists())

    def test_hardlink_source_rejected(self):
        os.link(self.src / 'note.md', self.src / 'other.md')
        with self.assertRaises((ValueError, OSError)):
            self.run_publish()

    def test_source_with_symlink_ancestor_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.src, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            publish(alias, self.pub, source={'kind':'test'})

    def test_destination_inside_source_rejected(self):
        with self.assertRaises(ValueError):
            publish(self.src, self.src / 'output', source={'kind':'test'})

    def test_failed_update_preserves_last_good_pointer(self):
        self.run_publish()
        self.assertTrue((self.pub / 'current.json').exists())
        before = (self.pub / 'current.json').read_bytes()
        (self.src / 'bad.md').symlink_to(self.root / 'absent')
        with self.assertRaises((ValueError, OSError)):
            self.run_publish()
        self.assertEqual((self.pub / 'current.json').read_bytes(), before)

    def test_fifo_rejected_without_blocking(self):
        os.mkfifo(self.src / 'pipe.md')
        with self.assertRaises((ValueError, OSError)):
            self.run_publish()

    def test_secret_metadata_rejected(self):
        with self.assertRaises(ValueError):
            publish(self.src, self.pub, source={'kind': 'git', 'auth_token':'fake-secret'})

    def test_service_umask_still_allows_reader_group_and_generation_lease(self):
        (self.src / 'nested').mkdir()
        (self.src / 'nested/other.md').write_text('nested')
        previous = os.umask(0o077)
        try:
            self.run_publish()
        finally:
            os.umask(previous)
        gen = self.current()
        for directory in (self.pub, self.pub / 'generations', gen, gen / 'files', gen / 'files/nested'):
            self.assertEqual(directory.stat().st_mode & 0o7777, 0o2750)
        for file in (self.pub / 'current.json', gen / '.lease', gen / 'manifest.json', gen / 'files/note.md'):
            self.assertEqual(file.stat().st_mode & 0o777, 0o640)

    def test_retention_preserves_leased_generation_then_reclaims_it(self):
        self.run_publish()
        old = self.current()
        with (old / '.lease').open('rb') as lease:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            for number in range(6):
                (self.src / 'note.md').write_text(str(number))
                self.run_publish()
            self.assertTrue(old.exists(), 'active reader must retain its pinned files')
            self.assertEqual(len(list((self.pub / 'generations').glob('gen-*'))), 4)
        (self.src / 'note.md').write_text('last')
        self.run_publish()
        self.assertFalse(old.exists())
        self.assertEqual(len(list((self.pub / 'generations').glob('gen-*'))), 3)

    def test_unchanged_content_and_revision_reuses_complete_generation(self):
        self.run_publish()
        old = self.current()
        self.run_publish()
        self.assertEqual(self.current(), old)
        self.assertEqual(len(list((self.pub / 'generations').iterdir())), 1)

    def test_corrupt_previous_copy_is_replaced_even_when_source_unchanged(self):
        self.run_publish()
        old = self.current()
        (old / 'files/note.md').write_text('corrupted')
        self.run_publish()
        self.assertNotEqual(self.current(), old)
        self.assertEqual((self.current() / 'files/note.md').read_text(), 'Original content')

    def test_failed_copy_is_cleaned_and_does_not_accumulate_disk_usage(self):
        self.run_publish()
        previous = self.current()
        (self.src / 'bad.md').symlink_to(self.root / 'absent')
        for _ in range(4):
            with self.assertRaises((ValueError, OSError)):
                self.run_publish()
        self.assertEqual(self.current(), previous)
        self.assertEqual(list((self.pub / 'generations').iterdir()), [previous])

    def test_free_space_guard_preserves_previous_generation(self):
        self.run_publish()
        previous = self.current()
        (self.src / 'note.md').write_text('new version')
        low_space = type('Space', (), {'f_bavail': 1, 'f_frsize': 4096})()
        with patch.object(snapshots.os, 'fstatvfs', return_value=low_space):
            with self.assertRaises((OSError, ValueError)):
                self.run_publish()
        self.assertEqual(self.current(), previous)

    def test_retention_does_not_follow_a_replaced_old_directory(self):
        self.run_publish()
        old = self.current()
        outside = self.root / 'human-notes'
        outside.mkdir()
        (outside / 'note.md').write_text('human note')
        (old / 'files/note.md').unlink()
        (old / 'files').rmdir()
        (old / 'files').symlink_to(outside, target_is_directory=True)
        (self.src / 'note.md').write_text('new version')
        with self.assertRaises((ValueError, OSError)):
            self.run_publish()
        self.assertEqual((outside / 'note.md').read_text(), 'human note')

    def test_disk_write_failure_leaves_previous_pointer_and_no_partial_generation(self):
        self.run_publish()
        old = self.current()
        (self.src / 'note.md').write_text('updated')
        with patch.object(snapshots.os, 'write', side_effect=OSError(28, 'synthetic disk full')):
            with self.assertRaises(OSError):
                self.run_publish()
        self.assertEqual(self.current(), old)
        self.assertEqual(list((self.pub / 'generations').iterdir()), [old])

    def test_failure_after_pointer_replace_still_exposes_only_complete_generation(self):
        self.run_publish()
        old = self.current()
        (self.src / 'note.md').write_text('updated')
        original_replace, original_fsync = os.replace, os.fsync
        committed = False
        def replace(*args, **kwargs):
            nonlocal committed
            result = original_replace(*args, **kwargs)
            committed = True
            return result
        def fsync(fd):
            if committed:
                raise OSError(5, 'synthetic directory sync failure')
            return original_fsync(fd)
        with patch.object(snapshots.os, 'replace', side_effect=replace), patch.object(snapshots.os, 'fsync', side_effect=fsync):
            with self.assertRaises(OSError):
                self.run_publish()
        self.assertNotEqual(self.current(), old)
        manifest = json.loads((self.current() / 'manifest.json').read_text())
        self.assertEqual(manifest['files']['note.md']['sha256'], hashlib.sha256(b'updated').hexdigest())
        self.assertTrue((self.current() / '.lease').is_file())
        self.run_publish()

    def test_recovers_interrupted_staging_and_pointer_without_touching_source(self):
        self.run_publish()
        old = self.current()
        stage = self.pub / 'generations' / ('.pending-gen-' + 'a' * 32)
        stage.mkdir()
        (stage / 'partial.md').write_text('incomplete')
        pointer = self.pub / ('.current-' + 'b' * 32)
        pointer.write_text('{')
        self.run_publish()
        self.assertFalse(stage.exists())
        self.assertFalse(pointer.exists())
        self.assertEqual(self.current(), old)
        self.assertEqual((self.src / 'note.md').read_text(), 'Original content')

    def test_unreadable_filename_is_rejected_before_becoming_current(self):
        (self.src / 'back\\slash.md').write_text('invalid path for the reader')
        with self.assertRaises(ValueError):
            self.run_publish()
        self.assertFalse((self.pub / 'current.json').exists())

    def test_restart_retirement_respects_a_delayed_readers_lease(self):
        self.run_publish()
        old = self.current()
        old_fd = os.open(old, os.O_RDONLY | os.O_DIRECTORY)
        try:
            (self.src / 'note.md').write_text('next')
            self.run_publish()
            retired = old.with_name('.retired-' + old.name)
            # Simulate death after durable retirement rename, before cleanup.
            old.rename(retired)
            from hardened.reader import _generation_lease
            with _generation_lease(old_fd):
                self.run_publish()
                self.assertEqual((retired / 'files/note.md').read_text(), 'Original content')
            self.run_publish()
            self.assertFalse(retired.exists())
        finally:
            os.close(old_fd)

    def test_invalid_reader_contract_cannot_be_reused_as_success(self):
        from hardened.reader import Reader
        for defect in ('missing lease', 'bad timestamp', 'boolean schema', 'private content mode'):
            with self.subTest(defect=defect):
                self.pub = self.root / defect.replace(' ', '-') / 'obsidian'
                self.run_publish()
                old = self.current()
                if defect == 'missing lease':
                    (old / '.lease').unlink()
                elif defect == 'private content mode':
                    (old / 'files/note.md').chmod(0o600)
                else:
                    path = old / 'manifest.json'
                    value = json.loads(path.read_text())
                    value['created_at' if defect == 'bad timestamp' else 'schema_version'] = 'broken' if defect == 'bad timestamp' else True
                    path.write_text(json.dumps(value))
                with self.assertRaises((ValueError, OSError)):
                    self.run_publish()
                self.assertEqual(self.current(), old, 'invalid control state must remain available for deliberate recovery')

    def test_rejected_directory_mode_does_not_leak_descriptors_on_retry(self):
        self.run_publish()
        old = self.current()
        (self.pub / 'generations').chmod(0o700)
        before = len(os.listdir('/proc/self/fd'))
        for _ in range(4):
            with self.assertRaises(ValueError):
                self.run_publish()
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        self.assertEqual(self.current(), old)


if __name__ == '__main__':
    unittest.main()
