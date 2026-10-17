#!/usr/bin/env python3
"""Run the built image through /init with isolated synthetic integration inputs.

Usage: python3 tests/container/run.py --image TAG --artifacts DIRECTORY
Requires a local Docker daemon and openssl. Nothing is published; no vendor
accounts, host ports, bind mounts or external container networks are used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import uuid

from fixtures import INHERITED, ROOT, SERVICES, prepare, protected_path

REPOSITORY = Path(__file__).resolve().parents[2]
INTERRUPTED = None
LIMITS = [
    'Offline Docker/S6 integration using synthetic documents and synthetic credential markers only.',
    'Only /usr/local/bin/ob and /usr/local/bin/rmapi are replaced in each disposable container.',
    'Cloud adapters do not validate Obsidian/reMarkable vendor authentication, network protocols, or current cloud membership.',
    'The actual installed Git fetches a deterministic repository through its own git-http-backend over trusted loopback HTTPS.',
    'The actual Go converter processes a synthetic notebook metadata bundle; this does not validate OCR, pen strokes, or PDF rendering.',
    'A fresh synthetic CA is added only to the disposable container trust bundle; TLS verification remains enabled.',
    'The loopback Git test server is an additional root test process, separate from the four production service workers.',
    'Orderly container stop must complete before Docker force-kill; existing worker cleanup may intentionally SIGKILL child groups.',
    'Crash-restart tests deliberately SIGKILL one synthetic service worker to exercise the actual 30-second finish backoff.',
    'No Home Assistant Supervisor, real account, ARM emulation, production deployment, restore qualification, or publication is implied.',
]


def need(condition, message):
    if not condition:
        raise AssertionError(message)


def check_interruption():
    if INTERRUPTED is not None:
        raise RuntimeError(f'Integration harness interrupted by signal {INTERRUPTED}')


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2)+'\n')


def execute(args, *, timeout=20, check=True, binary=False):
    result = subprocess.run(args, capture_output=True, text=not binary, timeout=timeout)
    if check and result.returncode:
        output = result.stderr if binary else result.stderr[-4000:]
        raise RuntimeError(f'Command failed ({result.returncode}): {args!r}: {output!r}')
    return result


class Container:
    def __init__(self, suite, name, *, enabled=False, invalid=False):
        self.suite, self.name = suite, name
        self.container = 'obsidian-integration-'+name+'-'+uuid.uuid4().hex[:10]
        self.volume = self.container+'-data'
        self.created = self.volume_created = self.stopped = False
        self.directory = suite.temporary/name
        self.metadata = prepare(self.directory, enabled=enabled, invalid=invalid)
        self.evidence = {'name': name, 'result': 'running', 'started_at': utc(), 'checks': [],
                         'fixture': self.metadata, 'container': self.container, 'volume': self.volume}
        suite.evidence['scenarios'].append(self.evidence)
        suite.save()

    def docker(self, *args, **kwargs):
        return execute(['docker', *args], **kwargs)

    def inspect(self):
        return json.loads(self.docker('inspect', self.container).stdout)[0]

    def record(self, label, value):
        write_json(self.suite.artifacts/f'{self.name}-{label}.json', value)
        self.evidence['checks'].append(label)
        self.suite.save()
        print(f'[{self.name}] {label}', flush=True)
        return value

    def start(self):
        # Track intent before talking to the daemon: a timeout can occur after
        # creation has completed remotely but before the CLI returns to us.
        self.volume_created = True
        self.docker('volume', 'create', '--label', 'obsidian.synthetic-integration=true', self.volume)
        self.created = True
        self.docker('create', '--name', self.container, '--network', 'none',
                    '--mount', f'type=volume,source={self.volume},target=/data',
                    '--env', 'SUPERVISOR_TOKEN='+INHERITED,
                    '--env', 'UNRELATED_TEST_SECRET='+INHERITED, self.suite.image)
        # Copy into a directory, rather than to the ob symlink destination: the
        # archive replaces that directory entry with a regular test executable.
        self.docker('cp', str(self.directory), self.container+':'+str(ROOT))
        self.docker('cp', str(self.directory/'bin')+'/.', self.container+':/usr/local/bin/')
        self.docker('cp', str(self.directory/'options.json'), self.container+':/data/options.json')
        trust = self.directory/'container-trust.pem'
        self.docker('cp', self.container+':/etc/ssl/certs/ca-certificates.crt', str(trust))
        with trust.open('ab') as stream:
            stream.write(b'\n'+(self.directory/'tls/ca.pem').read_bytes())
        self.docker('cp', str(trust), self.container+':/etc/ssl/certs/ca-certificates.crt')
        inspection = self.inspect()
        need(inspection['Config']['Entrypoint'] == ['/init'], 'Container entrypoint is not the image /init')
        need(inspection['Path'] == '/init' and inspection['Args'] == [], 'Container did not retain the actual /init command')
        need(inspection['HostConfig']['NetworkMode'] == 'none', 'Test container has an external network')
        need(not inspection['HostConfig'].get('PortBindings'), 'Test container publishes host ports')
        mounts = inspection['Mounts']
        need(len(mounts) == 1 and mounts[0]['Type'] == 'volume' and mounts[0]['Name'] == self.volume
             and mounts[0]['Destination'] == '/data', f'Unexpected mounts: {mounts}')
        self.record('isolation', {'entrypoint': inspection['Config']['Entrypoint'], 'path': inspection['Path'],
                                 'args': inspection['Args'], 'network': 'none', 'port_bindings': {},
                                 'mounts': [{'type': 'volume', 'name': self.volume, 'destination': '/data'}],
                                 'overrides': ['/usr/local/bin/ob', '/usr/local/bin/rmapi', '/etc/ssl/certs/ca-certificates.crt'],
                                 'image_id': inspection['Image']})
        self.docker('start', self.container)

    def probe(self, label, *args, timeout=105):
        result = self.docker('exec', self.container, '/usr/local/bin/python3', str(ROOT/'probe.py'),
                             *args, timeout=timeout, check=False)
        if result.returncode:
            (self.suite.artifacts/f'{self.name}-{label}-failure.log').write_text(result.stdout+'\n'+result.stderr)
            raise AssertionError(f'{self.name}/{label} probe failed ({result.returncode}): {result.stderr[-5000:]} {result.stdout[-1000:]}')
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise AssertionError(f'{self.name}/{label} did not emit probe JSON: {result.stdout[-2000:]}') from error
        return self.record(label, value)

    def ready(self, label, expected):
        return self.probe(label, 'ready', json.dumps(expected))

    def control(self, **values):
        return self.probe('control-'+str(len(self.evidence['checks'])), 'control', json.dumps(values))

    def verify_runtime(self, label='runtime'):
        state = self.probe(label, 'snapshot')
        need(state['runtime_hashes'] == self.suite.runtime_hashes,
             'Installed Python modules or actual S6 scripts differ from the reviewed checkout')
        need(not {8421, 8422}.intersection(state['listening_tcp_ports']), 'A removed credential web port is listening')
        return state

    def wait_docker_health(self):
        deadline = time.monotonic()+45
        while True:
            state = self.inspect()['State']
            if state.get('Health', {}).get('Status') == 'healthy':
                return self.record('docker-healthcheck', state['Health'])
            need(state['Running'], 'Container exited while waiting for the actual Docker healthcheck')
            need(time.monotonic() < deadline, f'Actual Docker healthcheck did not become healthy: {state}')
            time.sleep(1)

    def stop(self, grace=20):
        started = time.monotonic()
        self.docker('stop', '--signal', 'SIGTERM', '--time', str(grace), self.container, timeout=grace+10)
        elapsed = time.monotonic()-started
        self.stopped = True
        state = self.inspect()['State']
        need(not state['Running'] and not state['OOMKilled'], f'Container did not stop normally: {state}')
        need(state['ExitCode'] != 137 and elapsed < grace,
             f'Container hit the Docker SIGKILL deadline ({elapsed:.3f}s, exit {state["ExitCode"]})')
        # Docker logs splits stdout and stderr; S6 normally uses stderr.
        log_result = self.docker('logs', self.container)
        logs = log_result.stdout+log_result.stderr
        for service in SERVICES.values():
            need(f'service {service} successfully stopped' in logs,
                 f'No completed S6 stop event for {service}; see container log')
        self.record('sigterm-shutdown', {'signal': 'SIGTERM', 'grace_seconds': grace, 'elapsed_seconds': elapsed,
                                         'exit_code': state['ExitCode'], 'oom_killed': state['OOMKilled'],
                                         'docker_force_kill': False, 's6_services_stopped': list(SERVICES.values()),
                                         'child_cleanup_policy': 'Application cleanup may SIGKILL child groups; Docker did not force-kill PID 1.'})

    def stopped_inventory(self):
        need(self.stopped, 'Stopped-volume integrity inspection requires a stopped container')
        payload = self.docker('cp', self.container+':/data', '-', binary=True).stdout
        result = {}
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:*') as archive:
            for member in archive:
                parts = Path(member.name).parts
                relative = '/'.join(parts[1:]) if parts and parts[0] == 'data' else '/'.join(parts)
                if not member.isfile() or not protected_path(relative):
                    continue
                stream = archive.extractfile(member)
                need(stream is not None, f'Cannot read stopped data member: {member.name}')
                result[relative] = {'sha256': hashlib.sha256(stream.read()).hexdigest(), 'size': member.size,
                                    'uid': member.uid, 'gid': member.gid, 'mode': member.mode}
        need('options.json' in result, 'Docker copy did not contain the stopped /data volume')
        return self.record('stopped-data-inventory', result)

    def finish(self, error=None):
        self.evidence['result'] = 'failed' if error else 'passed'
        self.evidence['finished_at'] = utc()
        if error:
            self.evidence['error'] = str(error)
        if self.created:
            for label, args in [('container', ('logs', '--timestamps', self.container)),
                                ('final-inspect', ('inspect', self.container))]:
                try:
                    result = self.docker(*args, check=False, timeout=10)
                    (self.suite.artifacts/f'{self.name}-{label}.log').write_text(result.stdout+'\n'+result.stderr)
                except Exception as caught:
                    self.evidence.setdefault('diagnostic_errors', []).append(str(caught))
            if not self.stopped:
                try:
                    self.docker('stop', '--time', '5', self.container, timeout=10, check=False)
                except Exception as caught:
                    self.evidence.setdefault('cleanup_errors', []).append(str(caught))
            try:
                result = self.docker('rm', '--force', self.container, timeout=10, check=False)
                need(result.returncode == 0 or 'No such container:' in result.stderr,
                     f'Container cleanup failed: {result.stderr}')
            except Exception as caught:
                self.evidence.setdefault('cleanup_errors', []).append(str(caught))
        if self.volume_created:
            try:
                result = self.docker('volume', 'rm', self.volume, timeout=10, check=False)
                need(result.returncode == 0 or 'no such volume' in result.stderr.lower(),
                     f'Volume cleanup failed: {result.stderr}')
            except Exception as caught:
                self.evidence.setdefault('cleanup_errors', []).append(str(caught))
        if INTERRUPTED is not None:
            self.evidence['result'] = 'failed'
            self.evidence['interrupted_signal'] = INTERRUPTED
        if self.evidence.get('cleanup_errors'):
            self.evidence['result'] = 'failed'
            self.suite.save()
            raise RuntimeError(f'Docker resource cleanup failed: {self.evidence["cleanup_errors"]}')
        self.suite.save()


class Suite:
    def __init__(self, args, temporary):
        self.image, self.artifacts, self.temporary = args.image, args.artifacts, temporary
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.evidence = {'schema_version': 1, 'result': 'running', 'started_at': utc(), 'commit': args.commit,
                         'image': args.image, 'expected_arch': args.expected_arch, 'fixture_limits': LIMITS,
                         'scenarios': []}
        self.runtime_hashes = {}
        for name in ('config.py', 'runtime.py', 'reader.py', 'snapshots.py', 'git_sync.py'):
            self.runtime_hashes['/usr/local/lib/python3.12/site-packages/hardened/'+name] = hashlib.sha256((REPOSITORY/'hardened'/name).read_bytes()).hexdigest()
        for service in SERVICES.values():
            for name in ('run', 'finish', 'type', 'timeout-finish'):
                path = Path('etc/s6-overlay/s6-rc.d')/service/name
                self.runtime_hashes['/'+str(path)] = hashlib.sha256((REPOSITORY/'rootfs'/path).read_bytes()).hexdigest()
            membership = Path('etc/s6-overlay/s6-rc.d/user/contents.d')/service
            self.runtime_hashes['/'+str(membership)] = hashlib.sha256((REPOSITORY/'rootfs'/membership).read_bytes()).hexdigest()
        self.save()

    def save(self):
        write_json(self.artifacts/'evidence.json', self.evidence)

    def preflight(self):
        check_interruption()
        need(shutil.which('docker') is not None, 'Docker is required; absence is a failure, never a skipped integration pass')
        need(shutil.which('openssl') is not None, 'openssl is required for fresh synthetic TLS certificates')
        execute(['docker', 'info'], timeout=20)
        image = json.loads(execute(['docker', 'image', 'inspect', self.image]).stdout)[0]
        need(image['Config']['Entrypoint'] == ['/init'], 'Image does not inherit the actual /init entrypoint')
        need(not image['Config'].get('Cmd'), 'Image has an unexpected command after /init')
        if self.evidence['expected_arch']:
            need(image['Architecture'] == self.evidence['expected_arch'],
                 f'Wrong image architecture: {image["Architecture"]}')
        self.evidence['image_metadata'] = {'id': image['Id'], 'architecture': image['Architecture'],
                                           'os': image['Os'], 'entrypoint': image['Config']['Entrypoint']}
        self.save()

    def case(self, name, function, **kwargs):
        check_interruption()
        container = Container(self, name, **kwargs)
        error = None
        try:
            container.start()
            function(container)
        except BaseException as caught:
            error = caught
            raise
        finally:
            container.finish(error)
            # Diagnostics/cleanup keep going on exceptions. A signal delivered
            # there is latched so it cannot be swallowed into a successful case
            # or allow the next scenario to start after an external timeout.
            check_interruption()


def disabled(container):
    container.ready('ready-disabled', {'obsidian': 'ok', 'git': 'disabled', 'remarkable': 'disabled', 'mcp': 'disabled'})
    snapshot = container.verify_runtime()
    need(8420 not in snapshot['listening_tcp_ports'], 'Disabled MCP unexpectedly opened its listener')
    container.probe('healthy-disabled', 'health', '0')
    container.probe('disabled-no-respawn', 'no-respawn', 'git', 'remarkable', 'mcp', timeout=50)
    container.wait_docker_health()
    before = container.probe('before-stop-data', 'inventory')
    container.stop()
    need(container.stopped_inventory() == before, 'Disabled-scenario shutdown changed protected application data')


def enabled(container):
    container.ready('concurrent-start-with-unavailable-dependencies',
                    {'obsidian': 'acquiring', 'git': 'error', 'remarkable': 'ok', 'mcp': 'ok'})
    container.probe('held-first-acquisition', 'active', 'obsidian')
    initial = container.verify_runtime('concurrent-runtime')
    container.probe('unhealthy-before-source-ready', 'health', '1')
    container.probe('tls-reader-rejects-unpublished-source', 'mcp', 'pending')
    container.docker('exec', '--detach', container.container, '/usr/local/bin/python3', str(ROOT/'fixtures.py'), 'serve-git')
    container.control(obsidian='ok')
    recovered = container.ready('all-services-ready', {service: 'ok' for service in SERVICES})
    git_pid = initial['workers']['git']['pid']
    need(initial['statuses']['git'].get('pid') == git_pid and recovered['git']['pid'] == git_pid,
         'An ordinary Git dependency error replaced its worker instead of retrying in the same process')
    elapsed = recovered['git']['last_success']-initial['statuses']['git']['updated_at']
    need(30 <= elapsed < 65, f'Git dependency retry did not use its configured 30-second interval: {elapsed}')
    container.record('natural-dependency-retry', {'service': 'git', 'error_pid': git_pid,
                                                'recovered_pid': recovered['git']['pid'], 'elapsed_seconds': elapsed,
                                                'same_worker': True, 'configured_interval_seconds': 30})
    publications = container.probe('real-publications', 'publications')
    revisions = json.loads(publications['git']['source']['revisions'])
    need(revisions[0]['commit'] == container.metadata['git_commit'], 'Actual Git export did not match the deterministic commit')
    container.probe('process-security-boundaries', 'security')
    container.probe('healthy-enabled', 'health', '0')
    container.wait_docker_health()
    container.probe('term-restart', 'restart', 'obsidian')
    container.probe('crash-restart-backoff', 'restart', 'git', 'crash', timeout=65)
    before = container.probe('last-good-publications', 'publications')
    container.control(obsidian='error', git='error', remarkable='error')
    container.probe('failed-acquisition-cycles', 'cycle', 'error', 'obsidian', 'git', 'remarkable', timeout=40)
    container.probe('unhealthy-after-acquisition-errors', 'health', '1')
    need(container.probe('preserved-after-acquisition-errors', 'publications') == before,
         'A failed acquisition replaced a last-good publication')
    container.probe('tls-mcp-last-good-read-only', 'mcp')
    container.probe('publication-permission-fault', 'publication-fault')
    container.control(obsidian='ok')
    container.probe('failed-publication-cycle', 'cycle', 'error', 'obsidian', timeout=35)
    need(container.probe('preserved-after-publication-error', 'publications') == before,
         'A failed publication replaced the last-good generation')
    container.probe('tls-mcp-during-publication-error', 'mcp')
    container.probe('publication-permission-repair', 'publication-repair')
    container.control(obsidian='ok', git='ok', remarkable='ok')
    container.probe('recovery-cycles', 'cycle', 'ok', 'obsidian', 'git', 'remarkable', timeout=45)
    container.probe('healthy-after-recovery', 'health', '0')
    container.probe('cloud-boundary-and-real-git-events', 'fixture-events')
    container.control(obsidian='hold', remarkable='hold')
    container.probe('active-acquisition-cycles', 'cycle', 'acquiring', 'obsidian', 'remarkable', timeout=35)
    container.probe('obsidian-child-cleanup-with-live-container', 'stop-service', 'obsidian', timeout=25)
    container.probe('remarkable-child-cleanup-with-live-container', 'stop-service', 'remarkable', timeout=25)
    for service in ('obsidian', 'remarkable'):
        container.probe(service+'-reenable', 'service-up', service)
        container.probe(service+'-active-before-stop', 'active', service)
    container.probe('security-after-restarts', 'security')
    before_data = container.probe('before-stop-data', 'inventory')
    container.stop()
    need(container.stopped_inventory() == before_data, 'SIGTERM shutdown changed protected original/publication data')


def invalid_options(container):
    deadline = time.monotonic()+30
    while True:
        logs = container.docker('logs', container.container)
        text = logs.stdout+logs.stderr
        if all(service+': Unsupported managed option' in text for service in SERVICES):
            break
        need(time.monotonic() < deadline, 'S6 did not surface permanent option failures for all four services')
        time.sleep(.25)
    container.verify_runtime()
    container.probe('invalid-options-no-respawn', 'no-respawn', *SERVICES, timeout=50)
    result = container.docker('exec', container.container, '/usr/local/bin/python3', '-m', 'hardened.runtime', 'status', check=False)
    need(result.returncode == 78, f'Invalid configuration health exited {result.returncode}, expected 78')
    container.record('invalid-options-health', {'exit_code': result.returncode, 'message': result.stderr.strip()})
    container.stop()


def backoff_shutdown(container):
    container.ready('ready-before-crash', {'obsidian': 'ok', 'git': 'disabled', 'remarkable': 'disabled', 'mcp': 'disabled'})
    container.probe('crash-entered-finish-backoff', 'crash', 'obsidian')
    before = container.probe('before-stop-data', 'inventory')
    container.stop(grace=45)
    need(container.stopped_inventory() == before, 'Shutdown during finish backoff changed protected data')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--expected-arch', choices=('amd64', 'arm64'))
    parser.add_argument('--commit', default='not-supplied')
    args = parser.parse_args()
    def interrupted(signum, _frame):
        global INTERRUPTED
        INTERRUPTED = signum
        raise RuntimeError(f'Integration harness interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with tempfile.TemporaryDirectory(prefix='obsidian-container-fixtures-') as temporary:
        suite = Suite(args, Path(temporary))
        try:
            suite.preflight()
            suite.case('optional-disabled', disabled)
            suite.case('all-enabled', enabled, enabled=True)
            suite.case('invalid-options', invalid_options, invalid=True)
            suite.case('shutdown-during-backoff', backoff_shutdown)
            check_interruption()
            suite.evidence['result'] = 'passed'
            return 0
        except BaseException as error:
            suite.evidence['result'] = 'failed'
            suite.evidence['error'] = str(error)
            (args.artifacts/'harness-failure.log').write_text(traceback.format_exc())
            print(f'Container integration FAILED: {error}', file=sys.stderr, flush=True)
            return 1
        finally:
            suite.evidence['finished_at'] = utc()
            suite.save()


if __name__ == '__main__':
    raise SystemExit(main())
