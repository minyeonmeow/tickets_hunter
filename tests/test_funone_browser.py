"""Real Chrome tests using local HTML only; no ticket service is contacted."""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
import zendriver
from zendriver import cdp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from platforms import funone
import settings

TICKET_URL = "https://tickets.funone.io/purchase/purchase_choose_ticket_has_map/1/2"


def png(color):
    data = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(data, "PNG")
    return "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()


class FunOneBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_dom_regressions(self):
        chrome = os.environ.get("FUNONE_TEST_CHROME") or str(Path(
            os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe")
        if not Path(chrome).exists():
            self.skipTest("Set FUNONE_TEST_CHROME to a Chrome executable")
        browser = await zendriver.start(headless=True, browser_executable_path=chrome)
        try:
            self.tab = await browser.get("about:blank")
            self.config = settings.get_default_config()
            self.config["advanced"]["verbose"] = False
            self.config["keyword_exclude"] = ""
            self.config["area_auto_select"]["area_keyword"] = '"VIP"'
            await self.check_session_next()
            with patch.object(funone, "nodriver_current_url_safe", AsyncMock(return_value=TICKET_URL)):
                await self.check_area_index()
                await self.check_keyword_priority()
                await self.check_refresh_target()
                await self.check_quantity_verification()
                await self.check_manual_seats()
                await self.check_captcha_loading_and_manual_input()
                await self.check_ocr_and_rejected_image()
                await self.check_explicit_step()
            await self.check_browser_reload()
        finally:
            process = browser._process
            tasks = []
            for connection in (browser.connection, getattr(self, "tab", None)):
                listener = getattr(connection, "listener", None)
                if listener and listener.task:
                    tasks.append(listener.task)
            await browser.stop()
            # Release resources left open by Zendriver 0.16 during shutdown.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        stream.close()

    async def html(self, html):
        funone._state.clear()
        await self.tab.evaluate("document.body.innerHTML = " + json.dumps(html))
        await asyncio.sleep(0.1)

    async def check_area_index(self):
        await self.html('''<div class="zone_box disabled">Sold out</div>
            <div class="zone_box" onclick="document.body.dataset.clicked='VIP'"><span class="zone_name">VIP</span></div>
            <div class="zone_box" onclick="document.body.dataset.clicked='General'"><span class="zone_name">General</span></div>''')
        self.assertTrue(await funone.nodriver_funone_area_auto_select(self.tab, TICKET_URL, self.config))
        self.assertEqual(await self.tab.evaluate("document.body.dataset.clicked"), "VIP")

    async def check_session_next(self):
        # The Next button can be a sibling of .round_info, not its child.
        await self.html('''<section><div class="round_info"><div class="info">11/15 台北場</div></div>
            <button disabled onclick="document.body.dataset.entered='yes'">下一步</button></section>''')
        url = "https://tickets.funone.io/activity/activity_detail/test"
        self.config['date_auto_select']['date_keyword'] = '"11/15 台北"'
        self.assertFalse(await funone.nodriver_funone_date_auto_select(self.tab, url, self.config))
        await self.tab.evaluate("document.querySelector('button').disabled=false")
        self.assertTrue(await funone.nodriver_funone_date_auto_select(self.tab, url, self.config))
        self.assertEqual(await self.tab.evaluate("document.body.dataset.entered"), "yes")
        self.assertFalse(await funone.nodriver_funone_date_auto_select(self.tab, url, self.config))
        await self.html('''<section><div class="round_info">11/15 台北場</div><button disabled>下一步</button></section>
            <section><div class="round_info">11/16 台中場</div><button>下一步</button></section>''')
        self.assertFalse(await funone.nodriver_funone_date_auto_select(self.tab, url, self.config))

    async def check_refresh_target(self):
        await self.html('<button onclick="document.body.dataset.clicked=\'plus\'"><svg></svg></button>')
        self.assertFalse(await funone.nodriver_funone_click_refresh_button(self.tab, self.config))
        await self.html('<button class="round_info_refresh" onclick="document.body.dataset.clicked=\'refresh\'"><svg></svg></button>')
        self.assertTrue(await funone.nodriver_funone_click_refresh_button(self.tab, self.config))
        self.assertEqual(await self.tab.evaluate("document.body.dataset.clicked"), "refresh")

    async def check_keyword_priority(self):
        await self.html('''<div class="zone_box" onclick="document.body.dataset.clicked='A'">VIP A 區</div>
            <div class="zone_box" onclick="document.body.dataset.clicked='B'">VIP B 區</div>''')
        self.config["area_auto_select"]["area_keyword"] = '"VIP B","VIP A"'
        self.assertTrue(await funone.nodriver_funone_area_auto_select(self.tab, TICKET_URL, self.config))
        self.assertEqual(await self.tab.evaluate("document.body.dataset.clicked"), "B")
        self.config["area_auto_select"]["area_keyword"] = '"VIP"'

    async def check_quantity_verification(self):
        await self.html('''<div>VIP TWD 1000<div>
            <button onclick="this.nextElementSibling.value--">−</button><input value="0">
            <button onclick="this.previousElementSibling.value='1';this.disabled=true">+</button>
            </div></div>''')
        self.config["ticket_number"] = 2
        self.assertFalse(await funone.nodriver_funone_assign_ticket_number(self.tab, self.config))
        self.assertFalse(await funone.nodriver_funone_assign_ticket_number(self.tab, self.config))
        self.assertEqual(await self.tab.evaluate("document.querySelector('input').value"), "1")
        self.config["ticket_number"] = 1
        self.assertTrue(await funone.nodriver_funone_assign_ticket_number(self.tab, self.config))

    async def check_manual_seats(self):
        await self.html('<div class="seat selected">A1</div><div class="seat">A2</div>')
        self.config["ticket_number"] = 2
        self.assertFalse(await funone.nodriver_funone_assign_ticket_number(self.tab, self.config))
        await self.tab.evaluate("document.querySelectorAll('.seat')[1].classList.add('selected')")
        self.assertTrue(await funone.nodriver_funone_assign_ticket_number(self.tab, self.config))

    async def check_captcha_loading_and_manual_input(self):
        self.config["ocr_captcha"]["enable"] = False
        await self.html('<div class="captcha_form"><img alt="vCode"><input placeholder="驗證碼" maxlength="5"></div>')
        self.assertFalse(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))
        await self.tab.evaluate("document.querySelector('img').src=" + json.dumps(png("white")))
        await asyncio.sleep(0.1)
        await self.tab.evaluate("document.querySelector('input').value='AB'")
        self.assertFalse(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))
        await self.tab.evaluate("document.querySelector('input').value='ABCDE'")
        self.assertTrue(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))

    async def check_ocr_and_rejected_image(self):
        first, second = png("white"), png("black")
        await self.html('<div class="captcha_form"><img alt="vCode" src="' + first + '''">
            <input placeholder="驗證碼" maxlength="5"><div class="form_additional_text">
            <p class="input_error" style="display:none">驗證碼錯誤</p></div>
            <button class="captcha_reload_btn">換圖</button></div>''')
        self.config["ocr_captcha"]["enable"] = True
        await self.tab.evaluate("document.querySelector('button').onclick=()=>{document.querySelector('img').src=" +
                                json.dumps(second) + ";document.querySelector('input').value='';}")
        with patch.object(funone, "create_ocr_for_platform", return_value=SimpleNamespace(classification=lambda image: "AbCdE")):
            self.assertTrue(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))
            self.assertTrue(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))
            self.assertEqual(await self.tab.evaluate("document.querySelector('input').value"), "AbCdE")
            await self.tab.evaluate("document.querySelector('.input_error').classList.add('show');document.querySelector('.input_error').style.display='' ")
            self.assertFalse(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))
            await asyncio.sleep(0.6)
            # An old error label may remain visible while the new image is filled.
            self.assertTrue(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))
            self.assertEqual(await self.tab.evaluate("document.querySelector('input').value"), "AbCdE")
            self.assertTrue(await funone.nodriver_funone_captcha_handler(self.tab, self.config, True))
            self.assertEqual(funone._state['ocr_retry_count'], 2)

    async def check_explicit_step(self):
        evaluate = self.tab.evaluate

        async def with_ticket_url(script):
            return await evaluate(script.replace("window.location.href", json.dumps(TICKET_URL + "?step=2")))

        with patch.object(self.tab, "evaluate", side_effect=with_ticket_url):
            self.assertEqual(await funone.nodriver_funone_detect_step(self.tab), 2)

    async def check_browser_reload(self):
        # A browser-initiated reload of the same URL must invalidate old flags.
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as file:
            path = Path(file.name)
        try:
            path.write_text('<html><body>Sold out</body></html>', encoding="utf8")
            self.tab = await self.tab.browser.get(path.as_uri())
            first = await funone._funone_document_state(self.tab)
            self.assertIsNotNone(first)
            funone._state.clear()
            funone._funone_reset_page_state(first["url"], first["documentId"])
            funone._state["submission_pending"] = True
            path.write_text('''<html><body><div>VIP TWD 1000<div>
                <button>−</button><input value="2"><button disabled>+</button>
                </div></div></body></html>''', encoding="utf8")
            await self.tab.send(cdp.page.reload(ignore_cache=True))
            second = None
            for _ in range(40):
                second = await funone._funone_document_state(self.tab)
                if second and second["ready"] and second["documentId"] != first["documentId"]:
                    break
                await asyncio.sleep(0.1)
            self.assertIsNotNone(second)
            self.assertEqual(first["url"], second["url"])
            self.assertNotEqual(first["documentId"], second["documentId"])
            funone._funone_reset_page_state(second["url"], second["documentId"])
            self.assertFalse(funone._state["submission_pending"])
            self.config["ticket_number"] = 2
            self.assertTrue(await funone.nodriver_funone_assign_ticket_number(self.tab, self.config))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
