#!/usr/local/bin/python3
"""Synthetic inputs and the two deliberately narrow cloud CLI adapters.

This file is copied as /usr/local/bin/{ob,rmapi} only in disposable test
containers. All application modules, S6 scripts, Git and the Go converter stay
as built. The HTTPS fixture uses the installed git-http-backend, not a Git shim.
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
import zipfile
import zlib

ROOT = Path('/opt/container-test')
SERVICES = {'obsidian': 'obsidian-sync', 'git': 'vault-git-sync',
            'remarkable': 'remarkable-sync', 'mcp': 'obsidian-mcp-server'}
UIDS = {'obsidian': 10001, 'git': 10002, 'remarkable': 10003, 'mcp': 10004}
# These markers are test data, never account credentials.
TOKENS = {'obsidian_auth_token': 'synthetic-obsidian-token-ONLY-FOR-TESTS',
          'vault_password': 'synthetic-vault-password-ONLY-FOR-TESTS',
          'git_auth_token': 'synthetic-git-token-ONLY-FOR-TESTS',
          'remarkable_pairing_code': 'synthetic-pairing-code-ONLY-FOR-TESTS',
          'ha_ocr_token': 'synthetic-ocr-token-ONLY-FOR-TESTS',
          'mcp_auth_token': 'synthetic-mcp-bearer-token-ONLY-FOR-TESTS-0123456789'}
INHERITED = 'synthetic-supervisor-token-MUST-NOT-REACH-WORKERS'
VAULT = 'Synthetic vault ; $(literal shell text)'
OBSIDIAN_NOTE = b'# Synthetic Obsidian\n\ncontainer-fixture-obsidian\n'
GIT_NOTE = b'# Synthetic Git\n\ncontainer-fixture-git\n'


def write_json(path: Path, value, mode=0o644):
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
    temporary.chmod(mode)
    temporary.replace(path)


def control():
    return json.loads((ROOT / 'control.json').read_text())


def prepare(destination: Path, *, enabled: bool, invalid: bool = False):
    """Create fresh options, ephemeral TLS material and deterministic Git data."""
    destination.mkdir(parents=True)
    tls = destination / 'tls'
    tls.mkdir()
    config = tls / 'leaf.ext'
    config.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                      'extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n')
    commands = [
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2',
         '-subj', '/CN=Disposable container integration CA', '-keyout', str(tls/'ca.key'),
         '-out', str(tls/'ca.pem'), '-addext', 'basicConstraints=critical,CA:TRUE',
         '-addext', 'keyUsage=critical,keyCertSign,cRLSign'],
        ['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256',
         '-subj', '/CN=localhost', '-keyout', str(tls/'key.pem'), '-out', str(tls/'leaf.csr')],
        ['openssl', 'x509', '-req', '-sha256', '-days', '2', '-in', str(tls/'leaf.csr'),
         '-CA', str(tls/'ca.pem'), '-CAkey', str(tls/'ca.key'), '-set_serial', '1',
         '-extfile', str(config), '-out', str(tls/'cert.pem')],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError(f'Synthetic certificate generation failed: {result.stderr[-1000:]}')
    # Only the leaf and its public CA enter the container. The test server runs
    # as root, separate from the application workers; the key stays root-only.
    for filename in ('ca.key', 'leaf.csr', 'leaf.ext'):
        (tls/filename).unlink()
    (tls/'key.pem').chmod(0o600)
    options = dict(TOKENS, vault_name=VAULT, obsidian_sync_interval=30,
                   git_sync_interval=30, remarkable_sync_interval=30,
                   enable_git_sync=enabled, enable_remarkable=enabled, enable_mcp=enabled,
                   git_repos='Local|https://127.0.0.1:9443/notes.git|main',
                   mcp_tls_cert=(tls/'cert.pem').read_text(), mcp_tls_key=(tls/'key.pem').read_text(),
                   mcp_allowed_hosts='127.0.0.1:8420', mcp_allowed_origins='', ha_ocr_url='')
    if invalid:
        options['obsolete_test_option'] = True
    write_json(destination/'options.json', options, 0o600)
    write_json(destination/'control.json', {'obsidian': 'hold' if enabled else 'ok',
                                          'remarkable': 'ok', 'git': 'ok'})
    for filename in ('fixtures.py', 'probe.py'):
        shutil.copyfile(Path(__file__).with_name(filename), destination/filename)
    binaries = destination/'bin'
    binaries.mkdir()
    for filename in ('ob', 'rmapi'):
        shutil.copyfile(__file__, binaries/filename)
        (binaries/filename).chmod(0o755)
    commit = make_repository(destination/'repositories'/'notes.git')
    write_json(destination/'expected.json', {'git_commit': commit})
    return {'git_commit': commit, 'ca_sha256': hashlib.sha256((tls/'ca.pem').read_bytes()).hexdigest()}


def make_repository(repository: Path):
    """Write a tiny deterministic bare Git repository using ordinary Git objects."""
    (repository/'objects').mkdir(parents=True)
    (repository/'refs'/'heads').mkdir(parents=True)
    (repository/'HEAD').write_text('ref: refs/heads/main\n')
    (repository/'config').write_text('[core]\n\trepositoryformatversion = 0\n\tbare = true\n')
    def obj(kind, payload):
        raw = kind.encode() + b' ' + str(len(payload)).encode() + b'\0' + payload
        digest = hashlib.sha1(raw).hexdigest()
        folder = repository/'objects'/digest[:2]
        folder.mkdir(exist_ok=True)
        (folder/digest[2:]).write_bytes(zlib.compress(raw))
        return digest
    blob = obj('blob', GIT_NOTE)
    tree = obj('tree', b'100644 guide.md\0' + bytes.fromhex(blob))
    commit = obj('commit', (f'tree {tree}\nauthor Synthetic Fixture <fixture@example.invalid> 1700000000 +0000\n'
                            'committer Synthetic Fixture <fixture@example.invalid> 1700000000 +0000\n\n'
                            'Deterministic synthetic container fixture\n').encode())
    (repository/'refs'/'heads'/'main').write_text(commit+'\n')
    return commit


def event(service, command, **extra):
    path = Path('/data')/service/'fixture-events.jsonl'
    value = {'command': command, 'pid': os.getpid(), 'uid': os.getuid(),
             'environment_keys': sorted(os.environ), **extra}
    with path.open('a') as stream:
        stream.write(json.dumps(value, sort_keys=True)+'\n')


def hold(service):
    home = Path('/data')/service
    child = None
    if service == 'obsidian':
        child = subprocess.Popen([sys.executable, str(ROOT/'fixtures.py'), 'heartbeat', str(home/'fixture-heartbeat')])
    write_json(home/'fixture-active.json', {'pid': os.getpid(), 'child_pid': child.pid if child else None}, 0o600)
    deadline = time.monotonic()+180
    try:
        while control()[service] == 'hold':
            if time.monotonic() >= deadline:
                raise RuntimeError('Cloud fixture hold exceeded its test deadline')
            time.sleep(.1)
    finally:
        if child is not None:
            child.terminate()
            child.wait(timeout=5)


def cloud_cli(name):
    args = sys.argv[1:]
    if name == 'ob':
        if os.environ.get('OBSIDIAN_AUTH_TOKEN') != TOKENS['obsidian_auth_token']:
            raise RuntimeError('Obsidian fixture did not receive its synthetic token')
        home = Path(os.environ['HOME'])
        vault = home/'vault'
        state_path = home/'fixture-ob-state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        command = args[0] if args else ''
        event('obsidian', args)
        if args == ['sync-list-local', '--json']:
            print(json.dumps({'vaults': [] if state is None else [state]}))
        elif args == ['sync-setup', '--vault', VAULT, '--path', str(vault)]:
            if sys.stdin.read() != TOKENS['vault_password']+'\n':
                raise RuntimeError('Vault password was not passed literally through stdin')
            write_json(state_path, {'vaultId': 'synthetic-vault-id', 'vaultName': VAULT,
                                   'vaultPath': str(vault), 'syncMode': 'bidirectional'}, 0o600)
            event('obsidian', ['password-stdin-verified'])
        elif args == ['sync-status', '--path', str(vault), '--json']:
            if state is None:
                raise RuntimeError('Obsidian fixture has not been set up')
            print(json.dumps(state))
        elif args == ['sync-config', '--path', str(vault), '--mode', 'pull-only', '--json']:
            state['syncMode'] = 'pull-only'
            write_json(state_path, state, 0o600)
            print(json.dumps(state))
        elif args == ['sync', '--path', str(vault)]:
            if state is None or state['syncMode'] != 'pull-only':
                raise RuntimeError('Acquisition attempted without verified pull-only configuration')
            if control()['obsidian'] == 'hold':
                hold('obsidian')
            if control()['obsidian'] == 'error':
                sys.exit(19)
            note = vault/'Fixture note.md'
            if not note.exists() or note.read_bytes() != OBSIDIAN_NOTE:
                note.write_bytes(OBSIDIAN_NOTE)
        else:
            raise RuntimeError(f'Unexpected Obsidian fixture invocation: {command}')
    else:
        event('remarkable', args)
        credentials = Path(os.environ['RMAPI_CONFIG'])
        if args == ['ls']:
            supplied = sys.stdin.read()
            if not credentials.exists():
                if supplied != TOKENS['remarkable_pairing_code']+'\n':
                    raise RuntimeError('Pairing code was not passed through stdin')
                credentials.write_text('devicetoken: synthetic-device-token-ONLY-FOR-TESTS\n')
                credentials.chmod(0o600)
                event('remarkable', ['pairing-stdin-verified'])
            elif supplied:
                raise RuntimeError('Existing credentials were unexpectedly paired again')
        elif args == ['-ni', 'ls']:
            if not credentials.exists():
                raise RuntimeError('Converter authentication started without credentials')
        elif len(args) == 6 and args[:4] == ['-ni', 'mget', '-i', '-o'] and args[5] == '/':
            if 'HA_OCR_TOKEN' in os.environ:
                raise RuntimeError('Downloader inherited the converter OCR token')
            if control()['remarkable'] == 'hold':
                hold('remarkable')
            if control()['remarkable'] == 'error':
                sys.exit(23)
            mirror = Path(args[4])
            if mirror != Path('/data/remarkable/private/mirror'):
                raise RuntimeError('Unexpected downloader destination')
            mirror.mkdir(parents=True, exist_ok=True)
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_STORED) as bundle:
                for filename, payload in {'doc.content': '{"fileType":"notebook","pageCount":1}',
                                          'doc.metadata': '{"visibleName":"Synthetic Notebook"}'}.items():
                    entry = zipfile.ZipInfo(filename, (2024, 1, 1, 0, 0, 0))
                    entry.external_attr = 0o600 << 16
                    bundle.writestr(entry, payload)
            target = mirror/'Notebook.rmdoc'
            if not target.exists() or target.read_bytes() != stream.getvalue():
                target.write_bytes(stream.getvalue())
        else:
            raise RuntimeError('Unexpected reMarkable fixture invocation')


class GitHandler(http.server.BaseHTTPRequestHandler):
    """Small, bounded smart-HTTP CGI bridge; repository bytes are synthetic."""
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        target = urllib.parse.urlsplit(self.path)
        if target.path not in ('/notes.git/info/refs', '/notes.git/git-upload-pack'):
            self.send_error(404)
            return
        if self.headers.get('Authorization'):
            write_json(ROOT/'git-credential-leak.json', {'authorization_header_received': True})
            self.send_error(403)
            return
        if control()['git'] == 'error':
            self.send_error(503)
            return
        length = int(self.headers.get('Content-Length', '0'))
        if length > 65536:
            self.send_error(413)
            return
        env = {'PATH': '/usr/bin:/bin', 'GIT_PROJECT_ROOT': str(ROOT/'repositories'),
               'GIT_HTTP_EXPORT_ALL': '1', 'REQUEST_METHOD': self.command,
               'PATH_INFO': target.path, 'QUERY_STRING': target.query,
               'CONTENT_TYPE': self.headers.get('Content-Type', ''), 'CONTENT_LENGTH': str(length),
               'SERVER_PROTOCOL': 'HTTP/1.1', 'REMOTE_ADDR': '127.0.0.1',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        result = subprocess.run(['/usr/lib/git-core/git-http-backend'], env=env,
                                input=self.rfile.read(length), capture_output=True, timeout=10)
        if result.returncode:
            self.send_error(502)
            return
        head, separator, body = result.stdout.partition(b'\r\n\r\n')
        if not separator:
            raise RuntimeError('Git backend returned malformed CGI headers')
        headers = [line.decode().split(':', 1) for line in head.split(b'\r\n')]
        code = next((int(v.strip().split()[0]) for k, v in headers if k.lower() == 'status'), 200)
        self.send_response(code)
        for key, value in headers:
            if key.lower() != 'status':
                self.send_header(key, value.strip())
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with (ROOT/'git-requests.jsonl').open('a') as stream:
            stream.write(json.dumps({'method': self.command, 'path': target.path,
                                     'status': code, 'authorization': False})+'\n')


def serve_git():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 9443), GitHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT/'tls/cert.pem', ROOT/'tls/key.pem')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    write_json(ROOT/'git-server.json', {'pid': os.getpid(), 'listen': '127.0.0.1:9443'})
    server.serve_forever(poll_interval=.2)


def protected_path(relative):
    """Stable application data compared before and after container shutdown."""
    if any(part.startswith(('.pending-', '.retired-', '.current-')) for part in relative.split('/')):
        return False
    return (relative == 'options.json' or relative.startswith(('published/', 'obsidian/vault/',
            'remarkable/private/originals/', 'remarkable/generated/', 'mcp/tls/')) or
            relative in ('obsidian/source-identity.json', 'remarkable/rmapi.json', 'remarkable/paired.json'))


if __name__ == '__main__':
    executable = Path(sys.argv[0]).name
    if executable in ('ob', 'rmapi'):
        cloud_cli(executable)
    elif sys.argv[1:] == ['serve-git']:
        serve_git()
    elif len(sys.argv) == 3 and sys.argv[1] == 'heartbeat':
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            Path(sys.argv[2]).write_text(str(time.monotonic()))
            time.sleep(.1)
    else:
        raise SystemExit('Use through run.py; this module supplies synthetic fixtures only.')
