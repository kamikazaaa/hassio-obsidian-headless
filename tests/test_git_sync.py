import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hardened import git_sync


def matching_proc_namespace():
    try:
        return sys.platform.startswith('linux') and int(Path('/proc/self/stat').read_text().split()[0]) == os.getpid()
    except (OSError, ValueError):
        return False


class GitImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / 'origin'
        self.origin.mkdir()
        self.git('init', '-b', 'main', str(self.origin))
        (self.origin / 'note.md').write_text('version one')
        self.commit('initial')
        self.state = self.root / 'state'
        self.pub = self.root / 'publication'
        self.config = {'git_repos':'Library|https://github.com/fixture/source.git|main'}

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git('-C', str(self.origin), 'add', '.')
        self.git('-C', str(self.origin), 'commit', '-m', message)

    def fixture_transport(self, *args, **kwargs):
        # Replace only the transport destination after production validation;
        # exercise real Git object fetch/archive and actual filesystem publication.
        args = tuple(str(self.origin) if arg == 'https://github.com/fixture/source.git' else
                     'protocol.file.allow=always' if arg == 'protocol.file.allow=never' else arg
                     for arg in args)
        return self.original_run('-c', 'protocol.file.allow=always', *args, **kwargs)

    def sync(self):
        if not hasattr(git_sync, '_run_git'):
            return git_sync.sync_once(self.config, self.state, self.pub)
        self.original_run = git_sync._run_git
        with patch.object(git_sync, '_run_git', self.fixture_transport):
            return git_sync.sync_once(self.config, self.state, self.pub)

    def current(self):
        p = json.loads((self.pub / 'current.json').read_text())
        return self.pub / 'generations' / p['generation']

    def test_import_publishes_version_and_preserves_other_workbench(self):
        original = self.root / 'user-vault/Library'
        original.mkdir(parents=True)
        (original / 'note.md').write_text('private human edit')
        (original / 'untracked.md').write_text('untracked')
        self.sync()
        self.assertTrue((self.pub / 'current.json').exists())
        self.assertEqual((original / 'note.md').read_text(), 'private human edit')
        self.assertEqual((original / 'untracked.md').read_text(), 'untracked')
        self.assertEqual((self.current() / 'files/Library/note.md').read_text(), 'version one')

    def test_update_uses_new_commit_and_retains_old_generation(self):
        self.sync()
        self.assertTrue((self.pub / 'current.json').exists())
        old = self.current()
        (self.origin / 'note.md').write_text('version two')
        self.commit('update')
        self.sync()
        self.assertEqual((self.current() / 'files/Library/note.md').read_text(), 'version two')
        self.assertEqual((old / 'files/Library/note.md').read_text(), 'version one')
        manifest = json.loads((self.current() / 'manifest.json').read_text())
        self.assertTrue(manifest['source']['revision'])

    def test_manifest_carries_exact_repository_commits(self):
        self.sync()
        manifest = json.loads((self.current() / 'manifest.json').read_text())
        revisions = json.loads(manifest['source']['revisions'])
        self.assertEqual(revisions, [{'id': git_sync.parse_repositories(self.config['git_repos'])[0]['id'],
                                     'commit': self.git('-C', str(self.origin), 'rev-parse', 'HEAD')}])

    def test_later_repository_failure_preserves_current_and_disposes_acquisition(self):
        self.sync()
        old = (self.pub / 'current.json').read_bytes()
        (self.origin / 'note.md').write_text('unpublished next version')
        self.commit('next version')
        self.config['git_repos'] += '\nUnavailable|https://github.com/fixture/source.git|missing-branch'
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assertEqual((self.pub / 'current.json').read_bytes(), old)
        self.assertEqual((self.current() / 'files/Library/note.md').read_text(), 'version one')
        self.assertEqual(list(self.state.glob('acquire-*')), [])

    def test_reject_credential_url_parent_path_and_option_branch(self):
        bad = [
            '../escape|https://github.com/a/b.git|main',
            'X|https://token@github.com/a/b.git|main',
            'X|http://github.com/a/b.git|main',
            'X|file:///etc|main',
            'X|https://github.com/a/b.git|--upload-pack=evil',
            'X|https://github.com/a/b.git?token=secret|main',
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                git_sync.parse_repositories(value)

    def test_detect_overlapping_and_case_colliding_namespaces(self):
        for value in [
            'A|https://github.com/a/b.git\nA/B|https://github.com/a/c.git',
            'A|https://github.com/a/b.git\na|https://github.com/a/c.git',
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                git_sync.parse_repositories(value)

    def test_distinct_repository_identities_for_punctuation_and_remote_changes(self):
        repos = git_sync.parse_repositories('a-b|https://github.com/a/b.git\na_b|https://github.com/a/b.git')
        self.assertEqual(len(repos), 2)
        self.assertNotEqual(repos[0]['id'], repos[1]['id'])
        changed = git_sync.parse_repositories('a-b|https://github.com/a/c.git')[0]
        self.assertNotEqual(repos[0]['id'], changed['id'])

    def test_repository_symlink_cannot_read_host_files(self):
        private = self.root / 'private.md'
        private.write_text('secret')
        (self.origin / 'escape.md').symlink_to(private)
        self.commit('link')
        with self.assertRaises((ValueError, RuntimeError)):
            self.sync()
        self.assertFalse((self.pub / 'current.json').exists())


@unittest.skipUnless(matching_proc_namespace(), 'requires Linux with /proc in the process PID namespace')
class GitProcessLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.pidfile = self.root / 'pids.json'
        self.addCleanup(self.kill_fixture_processes)

    def fake_git(self, code):
        executable = self.bin / 'git'
        executable.write_text('#!' + sys.executable + '\n' + code)
        executable.chmod(0o700)
        return {'PATH': str(self.bin), 'FIXTURE_PIDS': str(self.pidfile),
                'FIXTURE_STATE': str(self.state)}

    def sleeping_tree(self, before_sleep=''):
        return self.fake_git('''import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path(os.environ['FIXTURE_PIDS']).write_text(json.dumps([os.getpid(), child.pid]))
''' + before_sleep + '\ntime.sleep(2)\n')

    @staticmethod
    def running(pid):
        try:
            return (Path('/proc') / str(pid) / 'stat').read_text().split()[2] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            # procfs can report ESRCH if the process exits after stat is opened.
            return False

    def kill_fixture_processes(self):
        if self.pidfile.exists():
            for pid in json.loads(self.pidfile.read_text()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def assert_fixture_stopped(self):
        self.assertTrue(self.pidfile.exists(), 'fixture never reached the active-child checkpoint')
        pids = json.loads(self.pidfile.read_text())
        deadline = time.monotonic() + 2
        while any(self.running(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(any(self.running(pid) for pid in pids), 'acquisition descendant remained alive')

    def test_timeout_terminates_git_and_its_descendants(self):
        env = self.sleeping_tree()
        with patch.object(git_sync, 'MAX_COMMAND_SECONDS', 1), self.assertRaises(RuntimeError) as error:
            git_sync._run_git('private-argument-sentinel', env=env)
        self.assertNotIn('private-argument-sentinel', str(error.exception))
        self.assert_fixture_stopped()

    def check_worker_death(self, death_signal):
        env = self.sleeping_tree()
        code = '''import json, signal, sys
from hardened.git_sync import _run_git
def stop(*args): raise SystemExit(143)
signal.signal(signal.SIGTERM, stop)
_run_git('fixture', env=json.loads(sys.argv[1]))
'''
        worker = subprocess.Popen([sys.executable, '-c', code, json.dumps(env)],
                                  cwd=Path(__file__).resolve().parent.parent,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not self.pidfile.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.pidfile.exists(), 'Git fixture did not start')
            worker.send_signal(death_signal)
            _, errors = worker.communicate(timeout=3)
            self.assertNotEqual(worker.returncode, 0, errors)
            self.assert_fixture_stopped()
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait()
            worker.stderr.close()

    def test_interruption_terminates_git_descendants(self):
        self.check_worker_death(signal.SIGTERM)

    def test_uncatchable_parent_death_terminates_git_descendants(self):
        self.check_worker_death(signal.SIGKILL)

    def test_cumulative_disk_monitor_interrupts_a_running_command(self):
        env = self.sleeping_tree('''root = pathlib.Path(os.environ['FIXTURE_STATE'])
for number in range(8): (root / str(number)).write_bytes(b'x' * 8192)
''')
        started = time.monotonic()
        with patch.object(git_sync, 'MAX_ACQUISITION_BYTES', 32768, create=True), self.assertRaises(ValueError):
            git_sync._run_git('fixture', env=env, acquisition_root=self.state)
        self.assertLess(time.monotonic() - started, 1.5, 'limit was only checked after command completion')
        self.assert_fixture_stopped()

    def test_entry_monitor_interrupts_a_running_command(self):
        env = self.sleeping_tree('''root = pathlib.Path(os.environ['FIXTURE_STATE'])
for number in range(24): (root / str(number)).touch()
''')
        with patch.object(git_sync, 'MAX_ACQUISITION_ENTRIES', 12, create=True), self.assertRaises(ValueError):
            git_sync._run_git('fixture', env=env, acquisition_root=self.state)
        self.assert_fixture_stopped()

    def test_small_stdout_is_bounded_during_execution(self):
        env = self.sleeping_tree("os.write(1, b'private-output-sentinel' * 8192)")
        started = time.monotonic()
        with self.assertRaises(ValueError) as error:
            git_sync._run_git('fixture', env=env, acquisition_root=self.state)
        self.assertLess(time.monotonic() - started, 1.5, 'stdout was checked only after command completion')
        self.assertNotIn('private-output-sentinel', str(error.exception))
        self.assert_fixture_stopped()

    def test_free_space_reserve_stops_before_starting_a_writer(self):
        env = self.sleeping_tree()
        available = SimpleNamespace(f_bavail=1, f_frsize=4096)
        with patch.object(git_sync.os, 'fstatvfs', return_value=available), self.assertRaises(ValueError):
            git_sync._run_git('fixture', env=env, acquisition_root=self.state)
        self.assertFalse(self.pidfile.exists())

    def test_anonymous_archive_is_included_in_cumulative_budget(self):
        env = self.sleeping_tree("os.write(1, b'x' * 65536)")
        with tempfile.TemporaryFile(dir=self.state) as archive:
            with patch.object(git_sync, 'MAX_ACQUISITION_BYTES', 32768), self.assertRaises(ValueError):
                git_sync._run_git('fixture', env=env, output_file=archive, acquisition_root=self.state)
        self.assert_fixture_stopped()

    def test_stale_acquisition_also_consumes_private_budget(self):
        stale = self.state / 'acquire-interrupted'
        stale.mkdir()
        (stale / 'old.pack').write_bytes(b'x' * 65536)
        env = self.sleeping_tree()
        with patch.object(git_sync, 'MAX_ACQUISITION_BYTES', 32768), self.assertRaises(ValueError):
            git_sync._run_git('fixture', env=env, acquisition_root=self.state)
        self.assertFalse(self.pidfile.exists())
        self.assertEqual((stale / 'old.pack').read_bytes(), b'x' * 65536)

    def test_even_successful_command_does_not_leave_background_helpers(self):
        env = self.sleeping_tree('sys.exit(0)')
        self.assertEqual(git_sync._run_git('fixture', env=env), b'')
        self.assert_fixture_stopped()


if __name__ == '__main__':
    unittest.main()
