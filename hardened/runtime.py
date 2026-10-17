"""Isolated acquisition, snapshot publication and read-only MCP supervision.

Only root bootstrap reads managed options. Each long-lived service then drops to
its dedicated UID, empties its inherited environment and keeps its own secrets.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
import resource
import secrets
from pathlib import Path
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import time

from hardened.config import ConfigError, load_options

UIDS = {'obsidian': 10001, 'git': 10002, 'remarkable': 10003, 'mcp': 10004}
READER_GID = 10004
DATA = Path('/data')
MAX_ACQUISITION_BYTES = 2 * 1024 * 1024 * 1024
MAX_ACQUISITION_FILE_BYTES = 32 * 1024 * 1024
MAX_ACQUISITION_ENTRIES = 45000
MIN_ACQUISITION_FREE_BYTES = 256 * 1024 * 1024
SCOPES = {
    'obsidian': {'obsidian_auth_token', 'vault_name', 'vault_password', 'obsidian_sync_interval'},
    'git': {'enable_git_sync', 'git_repos', 'git_auth_token', 'git_sync_interval'},
    'remarkable': {'enable_remarkable', 'remarkable_pairing_code', 'remarkable_sync_interval',
                  'remarkable_tag_prefix', 'remarkable_extra_tags', 'ha_ocr_url', 'ha_ocr_token', 'ha_ocr_entity'},
    'mcp': {'enable_mcp', 'mcp_auth_token', 'mcp_allowed_hosts', 'mcp_allowed_origins'},
}


class RuntimeFailure(RuntimeError):
    """Sanitized transient acquisition error."""


class IdentityFailure(RuntimeFailure):
    """Permanent identity mismatch requiring deliberate operator action."""


FRESH_GUIDANCE = ('Source identity differs or the workspace is not a fresh managed instance. '
                  'Do not relink this workspace. Stop the addon, retain a backup, and configure '
                  'a fresh addon instance for the intended source.')


def enabled(service: str, cfg: dict) -> bool:
    return service == 'obsidian' or cfg.get({'git': 'enable_git_sync',
        'remarkable': 'enable_remarkable', 'mcp': 'enable_mcp'}[service], False)


def _verify_directory(path: Path, uid: int, gid: int, mode: int):
    """Open without following a final symlink, and validate before touching metadata."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        with _FD(fd):
            meta = os.fstat(fd)
            if meta.st_uid != uid or meta.st_gid != gid or stat.S_IMODE(meta.st_mode) != mode:
                raise RuntimeFailure('Unsafe directory ownership or permissions; restore the managed data layout.')
    except OSError:
        raise RuntimeFailure('Unsafe or unavailable managed directory.') from None


class _FD:
    def __init__(self, fd):
        self.fd = fd
    def __enter__(self):
        return self.fd
    def __exit__(self, *_):
        os.close(self.fd)


def _directory(path: Path, uid: int, gid: int, mode: int):
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        _verify_directory(path, uid, gid, mode)
        return
    except OSError:
        raise RuntimeFailure('Cannot create a managed directory.') from None
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with _FD(fd):
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)


def bootstrap(service: str, data_root: Path = DATA):
    """Root owns every service-root entry; source UIDs cannot replace peer roots."""
    if service not in UIDS or os.geteuid() != 0:
        raise RuntimeFailure('Bootstrap requires root and a recognized service.')
    # Every ancestor must be an actual directory. /data itself must be root-owned
    # and non-writable to unprivileged services, including on restored volumes.
    for ancestor in [*reversed(data_root.parents), data_root]:
        if ancestor.is_symlink() or not ancestor.is_dir():
            raise RuntimeFailure('Symlinked data roots are forbidden.')
    meta = data_root.stat()
    if meta.st_uid != 0 or meta.st_mode & 0o022:
        raise RuntimeFailure('The managed data root must be root-owned and not group/world writable.')
    uid = UIDS[service]
    _directory(data_root / service, uid, uid, 0o700)
    _directory(data_root / 'published', 0, 0, 0o755)
    if service != 'mcp':
        _directory(data_root / 'published' / service, uid, READER_GID, 0o2750)
    _repair_status_ownership(service, data_root)


