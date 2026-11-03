"""Offline regressions for FunOne submission and stage transitions."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from platforms import funone
import settings

TICKET_URL = "https://tickets.funone.io/purchase/purchase_choose_ticket_has_map/1/2?step=2"
FORM_URL = "https://tickets.funone.io/purchase/purchase_fill_form/1/2"


class FunOneFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        funone._state.clear()
        self.config = settings.get_default_config()
        self.config["advanced"]["verbose"] = False
        self.tab = AsyncMock()

    async def submit(self, result=None):
        self.tab.evaluate.return_value = result if result is not None else {"clicked": True}
        with patch.object(funone, "nodriver_current_url_safe", AsyncMock(return_value=TICKET_URL)), \
                patch.object(funone, "nodriver_funone_assign_ticket_number", AsyncMock(return_value=True)), \
                patch.object(funone, "nodriver_funone_captcha_handler", AsyncMock(return_value=True)), \
                patch.object(funone, "nodriver_funone_detect_step", AsyncMock(return_value=2)):
            return await funone.nodriver_funone_order_submit(self.tab, self.config)

    async def test_submit_waits_for_result_beyond_cooldown(self):
        self.assertTrue(await self.submit())
        funone._state["order_submitted_time"] -= 30
        self.tab.evaluate.return_value = False  # No explicit failure.
        with patch.object(funone, "nodriver_current_url_safe", AsyncMock(return_value=TICKET_URL)), \
                patch.object(funone, "nodriver_funone_detect_step", AsyncMock(return_value=2)):
            self.assertFalse(await funone.nodriver_funone_order_submit(self.tab, self.config))
        self.assertTrue(funone._state["submission_pending"])
        self.assertEqual(self.tab.evaluate.await_count, 2)  # One click, one failure probe.

    async def test_unknown_submit_response_does_not_allow_retry(self):
        self.assertFalse(await self.submit({"unexpected": True}))
        self.assertTrue(funone._state["submission_pending"])

    async def test_submit_timeout_keeps_pending(self):
        self.tab.evaluate.side_effect = TimeoutError("CDP response timeout")
        self.assertFalse(await self.submit())
        self.assertTrue(funone._state["submission_pending"])

    async def test_explicit_click_failure_can_retry(self):
        self.assertFalse(await self.submit({"clicked": False}))
        self.assertFalse(funone._state["submission_pending"])

    async def test_manual_submit_setting_is_honored(self):
        self.config["ocr_captcha"]["force_submit"] = False
        self.assertFalse(await self.submit())
        self.tab.evaluate.assert_not_awaited()

    async def test_no_submit_on_data_payment_or_completed_pages(self):
        for route in (FORM_URL, "https://tickets.funone.io/purchase/purchase_checkout/1",
                      "https://tickets.funone.io/purchase/purchase_completed/1"):
            with self.subTest(route=route), patch.object(
                    funone, "nodriver_current_url_safe", AsyncMock(return_value=route)):
                self.assertFalse(await funone.nodriver_funone_order_submit(self.tab, self.config))
        self.tab.evaluate.assert_not_awaited()

    async def test_navigation_releases_pending(self):
        await self.submit()
        self.assertFalse(await funone._funone_submission_pending(self.tab, FORM_URL, self.config))
        self.assertFalse(funone._state["submission_pending"])

    async def test_explicit_failure_releases_pending(self):
        await self.submit()
        self.tab.evaluate.return_value = True
        with patch.object(funone, "nodriver_funone_detect_step", AsyncMock(return_value=2)), \
                patch.object(funone, "nodriver_funone_close_popup", AsyncMock()) as close:
            self.assertFalse(await funone._funone_submission_pending(self.tab, TICKET_URL, self.config))
            close.assert_awaited_once()

    async def test_rejected_captcha_refreshes_before_retry(self):
        await self.submit()
        self.tab.evaluate.return_value = "captcha"
        with patch.object(funone, "nodriver_funone_detect_step", AsyncMock(return_value=2)), \
                patch.object(funone, "_funone_read_captcha", AsyncMock(return_value={"image": "old"})), \
                patch.object(funone, "nodriver_funone_close_popup", AsyncMock()), \
                patch.object(funone, "nodriver_funone_reload_captcha", AsyncMock()) as reload:
            self.assertFalse(await funone._funone_submission_pending(self.tab, TICKET_URL, self.config))
            reload.assert_awaited_once()
            self.assertEqual(funone._state["captcha_reload_from"], "old")

    async def test_unknown_captcha_probe_is_not_success(self):
        with patch.object(funone, "_funone_read_captcha", AsyncMock(return_value=None)):
            self.assertFalse(await funone.nodriver_funone_captcha_handler(self.tab, self.config))

    async def test_data_page_waits_for_late_captcha(self):
        with patch.object(funone, "_funone_read_captcha", AsyncMock(return_value={"hasCaptcha": False})), \
                patch.object(funone, "send_discord_notification") as notify:
            await funone._funone_handle_form(self.tab, self.config)
            self.assertNotIn("handoff_notified", funone._state)
            notify.assert_not_called()

    async def test_new_form_resets_handoff(self):
        funone._funone_reset_page_state(FORM_URL)
        funone._state.update(handoff_notified=True, ocr_retry_count=5)
        funone._funone_reset_page_state(FORM_URL + "?new=1")
        self.assertNotIn("handoff_notified", funone._state)
        self.assertEqual(funone._state["ocr_retry_count"], 0)

    async def test_handoff_notification_is_not_order_success(self):
        self.config["advanced"]["play_sound"]["order"] = False
        info = {"hasCaptcha": True, "ready": True, "image": "image", "value": "ABCDE", "valid": True}
        with patch.object(funone, "_funone_read_captcha", AsyncMock(return_value=info)), \
                patch.object(funone, "send_discord_notification") as discord, \
                patch.object(funone, "send_telegram_notification"):
            await funone._funone_handle_form(self.tab, self.config)
            await funone._funone_handle_form(self.tab, self.config)
            discord.assert_called_once()
            message = discord.call_args.args[0]["advanced"]["discord_message"]
            self.assertIn("訂單尚未確認成立", message)
        self.assertEqual(self.config["advanced"]["discord_message"], "")

    async def test_refresh_does_not_reload_after_navigation(self):
        with patch.object(funone.asyncio, "sleep", AsyncMock()), patch.object(
                funone, "nodriver_current_url_safe", AsyncMock(return_value=FORM_URL)):
            await funone._funone_refresh_stock(self.tab, self.config)
        self.tab.reload.assert_not_awaited()

    async def test_manual_reload_same_url_resumes_ticket_detection(self):
        funone._state.update(flow_url=TICKET_URL, flow_document_id=100,
                             submission_pending=True, order_submitted_url=TICKET_URL,
                             order_submitted_step=2, ocr_retry_count=5)

        async def evaluate(script):
            if "timeOrigin" in script:
                return {"documentId": 200, "url": TICKET_URL, "ready": True}
            return False

        self.tab.evaluate.side_effect = evaluate
        with patch.object(funone, "check_and_handle_pause", AsyncMock(return_value=False)), \
                patch.object(funone, "nodriver_funone_verify_login", AsyncMock(return_value=True)), \
                patch.object(funone, "nodriver_funone_close_popup", AsyncMock()), \
                patch.object(funone, "nodriver_funone_detect_step", AsyncMock(return_value=2)), \
                patch.object(funone, "_funone_handle_ticket_flow", AsyncMock()) as flow:
            await funone.nodriver_funone_main(self.tab, TICKET_URL, self.config)
            flow.assert_awaited_once()
        self.assertFalse(funone._state["submission_pending"])
        self.assertEqual(funone._state["ocr_retry_count"], 0)

    async def test_manual_reload_during_stock_wait_is_not_overwritten(self):
        funone._state.update(flow_document_id=100)
        self.tab.evaluate.return_value = {"documentId": 200, "url": TICKET_URL, "ready": True}
        with patch.object(funone.asyncio, "sleep", AsyncMock()), \
                patch.object(funone, "nodriver_current_url_safe", AsyncMock(return_value=TICKET_URL)), \
                patch.object(funone, "nodriver_funone_click_refresh_button", AsyncMock(return_value=True)) as refresh:
            await funone._funone_refresh_stock(self.tab, self.config)
            refresh.assert_not_awaited()
        self.tab.reload.assert_not_awaited()

    async def test_same_document_keeps_duplicate_submit_protection(self):
        funone._state.update(flow_url=TICKET_URL, flow_document_id=100,
                             submission_pending=True)
        funone._funone_reset_page_state(TICKET_URL, 100)
        self.assertTrue(funone._state["submission_pending"])

    async def test_missing_document_probe_keeps_state_and_skips_actions(self):
        funone._state.update(flow_url=TICKET_URL, flow_document_id=100,
                             submission_pending=True)
        self.tab.evaluate.side_effect = TimeoutError("page is navigating")
        with patch.object(funone, "check_and_handle_pause", AsyncMock(return_value=False)), \
                patch.object(funone, "_funone_handle_ticket_flow", AsyncMock()) as flow:
            await funone.nodriver_funone_main(self.tab, TICKET_URL, self.config)
            flow.assert_not_awaited()
        self.assertTrue(funone._state["submission_pending"])


if __name__ == "__main__":
    unittest.main()
