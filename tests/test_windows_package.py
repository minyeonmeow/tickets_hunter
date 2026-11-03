"""Sharing must never copy runtime credentials into the release or source ZIP."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('windows_packager', ROOT / 'build_scripts/build_windows_share.py')
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


class WindowsPackageTests(unittest.TestCase):
    def test_share_and_source_archives_exclude_personal_runtime_data(self):
        with tempfile.TemporaryDirectory(prefix='package-privacy-') as directory:
            root = Path(directory)
            (root / 'src/platforms').mkdir(parents=True)
            (root / 'src/assets').mkdir()
            (root / 'src/www').mkdir()
            (root / 'build_scripts').mkdir()
            (root / 'tests').mkdir()
            (root / 'LICENSE').write_text('test license', encoding='utf8')
            (root / 'build-windows.cmd').write_text('build entry', encoding='utf8')
            (root / 'src/settings.json').write_text('{"cookie":"do-not-share"}', encoding='utf8')
            (root / 'src/profiles').mkdir()
            (root / 'src/profiles/private.json').write_text('do-not-share', encoding='utf8')
            (root / 'src/private.har').write_text('do-not-share', encoding='utf8')
            (root / 'src/platforms/funone.py').write_text('# current source', encoding='utf8')
            (root / 'src/www/settings.html').write_text('UI', encoding='utf8')
            binaries = root / 'binaries'
            for name in packager.EXES:
                (binaries / name / '_internal').mkdir(parents=True)
                (binaries / name / (name + '.exe')).write_bytes(b'placeholder')
                (binaries / name / '_internal/runtime.dll').write_bytes(b'runtime')
            stage = root / 'release'
            with patch.object(packager, 'ROOT', root), \
                    patch.object(packager, 'BUILD_SCRIPTS', root / 'build_scripts'), \
                    patch.object(packager, 'default_config', return_value={'accounts': {'cookie': ''}}):
                packager.assemble(binaries, stage)
                packager.source_archive(stage)
                archive_path = packager.package(stage)
            self.assertEqual(json.loads((stage / 'settings.json').read_text()), {'accounts': {'cookie': ''}})
            self.assertFalse((stage / 'profiles').exists())
            with zipfile.ZipFile(stage / 'Source.zip') as archive:
                names = archive.namelist()
                self.assertTrue(any(name.endswith('funone.py') for name in names))
                self.assertFalse(any(name.endswith('.har') or '/profiles/' in name or name.endswith('settings.json') for name in names))
            with zipfile.ZipFile(archive_path) as archive:
                self.assertFalse(any(b'do-not-share' in archive.read(name) for name in archive.namelist()))
            self.assertTrue(archive_path.with_suffix('.zip.sha256').is_file())

    def test_runtime_folders_are_rejected_before_zip_creation(self):
        with tempfile.TemporaryDirectory(prefix='package-safety-') as directory:
            stage = Path(directory) / 'release'
            (stage / 'profiles').mkdir(parents=True)
            with self.assertRaises(RuntimeError):
                packager.package(stage)
            self.assertFalse(stage.with_suffix('.zip').exists())


if __name__ == '__main__':
    unittest.main()
