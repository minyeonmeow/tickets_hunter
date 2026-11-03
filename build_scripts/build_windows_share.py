"""Build a fresh, tested Windows folder and shareable ZIP without personal data."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPTS = ROOT / 'build_scripts'
EXES = ('nodriver_tixcraft', 'settings')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def clean_environment():
    env = dict(os.environ)
    for key in ('PYTHONHOME', 'PYTHONPATH'):
        env.pop(key, None)
    env['PYTHONUTF8'] = '1'
    return env


def run(command, **kwargs):
    env = kwargs.pop('env', None) or clean_environment()
    return subprocess.run(command, cwd=ROOT, env=env, check=True, **kwargs)


def default_config():
    # Import the factory, never the user's src/settings.json or profiles.
    sys.path.insert(0, str(ROOT / 'src'))
    import settings
    return settings.get_default_config()


def license_text():
    path = ROOT / 'LICENSE'
    if path.is_file():
        return path.read_text(encoding='utf8')
    # A removed working-copy LICENSE need not be restored to include the license.
    return run(['git', 'show', 'HEAD:LICENSE'], capture_output=True,
               text=True, encoding='utf8').stdout


def write_config(stage, config):
    (stage / 'settings.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf8')


def assemble(binary_root, stage):
    stage.mkdir(parents=True, exist_ok=False)
    for name in EXES:
        bundle = binary_root / name
        shutil.copy2(bundle / (name + '.exe'), stage)
        shutil.copytree(bundle / '_internal', stage / '_internal', dirs_exist_ok=True)
    for name in ('assets', 'www'):
        shutil.copytree(ROOT / 'src' / name, stage / name)
    write_config(stage, default_config())
    (stage / 'LICENSE.txt').write_text(license_text(), encoding='utf8')
    launcher = ('@echo off\r\nsetlocal\r\nset "PYTHONHOME="\r\n'
                'set "PYTHONPATH="\r\nset "PYTHONUTF8=1"\r\n'
                'cd /d "%~dp0"\r\n"%~dp0settings.exe"\r\n'
                'if errorlevel 1 pause\r\nendlocal\r\n')
    (stage / 'Start_Tickets_Hunter.cmd').write_bytes(launcher.encode('ascii'))
    (stage / '使用說明.txt').write_text(
        'Tickets Hunter 本機打包版\n\n'
        '1. 將整個 ZIP 解壓縮到可寫入的資料夾，例如桌面。\n'
        '2. 雙擊 Start_Tickets_Hunter.cmd，或直接執行 settings.exe。\n'
        '3. 瀏覽器會開啟設定介面，填入活動網址與選票條件，再按開始。\n'
        '4. 實例的停止會關閉搶票瀏覽器；暫停可保留畫面手動操作。\n\n'
        '不需安裝 Python，也不需另外安裝程式套件。\n'
        '請保留所有檔案及 _internal 資料夾，不要只複製 EXE，也不要直接在 ZIP 內執行。\n'
        '購票需要網路；建議已安裝 Chrome，程式亦會嘗試自動取得可用瀏覽器。\n'
        '設定頁預設：http://127.0.0.1:16888/settings.html\n'
        '全域自動重整開關位於執行狀態頁，可關閉自動重整並保留票況偵測。\n\n'
        '此分享包使用乾淨預設設定，沒有打包原使用者的帳號、Cookie、設定檔或紀錄。\n'
        '使用後產生的 settings.json、profiles、instances 與瀏覽器資料可能含有個資，\n'
        '重新分享時請使用打包腳本重新產生乾淨版本。\n'
        'Source.zip 包含對應原始碼及打包腳本，授權內容見 LICENSE.txt。\n', encoding='utf-8-sig')


def source_archive(stage):
    """Include corresponding source using an allowlist, never runtime folders."""
    files = list((ROOT / 'src').glob('*.py'))
    files += list((ROOT / 'src/platforms').glob('*.py'))
    for folder in ('src/assets', 'src/www'):
        files += [p for p in (ROOT / folder).rglob('*') if p.is_file()]
    files += [p for p in BUILD_SCRIPTS.rglob('*') if p.is_file() and
              p.suffix.lower() in ('.py', '.spec', '.ps1', '.bat', '.cmd', '.txt', '.md')]
    files += list((ROOT / 'tests').glob('*.py'))
    files += [ROOT / 'build-windows.cmd']
    with zipfile.ZipFile(stage / 'Source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, 'TicketsHunter_Source/' + path.relative_to(ROOT).as_posix())
        archive.writestr('TicketsHunter_Source/LICENSE', license_text())


def smoke(stage, config):
    print('[4/5] Verify EXE imports, both OCR models, settings page and global refresh API', flush=True)
    bot = stage / 'nodriver_tixcraft.exe'
    env = clean_environment()
    env['PATH'] = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')
    for flag, expected in (('--help', '--homepage'), ('--self-test', 'OCR self-test passed')):
        result = run([str(bot), flag], capture_output=True, text=True,
                     encoding='utf8', errors='replace', timeout=180, creationflags=NO_WINDOW, env=env)
        if expected not in result.stdout:
            raise RuntimeError(f'{flag} did not pass: {result.stdout[-2000:]} {result.stderr[-1000:]}')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    test_config = json.loads(json.dumps(config))
    test_config['advanced']['server_port'] = port
    write_config(stage, test_config)
    env['TICKETS_HUNTER_NO_BROWSER'] = '1'
    process = None
    # This port belongs only to our freshly launched test process.
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with open(stage.parent / (stage.name + '-smoke.log'), 'w', encoding='utf8') as log:
            process = subprocess.Popen([str(stage / 'settings.exe')], cwd=stage,
                                       env=env, stdout=log, stderr=log, creationflags=NO_WINDOW)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Frozen settings.exe exited before its HTTP server started')
                try:
                    with opener.open(base + '/auto_refresh', timeout=2) as response:
                        if json.load(response) != {'enabled': True}:
                            raise RuntimeError('Unexpected initial refresh mode in clean package')
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.5)
            else:
                raise RuntimeError('Frozen settings.exe did not start within 120 seconds')
            with opener.open(base + '/settings.html', timeout=5) as response:
                if b'auto_refresh_btn' not in response.read():
                    raise RuntimeError('The packaged settings page is missing the new control')
            for enabled in (False, True):
                request = urllib.request.Request(base + '/auto_refresh',
                    data=json.dumps({'enabled': enabled}).encode(),
                    headers={'Content-Type': 'application/json'})
                with opener.open(request, timeout=5) as response:
                    if json.load(response) != {'enabled': enabled}:
                        raise RuntimeError('Frozen refresh-mode API failed')
            with opener.open(base + '/shutdown', timeout=5):
                pass
            process.wait(timeout=15)
            if process.returncode:
                raise RuntimeError(f'Frozen settings.exe exited with {process.returncode}')
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        write_config(stage, config)
        (stage / 'MAXBOT_AUTO_REFRESH_DISABLED.txt').unlink(missing_ok=True)
    print('EXE startup, OCR, web UI and refresh API checks passed.', flush=True)


def package(stage):
    forbidden = ('profiles', 'instances', 'chrome-win64', 'MAXBOT_AUTO_REFRESH_DISABLED.txt')
    if any((stage / name).exists() for name in forbidden):
        raise RuntimeError('Unexpected runtime data found in share folder')
    archive_path = stage.with_suffix('.zip')
    with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                archive.write(path, stage.name + '/' + path.relative_to(stage).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP integrity check failed')
    hasher = hashlib.sha256()
    with archive_path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    archive_path.with_suffix('.zip.sha256').write_text(digest + '  ' + archive_path.name + '\n', encoding='ascii')
    return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-install', action='store_true', help='Use an already prepared build environment')
    args = parser.parse_args()
    if sys.platform != 'win32' or struct.calcsize('P') != 8 or sys.version_info[:2] not in ((3, 10), (3, 11)):
        parser.error('Build with 64-bit Python 3.10 or 3.11 on Windows')
    print('[1/5] Install pinned build dependencies', flush=True)
    if not args.skip_install:
        run([sys.executable, '-m', 'pip', 'install', '-r', str(BUILD_SCRIPTS / 'requirements-windows.txt')])
    run([sys.executable, '-m', 'pip', 'check'])
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    work = ROOT / 'build' / ('windows-share-' + stamp)
    binaries = work / 'binaries'
    stage = ROOT / 'dist' / ('TicketsHunter_Windows_' + stamp)
    work.mkdir(parents=True, exist_ok=False)
    print('[2/5] Build browser engine and settings EXEs', flush=True)
    for name in EXES:
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
             '--distpath', str(binaries), '--workpath', str(work / 'work'),
             str(BUILD_SCRIPTS / (name + '.spec'))])
    print('[3/5] Assemble a clean share folder', flush=True)
    assemble(binaries, stage)
    smoke(stage, default_config())
    source_archive(stage)
    manifest = {'built_at': stamp, 'python': sys.version.split()[0],
                'packages': {key: importlib.metadata.version(key) for key in
                             ('PyInstaller', 'zendriver', 'tornado', 'ddddocr', 'onnxruntime')},
                'verified': ['bot-help', 'ocr-builtin', 'ocr-universal', 'settings-http', 'refresh-api']}
    (stage / 'build-info.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    print('[5/5] Create and verify share ZIP', flush=True)
    archive = package(stage)
    print(f'\nReady to share: {archive}\nStart: {stage / "Start_Tickets_Hunter.cmd"}', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'Build failed: {exc}', file=sys.stderr)
        sys.exit(1)
