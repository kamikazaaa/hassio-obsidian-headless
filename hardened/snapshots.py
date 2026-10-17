"""Durable generation publication after an isolated source has finished writing.

This module does not pause an external writer. Callers own their input directory
and must finish acquisition before publishing it. Readers pin one generation.
Three recent generations are retained. Older generations can be reclaimed only
while their reader lease is exclusively held. Publications are derived data;
original source data and human workspaces are never part of this cleanup.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 10000
MAX_ENTRIES = 40000
MAX_DEPTH = 24
MAX_PATH_BYTES = 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
RETAIN_GENERATIONS = 3
MAX_RETAINED_GENERATIONS = 8
MAX_PUBLICATION_BYTES = 2 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 256 * 1024 * 1024
_GENERATION = re.compile(r'gen-[0-9a-f]{32}\Z')
DOCUMENT_SUFFIXES = frozenset({'.md', '.markdown', '.txt', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.canvas', '.csv'})
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _mkdir(parent: int, name: str) -> int:
    os.mkdir(name, 0o2750, dir_fd=parent)
    child = os.open(name, _DIR_FLAGS, dir_fd=parent)
    try:
        # Service umask is intentionally 077. Publication alone grants the
        # inherited reader group read/traverse access, never write access.
        os.fchmod(child, 0o2750)
        os.fsync(child)
        os.fsync(parent)
    except BaseException:
        os.close(child)
        raise
    return child


def _path(path: Path) -> Path:
    path = Path(path)
    if '..' in path.parts:
        raise ValueError('parent components are forbidden')
    return Path(os.path.abspath(path))


@contextmanager
def directory_fd(path: Path, *, create: bool = False):
    """Open every ancestor without following links; optionally create directories."""
    path = _path(path)
    fd = os.open('/', _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    next_fd = _mkdir(fd, part)
                except FileExistsError:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)


def _metadata(source: dict) -> dict:
    if not isinstance(source, dict):
        raise ValueError('source metadata must be an object')
    for key, value in source.items():
        if not isinstance(key, str) or any(word in key.lower() for word in ('password', 'token', 'secret', 'credential')):
            raise ValueError('secret fields are forbidden in publication metadata')
        if not isinstance(value, (str, int, bool, type(None))):
            raise ValueError('source metadata must contain scalar values')
    raw = json.dumps(source, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > 4096:
        raise ValueError('source metadata too large')
    return dict(source)


def _write_new(directory: int, name: str, data: bytes, mode: int = 0o640) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=directory)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short write')
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_tree(src_fd: int, dst_fd: int, prefix: str, records: dict, budget: dict, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ValueError('source nesting limit exceeded')
    directory_before = _identity(os.fstat(src_fd))
    names = []
    with os.scandir(src_fd) as entries:
        for entry in entries:
            budget['entries'] += 1
            if budget['entries'] > MAX_ENTRIES:
                raise ValueError('source entry limit exceeded')
            names.append(entry.name)
    for name in sorted(names):
        if name.startswith('.'):
            budget['omitted'] += 1
            continue
        if '\\' in name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name) or name in ('', '.', '..'):
            raise ValueError('unsafe source filename')
        rel = f'{prefix}/{name}' if prefix else name
        if len(rel.encode('utf-8')) > MAX_PATH_BYTES:
            raise ValueError('source path limit exceeded')
        entry_stat = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise ValueError('source symlinks are not publishable')
        if stat.S_ISDIR(entry_stat.st_mode):
            child_src = os.open(name, _DIR_FLAGS, dir_fd=src_fd)
            child_dst = _mkdir(dst_fd, name)
            try:
                if (entry_stat.st_dev, entry_stat.st_ino) != (os.fstat(child_src).st_dev, os.fstat(child_src).st_ino):
                    raise ValueError('source directory changed during acquisition')
                _copy_tree(child_src, child_dst, rel, records, budget, depth + 1)
                os.fsync(child_dst)
            finally:
                os.close(child_src)
                os.close(child_dst)
            continue
        if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1:
            raise ValueError('only regular single-link source files are supported')
        if Path(name).suffix.lower() not in DOCUMENT_SUFFIXES:
            budget['omitted'] += 1
            continue
        if len(records) >= MAX_FILES or entry_stat.st_size > MAX_FILE_BYTES:
            raise ValueError('source file limit exceeded')
        input_fd = os.open(name, _FILE_FLAGS, dir_fd=src_fd)
        output_fd = None
        try:
            before = os.fstat(input_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _identity(before) != _identity(entry_stat):
                raise ValueError('source file changed during acquisition')
            output_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640, dir_fd=dst_fd)
            os.fchmod(output_fd, 0o640)
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(input_fd, 1024 * 1024)
                if not block:
                    break
                size += len(block)
                budget['bytes'] += len(block)
                if size > MAX_FILE_BYTES or budget['bytes'] > MAX_TOTAL_BYTES:
                    raise ValueError('publication size limit exceeded')
                _space(dst_fd, budget['existing_bytes'] + budget['bytes'])
                digest.update(block)
                view = memoryview(block)
                while view:
                    count = os.write(output_fd, view)
                    if count <= 0:
                        raise OSError('short write')
                    view = view[count:]
            after = os.fstat(input_fd)
            if _identity(before) != _identity(after) or size != before.st_size:
                raise ValueError('source file changed while copying')
            os.fsync(output_fd)
            records[rel] = {'sha256': digest.hexdigest(), 'size': size}
        finally:
            if output_fd is not None:
                os.close(output_fd)
            os.close(input_fd)
    if directory_before != _identity(os.fstat(src_fd)):
        raise ValueError('source directory changed while copying')
    os.fsync(dst_fd)



def _read_json(parent: int, name: str, maximum: int) -> dict:
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError('unsafe publication metadata')
        data = bytearray()
        while len(data) <= maximum:
            block = os.read(fd, min(65536, maximum + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        if len(data) > maximum or _identity(before) != _identity(os.fstat(fd)):
            raise ValueError('publication metadata changed or exceeds its limit')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate publication metadata key')
                result[key] = value
            return result
        value = json.loads(data, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite metadata')))
        if not isinstance(value, dict):
            raise ValueError('invalid publication metadata')
        return value
    finally:
        os.close(fd)


def _current(pub_fd: int) -> str | None:
    try:
        _require_access(os.stat('current.json', dir_fd=pub_fd, follow_symlinks=False), os.fstat(pub_fd).st_gid)
        pointer = _read_json(pub_fd, 'current.json', 4096)
    except FileNotFoundError:
        return None
    generation = pointer.get('generation')
    if (type(pointer.get('schema_version')) is not int or pointer['schema_version'] != 1
            or not isinstance(generation, str) or not _GENERATION.fullmatch(generation)):
        raise ValueError('invalid current publication pointer')
    return generation


def _require_access(info: os.stat_result, reader_gid: int, *, directory: bool = False) -> None:
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    expected_mode = 0o2750 if directory else 0o640
    if (not kind_ok or info.st_uid != os.geteuid() or info.st_gid != reader_gid
            or stat.S_IMODE(info.st_mode) != expected_mode):
        raise ValueError('publication access permissions are invalid; restore validated managed state')


def _cleanup_pointers(pub_fd: int) -> None:
    with os.scandir(pub_fd) as entries:
        names = []
        for entry in entries:
            if len(names) >= 128:
                raise ValueError('too many publication control entries; inspect storage')
            names.append(entry.name)
    for name in names:
        if name in ('.publish.lock', 'current.json', 'generations'):
            continue
        if not re.fullmatch(r'\.current-[0-9a-f]{32}', name):
            raise ValueError('unknown publication control entry; inspect storage')
        info = os.stat(name, dir_fd=pub_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ValueError('unsafe interrupted publication pointer')
        os.unlink(name, dir_fd=pub_fd)
    os.fsync(pub_fd)


def _space(fd: int, occupied: int) -> None:
    available = os.fstatvfs(fd)
    if occupied > MAX_PUBLICATION_BYTES or available.f_bavail * available.f_frsize < MIN_FREE_BYTES:
        raise ValueError('publication storage budget or free-space reserve exceeded')


def _walk_owned(fd: int, budget: dict, *, remove: bool = False, depth: int = 0, reader_gid: int | None = None) -> None:
    """Bounded, descriptor-relative traversal of a known derived generation.

    Never follows a directory/file link. Deletion is used only for managed
    generation names under a held publication lock and, for complete historical
    generations, an exclusive reader lease. Unknown entries fail visibly.
    """
    if depth > MAX_DEPTH + 4:
        raise ValueError('publication directory nesting exceeds limit')
    if reader_gid is not None:
        _require_access(os.fstat(fd), reader_gid, directory=True)
    with os.scandir(fd) as entries:
        names = []
        for entry in entries:
            budget['entries'] += 1
            if budget['entries'] > MAX_ENTRIES + 16:
                raise ValueError('publication directory entry limit exceeded')
            names.append(entry.name)
    for name in names:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_uid != os.geteuid():
            raise ValueError('publication contains unexpected ownership')
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            try:
                held = os.fstat(child)
                if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError('publication directory changed')
                _walk_owned(child, budget, remove=remove, depth=depth + 1, reader_gid=reader_gid)
            finally:
                os.close(child)
            if remove:
                os.rmdir(name, dir_fd=fd)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if reader_gid is not None:
                _require_access(info, reader_gid)
            budget['bytes'] += info.st_size
            if remove:
                os.unlink(name, dir_fd=fd)
        else:
            raise ValueError('publication contains a link or special file')
    if remove:
        os.fsync(fd)


def _remove_generation(generations_fd: int, name: str) -> None:
    # First validate the entire candidate, so a late unsafe entry does not cause
    # partial cleanup before the condition is reported. No source roots accepted.
    fd = os.open(name, _DIR_FLAGS, dir_fd=generations_fd)
    try:
        _walk_owned(fd, {'entries': 0, 'bytes': 0})
        _walk_owned(fd, {'entries': 0, 'bytes': 0}, remove=True)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=generations_fd)
    os.fsync(generations_fd)


def _recover_retired(generations_fd: int, name: str) -> tuple[str, int, int] | None:
    """A reader may have acquired its lease after the retiring process died.

    A remaining lease must be locked again, not presumed inactive from the
    retired name. A missing lease is safe only in this retired cleanup state:
    cleanup unlinked it under EX, and any delayed reader holding the old inode
    must fail the reader's single-link/path revalidation before reading data.
    """
    fd = os.open(name, _DIR_FLAGS, dir_fd=generations_fd)
    lease = None
    try:
        try:
            lease = os.open('.lease', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            pass
        if lease is not None:
            held = os.fstat(lease)
            if not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
                raise ValueError('unsafe retired publication lease')
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                budget = {'entries': 0, 'bytes': 0}
                _walk_owned(fd, budget)
                return (name, os.fstat(fd).st_mtime_ns, budget['bytes'])
            linked = os.stat('.lease', dir_fd=fd, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (linked.st_dev, linked.st_ino):
                raise ValueError('retired publication lease changed')
        _remove_generation(generations_fd, name)
        return None
    finally:
        if lease is not None:
            os.close(lease)
        os.close(fd)


def _inventory(generations_fd: int, current: str | None) -> list[tuple[str, int, int]]:
    with os.scandir(generations_fd) as entries:
        names = []
        for entry in entries:
            if len(names) >= MAX_RETAINED_GENERATIONS + 8:
                raise ValueError('too many interrupted or retained publications; inspect storage')
            names.append(entry.name)
    result = []
    for name in names:
        if name.startswith('.pending-') and _GENERATION.fullmatch(name[len('.pending-'):]):
            _remove_generation(generations_fd, name)
            continue
        if name.startswith('.retired-') and _GENERATION.fullmatch(name[len('.retired-'):]):
            retained = _recover_retired(generations_fd, name)
            if retained is not None:
                result.append(retained)
            continue
        if not _GENERATION.fullmatch(name):
            raise ValueError('unknown publication directory; inspect storage')
        fd = os.open(name, _DIR_FLAGS, dir_fd=generations_fd)
        try:
            budget = {'entries': 0, 'bytes': 0}
            _walk_owned(fd, budget)
            manifest = _read_json(fd, 'manifest.json', MAX_MANIFEST_BYTES)
            if manifest.get('generation') != name or manifest.get('schema_version') != 1:
                raise ValueError('invalid historical publication manifest')
            info = os.stat('manifest.json', dir_fd=fd, follow_symlinks=False)
            result.append((name, info.st_mtime_ns, budget['bytes']))
        finally:
            os.close(fd)
    if current is not None and not any(item[0] == current for item in result):
        raise ValueError('current publication generation is missing')
    return result


def _collect(generations_fd: int, current: str | None) -> list[tuple[str, int, int]]:
    inventory = _inventory(generations_fd, current)
    recent = sorted((item for item in inventory if _GENERATION.fullmatch(item[0])),
                    key=lambda item: (item[1], item[0]), reverse=True)
    protected = {item[0] for item in recent[:RETAIN_GENERATIONS]}
    protected.add(current)
    retained = []
    for name, timestamp, size in inventory:
        if name in protected or name.startswith('.retired-'):
            retained.append((name, timestamp, size))
            continue
        gen_fd = os.open(name, _DIR_FLAGS, dir_fd=generations_fd)
        lease_fd = None
        try:
            lease_fd = os.open('.lease', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=gen_fd)
            held = os.fstat(lease_fd)
            if not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
                raise ValueError('unsafe publication generation lease')
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                retained.append((name, timestamp, size))
                continue
            linked = os.stat('.lease', dir_fd=gen_fd, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (linked.st_dev, linked.st_ino):
                raise ValueError('publication generation lease changed')
            # Mark incomplete cleanup so a later restart can safely resume it.
            retired = '.retired-' + name
            os.rename(name, retired, src_dir_fd=generations_fd, dst_dir_fd=generations_fd)
            os.fsync(generations_fd)
            _remove_generation(generations_fd, retired)
        finally:
            if lease_fd is not None:
                os.close(lease_fd)
            os.close(gen_fd)
    return retained


def _same_generation(generations_fd: int, current: str | None, candidate: dict) -> dict | None:
    if current is None:
        return None
    fd = os.open(current, _DIR_FLAGS, dir_fd=generations_fd)
    try:
        old = _read_json(fd, 'manifest.json', MAX_MANIFEST_BYTES)
        # Use the same validation as the reader. An invalid control contract is
        # an explicit unhealthy condition, not a successfully reusable copy.
        # In particular, replacing an invalid old lease could lose a real lock.
        from hardened.reader import Reader, _generation_lease
        Reader._validate_manifest(old, current)
        _walk_owned(fd, {'entries': 0, 'bytes': 0}, reader_gid=os.fstat(generations_fd).st_gid)
        with _generation_lease(fd):
            pass
        # Fresh acquisition times are recorded by runtime health. Reusing a
        # generation keeps its original publication/acquisition time truthful.
        def stable(metadata):
            return {k: v for k, v in metadata.items() if k != 'last_successful_acquisition'}
        if (not isinstance(old.get('source'), dict) or stable(old['source']) != stable(candidate['source'])
                or old.get('files') != candidate['files'] or old.get('omitted_entries') != candidate['omitted_entries']):
            return None
        # A copy corrupted outside the publisher must be repaired, not reused
        # merely because its manifest still advertises the expected hashes.
        files_fd = os.open('files', _DIR_FLAGS, dir_fd=fd)
        try:
            for path, entry in candidate['files'].items():
                parent = os.dup(files_fd)
                try:
                    parts = path.split('/')
                    for part in parts[:-1]:
                        child = os.open(part, _DIR_FLAGS, dir_fd=parent)
                        os.close(parent)
                        parent = child
                    doc = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
                    try:
                        before = os.fstat(doc)
                        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                                or before.st_size != entry['size']):
                            return None
                        digest = hashlib.sha256()
                        remaining = entry['size']
                        while remaining:
                            block = os.read(doc, min(1024 * 1024, remaining))
                            if not block:
                                return None
                            remaining -= len(block)
                            digest.update(block)
                        if digest.hexdigest() != entry['sha256'] or _identity(before) != _identity(os.fstat(doc)):
                            return None
                    finally:
                        os.close(doc)
                finally:
                    os.close(parent)
        except FileNotFoundError:
            return None
        finally:
            os.close(files_fd)
        return old
    finally:
        os.close(fd)


def publish(source_root: Path, publication_root: Path, *, source: dict) -> dict:
    """Publish a complete isolated source, then retain recent/actively read copies.

    Failure before pointer replacement preserves the last-good pointer. Failure
    after replacement (including directory fsync/retention) may leave the new,
    complete generation current; it never advertises an incomplete generation.
    Derived staging/retired trees are cleaned safely after crashes. This is not a
    backup or a transaction across remote sources and underlying storage devices.
    """
    source_root, publication_root = _path(source_root), _path(publication_root)
    if source_root == publication_root or source_root in publication_root.parents or publication_root in source_root.parents:
        raise ValueError('source and publication directories must not overlap')
    source = _metadata(source)
    with directory_fd(source_root) as src_fd, directory_fd(publication_root, create=True) as pub_fd:
        _require_access(os.fstat(pub_fd), os.fstat(pub_fd).st_gid, directory=True)
        lock_fd = os.open('.publish.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=pub_fd)
        try:
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise ValueError('unsafe publication lock')
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _cleanup_pointers(pub_fd)
            try:
                generations_fd = _mkdir(pub_fd, 'generations')
            except FileExistsError:
                generations_fd = os.open('generations', _DIR_FLAGS, dir_fd=pub_fd)
            pending = pointer_name = None
            try:
                _require_access(os.fstat(generations_fd), os.fstat(pub_fd).st_gid, directory=True)
                current = _current(pub_fd)
                retained = _collect(generations_fd, current)
                if len(retained) >= MAX_RETAINED_GENERATIONS:
                    raise ValueError('publication retention is blocked by active readers')
                occupied = sum(item[2] for item in retained)
                _space(pub_fd, occupied)
                generation = 'gen-' + uuid.uuid4().hex
                pending = '.pending-' + generation
                gen_fd = _mkdir(generations_fd, pending)
                try:
                    _write_new(gen_fd, '.lease', b'')
                    files_fd = _mkdir(gen_fd, 'files')
                    records: dict = {}
                    budget = {'bytes': 0, 'entries': 0, 'omitted': 0, 'existing_bytes': occupied}
                    try:
                        _copy_tree(src_fd, files_fd, '', records, budget)
                    finally:
                        os.close(files_fd)
                    manifest = {
                        'schema_version': 1, 'generation': generation,
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'source': source, 'files': records,
                        'total_bytes': budget['bytes'], 'omitted_entries': budget['omitted'],
                    }
                    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
                    if len(raw) > MAX_MANIFEST_BYTES:
                        raise ValueError('publication manifest limit exceeded')
                    _write_new(gen_fd, 'manifest.json', raw)
                    os.fsync(gen_fd)
                finally:
                    os.close(gen_fd)
                same = _same_generation(generations_fd, current, manifest)
                if same is not None:
                    return same
                os.rename(pending, generation, src_dir_fd=generations_fd, dst_dir_fd=generations_fd)
                pending = None
                os.fsync(generations_fd)
                pointer_name = '.current-' + uuid.uuid4().hex
                pointer = json.dumps({'schema_version': 1, 'generation': generation}, separators=(',', ':')).encode()
                _write_new(pub_fd, pointer_name, pointer)
                os.replace(pointer_name, 'current.json', src_dir_fd=pub_fd, dst_dir_fd=pub_fd)
                pointer_name = None
                os.fsync(pub_fd)
                _collect(generations_fd, generation)
                return manifest
            finally:
                try:
                    if pending is not None:
                        _remove_generation(generations_fd, pending)
                    if pointer_name is not None:
                        os.unlink(pointer_name, dir_fd=pub_fd)
                        os.fsync(pub_fd)
                finally:
                    os.close(generations_fd)
        finally:
            os.close(lock_fd)
