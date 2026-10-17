"""Component boundaries with real published data, users, and reader leases."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from hardened.reader import Reader
from hardened.runtime import bootstrap
from hardened.snapshots import publish


def mapped(uid):
    return any(int(start) <= uid < int(start) + int(length)
               for start, _, length in (line.split() for line in Path('/proc/self/uid_map').read_text().splitlines()))


class PublicationIntegrationTests(unittest.TestCase):
    def test_real_publisher_output_is_read_and_searched_with_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'note.md').write_text('Sensor calibration procedure', encoding='utf-8')
            (source / 'attachment.pdf').write_bytes(b'%PDF-binary\xff')
            publication = root / 'published' / 'obsidian'
            manifest = publish(source, publication, source={'kind': 'obsidian', 'revision': 'fixture'})
            reader = Reader(root / 'published')
            self.assertEqual(len(reader.list_documents('obsidian')['files']), 2)
            result = reader.read_document('obsidian', 'note.md')
            self.assertEqual(result['content'], 'Sensor calibration procedure')
            self.assertEqual(result['provenance']['generation'], manifest['generation'])
            search = reader.search_documents('obsidian', 'calibration')
            self.assertEqual(search['matches'][0]['path'], 'note.md')

    @unittest.skipUnless(os.geteuid() == 0 and mapped(10004), 'requires real service UID/GID mappings')
    def test_service_publisher_and_mcp_reader_use_actual_group_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            # Model the installed, root-owned package. The checkout itself may
            # live below a private host directory and must not be needed after
            # setuid, including for imports first reached on the second publish.
            installed = root / 'site-packages'
            installed.mkdir(mode=0o755)
            shutil.copytree(Path(__file__).resolve().parents[1] / 'hardened',
                            installed / 'hardened',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            for path in (installed / 'hardened').rglob('*'):
                path.chmod(0o755 if path.is_dir() else 0o644)
            (installed / 'hardened').chmod(0o755)
            for service in ('obsidian', 'mcp'):
                bootstrap(service, root)
            producer = r'''import pathlib, sys
from hardened.runtime import drop_privileges
from hardened.snapshots import publish
root=pathlib.Path(sys.argv[1]); drop_privileges('obsidian')
vault=root/'obsidian'/'vault'; vault.mkdir()
(vault/'note.md').write_text('service-owned note')
(root/'obsidian'/'credential').write_text('fixture-private')
for number in range(6):
 (vault/'revision.md').write_text(str(number))
 publish(vault,root/'published'/'obsidian',source={'kind':'obsidian','revision':str(number)})
'''
            consumer = r'''import pathlib, sys
from hardened.runtime import drop_privileges
from hardened.reader import Reader
root=pathlib.Path(sys.argv[1]); drop_privileges('mcp')
result=Reader(root/'published').read_document('obsidian','note.md')
assert result['content']=='service-owned note'
for action in [lambda: (root/'obsidian'/'credential').read_text(),
               lambda: (root/'published'/'obsidian'/'current.json').write_text('{}')]:
 try: action()
 except PermissionError: pass
 else: raise AssertionError('private read or publication write was permitted')
'''
            for code in (producer, consumer):
                result = subprocess.run([sys.executable, '-c', code, str(root)],
                                        cwd=root, env={**os.environ, 'PYTHONPATH': str(installed)},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            generations = list((root / 'published/obsidian/generations').iterdir())
            self.assertEqual(len(generations), 3)
            for path in (root / 'published/obsidian').rglob('*'):
                self.assertEqual(path.stat().st_uid, 10001)
                self.assertEqual(path.stat().st_gid, 10004)


if __name__ == '__main__':
    unittest.main()
