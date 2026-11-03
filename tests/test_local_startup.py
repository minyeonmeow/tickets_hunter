"""First-run Windows startup with no pre-existing local virtual environment."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


@unittest.skipUnless(sys.platform == 'win32', 'Windows launcher')
class LocalStartupTests(unittest.TestCase):
    def prepare(self, root):
        (root / 'build_scripts').mkdir()
        (root / 'src').mkdir()
        shutil.copy2(ROOT / 'build_scripts/start_settings.ps1', root / 'build_scripts/start_settings.ps1')
        (root / 'src/settings.py').write_text('raise RuntimeError("CheckOnly must not launch the app")', encoding='utf8')
        (root / 'requirement.txt').write_text('# Empty offline test dependency list\n', encoding='utf8')

    def run_launcher(self, root, env):
        return subprocess.run([str(POWERSHELL), '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(root / 'build_scripts/start_settings.ps1'), '-CheckOnly'],
                              env=env, capture_output=True, encoding='utf8', errors='replace', timeout=180)

    def test_fresh_clone_with_spaces_and_unicode_path(self):
        with tempfile.TemporaryDirectory(prefix='啟動 測試 ') as folder:
            root = Path(folder)
            self.prepare(root)
            env = dict(os.environ, PYTHONHOME='C:/missing-python', PYTHONPATH='C:/missing-modules',
                       PIP_DISABLE_PIP_VERSION_CHECK='1')
            first = self.run_launcher(root, env)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            marker = root / '.venv/.tickets-hunter-requirements.sha256'
            self.assertTrue(marker.is_file())
            before = marker.stat().st_mtime_ns
            second = self.run_launcher(root, env)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(before, marker.stat().st_mtime_ns)

    def test_missing_python_reports_actionable_error(self):
        with tempfile.TemporaryDirectory(prefix='startup-no-python-') as folder:
            root = Path(folder)
            self.prepare(root)
            env = dict(os.environ, PATH=str(POWERSHELL.parents[2]), PYTHONHOME='C:/missing-python')
            result = self.run_launcher(root, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Python 3.10', result.stdout)
            self.assertIn('3.11', result.stdout)
            self.assertFalse((root / '.venv').exists())


if __name__ == '__main__':
    unittest.main()
