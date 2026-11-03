"""Global manual-refresh mode: persistence, HTTP contract and browser behavior."""
import ast
import importlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tornado.testing import AsyncHTTPTestCase, gen_test
import zendriver

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import settings
import util
import nodriver_common as common
from platforms import funone


class RefreshControlTests(AsyncHTTPTestCase):
    def setUp(self):
        # Only files inside this newly created test directory are touched.
        self.directory = tempfile.TemporaryDirectory(prefix="refresh-control-")
        self.root_patch = patch.object(util, "get_app_root", return_value=self.directory.name)
        self.root_patch.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.root_patch.stop()
        self.directory.cleanup()

    def get_app(self):
        return settings.create_application()

    def test_api_persists_and_validates_mode(self):
        self.assertEqual(json.loads(self.fetch('/auto_refresh').body), {"enabled": True})
        result = self.fetch('/auto_refresh', method='POST', body='{"enabled":false}')
        self.assertEqual(result.code, 200)
        self.assertFalse(json.loads(result.body)["enabled"])
        self.assertFalse(util.is_auto_refresh_enabled())
        self.assertTrue((Path(self.directory.name) / util.CONST_AUTO_REFRESH_DISABLED_FILE).exists())
        for body in ('{}', '[]', 'null', '{"enabled":"false"}', '{"enabled":0}', 'bad json'):
            with self.subTest(body=body):
                self.assertEqual(self.fetch('/auto_refresh', method='POST', body=body).code, 400)
                self.assertFalse(util.is_auto_refresh_enabled())
        self.fetch('/auto_refresh', method='POST', body='{"enabled":true}')
        self.assertTrue(util.is_auto_refresh_enabled())

    @gen_test
    async def test_shared_guards_block_only_bot_refreshes(self):
        tab = AsyncMock()
        util.set_auto_refresh_enabled(False)
        self.assertFalse(await common.reload_page_if_enabled(tab))
        self.assertFalse(await common.evaluate_refresh_if_enabled(tab, 'location.reload()'))
        self.assertFalse(await common.back_if_auto_refresh_enabled(tab))
        button = AsyncMock()
        self.assertFalse(await common.click_refresh_if_enabled(button))
        self.assertFalse(await common.sleep_before_auto_refresh(60))
        tab.reload.assert_not_awaited()
        tab.evaluate.assert_not_awaited()
        tab.back.assert_not_awaited()
        button.click.assert_not_awaited()
        with patch.object(common, 'nodriver_current_url_safe', AsyncMock(return_value='https://example.test/tickets')):
            await common.navigate_page_if_enabled(tab, 'https://example.test/tickets')
            tab.get.assert_not_awaited()
            await common.navigate_page_if_enabled(tab, 'https://example.test/checkout')
            tab.get.assert_awaited_once_with('https://example.test/checkout')
        util.set_auto_refresh_enabled(True)
        self.assertTrue(await common.reload_page_if_enabled(tab))
        tab.reload.assert_awaited_once()

    @gen_test
    async def test_special_refresh_paths_stop_without_browser_actions(self):
        names = {
            'funone': ['nodriver_funone_click_refresh_button', 'nodriver_funone_auto_reload',
                       'nodriver_funone_error_handler', '_funone_refresh_stock'],
            'ticketplus': ['_ticketplus_click_refresh_button'],
            'cityline': ['nodriver_cityline_auto_retry_access'],
            'kham': ['nodriver_kham_handle_seat_unavailable'],
            'ibon': ['nodriver_ibon_navigate_on_sold_out'],
            'hkticketing': ['nodriver_hkticketing_type02_check_traffic_overload',
                           'nodriver_hkticketing_content_refresh',
                           'nodriver_hkticketing_url_redirect', 'nodriver_hkticketing_travel_iframe'],
        }
        util.set_auto_refresh_enabled(False)
        for module, functions in names.items():
            for name in functions:
                with self.subTest(platform=module, function=name):
                    tab = AsyncMock()
                    fn = getattr(importlib.import_module('platforms.' + module), name)
                    values = {'tab': tab, 'config_dict': settings.get_default_config(),
                              'url': 'https://example.test/tickets', 'error': TimeoutError(),
                              'debug': SimpleNamespace(log=lambda *args: None)}
                    args = {key: values[key] for key in inspect.signature(fn).parameters}
                    self.assertFalse(await fn(**args))
                    tab.evaluate.assert_not_awaited()
                    tab.reload.assert_not_awaited()
                    tab.get.assert_not_awaited()
                    tab.query_selector.assert_not_awaited()

    def test_all_platforms_use_shared_reload_and_navigation_guards(self):
        for path in (ROOT / 'src/platforms').glob('*.py'):
            with self.subTest(platform=path.name):
                tree = ast.parse(path.read_text(encoding='utf8'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        fn = node.func
                        if isinstance(fn.value, ast.Name) and fn.value.id == 'tab':
                            self.assertNotIn(fn.attr, ('reload', 'get', 'back'),
                                             f'{path.name}:{node.lineno}: unguarded {fn.attr}')

    @gen_test(timeout=90)
    async def test_settings_button_and_live_detection_in_real_chrome(self):
        chrome = os.environ.get('FUNONE_TEST_CHROME') or str(Path(
            os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe')
        if not Path(chrome).exists():
            self.skipTest('Chrome is not installed')
        browser = await zendriver.start(headless=True, browser_executable_path=chrome)
        tab = None
        try:
            tab = await browser.get(self.get_url('/settings.html'))
            await tab.wait_for('#auto_refresh_btn:not([disabled])', timeout=10)
            self.assertTrue(util.is_auto_refresh_enabled())
            await tab.evaluate("document.querySelector('#auto_refresh_btn').click()")
            await tab.wait_for('#auto_refresh_btn[aria-pressed="false"]:not([disabled])', timeout=10)
            self.assertFalse(util.is_auto_refresh_enabled())
            self.assertIn(await tab.evaluate("document.querySelector('#auto_refresh_status').textContent"),
                          ('手動重整模式，持續偵測中', 'Manual refresh; detection continues'))
            await tab.reload()  # Manual browser refresh remains possible.
            await tab.wait_for('#auto_refresh_btn[aria-pressed="false"]:not([disabled])', timeout=10)
            await tab.evaluate("document.querySelector('#auto_refresh_btn').click()")
            await tab.wait_for('#auto_refresh_btn[aria-pressed="true"]:not([disabled])', timeout=10)
            self.assertTrue(util.is_auto_refresh_enabled())
            # Detection/selection still operates with the global mode off.
            util.set_auto_refresh_enabled(False)
            funone._state.clear()
            await tab.evaluate('document.body.innerHTML = ' + json.dumps('''
                <div>VIP TWD 1000<div><button>−</button><input value="2">
                <button disabled>+</button></div></div>'''))
            config = settings.get_default_config()
            config['keyword_exclude'] = ''
            config['area_auto_select']['area_keyword'] = '"VIP"'
            self.assertTrue(await funone.nodriver_funone_assign_ticket_number(tab, config))
        finally:
            process = browser._process
            tasks = []
            for connection in (browser.connection, tab):
                listener = getattr(connection, 'listener', None)
                if listener and listener.task:
                    tasks.append(listener.task)
            await browser.stop()
            for task in tasks:
                task.cancel()
            import asyncio
            await asyncio.gather(*tasks, return_exceptions=True)
            if process:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        stream.close()


if __name__ == '__main__':
    unittest.main()
