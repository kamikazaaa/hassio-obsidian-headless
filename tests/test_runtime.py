"""Local behavior tests: no real accounts or network calls."""
import importlib
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


def _process_running(pid):
    try:
        return (Path('/proc') / str(pid) / 'stat').read_text().split()[2] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        # Exit can race both opening and reading the procfs entry.
        return False


def _mapped_uid(uid):
    try:
        return any(int(a) <= uid < int(a)+int(c) for a,b,c in
                   (line.split() for line in Path('/proc/self/uid_map').read_text().splitlines()))
    except OSError:
        return False


def _can_bind_localhost():
    import socket
    try:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
        return True
    except OSError:
        return False


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = importlib.import_module('hardened.config')
        self.runtime = importlib.import_module('hardened.runtime')
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.opts = {'obsidian_auth_token': 'ob-secret', 'vault_name': 'My vault'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_options_and_disabled_services(self):
        cfg = self.config.validate_options(self.opts)
        self.assertTrue(self.runtime.enabled('obsidian', cfg))
        self.assertFalse(self.runtime.enabled('mcp', cfg))
        self.assertGreaterEqual(cfg['obsidian_sync_interval'], 30)

    def test_unsupported_and_path_options_fail_closed(self):
        for key, value in [('oauth_client_id', 'old'), ('tunnel_mode', 'none'),
                           ('remarkable_output_dir', '../vault'), ('vault_path', '/share'),
                           ('tls_key_path', '/etc/shadow'), ('enable_mcp', 'false'),
                           ('obsidian_sync_interval', True), ('ha_ocr_url', 'http://example.com'),
                           ('vault_password', 'trailing-space ') ]:
            with self.subTest(key=key), self.assertRaises(self.config.ConfigError):
                self.config.validate_options(dict(self.opts, **{key: value}))

    def test_mcp_requires_auth_tls_and_host_policy(self):
        for extra in [{}, {'mcp_auth_token': 'a'*40},
                      {'mcp_auth_token': 'a'*40, 'mcp_tls_cert': 'x', 'mcp_tls_key': 'x'}]:
            with self.assertRaises(self.config.ConfigError):
                self.config.validate_options(dict(self.opts, enable_mcp=True, **extra))

    def test_environment_contains_only_own_secrets(self):
        cfg = self.config.validate_options(dict(self.opts, git_auth_token='git-secret',
            remarkable_pairing_code='pair-secret', ha_ocr_token='ocr-secret', mcp_auth_token='m'*40))
        os.environ['SUPERVISOR_TOKEN'] = 'super-secret'
        expected = {'obsidian': {'ob-secret'}, 'git': set(),
                    'remarkable': {'ocr-secret'}, 'mcp': {'m'*40}}
        secrets = {'ob-secret', 'git-secret', 'pair-secret', 'ocr-secret', 'm'*40, 'super-secret'}
        for service, present in expected.items():
            env = self.runtime.service_environment(service, cfg, self.root)
            found = set(env.values()) & secrets
            self.assertEqual(found, present, service)
            self.assertNotIn('SUPERVISOR_TOKEN', env)

    def test_bootstrap_refuses_symlinked_private_directory(self):
        (self.root / 'outside').mkdir()
        (self.root / 'obsidian').symlink_to(self.root / 'outside', target_is_directory=True)
        with self.assertRaises(self.runtime.RuntimeFailure):
            self.runtime.bootstrap('obsidian', self.root)

    @unittest.skipUnless(os.geteuid() == 0 and _mapped_uid(10004), 'requires root with service UID/GID mappings')
    def test_actual_source_and_reader_privilege_boundaries(self):
        os.chmod(self.root, 0o755)
        for service in ['obsidian', 'git', 'remarkable', 'mcp']:
            self.runtime.bootstrap(service, self.root)
        secret = self.root / 'obsidian' / 'secret'
        secret.write_text('private')
        os.chown(secret, 10001, 10001)
        os.chmod(secret, 0o600)
        public = self.root / 'published' / 'obsidian' / 'note.md'
        public.write_text('publication')
        os.chown(public, 10001, 10004)
        os.chmod(public, 0o640)
        code = """import os, pathlib, sys
from hardened.runtime import drop_privileges
root=pathlib.Path(sys.argv[1]); service=sys.argv[2]
drop_privileges(service)
assert os.geteuid()==int(sys.argv[3]); assert 0 not in os.getgroups()
try: (root/'obsidian'/'secret').read_text()
except PermissionError: pass
else: raise AssertionError('peer credential readable')
if service=='mcp':
 assert (root/'published'/'obsidian'/'note.md').read_text()=='publication'
 try: (root/'published'/'obsidian'/'evil').write_text('x')
 except PermissionError: pass
 else: raise AssertionError('reader could write')
else:
 try: (root/'published'/'obsidian'/'note.md').write_text('x')
 except PermissionError: pass
 else: raise AssertionError('peer could write')
"""
        for service, uid in [('git',10002), ('remarkable',10003), ('mcp',10004)]:
            result = subprocess.run([sys.executable, '-c', code, str(self.root), service, str(uid)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE((self.root/'obsidian').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root/'published'/'obsidian').stat().st_mode), 0o2750)

    def _write_status_as_service(self, service):
        code="""import pathlib,sys
from hardened.runtime import drop_privileges, write_status
service=sys.argv[1]; drop_privileges(service)
write_status(service,pathlib.Path(sys.argv[2]),'acquiring',now=2000)
"""
        result=subprocess.run([sys.executable,'-c',code,service,str(self.root)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        status=self.root/service/'status.json'
        self.assertEqual(json.loads(status.read_text())['state'],'acquiring')
        self.assertEqual((status.stat().st_uid,status.stat().st_gid),(self.runtime.UIDS[service],)*2)
        self.assertEqual(stat.S_IMODE(status.stat().st_mode),0o600)

    @unittest.skipUnless(os.geteuid()==0 and _mapped_uid(10004),'requires real service UID/GID mappings')
    def test_disabled_service_can_be_enabled_without_status_permission_poison(self):
        os.chmod(self.root,0o755)
        cfg=self.config.validate_options(self.opts)
        self.runtime.bootstrap('git',self.root)
        self.assertFalse(self.runtime.enabled('git',cfg))
        self.runtime.write_status('git',self.root,'disabled',now=1000)
        cfg['enable_git_sync']=True
        self.assertTrue(self.runtime.enabled('git',cfg))
        self.runtime.bootstrap('git',self.root)
        self._write_status_as_service('git')

    @unittest.skipUnless(os.geteuid()==0 and _mapped_uid(10004),'requires real service UID/GID mappings')
    def test_invalid_tls_startup_can_recover_with_corrected_pair(self):
        os.chmod(self.root,0o755)
        self.runtime.bootstrap('mcp',self.root)
        with self.assertRaises(self.config.ConfigError):
            self.runtime.provision_tls({'mcp_tls_cert':'bad','mcp_tls_key':'bad'},self.root)
        self.runtime.write_status('mcp',self.root,'error',now=1000,message='invalid TLS pair')
        cert=self.root/'cert.pem'; key=self.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=recovery.test','-keyout',str(key),'-out',str(cert)],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        self.runtime.bootstrap('mcp',self.root)
        self.runtime.provision_tls({'mcp_tls_cert':cert.read_text(),'mcp_tls_key':key.read_text()},self.root)
        self._write_status_as_service('mcp')

    @unittest.skipUnless(os.geteuid()==0 and _mapped_uid(10004),'requires real service UID/GID mappings')
    def test_bootstrap_repairs_legacy_root_owned_status_without_following_links(self):
        os.chmod(self.root,0o755)
        self.runtime.bootstrap('remarkable',self.root)
        path=self.root/'remarkable'/'status.json'
        previous={'schema_version':1,'service':'remarkable','state':'disabled','updated_at':1000,
                  'pid':os.getpid(),'message':''}
        path.write_text(json.dumps(previous)); path.chmod(0o600)
        self.assertEqual(path.stat().st_uid,0)
        self.runtime.bootstrap('remarkable',self.root)
        self._write_status_as_service('remarkable')
        path.unlink()
        outside=self.root/'outside'
        outside.write_text(json.dumps(previous)); outside.chmod(0o600)
        path.symlink_to(outside)
        with self.assertRaises(self.runtime.RuntimeFailure): self.runtime.bootstrap('remarkable',self.root)
        self.assertEqual(outside.stat().st_uid,0)
        path.unlink(); os.link(outside,path)
        with self.assertRaises(self.runtime.RuntimeFailure): self.runtime.bootstrap('remarkable',self.root)
        self.assertEqual(outside.stat().st_uid,0)

    def test_status_distinguishes_disabled_failure_and_stale_success(self):
        cfg = self.config.validate_options(self.opts)
        (self.root/'obsidian').mkdir()
        self.assertFalse(self.runtime.health(cfg, self.root, now=1000)[0])
        (self.root/'obsidian'/'status.json').write_text(json.dumps({'state':'ok','last_success':1000,
            'updated_at':1000,'pid':os.getpid()}))
        ok, statuses = self.runtime.health(cfg, self.root, now=1000)
        self.assertTrue(ok)
        self.assertEqual(statuses['mcp']['state'], 'disabled')
        self.assertFalse(self.runtime.health(cfg, self.root, now=999999)[0])
        (self.root/'obsidian'/'status.json').write_text(json.dumps({'state':'error','last_success':1000,
            'updated_at':1001,'pid':os.getpid()}))
        self.assertFalse(self.runtime.health(cfg, self.root, now=1002)[0])

    def fake_ob(self):
        home = self.root/'obsidian'
        home.mkdir(mode=0o700)
        (home/'tmp').mkdir()
        script = self.root/'fake-ob'
        script.write_text("""#!/usr/bin/python3
import json, os, pathlib, sys
home=pathlib.Path(os.environ['HOME']); args=sys.argv[1:]
with (home/'calls.jsonl').open('a') as stream: stream.write(json.dumps(args)+'\\n')
state=home/'actual.json'; command=args[0]
if command=='sync-list-local': print(json.dumps({'vaults': []})); sys.exit(0)
if command=='sync-setup':
 (home/'setup-stdin').write_text(sys.stdin.read())
 if (home/'fail-setup').exists(): sys.exit(3)
 obj={'vaultId':'remote-123', 'vaultName':args[args.index('--vault')+1],
      'vaultPath':args[args.index('--path')+1], 'syncMode':'bidirectional'}
 state.write_text(json.dumps(obj)); print(json.dumps(obj))
elif command=='sync-status': print(state.read_text())
elif command=='sync-config':
 obj=json.loads(state.read_text()); obj['syncMode']='pull-only'; state.write_text(json.dumps(obj)); print(json.dumps(obj))
elif command=='sync':
 if (home/'fail-sync').exists(): sys.exit(3)
 obj=json.loads(state.read_text()); assert obj['syncMode']=='pull-only'
 (pathlib.Path(obj['vaultPath'])/'note.md').write_text('acquired')
else: sys.exit(2)
""")
        script.chmod(0o755)
        return script, home

    def test_obsidian_special_characters_are_literal_and_publication_follows_sync(self):
        script, home = self.fake_ob()
        selector = 'Vault " ; $(touch INJECTED) `echo nope`'
        password = 'pw";$(touch SECRET_LEAK)'
        cfg = self.config.validate_options(dict(self.opts, vault_name=selector, vault_password=password))
        published = []
        def publish(source_root, publication_root, *, source):
            self.assertEqual((source_root/'note.md').read_text(), 'acquired')
            self.assertEqual(source['vault_id'], 'remote-123')
            self.assertIn('revision', source)
            published.append(source)
            return source
        service = self.runtime.ObsidianService(cfg, self.root, binary=str(script), publisher=publish)
        service.run_once()
        calls = [json.loads(line) for line in (home/'calls.jsonl').read_text().splitlines()]
        setup = next(c for c in calls if c[0]=='sync-setup')
        self.assertEqual(setup[setup.index('--vault')+1], selector)
        self.assertNotIn('--password', setup)
        self.assertNotIn(password, (home/'calls.jsonl').read_text())
        self.assertEqual((home/'setup-stdin').read_text(), password+'\n')
        self.assertTrue((home/'source-identity.json').is_file())
        self.assertEqual(len(published), 1)
        self.assertFalse((Path.cwd()/'INJECTED').exists())
        self.assertFalse((Path.cwd()/'SECRET_LEAK').exists())
        self.assertNotIn('--continuous', [x for call in calls for x in call])

    def test_setup_and_sync_failure_cannot_publish_success(self):
        script, home = self.fake_ob()
        (home/'fail-setup').touch()
        published = []
        service = self.runtime.ObsidianService(self.config.validate_options(self.opts), self.root,
            binary=str(script), publisher=lambda *a, **kw: published.append(kw))
        with self.assertRaises(self.runtime.RuntimeFailure): service.run_once()
        self.assertFalse((home/'source-identity.json').exists())
        self.assertEqual(published, [])
        (home/'fail-setup').unlink()
        (home/'fail-sync').touch()
        with self.assertRaises(self.runtime.RuntimeFailure): service.run_once()
        self.assertEqual(published, [])

    def test_ob_binding_does_not_override_actual_identity_or_config_change(self):
        script, home = self.fake_ob()
        cfg = self.config.validate_options(self.opts)
        service = self.runtime.ObsidianService(cfg, self.root, binary=str(script), publisher=lambda *a, **kw: {})
        service.run_once()
        actual = json.loads((home/'actual.json').read_text())
        actual['vaultId'] = 'different-account-or-vault'
        (home/'actual.json').write_text(json.dumps(actual))
        with self.assertRaises(self.runtime.IdentityFailure): service.run_once()
        calls_before = (home/'calls.jsonl').read_text().count('sync-setup')
        cfg['vault_name'] = 'different-vault'
        with self.assertRaises(self.runtime.IdentityFailure): service.run_once()
        self.assertEqual((home/'calls.jsonl').read_text().count('sync-setup'), calls_before)

    def test_health_refuses_symlinked_service_root(self):
        cfg = self.config.validate_options(self.opts)
        outside = self.root/'outside'
        outside.mkdir()
        (outside/'status.json').write_text(json.dumps({'state':'ok','last_success':1000,'updated_at':1000}))
        (self.root/'obsidian').symlink_to(outside)
        self.assertFalse(self.runtime.health(cfg, self.root, now=1000)[0])

    def test_tls_invalid_pair_does_not_write_credentials(self):
        # Root-owned temporary layout isolates SSL validation from ownership setup.
        (self.root/'mcp').mkdir()
        (self.root/'mcp'/'tls').mkdir(mode=0o700)
        with self.assertRaises(self.config.ConfigError):
            self.runtime.validate_tls_pair('invalid certificate', 'invalid key', self.root)
        self.assertFalse((self.root/'mcp'/'tls'/'key.pem').exists())

    def fake_remarkable(self):
        home = self.root/'remarkable'
        home.mkdir(mode=0o700)
        (home/'tmp').mkdir()
        script = self.root/'fake-rmapi'
        script.write_text("""#!/usr/bin/python3
import json, os, pathlib, sys
home=pathlib.Path(os.environ['HOME']); incoming=sys.stdin.read()
with (home/'pair-calls.jsonl').open('a') as f: f.write(json.dumps({'argv':sys.argv[1:], 'stdin':incoming})+'\\n')
if (home/'fail-auth').exists(): sys.exit(2)
credential=pathlib.Path(os.environ['RMAPI_CONFIG'])
if not credential.exists(): credential.write_text('device_token: private')
""")
        script.chmod(0o755)
        converter = self.root/'fake-converter'
        converter.write_text("""#!/usr/bin/python3
import os, pathlib, sys
assert os.environ['SYNC_ONCE']=='true'
assert os.environ['REMARKABLE_OUTPUT_DIR']=='documents'
home=pathlib.Path(os.environ['HOME'])
if (home/'fail-convert').exists(): sys.exit(3)
folder=pathlib.Path(os.environ['VAULT_PATH'])/'documents'; folder.mkdir(exist_ok=True)
(folder/'note.md').write_text('converted')
""")
        converter.chmod(0o755)
        return script, converter, home

    def test_remarkable_pairs_via_stdin_once_and_publishes_only_on_success(self):
        rmapi, converter, home = self.fake_remarkable()
        cfg = self.config.validate_options(dict(self.opts, enable_remarkable=True, remarkable_pairing_code='abc-code'))
        publications=[]
        def publish(source_root, publication_root, *, source):
            self.assertEqual((source_root/'documents'/'note.md').read_text(), 'converted')
            publications.append(source)
        self.runtime.remarkable_once(cfg,self.root,binary=str(converter),rmapi=str(rmapi),publisher=publish)
        calls=[json.loads(line) for line in (home/'pair-calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[0], {'argv':['ls'], 'stdin':'abc-code\n'})
        self.assertTrue((home/'paired.json').exists())
        self.assertEqual(stat.S_IMODE((home/'rmapi.json').stat().st_mode),0o600)
        self.runtime.remarkable_once(cfg,self.root,binary=str(converter),rmapi=str(rmapi),publisher=publish)
        calls=[json.loads(line) for line in (home/'pair-calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[-1]['stdin'],'')
        (home/'fail-auth').touch()
        with self.assertRaises(self.runtime.RuntimeFailure):
            self.runtime.remarkable_once(cfg,self.root,binary=str(converter),rmapi=str(rmapi),publisher=publish)
        calls=[json.loads(line) for line in (home/'pair-calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls[-1]['stdin'],'')
        self.assertEqual(len(publications),2)
        (home/'fail-auth').unlink()
        (home/'fail-convert').touch()
        with self.assertRaises(self.runtime.RuntimeFailure):
            self.runtime.remarkable_once(cfg,self.root,binary=str(converter),rmapi=str(rmapi),publisher=publish)
        self.assertEqual(len(publications),2)
        (home/'rmapi.json').unlink()
        with self.assertRaises(self.runtime.IdentityFailure):
            self.runtime.remarkable_once(cfg,self.root,binary=str(converter),rmapi=str(rmapi),publisher=publish)

    def test_pairing_rejects_hardlinked_credentials_before_success_marker(self):
        rmapi, converter, home = self.fake_remarkable()
        rmapi.write_text("""#!/usr/bin/python3
import os, pathlib
home=pathlib.Path(os.environ['HOME']); original=home/'original'
original.write_text('private'); os.link(original,os.environ['RMAPI_CONFIG'])
""")
        cfg=self.config.validate_options(dict(self.opts,remarkable_pairing_code='abc'))
        with self.assertRaises(self.runtime.RuntimeFailure):
            self.runtime.remarkable_once(cfg,self.root,binary=str(converter),rmapi=str(rmapi),publisher=lambda *a,**kw: {})
        self.assertFalse((home/'paired.json').exists())

    def test_yaml_options_match_parser_and_no_share_or_credential_ports(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML unavailable')
        manifest=yaml.safe_load(Path('config.yaml').read_text())
        self.assertEqual(set(manifest['options']),set(self.config.DEFAULTS))
        self.assertEqual(set(manifest['schema']),set(self.config.DEFAULTS))
        self.assertEqual(manifest['ports'],{'8420/tcp':None})
        self.assertNotIn('map',manifest)
        self.assertNotIn('image',manifest)
        self.assertEqual(manifest['slug'],'obsidian_headless_hardened')

    def test_mcp_token_rejects_non_bearer_punctuation(self):
        cfg=dict(self.opts,enable_mcp=True,mcp_auth_token='!'*40,
                 mcp_tls_cert='cert',mcp_tls_key='key',mcp_allowed_hosts='notes.example.com:8420')
        with self.assertRaises(self.config.ConfigError): self.config.validate_options(cfg)

    def test_health_rejects_dead_process_status(self):
        cfg=self.config.validate_options(self.opts)
        (self.root/'obsidian').mkdir()
        (self.root/'obsidian'/'status.json').write_text(json.dumps({'state':'ok','last_success':1000,
            'updated_at':1000,'pid':os.getpid()}))
        status=json.loads((self.root/'obsidian'/'status.json').read_text())
        status['pid']=2147483647
        (self.root/'obsidian'/'status.json').write_text(json.dumps(status))
        self.assertFalse(self.runtime.health(cfg,self.root,now=1000)[0])

    def test_tls_validation_accepts_matching_pair_rejects_mismatch(self):
        import shutil
        if not shutil.which('openssl'): self.skipTest('openssl unavailable')
        cert=self.root/'cert.pem'; key=self.root/'key.pem'; other=self.root/'other.pem'
        cmd=['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
             '-subj','/CN=runtime.test','-keyout',str(key),'-out',str(cert)]
        subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        self.runtime.validate_tls_pair(cert.read_text(),key.read_text(),self.root)
        subprocess.run(['openssl','genrsa','-out',str(other),'2048'],
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        with self.assertRaises(self.config.ConfigError):
            self.runtime.validate_tls_pair(cert.read_text(),other.read_text(),self.root)
        self.assertFalse(list(self.root.glob('.tls-*')))

    def test_fresh_worker_exec_has_no_inherited_proc_environment_or_peer_config(self):
        cfg=self.config.validate_options(dict(self.opts,git_auth_token='peer-git-secret'))
        os.environ['SUPERVISOR_TOKEN']='inherited-supervisor-sentinel'
        env=self.runtime.worker_environment('obsidian',cfg,self.root)
        code="""import os, pathlib, json
raw=pathlib.Path('/proc/self/environ').read_bytes()
assert b'inherited-supervisor-sentinel' not in raw
assert b'peer-git-secret' not in raw
cfg=json.loads(os.environ['HARDENED_SERVICE_CONFIG'])
assert cfg['obsidian_auth_token']=='ob-secret'
assert set(cfg)=={'obsidian_auth_token','vault_name','vault_password','obsidian_sync_interval'}
"""
        result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_shutdown_terminates_running_acquisition_child(self):
        pidfile=self.root/'child.pid'
        child_code="import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(pidfile)
        parent_code="""import os, sys
from hardened.runtime import install_shutdown_handler, run_command
install_shutdown_handler()
run_command([sys.executable,'-c',sys.argv[1]],dict(os.environ))
"""
        parent=subprocess.Popen([sys.executable,'-c',parent_code,child_code],
                                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        child_pid=None
        try:
            deadline=time.monotonic()+5
            while not pidfile.exists() and parent.poll() is None and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(pidfile.exists(),'acquisition child did not start: '+(parent.stderr.read() if parent.poll() is not None else ''))
            child_pid=int(pidfile.read_text())
            parent.terminate()
            _,stderr=parent.communicate(timeout=3)
            self.assertNotEqual(parent.returncode,0,stderr)
            self.assertFalse(_process_running(child_pid),
                             'acquisition child remained alive after runtime shutdown')
        finally:
            if parent.poll() is None: parent.kill()
            parent.wait()
            parent.stderr.close()
            if child_pid:
                try: os.kill(child_pid,9)
                except ProcessLookupError: pass

    def test_atomic_write_remains_in_opened_directory_during_symlink_substitution(self):
        for filename in ('key.pem','status.json'):
            with self.subTest(filename=filename):
                case=self.root/filename.replace('.','-'); case.mkdir()
                managed=case/'managed'; managed.mkdir()
                original=case/'original'; outside=case/'outside'; outside.mkdir()
                victim=outside/filename; victim.write_bytes(b'outside preserved')
                original_replace=os.replace
                def substitute_then_replace(src,dst,**kwargs):
                    managed.rename(original)
                    managed.symlink_to(outside,target_is_directory=True)
                    # Same basename lets a path-based replace complete after the
                    # substitution; descriptor-bound replace stays in original.
                    for temporary in original.glob('.runtime-*'):
                        (outside/temporary.name).write_bytes(b'outside source')
                    return original_replace(src,dst,**kwargs)
                with mock.patch('hardened.runtime.os.replace',side_effect=substitute_then_replace):
                    try: self.runtime._atomic_write(managed/filename,b'private fixture')
                    except (OSError,self.runtime.RuntimeFailure): pass
                self.assertEqual(victim.read_bytes(),b'outside preserved')
                self.assertEqual((original/filename).read_bytes(),b'private fixture')

    def test_atomic_write_rejects_symlinked_ancestor(self):
        outside=self.root/'outside'; outside.mkdir(); (outside/'child').mkdir()
        linked=self.root/'linked'; linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(self.runtime.RuntimeFailure):
            self.runtime._atomic_write(linked/'child'/'status.json',b'private fixture')
        self.assertFalse((outside/'child'/'status.json').exists())

    def test_oversized_json_child_is_bounded_and_whole_group_is_stopped(self):
        pidfile=self.root/'oversized-grandchild.pid'
        child="""import os,pathlib,subprocess,sys,time
other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(str(other.pid))
try:
 while True: os.write(1,b'private-output-sentinel'*(65536//23))
except OSError:
 time.sleep(60)
"""
        started=time.monotonic()
        try:
            with self.assertRaises(self.runtime.RuntimeFailure) as raised:
                self.runtime.run_command([sys.executable,'-c',child,str(pidfile)],dict(os.environ),
                                         json_output=True,timeout=2)
            self.assertLess(time.monotonic()-started,3)
            self.assertNotIn('private-output-sentinel',str(raised.exception))
            pid=int(pidfile.read_text())
            deadline=time.monotonic()+2
            while _process_running(pid) and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertFalse(_process_running(pid),
                             'oversized acquisition left its descendant alive')
        finally:
            if pidfile.exists():
                try: os.kill(int(pidfile.read_text()),9)
                except ProcessLookupError: pass

    def test_timeout_stops_acquisition_group_without_leaking_arguments(self):
        pidfile=self.root/'timeout-grandchild.pid'
        child="""import pathlib,subprocess,sys,time
other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(str(other.pid)); time.sleep(60)
"""
        try:
            with self.assertRaises(self.runtime.RuntimeFailure) as raised:
                self.runtime.run_command([sys.executable,'-c',child,str(pidfile),'private-argument-sentinel'],
                    dict(os.environ),json_output=True,timeout=.2)
            self.assertNotIn('private-argument-sentinel',str(raised.exception))
            pid=int(pidfile.read_text()); deadline=time.monotonic()+2
            while _process_running(pid) and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertFalse(_process_running(pid))
        finally:
            if pidfile.exists():
                try: os.kill(int(pidfile.read_text()),9)
                except ProcessLookupError: pass

    @unittest.skipUnless(sys.platform.startswith('linux') and _can_bind_localhost(),
                         'requires Linux and local TCP sockets')
    def test_mcp_reader_stops_after_uncatchable_parent_death(self):
        package=self.root/'module'; (package/'hardened').mkdir(parents=True)
        home=self.root/'mcp'; home.mkdir()
        (package/'hardened'/'__init__.py').write_text('')
        (package/'hardened'/'reader.py').write_text("""import json,os,pathlib,socket,time
sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen()
(pathlib.Path(os.environ['HOME'])/'listener.json').write_text(json.dumps({'pid':os.getpid(),'port':sock.getsockname()[1]}))
time.sleep(60)
""")
        code="""import os,pathlib,sys,time
from hardened.runtime import _start_mcp_process
os.chdir(sys.argv[1]); child=_start_mcp_process({},pathlib.Path(sys.argv[2])); child.wait()
"""
        parent=subprocess.Popen([sys.executable,'-c',code,str(package),str(self.root)],stderr=subprocess.PIPE,text=True)
        state=home/'listener.json'; child_pid=None
        try:
            deadline=time.monotonic()+5
            while not state.exists() and parent.poll() is None and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue(state.exists(),'reader fixture did not start: '+(parent.stderr.read() if parent.poll() is not None else ''))
            data=json.loads(state.read_text()); child_pid=data['pid']
            parent.kill(); parent.wait(timeout=3)
            import socket
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if not _process_running(child_pid): break
                time.sleep(.02)
            with self.assertRaises(OSError): socket.create_connection(('127.0.0.1',data['port']),timeout=.2)
        finally:
            if parent.poll() is None: parent.kill()
            parent.wait(); parent.stderr.close()
            if child_pid:
                try: os.kill(child_pid,9)
                except ProcessLookupError: pass

    def test_options_file_rejects_symlink_and_duplicate_keys(self):
        target = self.root/'target'
        target.write_text(json.dumps(self.opts))
        options = self.root/'options.json'
        options.symlink_to(target)
        with self.assertRaises(self.config.ConfigError):
            self.config.load_options(options)
        options.unlink()
        options.write_text('{"vault_name":"a","vault_name":"b"}')
        with self.assertRaises(self.config.ConfigError):
            self.config.load_options(options)


if __name__ == '__main__':
    unittest.main()
