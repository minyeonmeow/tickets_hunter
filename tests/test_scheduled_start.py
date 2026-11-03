"""Exercise production timer functions without importing the executable's global hooks."""
import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import urllib.parse


class FixedDatetime(datetime):
    @classmethod
    def now(cls):
        return cls(2026, 11, 4, 12, 0, 0)


class ScheduledStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'src/nodriver_tixcraft.py'
        tree = ast.parse(source.read_text(encoding='utf8'))
        names = {'parse_refresh_datetime', '_release_scheduled_start', 'check_refresh_datetime_gate'}
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.ns = {'datetime': FixedDatetime, 'urllib': urllib,
                   'asyncio': SimpleNamespace(sleep=AsyncMock()),
                   'time': SimpleNamespace(time=lambda: 100),
                   'util': SimpleNamespace(create_debug_logger=lambda cfg: SimpleNamespace(log=lambda *args: None)),
                   'check_and_handle_pause': AsyncMock(return_value=False),
                   'check_and_handle_quit': AsyncMock(return_value=False),
                   'reload_page_if_enabled': AsyncMock(),
                   'nodriver_current_url_safe': AsyncMock(return_value='https://tickets.funone.io/activity/activity_detail/test')}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), self.ns)
        self.state = {'target_str': '', 'reached': False, 'last_countdown_print': 0}

    async def test_funone_releases_gate_without_reloading_activity(self):
        result = await self.ns['check_refresh_datetime_gate'](
            object(), {'refresh_datetime': '2026/11/04 12:00:00'}, self.state)
        self.assertFalse(result)
        self.assertTrue(self.state['reached'])
        self.ns['reload_page_if_enabled'].assert_not_awaited()

    async def test_other_platform_keeps_scheduled_refresh(self):
        self.ns['nodriver_current_url_safe'].return_value = 'https://tixcraft.com/activity/game/test'
        self.assertFalse(await self.ns['check_refresh_datetime_gate'](
            object(), {'refresh_datetime': '2026/11/04 12:00:00'}, self.state))
        self.ns['reload_page_if_enabled'].assert_awaited_once()

    async def test_before_target_time_does_not_release(self):
        self.assertTrue(await self.ns['check_refresh_datetime_gate'](
            object(), {'refresh_datetime': '2026/11/04 13:00:00'}, self.state))
        self.assertFalse(self.state['reached'])
        self.ns['reload_page_if_enabled'].assert_not_awaited()

    async def test_pause_during_final_countdown_prevents_start(self):
        async def sleep(seconds):
            self.ns['check_and_handle_pause'].return_value = True

        self.ns['asyncio'].sleep.side_effect = sleep
        self.assertTrue(await self.ns['check_refresh_datetime_gate'](
            object(), {'refresh_datetime': '2026/11/04 12:00:01'}, self.state))
        self.assertFalse(self.state['reached'])
        self.ns['reload_page_if_enabled'].assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