def _repair_status_ownership(service: str, data_root: Path):
    """Repair only a recognized fixed-name status inode, never links or peer data."""
    path = data_root / service / 'status.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    except OSError:
        raise RuntimeFailure('Cannot securely inspect existing service status.') from None
    with os.fdopen(fd, 'r') as stream:
        meta = os.fstat(stream.fileno())
        uid = UIDS[service]
        if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                or meta.st_uid not in (0, uid) or meta.st_size > 16384):
            raise RuntimeFailure('Unsafe existing service status ownership or file type.')
        try:
            value = json.load(stream)
        except (ValueError, OSError):
            raise RuntimeFailure('Existing service status is not recognized managed state.') from None
        required = {'schema_version', 'service', 'state', 'updated_at', 'pid', 'message'}
        if (type(value) is not dict or not required <= value.keys()
                or set(value) - required - {'last_success'}
                or value['schema_version'] != 1 or value['service'] != service
                or value['state'] not in ('disabled', 'starting', 'acquiring', 'ok', 'error')
                or type(value['updated_at']) not in (int, float)
                or type(value['pid']) is not int or type(value['message']) is not str
                or ('last_success' in value and type(value['last_success']) not in (int, float))):
            raise RuntimeFailure('Existing service status is not recognized managed state.')
        # Descriptor-based metadata changes cannot follow a concurrently replaced
        # path. Hardlinks and other services' inodes were rejected above.
        os.fchown(stream.fileno(), uid, uid)
        os.fchmod(stream.fileno(), 0o600)
        os.fsync(stream.fileno())


def drop_privileges(service: str):
    uid = UIDS[service]
    if os.geteuid() != 0:
        raise RuntimeFailure('Services must start through the root bootstrap.')
    os.setgroups([READER_GID] if service != 'mcp' else [])
    os.setgid(uid)
    os.setuid(uid)
    os.umask(0o077)
    if os.geteuid() != uid or os.getegid() != uid or 0 in os.getgroups():
        raise RuntimeFailure('Privilege drop failed.')
    if sys.platform.startswith('linux'):
        # Prevent exec of setuid binaries/file capabilities from restoring rights.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise RuntimeFailure('Cannot enforce no-new-privileges.')


def service_environment(service: str, cfg: dict, data_root: Path = DATA) -> dict[str, str]:
    home = data_root / service
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home),
           'XDG_CONFIG_HOME': str(home / 'config'), 'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1',
           'TMPDIR': str(home / 'tmp')}
    if service == 'obsidian':
        env['OBSIDIAN_AUTH_TOKEN'] = cfg['obsidian_auth_token']
    elif service == 'remarkable':
        env.update({'RMAPI_CONFIG': str(home / 'rmapi.json'),
            'VAULT_PATH': str(home / 'generated'), 'REMARKABLE_OUTPUT_DIR': 'documents',
            'REMARKABLE_CACHE_DIR': str(home / 'private'), 'SYNC_ONCE': 'true',
            'REMARKABLE_SYNC_ENABLED': 'true', 'REMARKABLE_OCR_POLICY': 'required',
            'REMARKABLE_TAG_PREFIX': cfg.get('remarkable_tag_prefix', ''),
            'REMARKABLE_EXTRA_TAGS': cfg.get('remarkable_extra_tags', ''),
            'HA_OCR_URL': cfg.get('ha_ocr_url', ''), 'HA_OCR_TOKEN': cfg.get('ha_ocr_token', ''),
            'HA_OCR_ENTITY': cfg.get('ha_ocr_entity', '')})
    elif service == 'git':
        env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                    'GIT_TERMINAL_PROMPT': '0'})
    elif service == 'mcp':
        env.update({'MCP_PUBLICATION_ROOT': str(data_root / 'published'),
            'MCP_BEARER_TOKEN': cfg.get('mcp_auth_token', ''), 'MCP_HOST': '0.0.0.0', 'MCP_PORT': '8420',
            'MCP_ALLOWED_HOSTS': cfg.get('mcp_allowed_hosts', ''),
            'MCP_ALLOWED_ORIGINS': cfg.get('mcp_allowed_origins', ''),
            'MCP_TLS_CERTFILE': str(home / 'tls' / 'cert.pem'),
            'MCP_TLS_KEYFILE': str(home / 'tls' / 'key.pem')})
    return env


