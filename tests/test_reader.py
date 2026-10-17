"""Real files and HTTP transport regressions for the read-only MCP boundary."""
import hashlib
import json
import os
from pathlib import Path

import pytest


def publication(tmp_path, files=None, generation='g-1'):
    files = files or {'notes/hello.md': b'hello world\nsecond hello\n'}
    root = tmp_path / 'published'
    base = root / 'obsidian' / 'generations' / generation
    (base / 'files').mkdir(parents=True)
    entries = {}
    for name, data in files.items():
        dest = base / 'files' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        entries[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    manifest = {'schema_version': 1, 'generation': generation,
                'created_at': '2026-10-17T00:00:00Z',
                'source': {'kind': 'obsidian', 'revision': 'rev-1', 'last_successful_acquisition': '2026-10-17T00:00:00Z'},
                'files': entries}
    (base / '.lease').write_bytes(b'')
    (base / '.lease').chmod(0o640)
    (base / 'manifest.json').write_text(json.dumps(manifest))
    (root / 'obsidian' / 'current.json').write_text(json.dumps({'schema_version': 1, 'generation': generation}))
    return root, base, manifest


def test_reader_lists_reads_searches_with_namespace_and_provenance(tmp_path):
    from hardened.reader import Reader
    root, _, _ = publication(tmp_path)
    reader = Reader(root)
    listing = reader.list_documents('obsidian')
    assert listing['source'] == 'obsidian'
    assert listing['generation'] == 'g-1'
    assert listing['files'][0]['path'] == 'notes/hello.md'
    result = reader.read_document('obsidian', 'notes/hello.md')
    assert result['content'] == 'hello world\nsecond hello\n'
    assert result['provenance']['acquisition']['revision'] == 'rev-1'
    assert result['provenance']['sha256'] == hashlib.sha256(result['content'].encode()).hexdigest()
    found = reader.search_documents('obsidian', 'hello', limit=1)
    assert len(found['matches']) == 1
    assert found['truncated'] is False
    assert found['matches'][0]['provenance']['generation'] == 'g-1'


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', '.secret', 'a//b', 'a/./b', 'a/../b', 'a\\b', 'a\x00b'])
def test_rejects_unsafe_document_paths(tmp_path, path):
    from hardened.reader import Reader, ReaderError
    root, _, _ = publication(tmp_path)
    with pytest.raises(ReaderError):
        Reader(root).read_document('obsidian', path)


@pytest.mark.parametrize('attack', ['file_symlink', 'directory_symlink', 'hardlink', 'fifo', 'current_symlink', 'manifest_symlink', 'generation_symlink', 'root_symlink'])
def test_rejects_unsafe_filesystem_objects(tmp_path, attack):
    from hardened.reader import Reader, ReaderError
    root, base, _ = publication(tmp_path)
    path = base / 'files/notes/hello.md'
    outside = tmp_path / 'outside'
    outside.write_text('private')
    if attack == 'file_symlink':
        path.unlink(); path.symlink_to(outside)
    elif attack == 'directory_symlink':
        path.unlink(); path.parent.rmdir(); path.parent.symlink_to(tmp_path, target_is_directory=True)
    elif attack == 'hardlink':
        os.link(path, tmp_path / 'copy')
    elif attack == 'fifo':
        path.unlink(); os.mkfifo(path)
    elif attack in {'current_symlink', 'manifest_symlink'}:
        target = root / 'obsidian/current.json' if attack == 'current_symlink' else base / 'manifest.json'
        target.rename(outside); target.symlink_to(outside)
    elif attack == 'generation_symlink':
        new = base.with_name('moved'); base.rename(new); base.symlink_to(new, target_is_directory=True)
    else:
        new = root.with_name('moved'); root.rename(new); root.symlink_to(new, target_is_directory=True)
    with pytest.raises(ReaderError):
        Reader(root).read_document('obsidian', 'notes/hello.md')


@pytest.mark.parametrize('attack', ['bad_generation', 'manifest_generation', 'hidden_manifest_path', 'oversized', 'digest', 'duplicate_keys', 'bad_utf8', 'large_source'])
def test_rejects_corrupt_publications(tmp_path, attack):
    from hardened.reader import Reader, ReaderError
    root, base, manifest = publication(tmp_path)
    if attack == 'bad_generation':
        (root / 'obsidian/current.json').write_text('{"schema_version":1,"generation":"../outside"}')
    elif attack == 'duplicate_keys':
        (root / 'obsidian/current.json').write_text('{"schema_version":1,"generation":"g-1","generation":"g-1"}')
    elif attack == 'bad_utf8':
        data = b'\xff'; (base / 'files/notes/hello.md').write_bytes(data)
        manifest['files']['notes/hello.md'] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    elif attack == 'manifest_generation':
        manifest['generation'] = 'other'
    elif attack == 'hidden_manifest_path':
        manifest['files']['.private'] = manifest['files']['notes/hello.md']
    elif attack == 'oversized':
        manifest['files']['notes/hello.md']['size'] = 1024 * 1024 + 1
    elif attack == 'large_source':
        manifest['source']['huge'] = 'x' * 4097
    else:
        (base / 'files/notes/hello.md').write_text('tampered')
    (base / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ReaderError):
        Reader(root).read_document('obsidian', 'notes/hello.md')


def test_generation_is_pinned_even_when_current_changes(tmp_path, monkeypatch):
    from hardened.reader import Reader
    root, base, _ = publication(tmp_path)
    reader = Reader(root)
    original = reader._content
    def changed(fd, path, entry):
        (root / 'obsidian/current.json').write_text('{"schema_version":1,"generation":"missing-new-generation"}')
        return original(fd, path, entry)
    monkeypatch.setattr(reader, '_content', changed)
    assert reader.read_document('obsidian', 'notes/hello.md')['provenance']['generation'] == 'g-1'


def test_search_and_argument_limits(tmp_path):
    from hardened.reader import Reader, ReaderError
    root, _, _ = publication(tmp_path, {f'{i:03}.md': b'hello' for i in range(105)})
    reader = Reader(root)
    result = reader.search_documents('obsidian', 'absent')
    assert result['scanned_files'] == 100
    assert result['truncated'] and result['next_offset'] == 100
    for query in ['', 'x' * 257, 'a\nb']:
        with pytest.raises(ReaderError): reader.search_documents('obsidian', query)
    for limit in [0, 101, True]:
        with pytest.raises(ReaderError): reader.list_documents('obsidian', limit=limit)
    with pytest.raises(ReaderError): reader.list_documents('private')


TOKEN = 'a' * 48


def test_http_authentication_host_origin_and_body_guards(tmp_path):
    from hardened.reader import create_app
    from starlette.testclient import TestClient
    root, _, _ = publication(tmp_path)
    app = create_app(root, token=TOKEN, allowed_hosts=['testserver'], allowed_origins=['https://trusted.example'])
    with TestClient(app) as client:
        for path in ['/mcp', '/files/notes/hello.md', '/health', '/']:
            response = client.get(path)
            assert response.status_code == 401
            assert response.headers['www-authenticate'].startswith('Bearer')
        assert client.get('/mcp', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        auth = {'Authorization': f'Bearer {TOKEN}'}
        assert client.get('/mcp', headers={**auth, 'Host': 'evil.example'}).status_code == 421
        assert client.get('/mcp', headers={**auth, 'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/mcp', headers={**auth, 'Host': 'testserver', 'Origin': 'null'}).status_code == 403
        assert client.get('/private', headers=auth).status_code == 404
        assert client.post('/mcp', headers=auth, content=b'x' * 32769).status_code == 413
        duplicate = [('Authorization', f'Bearer {TOKEN}'), ('Authorization', f'Bearer {TOKEN}')]
        assert client.get('/mcp', headers=duplicate).status_code == 401


def test_startup_rejects_weak_token_wildcards_and_remote_without_tls(tmp_path):
    from hardened.reader import create_app, settings_from_env
    for token in ['', 'short', 'a' * 31, 'a' * 32 + '\n']:
        with pytest.raises(ValueError): create_app(tmp_path, token=token, allowed_hosts=['localhost'], allowed_origins=[])
    for hosts in [[], ['*'], ['example:*'], ['https://example'], ['evil/path']]:
        with pytest.raises(ValueError): create_app(tmp_path, token=TOKEN, allowed_hosts=hosts, allowed_origins=[])
    with pytest.raises(ValueError):
        settings_from_env({'MCP_BEARER_TOKEN': TOKEN, 'MCP_HOST': '0.0.0.0', 'MCP_ALLOWED_HOSTS': 'vault.example:8420'})


@pytest.mark.parametrize('tls', [False, True])
def test_official_sdk_http_initialize_list_call_and_reject_mutations(tmp_path, tls):
    """A real ephemeral TCP listener, official SDK client and official server."""
    import asyncio
    import socket
    import threading
    import time
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from hardened.reader import create_app
    root, _, _ = publication(tmp_path)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    app = create_app(root, token=TOKEN, allowed_hosts=[f'127.0.0.1:{port}'], allowed_origins=[])
    tls_settings = {}
    verify = True
    scheme = 'https' if tls else 'http'
    if tls:
        import ssl
        cert, key = tls_files(tmp_path)
        tls_settings = {'ssl_certfile': str(cert), 'ssl_keyfile': str(key)}
        verify = ssl.create_default_context(cafile=str(cert))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', proxy_headers=False, **tls_settings))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started: break
            if not thread.is_alive(): pytest.fail('Local MCP server exited before startup')
            time.sleep(.01)
        assert server.started
        async def exercise():
            import httpx
            async with httpx.AsyncClient(headers={'Authorization': f'Bearer {TOKEN}'}, trust_env=False, verify=verify) as http_client:
              async with streamable_http_client(f'{scheme}://127.0.0.1:{port}/mcp', http_client=http_client) as (read, write, _):
                  async with ClientSession(read, write) as session:
                      initialized = await session.initialize()
                      assert initialized.serverInfo.name == 'Read-only published notes'
                      tools = (await session.list_tools()).tools
                      assert {tool.name for tool in tools} == {'list_documents', 'read_document', 'search_documents'}
                      assert all(tool.annotations.readOnlyHint for tool in tools)
                      result = await session.call_tool('read_document', {'source': 'obsidian', 'path': 'notes/hello.md'})
                      assert not result.isError
                      assert result.structuredContent['provenance']['generation'] == 'g-1'
                      assert result.structuredContent['content'].startswith('hello')
                      listing = await session.call_tool('list_documents', {'source': 'obsidian'})
                      assert listing.structuredContent['files'][0]['path'] == 'notes/hello.md'
                      search = await session.call_tool('search_documents', {'source': 'obsidian', 'query': 'world'})
                      assert search.structuredContent['matches'][0]['line'] == 1
                      invalid = await session.call_tool('read_document', {'source': 'obsidian', 'path': '../secret'})
                      assert invalid.isError
                      mutation = await session.call_tool('write_document', {'source': 'obsidian', 'path': 'notes/hello.md', 'content': 'changed'})
                      assert mutation.isError
                      assert (await session.list_resources()).resources == []
        asyncio.run(exercise())
        assert (root / 'obsidian/generations/g-1/files/notes/hello.md').read_text().startswith('hello')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


def test_search_cursor_progresses_past_matched_document(tmp_path):
    from hardened.reader import Reader
    root, _, _ = publication(tmp_path, {'a.md': b'hello\nhello', 'b.md': b'hello'})
    reader = Reader(root)
    first = reader.search_documents('obsidian', 'hello', limit=1)
    assert first['next_offset'] == 1
    second = reader.search_documents('obsidian', 'hello', limit=1, offset=first['next_offset'])
    assert second['matches'][0]['path'] == 'b.md'
    assert second['next_offset'] is None
    assert second['truncated'] is False


def test_manifest_file_entry_cannot_override_provenance(tmp_path):
    from hardened.reader import Reader, ReaderError
    root, base, manifest = publication(tmp_path)
    manifest['files']['notes/hello.md']['source'] = 'forged'
    (base / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ReaderError): Reader(root).read_document('obsidian', 'notes/hello.md')


def test_case_insensitive_bearer_scheme_and_chunked_body_limit(tmp_path):
    from hardened.reader import create_app
    from starlette.testclient import TestClient
    root, _, _ = publication(tmp_path)
    with TestClient(create_app(root, token=TOKEN, allowed_hosts=['testserver'], allowed_origins=[])) as client:
        auth = {'Authorization': f'bearer {TOKEN}'}
        assert client.get('/private', headers=auth).status_code == 404
        assert client.post('/mcp', headers=auth, content=iter([b'x' * 16000] * 3)).status_code == 413


def test_reader_rejects_metadata_limits_and_incomplete_publications(tmp_path):
    from hardened.reader import Reader, ReaderError
    root, base, manifest = publication(tmp_path)
    (base / 'manifest.json').write_bytes(b' ' * (4 * 1024 * 1024 + 1))
    with pytest.raises(ReaderError): Reader(root).list_documents('obsidian')
    (base / 'manifest.json').unlink()
    with pytest.raises(ReaderError): Reader(root).list_documents('obsidian')
    with pytest.raises(ReaderError): Reader(root).list_documents('remarkable')


def test_search_byte_budget_is_explicit(tmp_path):
    from hardened.reader import Reader
    root, _, _ = publication(tmp_path, {f'{i:02}.txt': b'x' * (1024 * 1024) for i in range(10)})
    result = Reader(root).search_documents('obsidian', 'absent')
    assert result['scanned_bytes'] == 8 * 1024 * 1024
    assert result['scanned_files'] == 8
    assert result['truncated'] is True and result['next_offset'] == 8


def test_attachments_do_not_disable_text_notes(tmp_path):
    from hardened.reader import Reader, ReaderError
    root, _, _ = publication(tmp_path, {'attachment.pdf': b'%PDF' + b'x' * (2 * 1024 * 1024), 'binary.png': b'\x89PNG\xff', 'note.md': b'find this note'})
    reader = Reader(root)
    assert len(reader.list_documents('obsidian')['files']) == 3
    assert reader.read_document('obsidian', 'note.md')['content'] == 'find this note'
    with pytest.raises(ReaderError, match='too large'):
        reader.read_document('obsidian', 'attachment.pdf')
    result = reader.search_documents('obsidian', 'find')
    assert result['matches'][0]['path'] == 'note.md'
    assert result['incomplete_scope'] is True
    assert {x['path'] for x in result['skipped']} == {'attachment.pdf', 'binary.png'}


def test_unicode_document_path(tmp_path):
    from hardened.reader import Reader
    root, _, _ = publication(tmp_path, {'日本語/考え.md': 'café'.encode()})
    assert Reader(root).read_document('obsidian', '日本語/考え.md')['content'] == 'café'


def tls_files(tmp_path):
    """Generate an ephemeral local-test certificate; never use external credentials."""
    import ipaddress
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'local MCP test')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


def test_nonfinite_json_metadata_is_rejected(tmp_path):
    from hardened.reader import Reader, ReaderError
    root, base, manifest = publication(tmp_path)
    manifest['source']['invalid'] = float('nan')
    (base / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ReaderError): Reader(root).list_documents('obsidian')


@pytest.mark.parametrize('attack', ['missing', 'symlink', 'hardlink', 'fifo'])
def test_generation_lease_must_be_present_regular_single_link(tmp_path, attack):
    from hardened.reader import Reader, ReaderError
    root, base, _ = publication(tmp_path)
    lease = base / '.lease'
    if attack == 'hardlink':
        os.link(lease, tmp_path / 'other-lease')
    else:
        lease.unlink()
        if attack == 'symlink':
            target = tmp_path / 'outside-lease'
            target.write_bytes(b'')
            lease.symlink_to(target)
        elif attack == 'fifo':
            os.mkfifo(lease)
    with pytest.raises(ReaderError):
        Reader(root).read_document('obsidian', 'notes/hello.md')


def test_active_reader_blocks_exclusive_cleanup_until_request_finishes(tmp_path, monkeypatch):
    import concurrent.futures
    import fcntl
    import threading
    from hardened.reader import Reader
    root, base, _ = publication(tmp_path)
    reader = Reader(root)
    entered, resume = threading.Event(), threading.Event()
    original = reader._content
    def pause_after_pin(fd, path, entry):
        entered.set()
        assert resume.wait(5), 'test did not release reader'
        return original(fd, path, entry)
    monkeypatch.setattr(reader, '_content', pause_after_pin)
    cleanup_fd = os.open(base / '.lease', os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reader.read_document, 'obsidian', 'notes/hello.md')
            try:
                assert entered.wait(5), 'reader did not enter generation'
                with pytest.raises(BlockingIOError):
                    fcntl.flock(cleanup_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                resume.set()
            assert future.result(timeout=5)['provenance']['generation'] == 'g-1'
        fcntl.flock(cleanup_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(cleanup_fd, fcntl.LOCK_UN)
    finally:
        os.close(cleanup_fd)


@pytest.mark.parametrize('race', ['unlinked', 'replaced'])
def test_retired_or_replaced_lease_inode_is_rejected_before_read(tmp_path, monkeypatch, race):
    import fcntl
    from hardened.reader import Reader, ReaderError
    root, base, _ = publication(tmp_path)
    lease = base / '.lease'
    original = fcntl.flock
    def retire_before_shared_acquisition(fd, operation):
        if operation & fcntl.LOCK_SH:
            if race == 'unlinked':
                lease.unlink()
            else:
                lease.rename(base / '.old-lease')
                lease.write_bytes(b'')
        return original(fd, operation)
    monkeypatch.setattr(fcntl, 'flock', retire_before_shared_acquisition)
    with pytest.raises(ReaderError):
        Reader(root).read_document('obsidian', 'notes/hello.md')


def test_exclusive_retirement_lock_fails_closed_and_retry_succeeds(tmp_path):
    import fcntl
    from hardened.reader import Reader, ReaderError
    root, base, _ = publication(tmp_path)
    reader = Reader(root)
    fd = os.open(base / '.lease', os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ReaderError, match='retry'):
            reader.read_document('obsidian', 'notes/hello.md')
        fcntl.flock(fd, fcntl.LOCK_UN)
        assert reader.read_document('obsidian', 'notes/hello.md')['content'].startswith('hello')
    finally:
        os.close(fd)


def test_corrupt_content_error_releases_generation_lease(tmp_path):
    import fcntl
    from hardened.reader import Reader, ReaderError
    root, base, _ = publication(tmp_path)
    (base / 'files/notes/hello.md').write_text('corrupted')
    with pytest.raises(ReaderError):
        Reader(root).read_document('obsidian', 'notes/hello.md')
    fd = os.open(base / '.lease', os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
