"""External Chrome lifecycle tests; local HTTP only, no ticket service."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from tornado.testing import AsyncHTTPTestCase, gen_test

from zendriver import cdp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import nodriver_common as common
import settings
import util


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<title>Local checkout</title><input value='keep me'>")

    def log_message(self, *args):
        pass


class BrowserSettingsTests(unittest.TestCase):
    def test_defaults_and_migration(self):
        config = settings.get_default_config()
        self.assertEqual(config["advanced"]["browser_port"], 19327)
        self.assertEqual(config["advanced"]["browser_connection_mode"], "attach")
        del config["advanced"]["browser_port"]
        del config["advanced"]["browser_connection_mode"]
        settings.migrate_config(config)
        conf = common.get_extension_config(config)
        self.assertEqual((conf.host, conf.port), ("127.0.0.1", 19327))
        conf = common.get_extension_config(config, SimpleNamespace(mcp_connect=19431))
        self.assertEqual(conf.port, 19431)
        config["advanced"]["browser_port"] = 99999
        with self.assertRaises(ValueError):
            common.get_extension_config(config)

    def test_exclusive_port_ownership_released_on_close(self):
        port = free_port()
        handle = util.acquire_browser_port_lock(port)
        try:
            with self.assertRaises(ValueError):
                util.acquire_browser_port_lock(port)
        finally:
            handle.close()
        util.acquire_browser_port_lock(port).close()

    def test_launcher_defaults_custom_port_and_conflict(self):
        if os.name != "nt":
            self.skipTest("Windows launcher")
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(ROOT / "build_scripts/start_browser.ps1")]
        result = subprocess.run(command + ["-CheckOnly"], capture_output=True, check=True)
        default = json.loads(result.stdout.decode("utf-8-sig"))
        self.assertEqual(default["port"], 19327)
        result = subprocess.run(command + ["19431", "-CheckOnly"], capture_output=True, check=True)
        custom = json.loads(result.stdout.decode("utf-8-sig"))
        self.assertEqual(custom["port"], 19431)
        self.assertNotEqual(custom["profile"], default["profile"])
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            result = subprocess.run(command + [str(occupied.getsockname()[1])],
                                    capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)


class ExternalChromeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tab_selection_requires_matching_site_and_keeps_original_tab(self):
        first = SimpleNamespace(target=SimpleNamespace(target_id="one", url="https://example.com/checkout"),
                                evaluate=AsyncMock(return_value=False))
        second = SimpleNamespace(target=SimpleNamespace(target_id="two", url="https://example.com/activity"),
                                 evaluate=AsyncMock(return_value=False))
        browser = SimpleNamespace(tabs=[first, second])
        with self.assertRaises(ValueError):
            await common.nodriver_select_existing_tab(browser, "https://example.com")
        with self.assertRaises(ValueError):
            await common.nodriver_select_existing_tab(browser, "https://other.example")
        with self.assertRaises(ValueError):
            await common.nodriver_select_existing_tab(browser, "https://example.com", "closed")
        self.assertIs(await common.nodriver_select_existing_tab(browser, "https://example.com", "one"), first)

    async def test_attach_pause_resume_stop_and_profile_persistence(self):
        chrome = os.environ.get("FUNONE_TEST_CHROME") or "C:/Program Files/Google/Chrome/Application/chrome.exe"
        if not Path(chrome).exists():
            self.skipTest("Chrome not installed")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        page = f"http://127.0.0.1:{server.server_port}/checkout"
        port = free_port()
        instance = "attach_test_" + str(os.getpid())
        state_dir = ROOT / "src/instances" / instance
        state_dir.mkdir(parents=True, exist_ok=True)
        browser = None
        chrome_process = None
        bot = None
        with tempfile.TemporaryDirectory(prefix="ticket browser ") as tmp:
            try:
                def start_chrome():
                    return subprocess.Popen([chrome, "--headless=new", "--no-first-run",
                        "--no-default-browser-check", f"--remote-debugging-port={port}",
                        f"--user-data-dir={tmp}/profile", page],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                chrome_process = start_chrome()
                await self.wait_endpoint(port)
                config = settings.get_default_config()
                config["homepage"] = page
                config["ocr_captcha"]["enable"] = False
                config["advanced"]["browser_port"] = port
                conf = common.get_extension_config(config)
                browser, tab = await common.nodriver_attach_browser(conf, config)
                target_id = tab.target.target_id
                await self.wait_async(lambda: tab.evaluate("location.href === " + json.dumps(page) + " && document.readyState === 'complete'"), True)
                await tab.evaluate("localStorage.setItem('saved', 'yes'); document.cookie='saved=yes; max-age=3600'")
                await common.nodriver_disconnect_browser(browser)
                browser = None
                await self.wait_endpoint(port)

                config_file = Path(tmp) / "test-config.json"
                config_file.write_text(json.dumps(config), encoding="utf-8")
                env = dict(os.environ, PYTHONUTF8="1")
                env.pop("PYTHONHOME", None)
                log_file = open(Path(tmp) / "bot.log", "w", encoding="utf-8")
                try:
                    bot = subprocess.Popen([sys.executable, str(ROOT / "src/nodriver_tixcraft.py"),
                        "--input", str(config_file), "--instance", instance],
                        cwd=ROOT, env=env, stdout=log_file, stderr=log_file)
                    await self.wait_condition(lambda: (state_dir / "heartbeat.txt").exists(), bot)
                    # An independent observer asks CDP whether the bot has attached to the page.
                    observer = common.Browser(conf)
                    await observer.start()
                    try:
                        async def attached():
                            info = await observer.connection.send(cdp.target.get_target_info(target_id))
                            return info.attached
                        await self.wait_async(attached, True)
                        (state_dir / common.CONST_MAXBOT_INT28_FILE).touch()
                        await self.wait_async(attached, False)
                        self.assertIsNone(bot.poll())
                        await self.wait_endpoint(port)
                        (state_dir / common.CONST_MAXBOT_INT28_FILE).unlink()
                        await self.wait_async(attached, True)
                        (state_dir / common.CONST_MAXBOT_INT28_QUIT_FILE).touch()
                        await asyncio.to_thread(bot.wait, 20)
                        self.assertEqual(bot.returncode, 0)
                        self.assertFalse((state_dir / "heartbeat.txt").exists())
                        await self.wait_async(attached, False)
                    finally:
                        await common.nodriver_disconnect_browser(observer)
                finally:
                    if bot.poll() is None:
                        bot.terminate()
                        await asyncio.to_thread(bot.wait, 10)
                    log_file.close()

                browser, tab = await common.nodriver_attach_browser(conf, config, target_id)
                self.assertEqual(await tab.evaluate("location.pathname"), "/checkout")
                self.assertEqual(await tab.evaluate("document.querySelector('input').value"), "keep me")
                self.assertEqual(await tab.evaluate("localStorage.getItem('saved')"), "yes")
                # Close only this test-owned Chrome, then reopen the same profile.
                await browser.connection.send(cdp.browser.close())
                await common.nodriver_disconnect_browser(browser)
                browser = None
                await asyncio.to_thread(chrome_process.wait, 15)
                chrome_process = start_chrome()
                await self.wait_endpoint(port)
                browser, tab = await common.nodriver_attach_browser(conf, config)
                await self.wait_async(lambda: tab.evaluate("document.readyState === 'complete' && location.pathname === '/checkout'"), True)
                self.assertEqual(await tab.evaluate("localStorage.getItem('saved')"), "yes")
                self.assertIn("saved=yes", await tab.evaluate("document.cookie"))
            finally:
                if browser:
                    try:
                        await browser.connection.send(cdp.browser.close())
                    except Exception:
                        pass
                    await common.nodriver_disconnect_browser(browser)
                if chrome_process and chrome_process.poll() is None:
                    chrome_process.terminate()
                    await asyncio.to_thread(chrome_process.wait, 15)
                server.shutdown()
                server.server_close()
                shutil.rmtree(state_dir)

    async def wait_endpoint(self, port):
        def available():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                    return response.status == 200
            except OSError:
                return False
        await self.wait_condition(available)

    async def wait_condition(self, predicate, process=None):
        for _ in range(150):
            if predicate():
                return
            if process and process.poll() is not None:
                self.fail("Bot exited before connection; inspect test log")
            await asyncio.sleep(0.1)
        self.fail("Timed out waiting for browser state")

    async def wait_async(self, predicate, expected):
        for _ in range(150):
            if await predicate() == expected:
                return
            await asyncio.sleep(0.1)
        self.fail(f"Target attachment did not become {expected}")


class BrowserApiTests(AsyncHTTPTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="browser-api-")
        self.root_patch = patch.object(util, "get_app_root", return_value=self.directory.name)
        self.root_patch.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.root_patch.stop()
        self.directory.cleanup()

    def get_app(self):
        return settings.create_application()

    def test_save_and_run_validate_port_without_launching_another_browser(self):
        config = settings.get_default_config()
        config["advanced"]["browser_port"] = 19431
        self.assertEqual(self.fetch('/save', method='POST', body=json.dumps(config)).code, 200)
        loaded = json.loads(self.fetch('/load').body)
        self.assertEqual(loaded["advanced"]["browser_port"], 19431)
        with patch.object(util, "is_browser_debug_port", return_value=False), patch.object(settings, "launch_maxbot") as launch:
            response = self.fetch('/run')
            self.assertEqual(response.code, 409)
            self.assertIn("19431", json.loads(response.body)["error"])
            launch.assert_not_called()
        for invalid in (0, 65536, "19431", True):
            config["advanced"]["browser_port"] = invalid
            self.assertEqual(self.fetch('/save', method='POST', body=json.dumps(config)).code, 401)

    @gen_test(timeout=60)
    async def test_browser_port_fields_load_save_and_reject_invalid_input(self):
        chrome = os.environ.get("FUNONE_TEST_CHROME") or "C:/Program Files/Google/Chrome/Application/chrome.exe"
        if not Path(chrome).exists():
            self.skipTest("Chrome not installed")
        browser = common.Browser(common.Config(headless=True, browser_executable_path=chrome))
        await browser.start()
        try:
            tab = await browser.get(self.get_url('/settings.html'))
            await tab.wait_for('#auto_refresh_btn:not([disabled])', timeout=15)
            self.assertEqual(await tab.evaluate("document.querySelector('#browser_port').value"), "19327")
            self.assertTrue(await tab.evaluate("""(() => {
                document.querySelector('#browser_port').value = '19431';
                document.querySelector('#browser_connection_mode').value = 'launch';
                return save_changes_to_dict(true);
            })()"""))
            self.assertEqual(await tab.evaluate("settings.advanced.browser_port"), 19431)
            self.assertEqual(await tab.evaluate("settings.advanced.browser_connection_mode"), "launch")
            await tab.evaluate("maxbot_save_api()")
            for _ in range(50):
                saved = Path(self.directory.name) / "settings.json"
                if saved.exists():
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["advanced"]["browser_port"], 19431)
            await tab.reload()
            await tab.wait_for('#auto_refresh_btn:not([disabled])', timeout=15)
            self.assertEqual(await tab.evaluate("document.querySelector('#browser_port').value"), "19431")
            self.assertFalse(await tab.evaluate("document.querySelector('#browser_port').value = '0'; save_changes_to_dict(true)"))
        finally:
            process = browser._process
            await browser.stop()
            await common.nodriver_disconnect_browser(browser)
            if process:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        stream.close()