def worker_environment(service: str, cfg: dict, data_root: Path = DATA) -> dict[str, str]:
    env = service_environment(service, cfg, data_root)
    scoped = {key: value for key, value in cfg.items() if key in SCOPES[service]}
    env['HARDENED_SERVICE_CONFIG'] = json.dumps(scoped, separators=(',', ':'))
    return env


def _private_directory(path: Path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _verify_directory(path, os.geteuid(), os.getegid(), 0o700)


def _read_json(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r') as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 1048576:
                raise RuntimeFailure('Unsafe managed state file.')
            return json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise RuntimeFailure('Cannot securely read managed state.') from None


def _open_directory_chain(path: Path) -> int:
    """Resolve every directory component with O_NOFOLLOW and retain the final FD."""
    absolute = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except OSError:
        os.close(fd)
        raise RuntimeFailure('Cannot securely open the managed write directory.') from None


def _atomic_write(path: Path, content: bytes, *, uid: int | None = None, gid: int | None = None):
    # Keep one directory capability throughout creation, metadata updates,
    # replacement and durability. A renamed/symlink-substituted pathname cannot
    # redirect a privileged write into a different directory.
    with _FD(_open_directory_chain(path.parent)) as parentfd:
        name = '.runtime-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parentfd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                if uid is not None:
                    os.fchown(stream.fileno(), uid, gid)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path.name, src_dir_fd=parentfd, dst_dir_fd=parentfd)
            os.fsync(parentfd)
        finally:
            try:
                os.unlink(name, dir_fd=parentfd)
            except FileNotFoundError:
                pass


def _atomic_json(path: Path, value: dict):
    _atomic_write(path, json.dumps(value, sort_keys=True).encode())


def validate_tls_pair(cert_pem: str, key_pem: str, data_root: Path = DATA):
    """Parse and match the PEM pair in root-private temporary files."""
    with tempfile.TemporaryDirectory(prefix='.tls-', dir=data_root) as scratch:
        cert = Path(scratch) / 'cert.pem'
        key = Path(scratch) / 'key.pem'
        try:
            if 'ENCRYPTED' in key_pem:
                raise ConfigError('MCP TLS requires an unencrypted private key PEM.')
            _atomic_write(cert, cert_pem.encode())
            _atomic_write(key, key_pem.encode())
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key, password='')
        except (OSError, ssl.SSLError):
            raise ConfigError('MCP TLS certificate/key must be a valid matching, unencrypted PEM pair.') from None


def provision_tls(cfg: dict, data_root: Path = DATA):
    """Only fixed service-owned files receive managed PEM inputs; no path options."""
    home = data_root / 'mcp'
    validate_tls_pair(cfg['mcp_tls_cert'], cfg['mcp_tls_key'], data_root)
    _directory(home / 'tls', UIDS['mcp'], UIDS['mcp'], 0o700)
    _atomic_write(home / 'tls' / 'cert.pem', cfg['mcp_tls_cert'].encode(), uid=10004, gid=10004)
    _atomic_write(home / 'tls' / 'key.pem', cfg['mcp_tls_key'].encode(), uid=10004, gid=10004)


def install_shutdown_handler():
    def stop(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _arm_parent_death(parent_pid: int, output_limit: int | None = None):
    # Children are launched by single-threaded service workers. Limit captured
    # JSON at the kernel's file-write boundary before loading it into Python.
    if output_limit is not None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (output_limit, output_limit))
    if sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
            os._exit(1)


def _start_process(args: list[str], env: dict, *, output_limit=None, **kwargs):
    parent_pid = os.getpid()
    return subprocess.Popen(args, env=env, start_new_session=True,
        preexec_fn=lambda: _arm_parent_death(parent_pid, output_limit), **kwargs)


