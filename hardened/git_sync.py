"""Read-only Git acquisition into private, disposable workspaces.

A Git tree is exported at a verified immutable commit. It never adopts, resets,
merges or pushes a human worktree, and never executes repository hooks/filters.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .snapshots import MAX_ENTRIES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, directory_fd, publish

MAX_REPOS = 10
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 32 * 1024 * 1024
MAX_COMMAND_SECONDS = 180
MAX_RESPONSE_BYTES = 65536
MAX_ACQUISITION_BYTES = 1536 * 1024 * 1024
MAX_ACQUISITION_ENTRIES = 2 * MAX_ENTRIES
MIN_FREE_BYTES = 256 * 1024 * 1024
MONITOR_INTERVAL_SECONDS = .1

# Git and its helpers stay in the supervisor's process group. Linux's parent-
# death signal reaches only its immediate recipient, so a small supervisor
# handles it by terminating the entire group. A signal directly armed on Git
# would leave helpers alive after an uncatchable service-worker death.
_GIT_SUPERVISOR = '''import ctypes, os, signal, subprocess, sys
def stop(*_):
    try: os.killpg(os.getpgrp(), signal.SIGKILL)
    finally: os._exit(127)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
if sys.platform.startswith('linux'):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0: stop()
if os.getppid() != int(sys.argv[1]): stop()
try:
    child = subprocess.Popen(sys.argv[2:])
    result = child.wait()
except BaseException:
    stop()
sys.exit(result if result >= 0 else 128 - result)
'''


def _relative(raw: str) -> str:
    if not raw or raw.startswith('/') or '\\' in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError('unsafe relative path')
    parts = raw.rstrip('/').split('/')
    if any(p in ('', '.', '..') or p.startswith('.') for p in parts) or len(raw.encode()) > 1024 or len(parts) > 24:
        raise ValueError('unsafe relative path')
    return '/'.join(parts)


def parse_repositories(value: str) -> list[dict]:
    if not isinstance(value, str) or len(value.encode()) > 65536:
        raise ValueError('invalid Git repository configuration')
    repositories = []
    namespaces = []
    for line in value.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split('|')
        if len(fields) not in (2, 3):
            raise ValueError('Git source must be namespace|https-url|branch')
        name, raw_url = (f.strip() for f in fields[:2])
        name = _relative(name)
        folded = unicodedata.normalize('NFC', name).casefold()
        if any(folded == other or folded.startswith(other + '/') or other.startswith(folded + '/') for other in namespaces):
            raise ValueError('overlapping Git source namespaces')
        namespaces.append(folded)
        try:
            url = urlsplit(raw_url)
            port = url.port
        except ValueError as exc:
            raise ValueError('invalid Git source URL') from exc
        if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
            or url.query or url.fragment or not url.path or len(raw_url) > 2048
            or any(c.isspace() or ord(c) < 32 for c in raw_url)):
            raise ValueError('Git sources require HTTPS URLs without embedded credentials, queries or fragments')
        host = url.hostname.lower()
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
            raise ValueError('invalid Git source hostname')
        netloc = host + (f':{port}' if port and port != 443 else '')
        remote = urlunsplit(('https', netloc, url.path, '', ''))
        branch = fields[2].strip() if len(fields) == 3 else 'main'
        if (not branch or len(branch) > 256 or branch.startswith(('-', '/', '.')) or branch.endswith(('/', '.'))
            or '..' in branch or '@{' in branch or any(p.startswith('.') or p.endswith('.lock') or not p for p in branch.split('/'))
            or any(c.isspace() or ord(c) < 32 or c in '~^:?*[\\' for c in branch)):
            raise ValueError('invalid Git branch')
        identity = hashlib.sha256(json.dumps([name, remote, branch], separators=(',', ':')).encode()).hexdigest()
        repositories.append({'name': name, 'remote': remote, 'branch': branch, 'id': identity})
        if len(repositories) > MAX_REPOS:
            raise ValueError('too many Git repositories')
    if not repositories:
        raise ValueError('at least one Git repository is required')
    return repositories


def _limits(parent_pid: int):
    # Limit individual pack/archive files and command processes, including their
    # subprocesses. The entire acquisition tree is disposable after each cycle.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARCHIVE_BYTES, MAX_ARCHIVE_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_COMMAND_SECONDS, MAX_COMMAND_SECONDS + 5))
    if sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        # Before the interpreter installs its group-cleanup handler, death of
        # the parent must stop this supervisor before it can spawn any writer.
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent_pid:
            os._exit(1)


def _check_acquisition(root: Path, anonymous_files=()) -> None:
    """Sample private storage without following links or traversing other mounts.

    This is a cumulative monitor, not a filesystem quota: writers can overshoot
    between samples. The hard per-file limit still applies in every child.
    """
    totals = {'bytes': 0, 'entries': 0}

    def account(info):
        totals['entries'] += 1
        totals['bytes'] += max(info.st_size, info.st_blocks * 512)
        if totals['bytes'] > MAX_ACQUISITION_BYTES:
            raise ValueError('Git private storage budget exceeded')
        if totals['entries'] > MAX_ACQUISITION_ENTRIES:
            raise ValueError('Git private storage entry limit exceeded')

    def walk(fd, device, depth=0):
        if depth > 64:
            raise ValueError('Git private storage nesting limit exceeded')
        with os.scandir(fd) as entries:
            for entry in entries:
                try:
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if info.st_dev != device:
                        raise ValueError('Git private storage crosses a filesystem boundary')
                    account(info)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            walk(child, device, depth + 1)
                        finally:
                            os.close(child)
                    elif not stat.S_ISREG(info.st_mode):
                        raise ValueError('Git private storage contains an unsupported filesystem entry')
                except FileNotFoundError:
                    # Git creates/renames/unlinks temporary objects while the
                    # sample runs. A disappeared entry consumes no further space.
                    continue

    with directory_fd(root) as fd:
        available = os.fstatvfs(fd)
        if available.f_bavail * available.f_frsize < MIN_FREE_BYTES:
            raise ValueError('Git free-space reserve reached')
        walk(fd, os.fstat(fd).st_dev)
        for stream in anonymous_files:
            info = os.fstat(stream.fileno())
            # TemporaryFile is anonymous/unlinked and absent from scandir.
            # Linked output files inside the private root were already counted.
            if info.st_nlink == 0:
                account(info)
        available = os.fstatvfs(fd)
        if available.f_bavail * available.f_frsize < MIN_FREE_BYTES:
            raise ValueError('Git free-space reserve reached')


class _AcquisitionMonitor:
    def __init__(self, root: Path | None, anonymous_files=()):
        self.root = root
        self.anonymous_files = anonymous_files
        self.last_check = float('-inf')

    def check(self, *, force=False):
        now = time.monotonic()
        if self.root is not None and (force or now - self.last_check >= MONITOR_INTERVAL_SECONDS):
            _check_acquisition(self.root, self.anonymous_files)
            self.last_check = time.monotonic()


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run_git(*args: str, env: dict | None = None, output_file=None,
             acquisition_root: Path | None = None) -> bytes:
    safe_env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
    if env:
        safe_env.update(env)
    command = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
               '-c', 'protocol.ext.allow=never', '-c', 'protocol.file.allow=never',
               '-c', 'protocol.http.allow=never', '-c', 'http.followRedirects=false',
               '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true',
               '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', *map(str, args)]
    # Neither argv nor child stderr is returned in diagnostics. Small metadata
    # stdout uses a bounded pipe/read, avoiding an unbounded temporary error or
    # capture file. Archive stdout uses the private caller-owned file, checked
    # during execution and independently capped by the child's RLIMIT_FSIZE.
    monitor = _AcquisitionMonitor(acquisition_root, (output_file,) if output_file is not None else ())
    monitor.check(force=True)
    captured = bytearray()
    parent_pid = os.getpid()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, '-I', '-c', _GIT_SUPERVISOR, str(parent_pid), *command],
            env=safe_env, stdin=subprocess.DEVNULL,
            stdout=output_file if output_file is not None else subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
            preexec_fn=lambda: _limits(parent_pid))
        if output_file is None:
            os.set_blocking(process.stdout.fileno(), False)
        deadline = time.monotonic() + MAX_COMMAND_SECONDS
        while True:
            completed = process.poll() is not None
            if output_file is None:
                while True:
                    try:
                        block = os.read(process.stdout.fileno(), MAX_RESPONSE_BYTES + 1 - len(captured))
                    except BlockingIOError:
                        break
                    if not block:
                        break
                    captured.extend(block)
                    if len(captured) > MAX_RESPONSE_BYTES:
                        raise ValueError('Git response size limit exceeded')
            elif os.fstat(output_file.fileno()).st_size > MAX_ARCHIVE_BYTES:
                raise ValueError('Git archive size limit exceeded')
            monitor.check()
            if completed:
                monitor.check(force=True)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Git acquisition timed out')
            try:
                process.wait(timeout=min(MONITOR_INTERVAL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise RuntimeError(f'Git acquisition failed (exit {process.returncode}); no publication changed')
        return bytes(captured)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('Git acquisition command unavailable or failed') from None
    finally:
        if process is not None:
            _kill_group(process)
            if process.stdout is not None:
                process.stdout.close()


def _auth_env(repository: dict, token: str) -> dict:
    if not token:
        return {}
    # The managed option is a GitHub PAT. Never send it to another server,
    # even if that server is explicitly configured as an additional source.
    url = urlsplit(repository['remote'])
    if url.hostname != 'github.com' or (url.port not in (None, 443)):
        return {}
    if not isinstance(token, str) or len(token) > 4096 or any(c.isspace() or ord(c) < 32 for c in token):
        raise ValueError('invalid GitHub token format')
    encoded = base64.b64encode(('x-access-token:' + token).encode()).decode()
    return {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
            'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + encoded}


def _export(archive, destination: Path, budget: dict, monitor: _AcquisitionMonitor | None = None) -> None:
    seen = set()
    with tarfile.open(fileobj=archive, mode='r|') as tree:
        for member in tree:
            if monitor is not None:
                monitor.check()
            budget['entries'] += 1
            if budget['entries'] > MAX_ENTRIES:
                raise ValueError('Git tree entry limit exceeded')
            # Hidden control/configuration paths are outside the publication scope.
            raw_parts = member.name.rstrip('/').split('/')
            if any(part.startswith('.') and part not in ('.', '..') for part in raw_parts):
                continue
            relative = _relative(member.name)
            canonical = unicodedata.normalize('NFC', relative).casefold()
            if canonical in seen:
                raise ValueError('Git tree contains colliding paths')
            seen.add(canonical)
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            if not member.isfile() or member.size < 0 or member.size > MAX_FILE_BYTES:
                raise ValueError('Git tree contains a link, special or oversized file')
            budget['bytes'] += member.size
            if budget['bytes'] > MAX_TOTAL_BYTES:
                raise ValueError('Git tree size limit exceeded')
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stream = tree.extractfile(member)
            if stream is None:
                raise ValueError('invalid Git archive member')
            remaining = member.size
            with stream, target.open('xb') as out:
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('truncated Git archive')
                    out.write(block)
                    remaining -= len(block)
                    if monitor is not None:
                        monitor.check()
                out.flush()
                os.fsync(out.fileno())
            os.chmod(target, 0o600)


def sync_once(config: dict, state_root: Path, publication_root: Path) -> dict:
    """Acquire all sources or publish none; input worktrees are never modified."""
    repositories = parse_repositories(config.get('git_repos', ''))
    state_root, publication_root = Path(state_root), Path(publication_root)
    with directory_fd(state_root, create=True):
        pass
    revisions = []
    with tempfile.TemporaryDirectory(prefix='acquire-', dir=state_root) as temporary:
        acquisition = Path(temporary)
        output = acquisition / 'files'
        output.mkdir(mode=0o700)
        budget = {'entries': 0, 'bytes': 0}
        for repo in repositories:
            mirror = acquisition / (repo['id'] + '.git')
            _run_git('init', '--bare', str(mirror), acquisition_root=state_root)
            env = _auth_env(repo, config.get('git_auth_token', ''))
            _run_git('--git-dir=' + str(mirror), 'fetch', '--no-tags', '--depth=1', '--no-recurse-submodules',
                     '--', repo['remote'], 'refs/heads/' + repo['branch'], env=env, acquisition_root=state_root)
            commit = _run_git('--git-dir=' + str(mirror), 'rev-parse', '--verify', 'FETCH_HEAD^{commit}',
                              acquisition_root=state_root).decode().strip()
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
                raise ValueError('invalid resolved Git commit')
            namespace = output.joinpath(*repo['name'].split('/'))
            namespace.mkdir(parents=True, mode=0o700)
            with tempfile.TemporaryFile(dir=acquisition) as archive:
                _run_git('--git-dir=' + str(mirror), 'archive', '--format=tar', commit, output_file=archive,
                         acquisition_root=state_root)
                archive.seek(0)
                monitor = _AcquisitionMonitor(state_root, (archive,))
                _export(archive, namespace, budget, monitor)
                monitor.check(force=True)
            # Provenance is a generated text document, not credentials or repository
            # config. Each namespace retains its exact immutable source revision.
            control = namespace / '_source.md'
            if control.exists():
                raise ValueError('reserved Git provenance filename collision')
            control.write_text('# Git source\n\n' + json.dumps({k: repo[k] for k in ('name', 'remote', 'branch', 'id')}, ensure_ascii=False, sort_keys=True)
                               + '\n\nCommit: ' + commit + '\n', encoding='utf-8')
            revisions.append({'id': repo['id'], 'commit': commit})
            _check_acquisition(state_root)
        revision = hashlib.sha256(json.dumps(revisions, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return publish(output, publication_root, source={'kind': 'git', 'id': 'configured-repositories', 'revision': revision,
                       'revisions': json.dumps(revisions, sort_keys=True, separators=(',', ':'))})
