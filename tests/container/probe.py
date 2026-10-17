#!/usr/local/bin/python3
"""Assertions executed inside the isolated, normally booted test container."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import time

from fixtures import (ROOT, SERVICES, UIDS, TOKENS, INHERITED, OBSIDIAN_NOTE,
                      GIT_NOTE, protected_path, control, write_json)


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def proc(pid):
    try:
        text = Path(f'/proc/{pid}/status').read_text()
        fields = dict(line.split(':', 1) for line in text.splitlines())
        fields = {key: value.strip() for key, value in fields.items()}
        args = Path(f'/proc/{pid}/cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        return {'pid': int(pid), 'args': [arg.decode(errors='replace') for arg in args],
                'status': fields}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(pid):
    item = proc(pid)
    return bool(item and item['status']['State'].split()[0] not in ('Z', 'X'))


def workers():
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        item = proc(entry.name)
        if not item or not alive(item['pid']):
            continue
        args = item['args']
        if len(args) == 5 and args[1:3] == ['-m', 'hardened.runtime'] and args[-1] == '--worker':
            service = args[3]
            need(service not in result, f'Multiple live {service} workers: {result.get(service)} and {item}')
            result[service] = item
    return result


def statuses():
    result = {}
    for service in SERVICES:
        path = Path('/data')/service/'status.json'
        try:
            result[service] = json.loads(path.read_text())
        except FileNotFoundError:
            result[service] = {'state': 'missing'}
    return result


def command(args, timeout=10):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    need(result.returncode == 0, f'Probe command {args!r} failed ({result.returncode}): {result.stderr[-2000:]}')
    return result.stdout.strip()


def s6_binary(name):
    value = shutil.which(name) or '/command/'+name
    need(Path(value).is_file(), f'Real S6 utility is unavailable: {name}')
    return value


def service_dir(service):
    for prefix in ('/run/service', '/run/s6-rc/servicedirs'):
        path = Path(prefix)/SERVICES[service]
        if (path/'supervise').is_dir():
            return str(path)
    raise AssertionError(f'S6 supervise directory missing for {service}')


def s6_status():
    return {service: command([s6_binary('s6-svstat'), service_dir(service)]) for service in SERVICES}


def up(service):
    return command([s6_binary('s6-svstat'), '-o', 'up', service_dir(service)]) == 'true'


def wanted_up(service):
    return command([s6_binary('s6-svstat'), '-o', 'wantedup', service_dir(service)]) == 'true'


def wait(label, predicate, timeout=90, interval=.25):
    deadline = time.monotonic()+timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f'Timed out waiting for {label}; statuses={statuses()}; workers={workers()}')
        time.sleep(interval)


def ready(expected, timeout=90, *, after=None):
    def inspect():
        values = statuses()
        current_workers = workers()
        return values if all(values[s]['state'] == state and
                             (after is None or state != 'ok' or values[s].get('last_success', 0) > after) and
                             (state not in ('ok', 'acquiring') or s in current_workers and
                              values[s].get('pid') == current_workers[s]['pid'])
                             for s, state in expected.items()) else None
    return wait(f'service states {expected}', inspect, timeout)


def health(expected):
    result = subprocess.run(['/usr/local/bin/python3', '-m', 'hardened.runtime', 'status'],
                            capture_output=True, text=True, timeout=10)
    need(result.returncode == expected, f'Health returned {result.returncode}, expected {expected}: {result.stdout} {result.stderr}')
    return {'exit_code': result.returncode, 'statuses': json.loads(result.stdout)}


def inventory():
    result = {}
    for path in Path('/data').rglob('*'):
        relative = str(path.relative_to('/data'))
        if not protected_path(relative) or not path.is_file():
            continue
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f'Unsafe protected data entry: {relative}')
        result[relative] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'size': info.st_size, 'uid': info.st_uid, 'gid': info.st_gid,
                            'mode': stat.S_IMODE(info.st_mode)}
    return result


def publication(service):
    root = Path('/data/published')/service
    current = json.loads((root/'current.json').read_text())
    generation = root/'generations'/current['generation']
    manifest = json.loads((generation/'manifest.json').read_text())
    need(manifest['generation'] == current['generation'], f'{service} generation/manifest mismatch')
    for name, expected in manifest['files'].items():
        target = generation/'files'/name
        info = target.lstat()
        payload = target.read_bytes()
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f'{service}/{name} is not a unique regular file')
        need(info.st_uid == UIDS[service] and info.st_gid == 10004 and stat.S_IMODE(info.st_mode) == 0o640,
             f'Publication ownership/mode incorrect: {service}/{name}')
        need(len(payload) == expected['size'] and hashlib.sha256(payload).hexdigest() == expected['sha256'],
             f'Publication hash/size mismatch: {service}/{name}')
    return {'generation': current['generation'], 'source': manifest['source'], 'files': manifest['files']}


def identity(service, pid):
    """Run under the same real numeric UID as the independently observed worker."""
    env_bytes = Path(f'/proc/{pid}/environ').read_bytes()
    expected = {'obsidian': {'obsidian_auth_token', 'vault_password'}, 'git': {'git_auth_token'},
                'remarkable': {'remarkable_pairing_code', 'ha_ocr_token'}, 'mcp': {'mcp_auth_token'}}[service]
    for name, marker in TOKENS.items():
        need((marker.encode() in env_bytes) == (name in expected), f'{service} credential scope mismatch for {name}')
    need(INHERITED.encode() not in env_bytes, f'{service} inherited the synthetic Supervisor token')
    need(b'-----BEGIN PRIVATE KEY-----' not in env_bytes, f'{service} inherited the TLS private key')
    own = Path('/data')/service/'fixture-private'
    need(own.read_text() == 'synthetic-private-'+service, f'{service} cannot read its own private state')
    denied = []
    for peer in SERVICES:
        if peer == service:
            continue
        target = Path('/data')/peer/'fixture-private'
        try:
            target.read_bytes()
        except PermissionError:
            denied.append(str(target))
        else:
            raise AssertionError(f'{service} read private state belonging to {peer}')
    try:
        Path('/data/options.json').read_bytes()
    except PermissionError:
        denied.append('/data/options.json')
    else:
        raise AssertionError(f'{service} can read root-managed options')
    for producer in ('obsidian', 'git', 'remarkable'):
        root = Path('/data/published')/producer
        current = json.loads((root/'current.json').read_text())
        generation = root/'generations'/current['generation']
        manifest = json.loads((generation/'manifest.json').read_text())
        target = generation/'files'/next(iter(manifest['files']))
        target.read_bytes()
        if producer == service:
            continue
        try:
            fd = os.open(target, os.O_WRONLY)  # No truncation or write, even if the boundary fails.
        except PermissionError:
            denied.append(str(target))
        else:
            os.close(fd)
            raise AssertionError(f'{service} can open {producer} publication for writing')
    return {'pid': pid, 'uid': os.getuid(), 'groups': os.getgroups(),
            'credential_names': sorted(expected), 'denied_paths': denied}


def security():
    current = workers()
    need(set(current) == set(SERVICES), f'Expected four workers, found {list(current)}')
    for service, uid in UIDS.items():
        home = Path('/data')/service
        info = home.stat()
        need(info.st_uid == uid and info.st_gid == uid and stat.S_IMODE(info.st_mode) == 0o700,
             f'{service} private directory ownership/mode is incorrect')
        marker = home/'fixture-private'
        marker.write_text('synthetic-private-'+service)
        os.chown(marker, uid, uid)
        marker.chmod(0o600)
    result = {}
    for service, item in current.items():
        uid = UIDS[service]
        status = item['status']
        need([int(v) for v in status['Uid'].split()] == [uid]*4, f'{service} UID tuple is {status["Uid"]}')
        need([int(v) for v in status['Gid'].split()] == [uid]*4, f'{service} GID tuple is {status["Gid"]}')
        groups = [] if service == 'mcp' else [10004]
        need([int(v) for v in status['Groups'].split()] == groups, f'{service} supplemental groups are {status["Groups"]}')
        need(status['NoNewPrivs'] == '1', f'{service} lacks no_new_privs')
        for key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb'):
            need(int(status[key], 16) == 0, f'{service} retains {key}={status[key]}')
        check = subprocess.run([sys.executable, str(ROOT/'probe.py'), 'identity', service, str(item['pid'])],
                               user=uid, group=uid, extra_groups=groups, capture_output=True, text=True, timeout=10)
        need(check.returncode == 0, f'{service} identity probe failed: {check.stderr} {check.stdout}')
        result[service] = {'pid': item['pid'], 'proc_status': {key: status[key] for key in
                           ('Uid', 'Gid', 'Groups', 'NoNewPrivs', 'CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')},
                           'access': json.loads(check.stdout)}
    readers = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            item = proc(entry.name)
            if item and item['args'][1:] == ['-m', 'hardened.reader'] and alive(item['pid']):
                readers.append(item)
    need(len(readers) == 1, f'Expected one real reader child, found {readers}')
    child = readers[0]
    need(child['status']['Uid'].split() == ['10004']*4 and child['status']['NoNewPrivs'] == '1',
         'Reader child lost its isolated identity')
    result['reader_child'] = {'pid': child['pid'], 'parent_pid': child['status']['PPid']}
    return result


async def mcp(pending=False):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    context = ssl.create_default_context(cafile=str(ROOT/'tls/ca.pem'))
    url = 'https://127.0.0.1:8420'
    auth = {'Authorization': 'Bearer '+TOKENS['mcp_auth_token']}
    # Certificate verification is mandatory; no verify=False or system trust bypass.
    async with httpx.AsyncClient(verify=context, trust_env=False, timeout=10) as client:
        statuses_checked = {}
        for route in ('/', '/health', '/mcp', '/does-not-exist'):
            response = await client.get(url+route)
            need(response.status_code == 401, f'Unauthenticated {route}: {response.status_code}')
            statuses_checked[route] = response.status_code
        response = await client.get(url+'/mcp', headers={'Authorization': 'Bearer wrong-synthetic-token'})
        need(response.status_code == 401, 'Wrong bearer token was accepted')
        response = await client.get(url+'/mcp', headers={**auth, 'Host': 'untrusted.invalid'})
        need(response.status_code == 421, 'Untrusted Host was accepted')
        response = await client.get(url+'/mcp', headers={**auth, 'Origin': 'https://untrusted.invalid'})
        need(response.status_code == 403, 'Untrusted Origin was accepted')
    result = {'tls_verified': True, 'unauthenticated_routes': statuses_checked,
              'wrong_token': 401, 'wrong_host': 421, 'wrong_origin': 403}
    async with httpx.AsyncClient(verify=context, trust_env=False, headers=auth, timeout=10) as client:
        async with streamable_http_client(url+'/mcp', http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                need(initialized.serverInfo.name == 'Read-only published notes', 'Unexpected MCP server')
                tools = (await session.list_tools()).tools
                need({tool.name for tool in tools} == {'list_documents', 'read_document', 'search_documents'},
                     'MCP tool surface differs from the three read tools')
                need(all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools),
                     'MCP tools are not declared read-only')
                result['tools'] = sorted(tool.name for tool in tools)
                if pending:
                    listing = await session.call_tool('list_documents', {'source': 'obsidian'})
                    need(listing.isError, 'Reader served a publication before the producer completed')
                    result['unpublished_source_rejected'] = True
                    return result
                paths = {'obsidian': ('Fixture note.md', OBSIDIAN_NOTE.decode()),
                         'git': ('Local/guide.md', GIT_NOTE.decode()),
                         'remarkable': ('documents/Notebook.md', 'Synthetic Notebook')}
                result['documents'] = {}
                for source, (path, expected) in paths.items():
                    listing = await session.call_tool('list_documents', {'source': source})
                    need(not listing.isError and path in [f['path'] for f in listing.structuredContent['files']],
                         f'MCP listing lacks {source}/{path}')
                    document = await session.call_tool('read_document', {'source': source, 'path': path})
                    need(not document.isError and expected in document.structuredContent['content'],
                         f'MCP read did not return synthetic {source} bytes')
                    search = await session.call_tool('search_documents', {'source': source, 'query': expected.splitlines()[0]})
                    need(not search.isError and search.structuredContent['matches'], f'MCP search failed for {source}')
                    result['documents'][source] = document.structuredContent['provenance']
                for arguments in ({'source': 'obsidian', 'path': '../options.json'},
                                  {'source': 'obsidian', 'path': '/data/options.json'}):
                    need((await session.call_tool('read_document', arguments)).isError, 'MCP path traversal was accepted')
                mutation = await session.call_tool('write_document', {'source': 'obsidian', 'path': 'Fixture note.md', 'content': 'mutated'})
                need(mutation.isError, 'MCP accepted a mutation tool')
                need((await session.list_resources()).resources == [], 'Unexpected MCP resource surface')
    return result


def restart(service, crash=False):
    old = workers()[service]['pid']
    started = time.monotonic()
    if crash:
        os.kill(old, signal.SIGKILL)
    else:
        command([s6_binary('s6-svc'), '-t', service_dir(service)])
    wait(f'{service} previous S6 process to leave', lambda: not alive(old), 10)
    def respawned():
        text = command([s6_binary('s6-svstat'), '-o', 'up,pid', service_dir(service)]).split()
        return int(text[1]) if text[0] == 'true' and int(text[1]) != old else None
    spawned_pid = wait(f'{service} S6 process respawn', respawned, 45 if crash else 15, interval=.1)
    respawn_elapsed = time.monotonic()-started
    if crash:
        need(respawn_elapsed >= 30, f'Unexpected crash respawned before its 30-second finish delay: {respawn_elapsed}')
    def restarted():
        value = workers().get(service)
        status = statuses()[service]
        if value and value['pid'] != old and status['state'] == 'ok' and status.get('pid') == value['pid']:
            return value['pid']
    new = wait(f'{service} restarted worker readiness', restarted, 20)
    elapsed = time.monotonic()-started
    need(not alive(old), f'Old {service} worker survived restart')
    need(new == spawned_pid, 'S6 respawn PID and re-executed worker PID do not match')
    return {'old_pid': old, 'new_pid': new, 'signal': 'SIGKILL' if crash else 'SIGTERM',
            'respawn_elapsed_seconds': respawn_elapsed, 'ready_elapsed_seconds': elapsed}


def cycle(state, services):
    previous = {service: workers()[service]['pid'] for service in services}
    for service in services:
        command([s6_binary('s6-svc'), '-t', service_dir(service)])
    def complete():
        current = workers()
        observed = statuses()
        return observed if all(service in current and current[service]['pid'] != previous[service]
                               and observed[service]['state'] == state
                               and observed[service].get('pid') == current[service]['pid'] for service in services) else None
    values = wait(f'fresh {services} cycles to reach {state}', complete, 30)
    return {'previous_pids': previous, 'statuses': {service: values[service] for service in services}}


def active(service):
    def inspect():
        path = Path('/data')/service/'fixture-active.json'
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        return value if alive(value['pid']) and (not value['child_pid'] or alive(value['child_pid'])) else None
    return wait(f'active synthetic {service} acquisition child', inspect, 45)


def stop_service(service):
    item = active(service)
    need(control()[service] == 'hold', 'Child cleanup requires a still-held acquisition fixture')
    if service == 'obsidian':
        heartbeat = Path('/data/obsidian/fixture-heartbeat')
        first = heartbeat.read_text()
        wait('live acquisition descendant heartbeat to advance', lambda: heartbeat.read_text() != first, 3)
    worker_pid = workers()[service]['pid']
    watched = {worker_pid, item['pid']}
    if item['child_pid']:
        watched.add(item['child_pid'])
    # Include the real Go converter or other intermediate parent, if present.
    parent = int(proc(item['pid'])['status']['PPid'])
    while parent not in (1, worker_pid) and alive(parent):
        watched.add(parent)
        parent = int(proc(parent)['status']['PPid'])
    command([s6_binary('s6-svc'), '-d', service_dir(service)])
    started = time.monotonic()
    wait(f'{service} worker and acquisition descendants to exit while PID 1 remains alive',
         lambda: all(not alive(pid) for pid in watched) and not up(service), 12)
    need(alive(1), 'Container exited before child-cleanup assertion')
    need(service not in workers(), f'S6 respawned disabled {service}')
    return {'watched_pids': sorted(watched), 'all_non_live': True, 'pid1_still_live': True,
            'elapsed_seconds': time.monotonic()-started}


def no_respawn(services, duration=35):
    wait('permanent S6 down requests', lambda: all(not up(service) and not wanted_up(service) for service in services), 10)
    before = {service: statuses()[service] for service in services}
    exits = {}
    for service in services:
        fields = command([s6_binary('s6-svstat'), '-o', 'wantedup,exitcode,signal,pid', service_dir(service)]).split()
        expected = '0' if before[service]['state'] == 'disabled' else '78'
        need(fields == ['false', expected, 'NA', '-1'], f'{service} permanent S6 down result is {fields}, expected exit {expected}')
        exits[service] = {'wantedup': False, 'exitcode': int(expected), 'signal': None, 'pid': -1}
    started = time.monotonic()
    while time.monotonic()-started < duration:
        found = workers()
        for service in services:
            need(service not in found and not up(service) and not wanted_up(service), f'{service} unexpectedly respawned or remains restartable')
            need(statuses()[service] == before[service], f'{service} status changed after permanent stop')
        time.sleep(1)
    return {'services': services, 'duration_seconds': time.monotonic()-started, 'status_unchanged': True,
            's6_down_results': exits, 's6': s6_status()}


def runtime_hashes():
    paths = [Path('/usr/local/lib/python3.12/site-packages/hardened')/name for name in
             ('config.py', 'runtime.py', 'reader.py', 'snapshots.py', 'git_sync.py')]
    paths += [Path('/etc/s6-overlay/s6-rc.d')/service/name for service in SERVICES.values()
              for name in ('run', 'finish', 'type', 'timeout-finish')]
    paths += [Path('/etc/s6-overlay/s6-rc.d/user/contents.d')/service for service in SERVICES.values()]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def listening_ports():
    ports = set()
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A':
                ports.add(int(fields[1].split(':')[1], 16))
    return sorted(ports)


def main(args):
    action = args[0]
    if action == 'ready':
        return ready(json.loads(args[1]), after=float(args[2]) if len(args) > 2 else None)
    if action == 'snapshot':
        return {'statuses': statuses(), 'workers': workers(), 's6': s6_status(),
                'pid1': proc(1), 'runtime_hashes': runtime_hashes(), 'listening_tcp_ports': listening_ports()}
    if action == 'health':
        return health(int(args[1]))
    if action == 'inventory':
        return inventory()
    if action == 'publications':
        return {service: publication(service) for service in ('obsidian', 'git', 'remarkable')}
    if action == 'security':
        return security()
    if action == 'identity':
        return identity(args[1], int(args[2]))
    if action == 'mcp':
        # Compare the selected immutable generations; a scheduled producer may
        # legitimately create a different current generation during a request.
        before = {key: value for key, value in inventory().items() if '/generations/gen-' in key}
        result = asyncio.run(mcp(pending=len(args) > 1 and args[1] == 'pending'))
        after = inventory()
        need(all(after.get(key) == value for key, value in before.items()), 'MCP requests changed an existing generation')
        return result
    if action == 'restart':
        return restart(args[1], len(args) > 2 and args[2] == 'crash')
    if action == 'cycle':
        return cycle(args[1], args[2:])
    if action == 'active':
        return active(args[1])
    if action == 'stop-service':
        return stop_service(args[1])
    if action == 'service-up':
        return command([s6_binary('s6-svc'), '-u', service_dir(args[1])])
    if action == 'no-respawn':
        return no_respawn(args[1:])
    if action == 'crash':
        pid = workers()[args[1]]['pid']
        os.kill(pid, signal.SIGKILL)
        wait('crashed worker to enter finish', lambda: not alive(pid) and not up(args[1]), 10)
        need(wanted_up(args[1]), 'Crash did not leave a pending S6 restart')
        return {'pid': pid, 's6': s6_status(), 'signal': 'SIGKILL'}
    if action == 'publication-fault':
        target = Path('/data/published/obsidian/generations')
        target.chmod(0o2550)
        return {'path': str(target), 'mode': '02550', 'purpose': 'reject publication while retaining group read access'}
    if action == 'publication-repair':
        Path('/data/published/obsidian/generations').chmod(0o2750)
        return {'mode': '02750'}
    if action == 'control':
        value = control() | json.loads(args[1])
        write_json(ROOT/'control.json', value)
        return value
    if action == 'fixture-events':
        result = {}
        for service in ('obsidian', 'remarkable'):
            path = Path('/data')/service/'fixture-events.jsonl'
            result[service] = [json.loads(line) for line in path.read_text().splitlines()]
        need(not (ROOT/'git-credential-leak.json').exists(), 'Git sent credentials to the unrelated local fixture host')
        result['git_requests'] = [json.loads(line) for line in (ROOT/'git-requests.jsonl').read_text().splitlines()]
        need(result['git_requests'] and any(item['method'] == 'POST' for item in result['git_requests']),
             'Real Git smart-HTTP acquisition did not reach the local backend')
        need(any(item['command'] == ['password-stdin-verified'] for item in result['obsidian']),
             'Vault password stdin was not exercised')
        need(any(item['command'] == ['pairing-stdin-verified'] for item in result['remarkable']),
             'reMarkable pairing stdin was not exercised')
        return result
    raise AssertionError(f'Unknown probe command: {action}')


if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1:]), sort_keys=True))