def _kill_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _check_workspace(root: Path):
    """Sample a managed workspace without following links, including CLI state.

    This is a fail-stop monitor, not a filesystem quota: concurrent writes and
    filesystem overhead can exceed a sample before the next scan. The child
    also has a hard per-file size cap. No automatic source cleanup is performed.
    """
    from hardened.snapshots import directory_fd
    budget = {'entries': 0, 'bytes': 0}
    def walk(fd, depth=0):
        if depth > 40:
            raise RuntimeFailure('Acquisition workspace nesting exceeds its limit.')
        with os.scandir(fd) as entries:
            for entry in entries:
                budget['entries'] += 1
                if budget['entries'] > MAX_ACQUISITION_ENTRIES:
                    raise RuntimeFailure('Acquisition workspace entry limit exceeded.')
                try:
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                        try:
                            walk(child, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        budget['bytes'] += info.st_size
                        if info.st_size > MAX_ACQUISITION_FILE_BYTES or budget['bytes'] > MAX_ACQUISITION_BYTES:
                            raise RuntimeFailure('Acquisition workspace byte limit exceeded.')
                    else:
                        raise RuntimeFailure('Acquisition workspace contains an unsupported link or special file.')
                except FileNotFoundError:
                    # The known running acquisition can atomically rename its
                    # temporary state. It is counted under its new name next scan.
                    continue
    with directory_fd(root) as root_fd:
        space = os.fstatvfs(root_fd)
        if space.f_bavail * space.f_frsize < MIN_ACQUISITION_FREE_BYTES:
            raise RuntimeFailure('Acquisition stopped to preserve the filesystem free-space reserve.')
        walk(root_fd)


def run_command(args: list[str], env: dict, *, input_text: str | None = None,
                json_output: bool = False, timeout: float = 900, workspace: Path | None = None):
    """No shell/secrets in diagnostics; bounded JSON on disk and in memory."""
    limit = 1048576
    # TemporaryFile is unlinked/anonymous, private, and never grows beyond the
    # child RLIMIT_FSIZE of limit+1. No unbounded communicate(stdout=PIPE).
    with tempfile.TemporaryFile(dir=env.get('TMPDIR')) as capture:
        try:
            if workspace is not None:
                _check_workspace(workspace)
            with _start_process(args, env, text=True,
                    stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                    stdout=capture if json_output else subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    output_limit=limit + 1 if json_output else (MAX_ACQUISITION_FILE_BYTES if workspace is not None else None)) as process:
                deadline = time.monotonic() + timeout
                first = True
                try:
                    while True:
                        if workspace is not None:
                            _check_workspace(workspace)
                        if json_output and os.fstat(capture.fileno()).st_size > limit:
                            raise RuntimeFailure('Acquisition metadata exceeds the response limit.')
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeFailure('Acquisition command timed out.')
                        try:
                            process.communicate(input_text if first else None, timeout=min(.05, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            first = False
                    if workspace is not None:
                        _check_workspace(workspace)
                    if json_output and os.fstat(capture.fileno()).st_size > limit:
                        raise RuntimeFailure('Acquisition metadata exceeds the response limit.')
                    if process.returncode != 0:
                        raise RuntimeFailure('Acquisition command failed; check credentials and source availability.')
                except BaseException:
                    _kill_process_group(process)
                    raise
            if json_output:
                capture.seek(0)
                return json.loads(capture.read(limit).decode('utf-8'))
        except (OSError, subprocess.SubprocessError, ValueError):
            raise RuntimeFailure('Acquisition command unavailable, timed out, or returned invalid metadata.') from None


class ObsidianService:
    def __init__(self, cfg: dict, data_root: Path = DATA, *, binary: str = 'ob', publisher=None):
        self.cfg = cfg
        self.home = data_root / 'obsidian'
        self.vault = self.home / 'vault'
        self.publication = data_root / 'published' / 'obsidian'
        self.env = service_environment('obsidian', cfg, data_root)
        self.binary = binary
        self.publisher = publisher

    def command(self, *args, json_output=False, input_text=None):
        return run_command([self.binary, *args], self.env, json_output=json_output,
                           input_text=input_text, workspace=self.home)

    def _identity(self):
        actual = self.command('sync-status', '--path', str(self.vault), '--json', json_output=True)
        if not isinstance(actual, dict):
            raise IdentityFailure(FRESH_GUIDANCE)
        vault_id = actual.get('vaultId')
        if (not isinstance(vault_id, str) or not vault_id or len(vault_id) > 256
                or actual.get('vaultPath') != str(self.vault)
                or self.cfg['vault_name'] not in (vault_id, actual.get('vaultName'))):
            raise IdentityFailure(FRESH_GUIDANCE)
        return actual

    def run_once(self):
        _private_directory(self.vault)
        marker = self.home / 'source-identity.json'
        binding = _read_json(marker)
        if binding is None:
            local = self.command('sync-list-local', '--json', json_output=True)
            if type(local) is not dict or local.get('vaults') != [] or any(self.vault.iterdir()):
                raise IdentityFailure(FRESH_GUIDANCE)
            # The official non-TTY prompt accepts stdin until EOF. Avoid argv
            # credentials (visible through /proc/cmdline to peer service UIDs).
            args = ['sync-setup', '--vault', self.cfg['vault_name'], '--path', str(self.vault)]
            self.command(*args, input_text=self.cfg.get('vault_password', '') + '\n')
            actual = self._identity()
        else:
            if type(binding) is not dict or binding.get('selector') != self.cfg['vault_name']:
                raise IdentityFailure(FRESH_GUIDANCE)
            actual = self._identity()
            if binding.get('vault_id') != actual['vaultId'] or binding.get('path') != str(self.vault):
                raise IdentityFailure(FRESH_GUIDANCE)
        self.command('sync-config', '--path', str(self.vault), '--mode', 'pull-only', '--json', json_output=True)
        actual = self._identity()
        if actual.get('syncMode') != 'pull-only':
            raise IdentityFailure('Obsidian did not confirm pull-only mode; acquisition refused.')
        # A setup/mode failure never writes the success binding.
        if binding is None:
            _atomic_json(marker, {'schema_version': 1, 'selector': self.cfg['vault_name'],
                                 'vault_id': actual['vaultId'], 'path': str(self.vault)})
        self.command('sync', '--path', str(self.vault))
        # Sync returns before publication: no live sync writer races the snapshot.
        actual_after = self._identity()
        if any(actual_after.get(k) != actual.get(k) for k in ('vaultId', 'vaultPath', 'syncMode')):
            raise IdentityFailure(FRESH_GUIDANCE)
        revision = tree_revision(self.vault)
        publisher = self.publisher
        if publisher is None:
            from hardened.snapshots import publish
            publisher = publish
        return publisher(self.vault, self.publication, source={'name': 'obsidian',
            'revision': revision, 'vault_id': actual['vaultId'], 'last_successful_acquisition': utc_now()})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tree_revision(root: Path) -> str:
    """A deterministic acquisition revision; publication validates the full file policy."""
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            if name.startswith('.'):
                continue
            path = Path(directory) / name
            meta = path.lstat()
            if stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1:
                # Metadata identifies a completed acquisition without opening unsafe
                # source files; publisher computes content hashes with no-follow.
                digest.update(str(path.relative_to(root)).encode())
                digest.update(f'\0{meta.st_size}\0{meta.st_mtime_ns}\0'.encode())
    return digest.hexdigest()


def _validate_credentials(credentials: Path):
    try:
        fd = os.open(credentials, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with _FD(fd):
            meta = os.fstat(fd)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
                    or meta.st_uid != os.geteuid() or meta.st_size == 0):
                raise IdentityFailure('Unsafe reMarkable credentials; restore private managed state.')
            os.fchmod(fd, 0o600)
    except OSError:
        raise IdentityFailure('Unsafe or missing reMarkable credentials; restore private managed state.') from None


def remarkable_once(cfg: dict, data_root: Path = DATA, *, binary='remarkable-sync', rmapi='rmapi', publisher=None):
    home = data_root / 'remarkable'
    env = service_environment('remarkable', cfg, data_root)
    credentials = home / 'rmapi.json'
    pairing = home / 'paired.json'
    paired = _read_json(pairing)
    if credentials.exists() or credentials.is_symlink():
        # Credentials are opaque YAML to this layer; validate the filesystem only.
        _validate_credentials(credentials)
        # Never feed a code to an already registered instance, even if auth fails.
        run_command([rmapi, 'ls'], env)
    else:
        if paired is not None:
            raise IdentityFailure('reMarkable credentials are missing after pairing; restore credentials or use a fresh addon instance.')
        code = cfg.get('remarkable_pairing_code', '')
        if not code:
            raise IdentityFailure('Configure a reMarkable one-time pairing code in managed options, then restart.')
        run_command([rmapi, 'ls'], env, input_text=code + '\n')
        _validate_credentials(credentials)
        _atomic_json(pairing, {'schema_version': 1, 'paired_at': utc_now()})
    if paired is None:
        _atomic_json(pairing, {'schema_version': 1, 'paired_at': utc_now()})
    for name in ('generated', 'private'):
        _private_directory(home / name)
    run_command([binary], env)
    if publisher is None:
        from hardened.snapshots import publish
        publisher = publish
    return publisher(home / 'generated', data_root / 'published' / 'remarkable', source={
        'name': 'remarkable', 'revision': tree_revision(home / 'generated'),
        'acquisition_scope': 'retained_local_mirror', 'cloud_presence': 'unverified',
        'last_successful_acquisition': utc_now()})


def write_status(service: str, data_root: Path, state: str, *, now=None, message=''):
    now = time.time() if now is None else now
    path = data_root / service / 'status.json'
    uid = UIDS[service]
    _verify_directory(path.parent, uid, uid, 0o700)
    previous = _read_json(path) or {}
    value = {'schema_version': 1, 'service': service, 'state': state, 'updated_at': now,
             'pid': os.getpid(), 'message': message}
    if state == 'ok':
        value['last_success'] = now
    elif 'last_success' in previous:
        value['last_success'] = previous['last_success']
    _atomic_write(path, json.dumps(value, sort_keys=True).encode(),
                  uid=uid if os.geteuid() == 0 else None, gid=uid)


def health(cfg: dict, data_root: Path = DATA, *, now=None):
    now = time.time() if now is None else now
    statuses = {}
    healthy = True
    for service in UIDS:
        if not enabled(service, cfg):
            statuses[service] = {'state': 'disabled'}
            continue
        try:
            if (data_root / service).is_symlink():
                raise RuntimeFailure('Symlinked service root.')
            status = _read_json(data_root / service / 'status.json') or {'state': 'missing'}
            interval = cfg.get(f'{service}_sync_interval', 30)
            max_age = max(interval * 3 + 900, 120) if service != 'mcp' else 120
            good = (status.get('state') in ('ok', 'acquiring') and
                    isinstance(status.get('last_success'), (int, float)) and
                    0 <= now - status['last_success'] <= max_age and
                    now - status.get('updated_at', 0) <= max_age)
            if good:
                pid = status.get('pid')
                if type(pid) is not int or pid <= 0:
                    good = False
                else:
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        good = False
            if not good and status.get('state') == 'ok':
                status['state'] = 'stale'
        except (RuntimeFailure, TypeError, AttributeError):
            status, good = {'state': 'invalid'}, False
        statuses[service] = status
        healthy &= good
    return bool(healthy), statuses


def _start_mcp_process(cfg: dict, data_root: Path):
    return _start_process([sys.executable, '-m', 'hardened.reader'],
                          service_environment('mcp', cfg, data_root))


def _mcp_loop(cfg: dict, data_root: Path):
    install_shutdown_handler()
    child = _start_mcp_process(cfg, data_root)
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Local readiness only, never an upstream client.
        while child.poll() is None:
            try:
                import socket
                with socket.create_connection(('127.0.0.1', 8420), timeout=3) as sock:
                    with ctx.wrap_socket(sock, server_hostname='localhost'):
                        pass
                write_status('mcp', data_root, 'ok')
            except OSError:
                write_status('mcp', data_root, 'starting')
            time.sleep(5)
        raise RuntimeFailure('MCP reader exited; consult sanitized reader logs.')
    finally:
        # Includes signal exits and unexpected health/status failures. Linux's
        # parent-death signal additionally handles uncatchable supervisor death.
        _kill_process_group(child)


def _run_service(service: str, cfg: dict):
    install_shutdown_handler()
    os.environ.clear()
    os.environ.update(service_environment(service, cfg))
    for name in ('tmp', 'config'):
        _private_directory(DATA / service / name)
    os.chdir(DATA / service)
    if service == 'mcp':
        write_status(service, DATA, 'starting')
        _mcp_loop(cfg, DATA)
    interval = cfg[f'{service}_sync_interval']
    while True:
        try:
            write_status(service, DATA, 'acquiring')
            if service == 'obsidian':
                ObsidianService(cfg).run_once()
            elif service == 'git':
                from hardened.git_sync import sync_once
                sync_once(cfg, DATA / 'git', DATA / 'published' / 'git')
            else:
                remarkable_once(cfg)
            write_status(service, DATA, 'ok')
        except IdentityFailure:
            raise
        except Exception:
            # Third-party exception text can embed tokens, URLs, note contents
            # or subprocess arguments. Keep failure status/logs generic.
            write_status(service, DATA, 'error', message='Acquisition/publication failed; inspect configuration and source availability.')
            print(f'{service}: acquisition/publication failed; retry is rate-limited.', file=sys.stderr)
        time.sleep(interval)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    worker = len(args) == 2 and args[1] == '--worker'
    if (len(args) != 1 and not worker) or args[0] not in (*UIDS, 'status'):
        print('Usage: python3 -m hardened.runtime <obsidian|git|remarkable|mcp|status>', file=sys.stderr)
        return 64
    service = args[0]
    bootstrapped = False
    try:
        if worker:
            if (service not in UIDS or os.geteuid() != UIDS[service]
                    or os.getegid() != UIDS[service] or 0 in os.getgroups()):
                raise ConfigError('Private worker launch requires the isolated service identity.')
            cfg = json.loads(os.environ.pop('HARDENED_SERVICE_CONFIG'))
            if type(cfg) is not dict or set(cfg) != SCOPES[service]:
                raise ConfigError('Private worker configuration is invalid.')
            _verify_directory(DATA / service, UIDS[service], UIDS[service], 0o700)
            bootstrapped = True
            _run_service(service, cfg)
            return 0
        cfg = load_options()
        if service == 'status':
            good, statuses = health(cfg)
            print(json.dumps(statuses, sort_keys=True))
            return 0 if good else 1
        bootstrap(service)
        bootstrapped = True
        if not enabled(service, cfg):
            write_status(service, DATA, 'disabled')
            return 0
        if service == 'mcp':
            provision_tls(cfg)
        # A fresh exec is necessary: unsetenv alone leaves Linux's initial
        # /proc/self/environ bytes (and root-bootstrap Python objects) intact.
        env = worker_environment(service, cfg)
        del cfg
        drop_privileges(service)
        os.execve(sys.executable, [sys.executable, '-m', 'hardened.runtime', service, '--worker'], env)
        return 1  # execve never returns on success.
    except (ConfigError, IdentityFailure) as exc:
        if service in UIDS and bootstrapped:
            try:
                write_status(service, DATA, 'error', message=str(exc))
            except Exception:
                pass
        print(f'{service}: {exc}', file=sys.stderr)
        return 78
    except Exception:
        if service in UIDS and bootstrapped:
            try:
                write_status(service, DATA, 'error', message='Service bootstrap or runtime failed.')
            except Exception:
                pass
        print(f'{service}: service bootstrap or runtime failed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
