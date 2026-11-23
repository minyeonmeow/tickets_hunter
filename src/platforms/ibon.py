#encoding=utf-8
# =============================================================================
# iBon Platform Module
# Extracted from nodriver_tixcraft.py during modularization (Phase 1)
# Contains: ibon.com.tw, tour.ibon.com.tw (ibon family)
# =============================================================================

import asyncio
import json
import os
import random
import re
import time
import traceback
import webbrowser

from zendriver import cdp

import util
from nodriver_common import back_if_auto_refresh_enabled, navigate_page_if_enabled, reload_page_if_enabled, sleep_before_auto_refresh
from nodriver_common import (
    asyncio_sleep_with_pause_check,
    check_and_handle_pause,
    check_and_handle_quit,
    create_builtin_ocr,
    create_universal_ocr,
    nodriver_check_checkbox,
    nodriver_current_url,
    nodriver_get_captcha_image_from_dom_snapshot,
    nodriver_press_button,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    write_question_to_file,
    CONST_FROM_TOP_TO_BOTTOM,
    CONST_MAXBOT_ANSWER_ONLINE_FILE,
    CONST_MAXBOT_INT28_FILE,
    CONST_NATIVE_INPUT_SETTER_JS,
    CONST_OCR_CAPTCH_IMAGE_SOURCE_CANVAS,
    CONST_OCR_CAPTCH_IMAGE_SOURCE_NON_BROWSER,
)

# Backward-compatible alias (function moved to nodriver_common)
nodriver_ibon_get_captcha_image_from_shadow_dom = nodriver_get_captcha_image_from_dom_snapshot

__all__ = [
    "register_ibon_alert_handler",
    "dismiss_pending_ibon_dialog",
    "nodriver_ibon_login",
    "nodriver_ibon_date_auto_select_pierce",
    "nodriver_ibon_date_auto_select",
    "nodriver_ibon_date_auto_select_domsnapshot",
    "nodriver_ibon_ticket_agree",
    "nodriver_ibon_allow_not_adjacent_seat",
    "nodriver_ibon_event_area_auto_select",
    "nodriver_ibon_area_auto_select",
    "nodriver_ibon_ticket_number_auto_select",
    "nodriver_ibon_get_captcha_image_from_shadow_dom",  # alias for nodriver_get_captcha_image_from_dom_snapshot
    "nodriver_ibon_keyin_captcha_code",
    "nodriver_ibon_refresh_captcha",
    "nodriver_ibon_auto_ocr",
    "nodriver_ibon_captcha",
    "nodriver_ibon_purchase_button_press",
    "nodriver_ibon_check_sold_out",
    "nodriver_ibon_wait_for_select_elements",
    "nodriver_ibon_check_sold_out_on_ticket_page",
    "nodriver_ibon_navigate_on_sold_out",
    "nodriver_ibon_fill_verify_form",
    "nodriver_ibon_verification_question",
    "nodriver_ibon_card_vaildate",
    "nodriver_tour_ibon_event_detail",
    "nodriver_tour_ibon_options",
    "nodriver_tour_ibon_checkout",
    "nodriver_ibon_main",
    "nodriver_ibon_disconnect",
]

# Module-level state (replaces global ibon_dict)
_state = {}


def _ensure_state():
    """
    Initialize _state with all required keys (idempotent).

    Callable from any entry point (main loop, early alert-handler registration)
    so that touching _state["alert_handler_registered"] before nodriver_ibon_main
    runs cannot strand the other keys uninitialized.
    """
    defaults = {
        "fail_list": [],
        "is_popup_checkout": False,
        "played_sound_order": False,
        "queue_it_enter_time": None,
        "shown_checkout_message": False,
        "alert_handler_registered": False,
        "played_sound_ticket": False,
        "last_homepage_redirect_time": 0,
        "vaildate_submitted_url": None,
    }
    for key, default in defaults.items():
        _state.setdefault(key, default)


async def register_ibon_alert_handler(tab, config_dict):
    '''每個連線僅保留一個全域監聽，明確拒絕才更新送出狀態。'''
    _ensure_state()
    previous = _state.get('alert_callback')
    if _state.get('alert_tab') is tab and previous in tab.handlers.get(cdp.page.JavascriptDialogOpening, []):
        return

    async def handle_ibon_alert(event, *_unused):
        if await _ibon_actions_paused(config_dict) or _state.get('handoff_ready'):
            return
        url = getattr(getattr(tab, 'target', None), 'url', '') or ''
        if _ibon_is_checkout(url):
            return
        message = str(event.message or '')
        reason = None
        if re.search(r'驗證碼.*(?:錯|不正確|失敗)|(?:錯誤|不正確).*驗證碼|captcha.*(?:wrong|invalid|incorrect)', message, re.I):
            reason = 'captcha'
        elif re.search(r'已售完|售罄|票券不足|數量不足|別人搶先一步|無可配座位|已被購買', message):
            reason = 'stock'
        if not reason and '會員登入方式調整' not in message:
            return
        if await _ibon_actions_paused(config_dict):
            return
        try:
            await tab.send(cdp.page.handle_java_script_dialog(accept=True))
        except Exception:
            return
        if reason:
            await _ibon_mark_rejected(tab, reason)

    try:
        tab.add_handler(cdp.page.JavascriptDialogOpening, handle_ibon_alert)
        _state.update(alert_handler_registered=True, alert_tab=tab, alert_callback=handle_ibon_alert)
    except Exception:
        _state['alert_handler_registered'] = False


async def dismiss_pending_ibon_dialog(tab, config_dict):
    """
    Proactively dismiss a JS dialog that is already open on the page.

    Backstop for alerts that fire BEFORE register_ibon_alert_handler attaches:
    ibon's homepage onload notice fires during driver.get(), so the
    JavaScriptDialogOpening event has already been dispatched (and missed)
    by the time any handler is registered. Calling this after navigation
    completes clears that stuck dialog.

    No-op if no dialog is currently showing.
    """
    if await _ibon_actions_paused(config_dict) or _state.get('handoff_ready'):
        return
    if _ibon_is_checkout(getattr(getattr(tab, 'target', None), 'url', '')):
        return
    debug = util.create_debug_logger(config_dict)
    try:
        await tab.send(cdp.page.handle_java_script_dialog(accept=True))
        debug.log("[IBON ALERT] Pending dialog dismissed proactively")
    except Exception as exc:
        msg = str(exc)
        if "No dialog is showing" in msg or "-32602" in msg:
            return
        debug.log(f"[IBON ALERT] Proactive dismiss failed: {exc}")


async def nodriver_ibon_login(tab, config_dict, driver):
    """
    專門的 ibon 登入函數，整合 cookie 處理、頁面重新載入和登入狀態驗證
    """
    debug = util.create_debug_logger(config_dict)

    debug.log("=== ibon Auto-Login Started ===")

    # 檢查是否有 ibon cookie 設定
    ibonqware = config_dict["accounts"]["ibonqware"]
    if len(ibonqware) <= 1:
        debug.log("No ibon cookie configured, skipping auto-login")
        return {'success': False, 'reason': 'no_cookie_configured'}

    debug.log(f"Setting ibon cookie (NoDriver) with length: {len(ibonqware)}")
    debug.log(f"Cookie contains mem_id: {'mem_id=' in ibonqware}")
    debug.log(f"Cookie contains mem_email: {'mem_email=' in ibonqware}")
    debug.log(f"Cookie contains huiwanTK: {'huiwanTK=' in ibonqware}")
    debug.log(f"Cookie contains ibonqwareverify: {'ibonqwareverify=' in ibonqware}")

    try:
        from zendriver import cdp

        # Set ibon cookie using CDP
        cookie_result = await tab.send(cdp.network.set_cookie(
            name="ibonqware",
            value=ibonqware,
            domain=".ibon.com.tw",
            path="/",
            secure=True,
            http_only=True
        ))

        debug.log(f"CDP setCookie result: {cookie_result}")
        debug.log("ibon cookie set successfully (NoDriver)")

        # 驗證 cookie 是否設定成功
        updated_cookies = await driver.cookies.get_all()
        ibon_cookies = [c for c in updated_cookies if c.name == 'ibonqware']
        if not ibon_cookies:
            debug.log("Warning: ibon cookie not found after setting")
            return {'success': False, 'reason': 'cookie_not_set'}

        debug.log(f"Verified: ibon cookie exists with value length: {len(ibon_cookies[0].value)}")
        debug.log(f"Cookie domain: {ibon_cookies[0].domain}")
        debug.log("[SUCCESS] ibon cookie set successfully")
        debug.log("[INFO] Cookie will be applied when navigating to event page")

        # No page reload needed - cookie is set on homepage,
        # and navigation to event page will automatically apply the cookie
        return {'success': True, 'reason': 'cookie_set'}

    except Exception as cookie_error:
        debug.log(f"[IBON LOGIN] Failed to set ibon cookie (NoDriver): {cookie_error}")
        if debug.enabled:
            import traceback
            traceback.print_exc()
        return {'success': False, 'reason': 'exception', 'error': str(cookie_error)}

CONST_IBON_DATE_ROWS_JS = r'''
const dateRows=()=>Array.from(document.querySelectorAll('button.btn-buy')).map((button,index)=>{
    const row=button.closest('.tr,tr,[role="row"]')||button.closest('.d-flex');
    return {index,text:button.textContent.trim(),date_context:row?.textContent.trim()||'',
        disabled:button.disabled||button.getAttribute('aria-disabled')==='true'||!button.getClientRects().length};
});
'''


def _ibon_date_target(buttons, config_dict):
    enabled = [button for button in buttons if not button.get('disabled') and button.get('date_context') and
               not util.reset_row_text_if_match_keyword_exclude(config_dict, button['date_context'])]
    keyword = config_dict.get('date_auto_select', {}).get('date_keyword', '').strip()
    matched = enabled if not keyword else []
    for group in util.parse_keyword_string_to_array(keyword) if keyword else []:
        parts = [part.lower() for part in group.split() if part.strip()]
        found = next((button for button in enabled if parts and all(
            part in (button.get('text', '') + ' ' + button['date_context']).lower() for part in parts)), None)
        if found:
            matched = [found]
            break
    if not matched and keyword and config_dict.get('date_auto_fallback', False):
        matched = enabled
    return util.get_target_item_from_matched_list(matched, config_dict.get('date_auto_select', {}).get('mode', CONST_FROM_TOP_TO_BOTTOM))


async def nodriver_ibon_date_auto_select_pierce(tab, config_dict):
    """一般 DOM 一次讀取完整場次；Shadow DOM 由主入口保留 CDP 回退。"""
    if await _ibon_actions_paused(config_dict):
        return False
    generation = _ibon_reset_token()
    try:
        data = await tab.evaluate(_ibon_render(r'''(() => {
            DATE_ROWS
            const doc=location.href+'|'+performance.timeOrigin,active=window[Symbol.for('ticketsHunter.ibon.date')];
            return {doc,buttons:dateRows(),pending:active?.doc===doc&&active.resetToken===GENERATION};
        })()''', GENERATION=generation))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get('doc') or not data.get('buttons'):
        return None
    if data.get('pending'):
        return False
    if any(not button.get('disabled') and not button.get('date_context') for button in data['buttons']):
        return None
    target = _ibon_date_target(data['buttons'], config_dict)
    if not target or await _ibon_actions_paused(config_dict):
        return False
    try:
        result = await tab.evaluate(_ibon_render(r'''((expected,target) => {
            DATE_ROWS
            const key=Symbol.for('ticketsHunter.ibon.date'),doc=location.href+'|'+performance.timeOrigin;
            if(doc!==expected||window[key]?.doc===doc&&window[key].resetToken===GENERATION)return false;
            const live=dateRows().find(row=>row.index===target.index);
            if(!live||live.disabled||live.date_context!==target.date_context||live.text!==target.text)return false;
            const button=document.querySelectorAll('button.btn-buy')[target.index];
            window[key]={doc,index:target.index,resetToken:GENERATION};
            try{button.click();return true;}catch(_){return false;}
        })(EXPECTED,TARGET)''', EXPECTED=data['doc'], TARGET=target, GENERATION=generation))
        if result:
            util.create_debug_logger(config_dict).log('[IBON DATE] 已依日期關鍵字優先順序選擇場次。')
        return bool(result)
    except Exception:
        # A lost click reply must not invoke a second selection path.
        return False


async def nodriver_ibon_date_auto_select(tab, config_dict):
    result = await nodriver_ibon_date_auto_select_pierce(tab, config_dict)
    if result is not None:
        return result
    util.create_debug_logger(config_dict).log('[IBON DATE] 一般 DOM 無法取得完整場次，改用 CDP DOMSnapshot。')
    return await nodriver_ibon_date_auto_select_domsnapshot(tab, config_dict)

async def nodriver_ibon_date_auto_select_domsnapshot(tab, config_dict):
    """
    NoDriver ibon 日期自動選擇實作 - DOMSnapshot 回退版本
    使用 CDP DOMSnapshot 穿透 closed Shadow DOM 搜尋購票按鈕
    參考：NoDriver API Guide 範例 1
    """
    if await _ibon_actions_paused(config_dict):
        return False
    entry = await _ibon_snapshot(tab)
    if not entry: return False
    from zendriver import cdp
    from zendriver.cdp import input_ as cdp_input
    import random

    debug = util.create_debug_logger(config_dict)
    auto_select_mode = config_dict["date_auto_select"]["mode"]
    date_keyword = config_dict["date_auto_select"]["date_keyword"].strip()
    date_auto_fallback = config_dict.get('date_auto_fallback', False)  # T017: Safe access for new field
    auto_reload_coming_soon_page_enable = (config_dict["tixcraft"]["auto_reload_coming_soon_page"]) and util.is_auto_refresh_enabled()

    debug.log("[IBON DATE] Starting date selection on ActivityInfo/Details page")
    debug.log("date_keyword:", date_keyword)
    debug.log("auto_select_mode:", auto_select_mode)

    is_date_assigned = False

    # Auto-detect buttons (no fixed wait - responds immediately when ready)
    # IMPORTANT: get_document() must be called before perform_search() to initialize CDP DOM state
    try:
        await tab.send(cdp.dom.get_document(depth=0, pierce=False))
    except:
        pass

    import time
    max_wait = 3  # Maximum wait (safety limit)
    check_interval = 0.1  # Fast polling for quick response
    start_time = time.time()
    content_appeared = False

    debug.log("[IBON DATE] Auto-detecting purchase buttons...")

    while (time.time() - start_time) < max_wait:
        try:
            # Use CDP search to check button presence (penetrates Shadow DOM, same as pierce method)
            search_id, result_count = await tab.send(cdp.dom.perform_search(
                query='button.btn-buy',
                include_user_agent_shadow_dom=True
            ))

            # Clean up search
            try:
                await tab.send(cdp.dom.discard_search_results(search_id=search_id))
            except:
                pass

            if result_count > 0:
                content_appeared = True
                elapsed = time.time() - start_time
                debug.log(f"[IBON DATE] Found {result_count} purchase button(s) after {elapsed:.2f}s")
                break
        except:
            pass
        await tab.sleep(check_interval)

    if not content_appeared:
        elapsed = time.time() - start_time
        debug.log(f"[IBON DATE] No buttons found after {elapsed:.1f}s, proceeding with snapshot anyway...")

    # Capture DOM snapshot to penetrate closed Shadow DOM and search for purchase buttons
    debug.log("[IBON DATE] Capturing DOM snapshot with CDP...")

    try:
        documents, strings = await tab.send(cdp.dom_snapshot.capture_snapshot(
            computed_styles=[],
            include_dom_rects=True
        ))
    except Exception as e:
        debug.log(f"[IBON DATE] Error capturing snapshot: {e}")
        return False

    purchase_buttons = []

    if documents and len(documents) > 0:
        debug.log(f"[IBON DATE] Analyzing {len(documents)} document(s)...")

        document_snapshot = documents[0]
        nodes = document_snapshot.nodes

        node_names = [strings[i] for i in nodes.node_name]
        node_values = [strings[i] if i >= 0 else '' for i in nodes.node_value]
        attributes_list = nodes.attributes
        backend_node_ids = list(nodes.backend_node_id)

        debug.log(f"[IBON DATE] Total nodes in snapshot: {len(node_names)}")

        # Step 1: Extract parent_index for tracking node relationships
        parent_indices = list(nodes.parent_index) if hasattr(nodes, 'parent_index') else []

        # Debug: Count all buttons found
        button_count = sum(1 for name in node_names if name.upper() == 'BUTTON')
        debug.log(f"[IBON DATE] Total BUTTON nodes found: {button_count}")

        # Step 2: Search for purchase buttons and extract date context
        for i, node_name in enumerate(node_names):
            if node_name.upper() == 'BUTTON':
                attrs = {}
                if i < len(attributes_list):
                    attr_indices = attributes_list[i]
                    for j in range(0, len(attr_indices), 2):
                        if j + 1 < len(attr_indices):
                            key = strings[attr_indices[j]]
                            val = strings[attr_indices[j + 1]]
                            attrs[key] = val

                button_class = attrs.get('class', '')
                button_disabled = 'disabled' in attrs

                # ibon purchase buttons have 'btn-buy' or 'ng-tns-c57' in class
                if 'btn-buy' in button_class or ('ng-tns-c57' in button_class and 'btn' in button_class):
                    # Get button text from adjacent text nodes
                    button_text = ''
                    if i + 1 < len(node_names) and node_names[i + 1] == '#text':
                        button_text = node_values[i + 1].strip()

                    # Step 3: Extract date context by finding parent .tr container
                    date_context = ''
                    if parent_indices:
                        # Traverse up to find .tr container
                        current_idx = i
                        tr_container_idx = -1
                        for _ in range(10):  # Max 10 levels up
                            if current_idx < len(parent_indices):
                                parent_idx = parent_indices[current_idx]
                                if parent_idx >= 0 and parent_idx < len(attributes_list):
                                    # Check if this parent has class='tr' or class contains 'tr'
                                    parent_attrs = {}
                                    parent_attr_indices = attributes_list[parent_idx]
                                    for j in range(0, len(parent_attr_indices), 2):
                                        if j + 1 < len(parent_attr_indices):
                                            key = strings[parent_attr_indices[j]]
                                            val = strings[parent_attr_indices[j + 1]]
                                            parent_attrs[key] = val

                                    parent_class = parent_attrs.get('class', '')
                                    if ' tr ' in f' {parent_class} ' or parent_class.endswith(' tr') or parent_class.startswith('tr '):
                                        tr_container_idx = parent_idx
                                        break

                                current_idx = parent_idx
                            else:
                                break

                        # Step 4: Collect all text under the .tr container.
                        # The row has columns: date / session-name / venue / button.
                        # The session-name column (e.g. "雙日票", "08/29單日票") carries no
                        # marker class, so gather the whole row's text instead of only the
                        # .date cell. This lets keyword matching target ticket-type names
                        # when several sessions share the same start date.
                        if tr_container_idx >= 0:
                            context_parts = []
                            for j, j_name in enumerate(node_names):
                                if j_name != '#text':
                                    continue
                                ancestor = parent_indices[j] if j < len(parent_indices) else -1
                                hops = 0
                                while ancestor >= 0 and hops < 12:
                                    if ancestor == tr_container_idx:
                                        text_value = node_values[j].strip()
                                        if text_value:
                                            context_parts.append(text_value)
                                        break
                                    ancestor = parent_indices[ancestor] if ancestor < len(parent_indices) else -1
                                    hops += 1
                            date_context = ' '.join(context_parts)

                    purchase_buttons.append({
                        'backend_node_id': backend_node_ids[i],
                        'class': button_class,
                        'disabled': button_disabled,
                        'text': button_text,
                        'index': i,
                        'date_context': date_context
                    })

                    debug.log(f"[IBON DATE] Found button: class='{button_class[:50]}...', disabled={button_disabled}, text='{button_text}', date_context='{date_context}'")

    debug.log(f"[IBON DATE] Found {len(purchase_buttons)} purchase button(s)")

    if len(purchase_buttons) == 0:
        debug.log("[IBON DATE] No purchase buttons found in Shadow DOM")
        return False

    # Step 5: Filter disabled buttons + keyword_exclude (Stage 4: date selection)
    enabled_buttons = []
    for btn in purchase_buttons:
        if btn['disabled']:
            continue
        if btn.get('date_context') and util.reset_row_text_if_match_keyword_exclude(config_dict, btn['date_context']):
            debug.log(f"[IBON DATE] Excluded by keyword_exclude: {btn.get('date_context', '')[:50]}")
            continue
        enabled_buttons.append(btn)

    debug.log(f"[IBON DATE] Found {len(enabled_buttons)} enabled button(s)")

    if len(enabled_buttons) == 0:
        debug.log("[IBON DATE] All buttons are disabled")
        return False

    # Step 6: Apply keyword matching with early return pattern (T004-T007)
    # Unified keyword processing: JSON array parsing with AND/OR logic
    # Format: "AA BB","CC","DD" -> (AA AND BB) OR (CC) OR (DD)
    matched_buttons = []
    target_found = False

    if len(date_keyword) > 0 and enabled_buttons:
        # Parse as JSON array (auto-removes quotes)
        keyword_array = util.parse_keyword_string_to_array(date_keyword)
        debug.log(f"[IBON DATE KEYWORD] Start checking keywords in order: {keyword_array}")

        # NEW: Iterate keywords in priority order (early return)
        for keyword_index, keyword_item in enumerate(keyword_array):
            debug.log(f"[IBON DATE KEYWORD] Checking keyword #{keyword_index + 1}: {keyword_item}")

            # Check all buttons for this keyword
            for button in enabled_buttons:
                button_text = button.get('text', '').lower()
                date_context = button.get('date_context', '').lower()
                search_text = f"{button_text} {date_context}"

                # Split by space for AND logic (e.g., "AA BB" means AA AND BB)
                sub_keywords = [kw.strip() for kw in keyword_item.split(' ') if kw.strip()]
                # Check if all sub-keywords match (AND logic within group)
                is_match = all(sub_kw.lower() in search_text for sub_kw in sub_keywords)

                if is_match:
                    # T006: Keyword matched log - IMMEDIATELY select and stop
                    matched_buttons = [button]
                    target_found = True
                    debug.log(f"[IBON DATE KEYWORD] Keyword #{keyword_index + 1} matched: '{keyword_item}'")
                    debug.log(f"[IBON DATE SELECT] Selected date: {date_context[:50]} (keyword match)")
                    break

            if target_found:
                # EARLY RETURN: Stop checking further keywords
                break

        # T007: All keywords failed log
        if not target_found:
            debug.log(f"[IBON DATE KEYWORD] All keywords failed to match")
    else:
        matched_buttons = enabled_buttons

    # Step 7: Conditional fallback based on date_auto_fallback switch (T018-T020)
    if len(matched_buttons) == 0 and len(date_keyword) > 0:
        if date_auto_fallback:
            # T018: Fallback enabled - use auto_select_mode
            debug.log(f"[IBON DATE FALLBACK] date_auto_fallback=true, triggering auto fallback")
            debug.log(f"[IBON DATE FALLBACK] Selecting available date based on date_select_order='{auto_select_mode}'")
            matched_buttons = enabled_buttons
        else:
            # T019: Fallback disabled - strict mode (no selection, will reload)
            debug.log(f"[IBON DATE FALLBACK] date_auto_fallback=false, fallback is disabled")
            debug.log(f"[IBON DATE SELECT] No date selected, will reload page and retry")
            return False  # Return False to trigger reload logic in caller

    # Step 8: Select target button based on mode
    target_button = util.get_target_item_from_matched_list(matched_buttons, auto_select_mode)

    # Determine selection method (T013 equivalent)
    is_keyword_match = (len(date_keyword) > 0 and len(matched_buttons) < len(enabled_buttons))
    selection_type = "keyword match" if is_keyword_match else "fallback"
    selection_method = selection_type if is_keyword_match else f"mode '{auto_select_mode}'"

    debug.log(f"[IBON DATE SELECT] Selected date: {target_button.get('date_context', 'N/A')} ({selection_type})")
    debug.log(f"[IBON DATE] Selected target button ({selection_method}): date_context='{target_button.get('date_context', 'N/A')}'")

    try:
        await tab.send(cdp.dom.get_document())

        # Convert backend_node_id to node_id
        result = await tab.send(cdp.dom.push_nodes_by_backend_ids_to_frontend([target_button['backend_node_id']]))
        node_id = result[0]

        debug.log(f"[IBON DATE] Button node_id: {node_id}")

        # Scroll element into view
        await tab.send(cdp.dom.scroll_into_view_if_needed(node_id=node_id))

        # Use CDP Runtime.callFunctionOn to click button in closed Shadow DOM
        from zendriver.cdp import runtime

        # Resolve node to RemoteObject
        resolved = await tab.send(cdp.dom.resolve_node(node_id=node_id))

        # resolved should have an 'object' field containing the RemoteObject
        # But based on error, try accessing object_id directly
        if hasattr(resolved, 'object'):
            remote_object_id = resolved.object.object_id
        elif hasattr(resolved, 'object_id'):
            remote_object_id = resolved.object_id
        else:
            # Debug: print the actual structure
            debug.log(f"[IBON DATE] Resolved structure: {resolved}")
            debug.log(f"[IBON DATE] Resolved type: {type(resolved)}")
            debug.log(f"[IBON DATE] Resolved attributes: {dir(resolved)}")
            raise Exception("Could not find object_id in resolved node")

        debug.log(f"[IBON DATE] Resolved button object_id: {remote_object_id}")

        live = await _ibon_snapshot(tab)
        if not live or live['doc'] != entry['doc'] or await _ibon_actions_paused(config_dict):
            return False
        result, error = await tab.send(runtime.call_function_on(
            function_declaration=_ibon_render('''function() {
                const key=Symbol.for('ticketsHunter.ibon.date'),doc=location.href+'|'+performance.timeOrigin;
                if(doc!==EXPECTED||this.disabled||this.getAttribute('aria-disabled')==='true'||
                    window[key]?.doc===doc&&window[key].resetToken===GENERATION)return false;
                window[key]={doc,resetToken:GENERATION};this.click();return true;
            }''', EXPECTED=entry['doc'], GENERATION=_ibon_reset_token()),
            object_id=remote_object_id,
            return_by_value=True
        ))

        debug.log(f"[IBON DATE] Button clicked, result: {result}")

        if not error and result and result.value is True:
            debug.log("[IBON DATE] Purchase button clicked successfully")
            is_date_assigned = True
        else:
            debug.log("[IBON DATE] Click failed")

    except Exception as e:
        debug.log(f"[IBON DATE] Error clicking button: {e}")
        is_date_assigned = False

    return is_date_assigned

async def nodriver_ibon_ticket_agree(tab):
    is_finish_checkbox_click = False
    for i in range(3):
        is_finish_checkbox_click = await nodriver_check_checkbox(tab, '#agreen')
        if is_finish_checkbox_click:
            break
    return is_finish_checkbox_click

async def nodriver_ibon_allow_not_adjacent_seat(tab, config_dict):
    """
    Check and click the 'allow non-adjacent seats' checkbox on ibon

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary for debug settings

    Returns:
        bool: True if checkbox was clicked successfully, False otherwise
    """
    debug = util.create_debug_logger(config_dict)

    is_finish_checkbox_click = False

    # Selector for non-adjacent seat checkbox
    checkbox_selector = 'div.not-consecutive > div.custom-control > span > input[type="checkbox"]:not(:checked)'

    try:
        for i in range(3):
            is_finish_checkbox_click = await nodriver_check_checkbox(tab, checkbox_selector)
            if is_finish_checkbox_click:
                debug.log("[IBON] Non-adjacent seat checkbox clicked")
                break
    except Exception as e:
        debug.log(f"[IBON] Non-adjacent seat checkbox error: {e}")

    return is_finish_checkbox_click

async def nodriver_ibon_event_area_auto_select(tab, config_dict, area_keyword_item=""):
    """
    ibon seat area auto-selection for NEW Event page format (NoDriver version)

    Handles seat area selection on /Event/{id}/{id} page (Angular SPA).
    Uses DOMSnapshot for data extraction and CDP for clicking.

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary
        area_keyword_item: Area keyword string (space-separated for AND logic)

    Returns:
        tuple: (is_need_refresh, is_price_assign_by_bot)
            - is_need_refresh: Whether page refresh is needed
            - is_price_assign_by_bot: Whether area selection succeeded
    """
    if await _ibon_actions_paused(config_dict):
        return (False, False)
    entry = await _ibon_snapshot(tab)
    if not entry: return (False, False)
    debug = util.create_debug_logger(config_dict)
    auto_select_mode = config_dict["area_auto_select"]["mode"]
    area_auto_fallback = config_dict.get('area_auto_fallback', False)  # T021: Safe access for new field
    ticket_number = config_dict["ticket_number"]

    is_price_assign_by_bot = False
    is_need_refresh = False

    debug.log("[ibon] 區域選擇開始")

    # Optimized wait for Angular app to fully load (reduced from 2.3-2.7s to 1.0-1.4s)
    try:
        import random
        wait_time = random.uniform(0.6, 0.8)
        await tab.sleep(wait_time)
        # Reduced second wait (1.5s -> 0.6s) - total now 1.2-1.4s instead of 2.3-2.7s
        await tab.sleep(0.6)
    except:
        pass

    # Phase 1: Extract all area data using DOMSnapshot (to pierce Shadow DOM if present)
    try:
        from zendriver import cdp

        # Use DOMSnapshot to get flattened page structure
        documents, strings = await tab.send(cdp.dom_snapshot.capture_snapshot(
            computed_styles=[],
            include_paint_order=True,
            include_dom_rects=True
        ))

        areas_data = []

        if documents and len(documents) > 0:
            document_snapshot = documents[0]

            # Extract node information
            node_names = []
            node_values = []
            parent_indices = []
            attributes_list = []
            backend_node_ids = []

            if hasattr(document_snapshot, 'nodes'):
                nodes = document_snapshot.nodes
                if hasattr(nodes, 'node_name'):
                    node_names = [strings[i] if isinstance(i, int) and i < len(strings) else str(i)
                                 for i in nodes.node_name]
                if hasattr(nodes, 'node_value'):
                    node_values = [strings[i] if isinstance(i, int) and i >= 0 and i < len(strings) else ''
                                  for i in nodes.node_value]
                if hasattr(nodes, 'parent_index'):
                    parent_indices = list(nodes.parent_index)
                if hasattr(nodes, 'attributes'):
                    attributes_list = nodes.attributes
                if hasattr(nodes, 'backend_node_id'):
                    backend_node_ids = list(nodes.backend_node_id)

            # Build children map for traversal
            children_map = {}
            for i, parent_idx in enumerate(parent_indices):
                if parent_idx >= 0:
                    if parent_idx not in children_map:
                        children_map[parent_idx] = []
                    children_map[parent_idx].append(i)

            # Helper function to get attributes as dict
            def get_attributes_dict(node_index):
                attrs = {}
                if node_index < len(attributes_list):
                    attr_indices = attributes_list[node_index]
                    for j in range(0, len(attr_indices), 2):
                        if j + 1 < len(attr_indices):
                            key_idx = attr_indices[j]
                            val_idx = attr_indices[j + 1]
                            key = strings[key_idx] if key_idx < len(strings) else ''
                            val = strings[val_idx] if val_idx < len(strings) else ''
                            attrs[key] = val
                return attrs

            # Helper function to get all text content from node and its children
            def get_text_content(node_index, depth=0, max_depth=10):
                if depth > max_depth or node_index >= len(node_names):
                    return ''

                text_parts = []

                # If this is a text node, get its value
                if node_names[node_index] == '#text' and node_index < len(node_values):
                    text_parts.append(node_values[node_index])

                # Recursively get text from children
                if node_index in children_map:
                    for child_idx in children_map[node_index]:
                        child_text = get_text_content(child_idx, depth + 1, max_depth)
                        if child_text:
                            text_parts.append(child_text)

                return ' '.join(text_parts).strip()

            # Find all TR elements in the table
            tr_indices = []
            for i, node_name in enumerate(node_names):
                if node_name.upper() == 'TR':
                    tr_indices.append(i)

            # Extract data from each TR
            area_index = 0
            tr_count = 0
            for tr_idx in tr_indices:
                # Get TR attributes
                tr_attrs = get_attributes_dict(tr_idx)
                tr_class = tr_attrs.get('class', '')

                # Skip header rows (check if parent is THEAD)
                parent_idx = parent_indices[tr_idx] if tr_idx < len(parent_indices) else -1
                is_header = False
                if parent_idx >= 0 and parent_idx < len(node_names):
                    parent_name = node_names[parent_idx].upper()
                    if parent_name == 'THEAD':
                        is_header = True

                if is_header:
                    continue

                is_disabled = 'disabled' in tr_class.lower()

                # Find TD children
                td_indices = []
                if tr_idx in children_map:
                    for child_idx in children_map[tr_idx]:
                        if node_names[child_idx].upper() == 'TD':
                            td_indices.append(child_idx)

                # Get backend_node_id for this TR
                tr_backend_node_id = None
                if tr_idx < len(backend_node_ids):
                    tr_backend_node_id = backend_node_ids[tr_idx]

                tr_count += 1

                # Detect "giant TR" pattern (ibon Event page uses 1 TR with all areas)
                # Pattern: [color, area_name, price, seat_status] x N areas
                if len(td_indices) > 10:
                    # Giant TR: loop through TDs in groups of 4
                    for i in range(0, len(td_indices), 4):
                        if i + 3 < len(td_indices):
                            # Extract this group of 4 TDs
                            td_texts = [
                                get_text_content(td_indices[i]),     # color
                                get_text_content(td_indices[i+1]),   # area_name
                                get_text_content(td_indices[i+2]),   # price
                                get_text_content(td_indices[i+3])    # seat_status
                            ]

                            # Skip empty TDs (color-tag TDs may be empty)
                            area_name = td_texts[1].strip()
                            if len(area_name) == 0:
                                continue

                            price = td_texts[2].strip()
                            seat_text = td_texts[3].strip()

                            # For giant TR, determine disabled from seat_text
                            # (cannot use TR class since all areas share the same TR)
                            is_area_disabled = ('已售完' in seat_text)

                            # Build area data object
                            area_data = {
                                'index': area_index,
                                'disabled': is_area_disabled,
                                'areaName': area_name,
                                'price': price,
                                'seatText': seat_text,
                                'innerHTML': f'<tr><td>{area_name}</td><td>{price}</td><td>{seat_text}</td></tr>',
                                'tr_node_index': tr_idx,
                                'backend_node_id': tr_backend_node_id
                            }
                            areas_data.append(area_data)
                            area_index += 1
                else:
                    # Standard TR structure: each TR = 1 area
                    # Expected order: [0]=color, [1]=area_name, [2]=price, [3]=seat_status
                    td_texts = []
                    for td_idx in td_indices:
                        td_text = get_text_content(td_idx)
                        td_texts.append(td_text)

                    if len(td_texts) >= 4:
                        area_name = td_texts[1].strip()
                        price = td_texts[2].strip()
                        seat_text = td_texts[3].strip()

                        # Build area data object
                        area_data = {
                            'index': area_index,
                            'disabled': is_disabled,
                            'areaName': area_name,
                            'price': price,
                            'seatText': seat_text,
                            'innerHTML': f'<tr class="{tr_class}"><td>{area_name}</td><td>{price}</td><td>{seat_text}</td></tr>',
                            'tr_node_index': tr_idx,
                            'backend_node_id': tr_backend_node_id
                        }
                        areas_data.append(area_data)
                        area_index += 1

    except Exception as exc:
        if debug.enabled:
            debug.log(f"[NEW EVENT ERROR] Failed to extract area data: {exc}")
            import traceback
            traceback.print_exc()
        return True, False

    if not areas_data or len(areas_data) == 0:
        debug.log("[ibon] 頁面無區域")
        return True, False

    # Debug extraction (disabled by default)
    # if debug.enabled:
    #     print(f"[IBON EXTRACT DEBUG] Total extracted areas: {len(areas_data)}")

    # Phase 2: Filter areas (disabled, sold out, insufficient seats)
    valid_areas = []

    for area in areas_data:
        # Skip disabled areas
        if area['disabled']:
            debug.log(f"[ibon] 跳過: {area['areaName']}")
            continue

        # 同時檢查區域名稱、票價與內容
        row_text = area['areaName'] + ' ' + area.get('price', '') + ' ' + util.remove_html_tags(area['innerHTML'])

        # Skip sold out areas
        if '已售完' in area['seatText']:
            debug.log(f"[ibon] 已售完: {area['areaName']}")
            continue

        # Check exclude keywords
        if util.reset_row_text_if_match_keyword_exclude(config_dict, row_text):
            debug.log(f"[ibon] 排除: {area['areaName']}")
            continue

        # Check remaining seat count
        seat_text = area['seatText']
        if seat_text.isdigit():
            remaining_seats = int(seat_text)
            if remaining_seats < ticket_number:
                debug.log(f"[ibon] 座位不足: {area['areaName']} ({remaining_seats}/{ticket_number})")
                continue

        valid_areas.append(area)

    debug.log(f"[ibon] 有效區域: {len(valid_areas)}")

    # Phase 3: Keyword matching with early return pattern (T010-T016)
    matched_areas = []
    target_found = False

    if area_keyword_item and len(area_keyword_item) > 0:
        try:
            # NOTE: area_keyword_item is already a SINGLE keyword string from upper layer
            # Upper layer (line 11225) splits by comma using JSON parsing:
            #   Input: "\"5600\",\"5,600\""
            #   After JSON: ["5600", "5,600"]
            #   This function is called once per keyword: "5600" or "5,600"
            #
            # DO NOT split by comma again here, or "5,600" becomes ['5', '600'] (BUG!)
            # Only support space-separated AND logic within each keyword

            area_keyword_clean = area_keyword_item.strip()
            if area_keyword_clean.startswith('"') and area_keyword_clean.endswith('"'):
                area_keyword_clean = area_keyword_clean[1:-1]

            # Treat the entire string as a single keyword
            keyword_item = util.format_keyword_string(area_keyword_clean)

            if debug.enabled:
                debug.log(f"[IBON EVENT AREA KEYWORD] Checking keyword: {keyword_item}")
                debug.log(f"[IBON EVENT AREA KEYWORD] Total valid areas: {len(valid_areas)}")
                if len(valid_areas) > 0:
                    debug.log(f"[IBON EVENT AREA KEYWORD] First 5 areas: {[a['areaName'] for a in valid_areas[:5]]}")

            # Check all areas for this keyword
            for area in valid_areas:
                row_text = area['areaName'] + ' ' + area.get('price', '') + ' ' + util.remove_html_tags(area['innerHTML'])
                row_text = util.format_keyword_string(row_text)

                # Support AND logic with space-separated sub-keywords
                # Example: "VIP 區" → ['VIP', '區'] → must match both
                sub_keywords = [kw.strip() for kw in keyword_item.split(' ') if kw.strip()]
                is_match = all(sub_kw.lower() in row_text.lower() for sub_kw in sub_keywords)

                # Collect every area this keyword hits: the select order
                # (from bottom to top / center / random) picks among them below.
                # Stopping at the first hit always took the top one (#416).
                if is_match:
                    matched_areas.append(area)
                    target_found = True

            if target_found:
                debug.log(f"[IBON EVENT AREA KEYWORD] Keyword '{keyword_item}' matched "
                          f"{len(matched_areas)} area(s): {[a['areaName'] for a in matched_areas]}")

            # All keywords failed log
            if not target_found:
                debug.log(f"[IBON EVENT AREA KEYWORD] Keyword '{keyword_item}' failed to match")
        except Exception as e:
            debug.log(f"[IBON EVENT AREA] Keyword parse error: {e}")
            debug.log(f"[IBON EVENT AREA] Treating as 'all keywords failed'")
            matched_areas = []  # Let Feature 003 fallback logic handle this
    else:
        matched_areas = valid_areas

    if not target_found:
        debug.log(f"[IBON EVENT AREA] Total matched areas: {len(matched_areas)}")

    # T022-T024: Conditional fallback based on area_auto_fallback switch
    if len(matched_areas) == 0 and area_keyword_item and len(area_keyword_item) > 0:
        if area_auto_fallback:
            # T022: Fallback enabled - use all valid areas
            debug.log(f"[IBON EVENT AREA FALLBACK] area_auto_fallback=true, triggering auto fallback")
            debug.log(f"[IBON EVENT AREA FALLBACK] Selecting available area based on area_select_order='{auto_select_mode}'")
            matched_areas = valid_areas
        else:
            # T023: Fallback disabled - strict mode (no selection, will reload)
            debug.log(f"[IBON EVENT AREA FALLBACK] area_auto_fallback=false, fallback is disabled")
            debug.log(f"[IBON EVENT AREA SELECT] No area selected, will reload page and retry")
            # T024: No available options after keyword matching failed
            if len(valid_areas) == 0:
                debug.log(f"[IBON EVENT AREA FALLBACK] No available options after exclusion")
            is_need_refresh = True
            return is_need_refresh, False

    # Phase 4: Select target area based on mode
    target_area = util.get_target_item_from_matched_list(matched_areas, auto_select_mode)

    if not target_area:
        is_need_refresh = True
        debug.log("[ibon] 選擇失敗")
        return is_need_refresh, False

    debug.log(f"[ibon] 已選: {target_area['areaName']}")

    # Phase 5: Click target area using CDP
    try:
        from zendriver import cdp

        debug.log(f"[NEW EVENT CDP CLICK] Starting CDP click for area: {target_area['areaName']}")

        backend_node_id = target_area.get('backend_node_id')

        if not backend_node_id:
            debug.log(f"[NEW EVENT CDP CLICK] No backend_node_id available for TR")
            return is_need_refresh, is_price_assign_by_bot

        # Request document first
        try:
            document = await tab.send(cdp.dom.get_document(depth=-1, pierce=True))
            debug.log(f"[NEW EVENT CDP CLICK] Requested document with pierce=True")
        except Exception as doc_exc:
            debug.log(f"[NEW EVENT CDP CLICK] Document request failed: {doc_exc}")
            return is_need_refresh, is_price_assign_by_bot

        # Convert backend_node_id to node_id
        try:
            result = await tab.send(cdp.dom.push_nodes_by_backend_ids_to_frontend(backend_node_ids=[backend_node_id]))
            node_ids = result if isinstance(result, list) else (result.node_ids if hasattr(result, 'node_ids') else [])

            if not node_ids or len(node_ids) == 0:
                debug.log(f"[NEW EVENT CDP CLICK] Failed to convert backend_node_id to node_id")
                return is_need_refresh, is_price_assign_by_bot

            node_id = node_ids[0]

            debug.log(f"[NEW EVENT CDP CLICK] Node ID: {node_id}")

            # Scroll into view
            try:
                await tab.send(cdp.dom.scroll_into_view_if_needed(node_id=node_id))
                debug.log(f"[NEW EVENT CDP CLICK] Scrolled element into view")
            except Exception as e:
                debug.log(f"[NEW EVENT CDP CLICK] Scroll warning: {e}")

            # Focus element
            try:
                await tab.send(cdp.dom.focus(node_id=node_id))
                debug.log(f"[NEW EVENT CDP CLICK] Focused element")
            except Exception as e:
                debug.log(f"[NEW EVENT CDP CLICK] Focus warning: {e}")

            # Get box model
            box_model = await tab.send(cdp.dom.get_box_model(node_id=node_id))
            debug.log(f"[NEW EVENT CDP CLICK] Got box model")

            # Calculate center point
            content_quad = box_model.content if hasattr(box_model, 'content') else box_model.model.content
            x = (content_quad[0] + content_quad[2]) / 2
            y = (content_quad[1] + content_quad[5]) / 2

            debug.log(f"[NEW EVENT CDP CLICK] Click position: ({x:.1f}, {y:.1f})")

            # Execute mouse click
            live = await _ibon_snapshot(tab)
            if not live or live['doc'] != entry['doc'] or await _ibon_actions_paused(config_dict):
                return (False, False)
            await tab.mouse_click(x, y)

            debug.log(f"[NEW EVENT CDP CLICK] Mouse click executed successfully")

            # Wait for navigation
            await tab.sleep(1.5)

            is_price_assign_by_bot = True

            debug.log(f"[NEW EVENT SUCCESS] Clicked area: {target_area['areaName']}")

        except Exception as resolve_exc:
            if debug.enabled:
                debug.log(f"[NEW EVENT CDP CLICK] Resolve/click failed: {resolve_exc}")
                import traceback
                traceback.print_exc()

    except Exception as exc:
        if debug.enabled:
            debug.log(f"[NEW EVENT ERROR] Exception during click: {exc}")
            import traceback
            traceback.print_exc()

    return is_need_refresh, is_price_assign_by_bot

async def nodriver_ibon_area_auto_select(tab, config_dict, area_keyword_item=""):
    """
    ibon seat area auto-selection (NoDriver version)

    Handles seat area selection on UTK0201_000.aspx page after date selection.
    Uses JavaScript for data extraction and CDP for clicking.

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary
        area_keyword_item: Area keyword string (space-separated for AND logic)

    Returns:
        tuple: (is_need_refresh, is_price_assign_by_bot)
            - is_need_refresh: Whether page refresh is needed
            - is_price_assign_by_bot: Whether area selection succeeded
    """
    if await _ibon_actions_paused(config_dict):
        return (False, False)
    entry = await _ibon_snapshot(tab)
    if not entry: return (False, False)
    # 函數開始時檢查暫停
    if await check_and_handle_pause(config_dict):
        return False, False

    debug = util.create_debug_logger(config_dict)
    auto_select_mode = config_dict["area_auto_select"]["mode"]
    area_auto_fallback = config_dict.get('area_auto_fallback', False)  # T021: Safe access for new field
    ticket_number = config_dict["ticket_number"]

    is_price_assign_by_bot = False
    is_need_refresh = False

    debug.log("NoDriver ibon_area_auto_select started")
    debug.log(f"area_keyword_item: {area_keyword_item}")
    debug.log(f"auto_select_mode: {auto_select_mode}")
    debug.log(f"ticket_number: {ticket_number}")

    # Wait for Shadow DOM to fully load (ibon orders page needs more time for TR elements to render)
    # Auto-detect TR elements (no fixed wait - responds immediately when ready)
    try:
        # First, ensure page state is synced
        await tab  # Sync state

        # Quick check: Is this actually an area selection page?
        # If URL is verification page (UTK0201_0.aspx with rn=), skip area selection
        try:
            current_url = tab.target.url
            if current_url and '/UTK02/UTK0201_0.' in current_url.upper() and 'rn=' in current_url.lower():
                debug.log("[IBON AREA] Detected verification page URL, skipping area selection")
                # Return (False, False) to skip reload and let main loop re-check URL
                return False, False
        except:
            pass

        # Quick check: Does page have Cloudflare or verification form instead of area table?
        try:
            import time as time_module
            cloudflare_max_wait = 15  # Maximum wait for Cloudflare (seconds)
            cloudflare_check_interval = 0.5  # Check interval (seconds)
            cloudflare_start_time = time_module.time()
            cloudflare_detected_once = False
            page_type_result = "area"

            while (time_module.time() - cloudflare_start_time) < cloudflare_max_wait:
                page_type_result = await tab.evaluate('''
                    (function() {
                        // Check for Cloudflare verification page
                        var bodyText = document.body ? document.body.innerText : '';
                        var title = document.title || '';

                        // Cloudflare indicators
                        if (title === '請稍候...' ||
                            bodyText.indexOf('正在驗證') !== -1 ||
                            bodyText.indexOf('驗證您是否是人類') !== -1 ||
                            bodyText.indexOf('Checking your browser') !== -1 ||
                            bodyText.indexOf('verify you are human') !== -1) {
                            return "cloudflare";
                        }

                        // Check for ibon verification form markers
                        var verifyInputs = document.querySelectorAll('#content div.form-group input');
                        var areaTable = document.querySelector('table.table, table[class*="area"], tbody tr td a');

                        if (verifyInputs.length > 0 && !areaTable) {
                            return "verify";
                        }
                        return "area";
                    })()
                ''')

                if page_type_result == "cloudflare":
                    if not cloudflare_detected_once:
                        cloudflare_detected_once = True
                        debug.log("[IBON AREA] Detected Cloudflare verification, waiting for completion...")
                    # Continue waiting, check again
                    await tab.sleep(cloudflare_check_interval)
                    continue
                elif page_type_result == "verify":
                    debug.log("[IBON AREA] Detected verification form, skipping area selection")
                    return False, False
                else:
                    # page_type_result == "area" - Cloudflare completed or not present
                    if cloudflare_detected_once:
                        elapsed = time_module.time() - cloudflare_start_time
                        debug.log(f"[IBON AREA] Cloudflare verification completed after {elapsed:.1f}s")
                    break

            # If timeout while Cloudflare still active
            if cloudflare_detected_once and page_type_result == "cloudflare":
                debug.log("[IBON AREA] Cloudflare verification timeout, letting main loop retry")
                return False, False

        except Exception as cf_exc:
            debug.log(f"[IBON AREA] Cloudflare/page type check error: {cf_exc}")
            pass

        # Initialize CDP DOM state (required for perform_search to work)
        try:
            await tab.send(cdp.dom.get_document(depth=0, pierce=False))
        except:
            pass

        import time
        max_wait = 5  # Maximum wait (increased for page load after Cloudflare)
        # #322: 0.05 polling so detection reacts to render as soon as the table settles.
        # Keep the stability guard below to avoid reading a half-rendered area table.
        check_interval = 0.05  # Polling interval
        start_time = time.time()
        min_tr_count = 3  # Minimum TR elements to consider page loaded (header + at least 1 data row)

        debug.log("[IBON AREA] Auto-detecting area table...")

        last_tr_count = 0
        stable_count = 0  # Track if TR count is stable (page finished loading)

        while (time.time() - start_time) < max_wait:
            # Check if URL changed to verification page (UTK0201_0.aspx)
            try:
                current_url = tab.target.url
                if current_url and '/UTK02/UTK0201_0.' in current_url.upper() and 'rn=' in current_url.lower():
                    debug.log("[IBON AREA] URL changed to verification page, exiting area selection")
                    # Return (False, False) to skip reload and let main loop re-check URL
                    return False, False
            except:
                pass

            try:
                # Use CDP search to check TR presence (penetrates Shadow DOM)
                search_id, tr_count = await tab.send(cdp.dom.perform_search(
                    query='tbody tr',
                    include_user_agent_shadow_dom=True
                ))

                # Clean up search
                try:
                    await tab.send(cdp.dom.discard_search_results(search_id=search_id))
                except:
                    pass

                # Check if TR count is stable (same as last check)
                if tr_count == last_tr_count and tr_count >= min_tr_count:
                    stable_count += 1
                else:
                    stable_count = 0
                last_tr_count = tr_count

                # Page loaded: minimum TR count reached AND stable for 2 consecutive checks
                if tr_count >= min_tr_count and stable_count >= 1:
                    elapsed = time.time() - start_time
                    debug.log(f"[IBON AREA] Found {tr_count} TR elements after {elapsed:.2f}s")
                    break
            except:
                pass

            await tab.sleep(check_interval)

    except Exception as e:
        debug.log(f"[IBON AREA] Error during auto-detect: {e}")
        pass

    # Phase 1: Extract all area data using DOMSnapshot (to pierce closed Shadow DOM)
    try:
        # cdp already imported at function start (Line 10535)

        debug.log("[DOMSNAPSHOT] Capturing page structure for area extraction...")

        # Use DOMSnapshot to get flattened page structure (pierces Shadow DOM)
        documents, strings = await tab.send(cdp.dom_snapshot.capture_snapshot(
            computed_styles=[],
            include_paint_order=True,
            include_dom_rects=True
        ))

        areas_data = []

        if documents and len(documents) > 0:
            document_snapshot = documents[0]

            # Extract node information
            node_names = []
            node_values = []
            parent_indices = []
            attributes_list = []
            backend_node_ids = []

            if hasattr(document_snapshot, 'nodes'):
                nodes = document_snapshot.nodes
                if hasattr(nodes, 'node_name'):
                    node_names = [strings[i] if isinstance(i, int) and i < len(strings) else str(i)
                                 for i in nodes.node_name]
                if hasattr(nodes, 'node_value'):
                    node_values = [strings[i] if isinstance(i, int) and i >= 0 and i < len(strings) else ''
                                  for i in nodes.node_value]
                if hasattr(nodes, 'parent_index'):
                    parent_indices = list(nodes.parent_index)
                if hasattr(nodes, 'attributes'):
                    attributes_list = nodes.attributes
                if hasattr(nodes, 'backend_node_id'):
                    backend_node_ids = list(nodes.backend_node_id)

            debug.log(f"[DOMSNAPSHOT] Extracted {len(node_names)} nodes, {len(strings)} strings")

            # Build children map for traversal
            children_map = {}
            for i, parent_idx in enumerate(parent_indices):
                if parent_idx >= 0:
                    if parent_idx not in children_map:
                        children_map[parent_idx] = []
                    children_map[parent_idx].append(i)

            # Helper function to get attributes as dict
            def get_attributes_dict(node_index):
                attrs = {}
                if node_index < len(attributes_list):
                    attr_indices = attributes_list[node_index]
                    for j in range(0, len(attr_indices), 2):
                        if j + 1 < len(attr_indices):
                            key_idx = attr_indices[j]
                            val_idx = attr_indices[j + 1]
                            key = strings[key_idx] if key_idx < len(strings) else ''
                            val = strings[val_idx] if val_idx < len(strings) else ''
                            attrs[key] = val
                return attrs

            # Helper function to get all text content from node and its children
            def get_text_content(node_index, depth=0, max_depth=10):
                if depth > max_depth or node_index >= len(node_names):
                    return ''

                text_parts = []

                # If this is a text node, get its value
                if node_names[node_index] == '#text' and node_index < len(node_values):
                    text_parts.append(node_values[node_index])

                # Recursively get text from children
                if node_index in children_map:
                    for child_idx in children_map[node_index]:
                        child_text = get_text_content(child_idx, depth + 1, max_depth)
                        if child_text:
                            text_parts.append(child_text)

                return ' '.join(text_parts).strip()

            # Find all TR elements in the table
            tr_indices = []
            for i, node_name in enumerate(node_names):
                if node_name.upper() == 'TR':
                    # Check if it's inside a table (basic check)
                    tr_indices.append(i)

            debug.log(f"[DOMSNAPSHOT] Found {len(tr_indices)} TR elements")

            # Extract data from each TR
            area_index = 0
            for tr_idx in tr_indices:
                # Get TR attributes
                tr_attrs = get_attributes_dict(tr_idx)
                tr_id = tr_attrs.get('id', '')
                tr_class = tr_attrs.get('class', '')

                # Skip header rows (check if parent is THEAD)
                parent_idx = parent_indices[tr_idx] if tr_idx < len(parent_indices) else -1
                is_header = False
                if parent_idx >= 0 and parent_idx < len(node_names):
                    parent_name = node_names[parent_idx].upper()
                    if parent_name == 'THEAD':
                        is_header = True

                if is_header:
                    continue

                is_disabled = 'disabled' in tr_class.lower()

                # Find TD children
                td_indices = []
                if tr_idx in children_map:
                    for child_idx in children_map[tr_idx]:
                        if node_names[child_idx].upper() == 'TD':
                            td_indices.append(child_idx)

                # Extract text from each TD
                # Expected order: [0]=color, [1]=area_name, [2]=price, [3]=seat_status
                td_texts = []
                for td_idx in td_indices:
                    td_text = get_text_content(td_idx)
                    td_texts.append(td_text)

                if len(td_texts) >= 4:
                    area_name = td_texts[1].strip()
                    price = td_texts[2].strip()
                    seat_text = td_texts[3].strip()

                    # Get layout information (bounding box) for this TR
                    layout_rect = None
                    if hasattr(document_snapshot, 'layout'):
                        layout = document_snapshot.layout
                        if hasattr(layout, 'node_index') and hasattr(layout, 'bounds'):
                            # Find this TR's layout index
                            node_indices = list(layout.node_index)
                            bounds_list = list(layout.bounds)

                            if tr_idx in node_indices:
                                layout_idx = node_indices.index(tr_idx)
                                # bounds is an array of Rectangle objects: [x_rect, y_rect, width_rect, height_rect, ...]
                                bounds_idx = layout_idx * 4
                                if bounds_idx + 3 < len(bounds_list):
                                    # Each bound is a Rectangle object, extract the first value
                                    x_rect = bounds_list[bounds_idx]
                                    y_rect = bounds_list[bounds_idx + 1]
                                    width_rect = bounds_list[bounds_idx + 2]
                                    height_rect = bounds_list[bounds_idx + 3]

                                    # Rectangle objects contain an array, get the first value
                                    x = x_rect[0] if hasattr(x_rect, '__getitem__') else float(x_rect)
                                    y = y_rect[0] if hasattr(y_rect, '__getitem__') else float(y_rect)
                                    width = width_rect[0] if hasattr(width_rect, '__getitem__') else float(width_rect)
                                    height = height_rect[0] if hasattr(height_rect, '__getitem__') else float(height_rect)

                                    layout_rect = {'x': x, 'y': y, 'width': width, 'height': height}
                                    if area_index < 3:  # Only show first 3 for debugging
                                        debug.log(f"[DOMSNAPSHOT] TR #{area_index} (node {tr_idx}): layout_idx={layout_idx}, rect={layout_rect}")
                            else:
                                if area_index < 3:
                                    debug.log(f"[DOMSNAPSHOT] TR #{area_index} (node {tr_idx}): NOT in layout.node_index")

                    # Get backend_node_id for this TR
                    tr_backend_node_id = None
                    if tr_idx < len(backend_node_ids):
                        tr_backend_node_id = backend_node_ids[tr_idx]

                    # Build area data object (matching JavaScript version format)
                    area_data = {
                        'index': area_index,
                        'id': tr_id,
                        'disabled': is_disabled,
                        'areaName': area_name,
                        'price': price,
                        'seatText': seat_text,
                        'innerHTML': f'<tr id="{tr_id}" class="{tr_class}">...mock...</tr>',  # Mock HTML for compatibility
                        'tr_node_index': tr_idx,  # Store for reference
                        'layout_rect': layout_rect,  # Store bounding box for clicking
                        'backend_node_id': tr_backend_node_id  # Store for CDP node resolution
                    }
                    areas_data.append(area_data)
                    area_index += 1

        debug.log(f"[AREA EXTRACT] Found {len(areas_data)} total areas")

    except Exception as exc:
        if debug.enabled:
            debug.log(f"[ERROR] Failed to extract area data: {exc}")
            import traceback
            traceback.print_exc()
        return True, False

    if not areas_data or len(areas_data) == 0:
        debug.log("[AREA EXTRACT] No areas found on page")
        return True, False

    # Phase 2: Filter areas (disabled, sold out, insufficient seats)
    valid_areas = []

    for area in areas_data:
        # Skip disabled areas
        if area['disabled']:
            debug.log(f"[ibon] 跳過: {area['areaName']}")
            continue

        # 同時檢查區域名稱、票價與內容
        row_text = area['areaName'] + ' ' + area.get('price', '') + ' ' + util.remove_html_tags(area['innerHTML'])

        # Skip sold out areas
        if '已售完' in area['seatText']:
            debug.log(f"[ibon] 已售完: {area['areaName']}")
            continue

        if 'disabled' in area['innerHTML'].lower() or 'sold-out' in area['innerHTML'].lower():
            debug.log(f"[ibon] 跳過: {area['areaName']}")
            continue

        # Skip description rows (not actual seat areas)
        if row_text in ["座位已被選擇", "座位已售出", "舞台區域"]:
            continue

        # Check exclude keywords
        if util.reset_row_text_if_match_keyword_exclude(config_dict, row_text):
            debug.log(f"[ibon] 排除: {area['areaName']}")
            continue

        # Check remaining seat count
        seat_text = area['seatText']
        if seat_text.isdigit():
            remaining_seats = int(seat_text)
            if remaining_seats < ticket_number:
                debug.log(f"[ibon] 座位不足: {area['areaName']} ({remaining_seats}/{ticket_number})")
                continue

        valid_areas.append(area)

    debug.log(f"[ibon] 有效區域: {len(valid_areas)}")

    # Phase 3: Keyword matching with early return pattern (T010-T016)
    matched_areas = []
    target_found = False

    if area_keyword_item and len(area_keyword_item) > 0:
        try:
            # NOTE: area_keyword_item is already a SINGLE keyword string from upper layer
            # Upper layer (line 10908) splits by comma using JSON parsing:
            #   Input: "\"5600\",\"5,600\""
            #   After JSON: ["5600", "5,600"]
            #   This function is called once per keyword: "5600" or "5,600"
            #
            # DO NOT split by comma again here, or "5,600" becomes ['5', '600'] (BUG!)
            # Only support space-separated AND logic within each keyword

            area_keyword_clean = area_keyword_item.strip()
            if area_keyword_clean.startswith('"') and area_keyword_clean.endswith('"'):
                area_keyword_clean = area_keyword_clean[1:-1]

            # Treat the entire string as a single keyword
            keyword_item = util.format_keyword_string(area_keyword_clean)

            debug.log(f"[IBON AREA KEYWORD] Checking keyword: {keyword_item}")

            # Check all areas for this keyword
            for area in valid_areas:
                row_text = area['areaName'] + ' ' + area.get('price', '') + ' ' + util.remove_html_tags(area['innerHTML'])
                row_text = util.format_keyword_string(row_text)

                # Support AND logic with space-separated sub-keywords
                # Example: "VIP 區" → ['VIP', '區'] → must match both
                sub_keywords = [kw.strip() for kw in keyword_item.split(' ') if kw.strip()]
                is_match = all(sub_kw.lower() in row_text.lower() for sub_kw in sub_keywords)

                # Collect every area this keyword hits: the select order
                # (from bottom to top / center / random) picks among them below.
                # Stopping at the first hit always took the top one (#416).
                if is_match:
                    matched_areas.append(area)
                    target_found = True

            if target_found:
                debug.log(f"[IBON AREA KEYWORD] Keyword '{keyword_item}' matched "
                          f"{len(matched_areas)} area(s): {[a['areaName'] for a in matched_areas]}")

            # All keywords failed log
            if not target_found:
                debug.log(f"[IBON AREA KEYWORD] Keyword '{keyword_item}' failed to match")
        except Exception as e:
            debug.log(f"[IBON AREA] Keyword parse error: {e}")
            debug.log(f"[IBON AREA] Treating as 'all keywords failed'")
            matched_areas = []  # Let Feature 003 fallback logic handle this
    else:
        matched_areas = valid_areas

    if not target_found:
        debug.log(f"[IBON AREA] Total matched areas: {len(matched_areas)}")

    # T022-T024: Conditional fallback based on area_auto_fallback switch
    if len(matched_areas) == 0 and area_keyword_item and len(area_keyword_item) > 0:
        if area_auto_fallback:
            # T022: Fallback enabled - use all valid areas
            debug.log(f"[IBON AREA FALLBACK] area_auto_fallback=true, triggering auto fallback")
            debug.log(f"[IBON AREA FALLBACK] Selecting available area based on area_select_order='{auto_select_mode}'")
            matched_areas = valid_areas
        else:
            # T023: Fallback disabled - strict mode (no selection, will reload)
            debug.log(f"[IBON AREA FALLBACK] area_auto_fallback=false, fallback is disabled")
            debug.log(f"[IBON AREA SELECT] No area selected, will reload page and retry")
            # T024: No available options after keyword matching failed
            if len(valid_areas) == 0:
                debug.log(f"[IBON AREA FALLBACK] No available options after exclusion")
            is_need_refresh = True
            return is_need_refresh, False

    # Phase 4: Select target area based on mode
    target_area = util.get_target_item_from_matched_list(matched_areas, auto_select_mode)

    if not target_area:
        is_need_refresh = True
        debug.log("[RESULT] Failed to select target area, refresh needed")
        return is_need_refresh, False

    # T013 equivalent: Log selected area with selection type
    if debug.enabled:
        is_keyword_match = (area_keyword_item and len(area_keyword_item) > 0 and len(matched_areas) < len(valid_areas))
        selection_type = "keyword match" if is_keyword_match else "fallback"
        debug.log(f"[IBON AREA SELECT] Selected area: {target_area['areaName']} ({selection_type})")
        debug.log(f"[TARGET] Selected area: {target_area['areaName']} (index: {target_area['index']}, id: {target_area['id']})")

    # Phase 5: Click target area using CDP real-time coordinates
    try:
        # cdp already imported at file start (Line 29)

        # Get backend_node_id from target area
        backend_node_id = target_area.get('backend_node_id')

        if not backend_node_id:
            debug.log(f"[CDP CLICK] No backend_node_id available for TR")
        else:
            # Request document first (required for pushNodesByBackendIdsToFrontend)
            try:
                document = await tab.send(cdp.dom.get_document(depth=-1, pierce=True))
            except Exception as doc_exc:
                debug.log(f"[CDP CLICK] Document request failed: {doc_exc}")
                return is_need_refresh, is_price_assign_by_bot

            # Convert backend_node_id to node_id using pushNodesByBackendIdsToFrontend
            try:
                result = await tab.send(cdp.dom.push_nodes_by_backend_ids_to_frontend(backend_node_ids=[backend_node_id]))
                node_ids = result if isinstance(result, list) else (result.node_ids if hasattr(result, 'node_ids') else [])

                if not node_ids or len(node_ids) == 0:
                    debug.log(f"[CDP CLICK] Failed to convert backend_node_id to node_id")
                    return is_need_refresh, is_price_assign_by_bot

                node_id = node_ids[0]

                # Scroll into view
                try:
                    await tab.send(cdp.dom.scroll_into_view_if_needed(node_id=node_id))
                except Exception:
                    pass  # Scroll not always needed

                # Focus element (ignore focus warnings)
                try:
                    await tab.send(cdp.dom.focus(node_id=node_id))
                except Exception:
                    pass  # Element may not be focusable

                # Get real-time box model and click
                box_model = await tab.send(cdp.dom.get_box_model(node_id=node_id))
                content_quad = box_model.content if hasattr(box_model, 'content') else box_model.model.content
                x = (content_quad[0] + content_quad[2]) / 2
                y = (content_quad[1] + content_quad[5]) / 2

                # Execute mouse click
                live = await _ibon_snapshot(tab)
                if not live or live['doc'] != entry['doc'] or await _ibon_actions_paused(config_dict):
                    return (False, False)
                await tab.mouse_click(x, y)

                # Wait for navigation
                await tab.sleep(random.uniform(0.5, 0.8))

                is_price_assign_by_bot = True

                debug.log(f"[CLICK SUCCESS] Clicked area: {target_area['areaName']} (id: {target_area['id']})")

            except Exception as resolve_exc:
                if debug.enabled:
                    debug.log(f"[CDP CLICK] Resolve/click failed: {resolve_exc}")
                    import traceback
                    traceback.print_exc()

    except Exception as exc:
        if debug.enabled:
            debug.log(f"[CLICK ERROR] Exception during click: {exc}")
            import traceback
            traceback.print_exc()

    return is_need_refresh, is_price_assign_by_bot

CONST_IBON_FORM_JS = r'''
const doc=location.href+'|'+performance.timeOrigin,key='ticketsHunter.ibon.submit';
const input=document.querySelector('input[placeholder*="驗證碼"],input[value="驗證碼"],#ctl00_ContentPlaceHolder1_CHK');
const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
const area=(root=document)=>root.querySelector('#ctl00_ContentPlaceHolder1_PRICE_STR,span[id$="_PRICE_STR"]')?.textContent.trim()||'';
const rows=(root=document,detached=false)=>{
    let items=Array.from(root.querySelectorAll('table.rwdtable tbody tr'));
    if(!items.length)items=Array.from(root.querySelectorAll('table.table tbody tr'));
    if(!items.length)items=Array.from(root.querySelectorAll('select#input-qty,select[id*="qty"]'));
    return items.map((row,index)=>{
        const select=row.matches('select')?row:row.querySelector('select.form-control-sm,select[name*="AMOUNT_DDL"]');
        if(!select)return null;
        const shown=detached?!select.closest('[hidden]')&&!Array.from(select.closest('form')?.querySelectorAll('[style]')||[]).some(e=>e.contains(select)&&/display\s*:\s*none|visibility\s*:\s*hidden/i.test(e.getAttribute('style'))):visible(select);
        return {index,name:(row.matches('select')?root.querySelectorAll('h4')[index]:row.querySelector('td:first-child'))?.textContent.trim()||'',
            price:row.matches('select')?'':row.querySelector('td:nth-child(3)')?.textContent.trim()||'',
            id:select.id||select.name||'',field:select.name||'',value:select.value,enabled:!select.disabled&&shown,
            options:Array.from(select.options).filter(o=>!o.disabled&&!o.hidden&&!o.parentElement?.disabled).map(o=>o.value)};
    }).filter(Boolean);
};
'''

CONST_IBON_API_REPLY_JS = r'''
const parseReply=(text,status)=>{
    const classify=message=>{
        if(/驗證碼|captcha|recaptcha|turnstile/i.test(message))return 'captcha';
        if(/VIEWSTATE|EVENTVALIDATION|狀態資訊|狀態驗證|事件驗證/i.test(message))return 'form_state';
        if(/登入|login|sign.in|session.*expir|操作逾時|連線逾時/i.test(message))return 'session';
        if(/售完|售罄|無可配|票券不足|數量不足|已被購買/.test(message))return 'stock';
        if(/張數|購票數量|購買數量|購票上限|限購/.test(message))return 'quantity';
        return 'site_message';
    };
    const fallback=(reason,format='unknown',category=null)=>({kind:'unknown',status,reason,format,
        response_chars:text.length,message_class:category});
    if(status!==200)return fallback('http');
    let cursor=0;const records=[];
    try{
        while(cursor<text.length){
            const read=()=>{const end=text.indexOf('|',cursor);if(end<0)throw Error();const v=text.slice(cursor,end);cursor=end+1;return v;};
            const length=read();if(!/^\d+$/.test(length))throw Error();
            const size=Number(length),kind=read(),id=read(),end=cursor+size;
            if(size>2000000||text[end]!=='|')throw Error();
            records.push({kind,id,body:text.slice(cursor,end)});cursor=end+1;
        }
        if(!records.length||records[0].kind!=='#')throw Error();
    }catch(_){
        const html=/^\s*(?:<!doctype|<html|<head|<body)/i.test(text);
        const cloudflare=/challenge-platform|cf-chl-|Just a moment|驗證您是否是人類/i.test(text);
        return fallback('protocol',html?'html':'unknown',cloudflare?'cloudflare':null);
    }
    const errors=records.filter(r=>r.kind==='error');
    const messages=records.filter(r=>r.kind==='scriptStartupBlock'&&/(?:alert|_ev)\s*\(/.test(r.body));
    const category=errors.length?classify(errors.map(r=>r.body).join('\n')):
        messages.length?classify(messages.map(r=>r.body).join('\n')):null;
    const paths=new Set();
    const accept=value=>{
        try{
            const target=new URL(value,location.href);
            if(target.origin===location.origin&&/^\/application\/UTK02\/UTK0206_\.aspx$/i.test(target.pathname)&&!target.search&&!target.hash)
                paths.add(target.pathname);
        }catch(_){}
    };
    for(const record of records){
        if(record.kind==='pageRedirect'){
            try{accept(decodeURIComponent(record.body));}catch(_){}
        }
        if(record.kind==='scriptStartupBlock'){
            const pattern=/(?:top\.|window\.)?location\.replace\(\s*(['"])([^'"\r\n]+)\1\s*\)/g;
            for(const match of record.body.matchAll(pattern))accept(match[2]);
        }
    }
    const scripts=records.filter(r=>r.kind==='scriptStartupBlock');
    const refused=scripts.length>0&&scripts.every(r=>{
        const match=r.body.trim().match(/^(?:window\.)?alert\(\s*(['"])([^'"\\\r\n]*)\1\s*\)\s*;?$/);
        return match&&/已售完|售罄|無可配座位|票券不足|數量不足|別人搶先一步|已被購買/.test(match[2]);
    });
    if(paths.size===1&&!errors.length&&!messages.length)return {kind:'checkout',status,path:Array.from(paths)[0],format:'aspnet_delta'};
    if(!paths.size&&!errors.length&&refused&&!records.some(r=>r.kind==='pageRedirect'))
        return {kind:'rejected',status,reason:'stock',format:'aspnet_delta',message_class:'stock'};
    return fallback(errors.length?'server_error':'unconfirmed','aspnet_delta',category);
};
'''

CONST_IBON_API_POST_JS = r'''((expected,targetRow,amount,allowSplit,nonce,timeout) => {
    const commandAt=performance.timeOrigin+performance.now();
    FORM
    PARSE_REPLY
    const fail=reason=>({started:false,reason});
    if(document.readyState==='loading'||/^(?:Just a moment|請稍候|正在驗證|Checking your browser)/i.test(document.title.trim())||
        document.querySelector('#challenge-form,#challenge-running,#challenge-stage,.cf-browser-verification'))return fail('not_ready');
    const cached=expected.detached?window[Symbol.for('ticketsHunter.ibon.form')]:null;
    if(expected.detached&&(!cached||cached.doc!==doc||cached.url!==expected.url||cached.token!==expected.form_token))
        return fail('changed');
    const root=cached?.form||document,detached=!!cached;
    if(doc!==expected.doc||area(root)!==(expected.area||'')||JSON.stringify(rows(root,detached))!==JSON.stringify(expected.rows))
        return fail('changed');
    if(sessionStorage.getItem(key)||input)return fail('pending');
    const form=cached?.form||document.forms.namedItem('aspnetForm');
    if(!form)return fail('form');
    if(!form.checkValidity())return fail('validation');
    const action=new URL(form.getAttribute('action')||expected.url,expected.url),current=new URL(expected.url);
    if(action.origin!==location.origin||action.pathname!==current.pathname||action.search!==current.search||
        !/\/UTK0201_001\.aspx$/i.test(action.pathname))return fail('form');
    const target='ctl00$ContentPlaceHolder1$A2';
    const manager=window.Sys?.WebForms?.PageRequestManager?.getInstance?.();
    const managerID=cached?.managerID||manager?._scriptManagerID;
    if(managerID!=='ctl00$ScriptManager1')return fail('not_ready');
    if(manager?.get_isInAsyncPostBack?.())return fail('busy');
    const payload=new URLSearchParams();
    for(const [name,value] of new FormData(form)){if(typeof value!=='string')return fail('form');payload.append(name,value);}
    if(allowSplit){
        const check=form.elements.namedItem('ctl00$ContentPlaceHolder1$notConsecutive');
        if(check?.type==='checkbox'&&!check.disabled)payload.set(check.name,check.value||'on');
    }
    const currentRows=rows(root,detached);
    const selected=currentRows.find(row=>row.index===targetRow.index&&row.id===targetRow.id&&row.field===targetRow.field);
    if(!selected?.enabled||!selected.options.includes(amount)||!selected.field||!payload.has(selected.field))
        return fail('changed');
    for(const row of currentRows){
        const value=row.index===selected.index?amount:row.options.includes('0')?'0':row.options.includes('')?'':null;
        if(value===null||!row.field)return fail('changed');
        if(payload.has(row.field))payload.set(row.field,value);
        else if(value!==''&&value!=='0')return fail('changed');
    }
    const quantities=Array.from(payload).filter(([name,value])=>name.endsWith('$AMOUNT_DDL')&&value!==''&&value!=='0');
    if(quantities.length!==1||quantities[0][0]!==selected.field||quantities[0][1]!==amount)return fail('changed');
    for(const name of ['__VIEWSTATE','__EVENTVALIDATION'])if(!payload.get(name))return fail('state');
    if(payload.get('ctl00$ContentPlaceHolder1$PERFORMANCE_ID')!==current.searchParams.get('PERFORMANCE_ID')||
        payload.get('ctl00$ContentPlaceHolder1$PERFORMANCE_PRICE_AREA_ID')!==current.searchParams.get('PERFORMANCE_PRICE_AREA_ID'))
        return fail('changed');
    payload.set(managerID,'ctl00$ContentPlaceHolder1$AjaxPanel|'+target);
    payload.set('__EVENTTARGET',target);payload.set('__EVENTARGUMENT','');payload.set('__ASYNCPOST','true');
    const retry=JSON.parse(sessionStorage.getItem(key+'.retry')||'null');
    if(retry?.requireForm&&!detached)return fail('refresh');
    if(retry?.nextAt>performance.timeOrigin+performance.now())return fail('cooldown');
    const record={doc,route:location.href,nonce,state:'pending',captcha:'',phase:'ticket_api'};
    try{sessionStorage.setItem(key,JSON.stringify(record));}catch(_){return fail('storage');}
    const controller=new AbortController();
    record.abort=()=>controller.abort();window[Symbol.for(key)]=record;
    for(const row of currentRows){
        const select=form.elements.namedItem(row.field);
        if(select?.tagName==='SELECT')select.value=payload.get(row.field)||'0';
    }
    form.inert=true;
    if(detached){
        const sourceForm=document.forms.namedItem('aspnetForm');if(sourceForm)sourceForm.inert=true;
        delete window[Symbol.for('ticketsHunter.ibon.form')];
    }
    const publish=result=>{
        try{
            const saved=JSON.parse(sessionStorage.getItem(key)||'null'),active=window[Symbol.for(key)];
            if(!saved||saved.nonce!==nonce||active?.nonce!==nonce||active.stopped||doc!==location.href+'|'+performance.timeOrigin)return;
            saved.apiResult=result;sessionStorage.setItem(key,JSON.stringify(saved));
            window.dispatchEvent(new CustomEvent('ticketsHunter.ibon.result',{detail:{nonce}}));
        }catch(_){}
    };
    const requestBody=payload.toString();let sentAt=null;
    void (async()=>{
        const started=performance.now(),timer=setTimeout(()=>controller.abort(),timeout);
        sentAt=performance.timeOrigin+started;
        try{
            const response=await fetch(action.href,{method:'POST',credentials:'same-origin',redirect:'error',
                headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                         'X-MicrosoftAjax':'Delta=true','X-Requested-With':'XMLHttpRequest'},
                body:requestBody,signal:controller.signal});
            const headersAt=performance.now();
            const text=await response.text();
            const bodyAt=performance.now();
            const result=text.length<=2000000?parseReply(text,response.status):{kind:'unknown',status:response.status,reason:'protocol'};
            const completedAt=performance.now();
            publish({...result,elapsed_ms:completedAt-started,request_ms:bodyAt-started,
                     headers_ms:headersAt-started,body_ms:bodyAt-headersAt,parse_ms:completedAt-bodyAt,
                     sent_at_ms:sentAt,completed_at_ms:performance.timeOrigin+completedAt});
        }catch(error){
            publish({kind:'unknown',status:0,reason:error.name==='AbortError'?'timeout':'network',
                     elapsed_ms:performance.now()-started,sent_at_ms:sentAt,
                     completed_at_ms:performance.timeOrigin+performance.now()});
        }finally{clearTimeout(timer);}
    })();
    return {started:true,timing:{build_ms:sentAt===null?null:sentAt-commandAt}};
})(SNAPSHOT,TARGET,AMOUNT,ALLOW_SPLIT,NONCE,TIMEOUT)'''

CONST_IBON_FORM_FETCH_JS = r'''(async (endpoint,expected,timeout)=>{
    FORM
    if(doc!==expected||sessionStorage.getItem(key))return {discarded:true};
    const target=new URL(endpoint);
    if(target.origin!==location.origin||!/\/UTK0201_001\.aspx$/i.test(target.pathname))return null;
    const slot=Symbol.for('ticketsHunter.ibon.form'),token=crypto.randomUUID();
    window[slot]={doc,url:target.href,token};
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout),started=performance.now();
    try{
        const response=await fetch(target.href,{method:'GET',credentials:'same-origin',cache:'no-store',
            redirect:'error',headers:{Accept:'text/html'},signal:controller.signal});
        const headersAt=performance.now();
        if(!response.ok)return null;
        const text=await response.text(),completed=performance.now();
        if(doc!==location.href+'|'+performance.timeOrigin||window[slot]?.token!==token)return {discarded:true};
        if(text.length>2000000)return null;
        const template=document.createElement('template');template.innerHTML=text;
        const content=template.content,forms=content.querySelectorAll('form#aspnetForm'),form=forms[0];
        const scripts=Array.from(content.querySelectorAll('script:not([src])')).map(s=>s.textContent).join('\n');
        if(forms.length!==1||!/PageRequestManager\._initialize\(\s*(['"])ctl00\$ScriptManager1\1\s*,\s*(['"])aspnetForm\2\s*,/.test(scripts))
            return null;
        for(const element of [form,...form.querySelectorAll('*')]){
            for(const attribute of Array.from(element.attributes))if(/^on/i.test(attribute.name))element.removeAttribute(attribute.name);
        }
        if(form.querySelector('input[placeholder*="驗證碼"],input[value="驗證碼"],#ctl00_ContentPlaceHolder1_CHK,img[src*="pic.aspx"],.g-recaptcha,.cf-turnstile,iframe')||
            /(?:grecaptcha|turnstile)\.(?:execute|render)/i.test(scripts))return null;
        for(const name of ['__VIEWSTATE','__EVENTVALIDATION']){
            if(!form.elements.namedItem(name)?.value)return null;
        }
        if(form.elements.namedItem('ctl00$ContentPlaceHolder1$PERFORMANCE_ID')?.value!==target.searchParams.get('PERFORMANCE_ID')||
            form.elements.namedItem('ctl00$ContentPlaceHolder1$PERFORMANCE_PRICE_AREA_ID')?.value!==target.searchParams.get('PERFORMANCE_PRICE_AREA_ID'))
            return null;
        const formRows=rows(form,true);if(!formRows.length)return null;
        window[slot]={doc,url:target.href,token,form,managerID:'ctl00$ScriptManager1'};
        return {doc,url:target.href,source_url:location.href,area:area(form),rows:formRows,validForm:form.checkValidity(),
            hasCaptcha:false,ready:'complete',verifying:false,detached:true,form_token:token,
            navigation:{html_ms:completed-started,first_byte_ms:headersAt-started,dom_ready_ms:null}};
    }catch(_){return null;}finally{clearTimeout(timer);}
})(ENDPOINT,EXPECTED,TIMEOUT)'''

CONST_IBON_RESULT_WAIT_JS = r'''((nonce,expected,timeout)=>new Promise(resolve=>{
    const key='ticketsHunter.ibon.submit';let timer;
    const finish=value=>{
        clearTimeout(timer);window.removeEventListener('ticketsHunter.ibon.result',receive);resolve(value);
    };
    const receive=()=>{
        try{
            const doc=location.href+'|'+performance.timeOrigin,saved=JSON.parse(sessionStorage.getItem(key)||'null');
            if(doc!==expected||!saved||saved.nonce!==nonce)return finish(null);
            if(saved.apiResult)finish({doc,saved,browser_now_ms:performance.timeOrigin+performance.now(),event_driven:true});
        }catch(_){finish(null);}
    };
    window.addEventListener('ticketsHunter.ibon.result',receive);
    timer=setTimeout(()=>finish(null),timeout);receive();
}))(NONCE,EXPECTED,TIMEOUT)'''


async def _ibon_actions_paused(config_dict=None):
    return await check_and_handle_pause(config_dict) or await check_and_handle_quit(config_dict)


def _ibon_render(script, **parameters):
    values = {'FORM': CONST_IBON_FORM_JS, 'NATIVE': CONST_NATIVE_INPUT_SETTER_JS,
              'PARSE_REPLY': CONST_IBON_API_REPLY_JS, 'DATE_ROWS': CONST_IBON_DATE_ROWS_JS}
    values.update({key: json.dumps(value) for key, value in parameters.items()})
    return re.sub(r'\b[A-Z][A-Z_]*\b', lambda match: values.get(match.group(), match.group()), script)


def _ibon_is_checkout(url):
    from urllib.parse import urlsplit
    path = urlsplit(url or '').path.lower()
    return bool(re.search(r'/utk02/utk020[67]_[^/]*\.aspx$', path) or
                re.fullmatch(r'/event/[^/]+/checkout/?', path))


async def _ibon_snapshot(tab):
    try:
        result = await tab.evaluate(r'''(() => {
            FORM
            const invalid=Array.from(document.forms).some(f=>f.querySelector('select.form-control-sm,select[name*="AMOUNT_DDL"],select[id*="qty"]')&&f.matches(':invalid'));
            const verifying=/^(?:Just a moment|請稍候|正在驗證|Checking your browser)/i.test(document.title.trim())||
                !!document.querySelector('#challenge-form,#challenge-running,#challenge-stage,.cf-browser-verification');
            const nav=performance.getEntriesByType('navigation')[0];
            const navigation=nav?{html_ms:nav.responseEnd>0?nav.responseEnd:null,
                first_byte_ms:nav.responseStart>0?nav.responseStart-nav.requestStart:null,
                dom_ready_ms:nav.domContentLoadedEventEnd>0?nav.domContentLoadedEventEnd:null}:null;
            const retry=JSON.parse(sessionStorage.getItem(key+'.retry')||'null');
            return {doc,url:location.href,referrer:document.referrer,ready:document.readyState,verifying,area:area(),validForm:!invalid,rows:rows(),hasCaptcha:!!input,needsFreshForm:!!retry?.requireForm,
                value:input?.value||'',inputReady:visible(input)&&!input.disabled,navigation};
        })()'''.replace('FORM', CONST_IBON_FORM_JS))
        return result if isinstance(result, dict) and result.get('doc') else None
    except Exception:
        return None


def _ibon_origin(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url or '')
    return parsed.scheme + '://' + parsed.netloc.rsplit('@', 1)[-1] if parsed.scheme in ('http', 'https') else ''


def _ibon_flow_path(tab):
    import hashlib
    target = getattr(getattr(tab, 'target', None), 'target_id', None)
    if not target:
        raise ValueError('無法確認 ibon 分頁身分。')
    name = hashlib.sha256(str(target).encode('utf8')).hexdigest() + '.json'
    return util.get_instance_state_path('MAXBOT_IBON_FLOW_' + name)


def _ibon_read_flow(tab):
    try:
        with open(_ibon_flow_path(tab), encoding='utf8') as source:
            value = json.loads(source.read(4097))
        return value if isinstance(value, dict) else {'handoff': False}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {'handoff': False}


def _ibon_remove_flow(tab, nonce):
    current = _ibon_read_flow(tab)
    if current and current.get('nonce') == nonce:
        try:
            os.unlink(_ibon_flow_path(tab))
        except OSError:
            pass


def _ibon_reset_token():
    return util.get_browser_session_generation()


async def _ibon_reset_disconnected(tab, config_dict):
    token = _ibon_reset_token()
    if not token:
        return False
    flow = _ibon_read_flow(tab)
    if not flow or token == flow.get('reset_token') or not flow.get('nonce'):
        return False
    try:
        result = await tab.evaluate(_ibon_render(r'''((expected) => {
            FORM
            if(location.origin!==expected.origin)return {away:true};
            let saved=null;try{saved=JSON.parse(sessionStorage.getItem(key)||'null');}catch(_){}
            const active=window[Symbol.for(key)];
            if(saved&&saved.nonce!==expected.nonce||active&&active.nonce!==expected.nonce)return null;
            if(active){active.stopped=true;if(typeof active.abort==='function')active.abort();}
            if(active&&typeof Sys!=='undefined'){
                const manager=Sys.WebForms?.PageRequestManager?.getInstance?.();
                if(manager?.get_isInAsyncPostBack?.())manager.abortPostBack?.();
            }
            sessionStorage.removeItem(key);delete window[Symbol.for(key)];
            delete window[Symbol.for('ticketsHunter.ibon.form')];
            const form=document.forms.namedItem('aspnetForm');if(form)form.inert=false;
            sessionStorage.setItem(key+'.retry',JSON.stringify({count:0,nextAt:0,requireForm:true}));
            window.dispatchEvent(new CustomEvent('ticketsHunter.ibon.result',{detail:{nonce:expected.nonce}}));
            return {cleared:true};
        })(EXPECTED)''', EXPECTED=flow))
    except Exception:
        return False
    if not isinstance(result, dict) or not (result.get('cleared') or result.get('away')):
        return False
    if result.get('cleared'):
        _ibon_remove_flow(tab, flow['nonce'])
    for key in ('pending_notice', 'api_result_notice', 'api_following_nonce', 'captcha_fill', 'stock_poll', 'booking_timing'):
        _state.pop(key, None)
    notice = (flow['nonce'], token)
    if _state.get('disconnect_reset_notice') != notice:
        _state['disconnect_reset_notice'] = notice
        util.create_debug_logger(config_dict, enabled=True).log(
            '[IBON] 已依「斷開瀏覽器」解除本輪本機送出鎖定；重新開始可沿用原分頁。已送達網站的請求無法撤回，購物車與訂單請以網站為準。')
    return True


async def nodriver_ibon_disconnect(tab, config_dict=None):
    """Explicit disconnect ends this run; pause and unexpected failures retain their guards."""
    if tab is None:
        return
    await _ibon_reset_disconnected(tab, config_dict)


def _ibon_recovery_policy(config_dict):
    import math
    advanced = config_dict.get('advanced', {})
    try:
        interval = float(advanced.get('auto_reload_page_interval', 1))
    except (TypeError, ValueError):
        interval = 1
    if not math.isfinite(interval) or interval <= 0:
        interval = 1
    limit = advanced.get('ibon_submit_recovery_limit', 3)
    if type(limit) is not int or not 1 <= limit <= 20:
        limit = 3
    return interval, limit


async def _ibon_recover_api_rejection(tab, config_dict, saved, flow):
    if (not flow or flow.get('nonce') != saved.get('nonce') or
            flow.get('origin') != _ibon_origin(saved.get('route')) or await _ibon_actions_paused(config_dict)):
        return False
    interval, limit = _ibon_recovery_policy(config_dict)
    try:
        result = await tab.evaluate(_ibon_render(r'''((nonce,interval,limit,generation) => {
            FORM
            const saved=JSON.parse(sessionStorage.getItem(key)||'null'),active=window[Symbol.for(key)];
            const reply=saved?.apiResult;
            if(!saved||saved.nonce!==nonce||active&&active.nonce!==nonce||reply?.kind!=='rejected'||
                reply.status!==200||reply.reason!=='stock'||reply.format!=='aspnet_delta')return null;
            let retry=JSON.parse(sessionStorage.getItem(key+'.retry')||'null');
            if(retry?.generation!==generation)retry=null;
            const count=Number.isInteger(retry?.count)&&retry.count>=0?retry.count:0;
            if(count>=limit)return {limit:true,count};
            sessionStorage.setItem(key+'.retry',JSON.stringify({count:count+1,
                nextAt:performance.timeOrigin+performance.now()+interval*1000,requireForm:true,generation}));
            sessionStorage.removeItem(key);delete window[Symbol.for(key)];
            delete window[Symbol.for('ticketsHunter.ibon.form')];
            const form=document.forms.namedItem('aspnetForm');if(form)form.inert=false;
            return {cleared:true,count:count+1};
        })(NONCE,INTERVAL,LIMIT,GENERATION)''', NONCE=saved['nonce'], INTERVAL=interval,
            LIMIT=limit, GENERATION=_ibon_reset_token()))
    except Exception:
        return False
    if not isinstance(result, dict):
        return False
    debug = util.create_debug_logger(config_dict, enabled=True)
    if result.get('cleared'):
        _ibon_remove_flow(tab, saved['nonce'])
        _state.pop('stock_poll', None)
        _state.pop('booking_timing', None)
        message = '網站已明確回覆票券不足，已自動解除本輪鎖定；' + format(interval, '.3f') + ' 秒後重新讀取表單與票況（自動恢復 ' + str(result['count']) + '/' + str(limit) + '）。'
        debug.log('[IBON 自動恢復] ' + message)
        util.write_instance_status('running', message)
        return True
    if result.get('limit') and _state.get('recovery_limit_notice') != saved['nonce']:
        _state['recovery_limit_notice'] = saved['nonce']
        message = '連續自動恢復已達上限，暫停重送；可按「斷開瀏覽器」結束本輪後重新開始。'
        debug.log('[IBON 自動恢復] ' + message)
        util.write_instance_status('waiting_manual', message)
    return False


async def _ibon_clear_completed(tab, saved, flow):
    if not flow or flow.get('handoff') is not True or saved.get('nonce') != flow.get('nonce'):
        return False
    if _ibon_origin(saved.get('route')) != flow.get('origin'):
        return False
    try:
        cleared = await tab.evaluate(_ibon_render(r'''((nonce) => {
            FORM
            const saved=JSON.parse(sessionStorage.getItem(key)||'null');
            if(!saved||saved.nonce!==nonce)return false;
            sessionStorage.removeItem(key);delete window[Symbol.for(key)];return true;
        })(EXPECTED)''', EXPECTED=flow['nonce']))
        if cleared:
            _ibon_remove_flow(tab, flow['nonce'])
        return bool(cleared)
    except Exception:
        return False


def _ibon_candidates(snapshot, config_dict):
    requested = str(config_dict.get('ticket_number', 2))
    if not re.fullmatch(r'[1-9]\d{0,2}', requested):
        return []
    # Stage 6: retain the selected area when matching the ticket-type rows.
    def label(row):
        return ' '.join(str(value or '') for value in (snapshot.get('area'), row.get('name'), row.get('price'))).strip()

    rows = [row for row in snapshot.get('rows', []) if row.get('enabled') and
            requested in row.get('options', []) and row.get('name') and
            not util.reset_row_text_if_match_keyword_exclude(config_dict, label(row))]
    keyword = config_dict.get('area_auto_select', {}).get('area_keyword', '').strip()
    if not keyword:
        return rows
    for group in util.parse_keyword_string_to_array(keyword):
        parts = [util.format_keyword_string(part) for part in group.split()]
        matched = [row for row in rows if parts and all(
            part in util.format_keyword_string(label(row)) for part in parts)]
        if matched:
            return matched
    return rows if config_dict.get('area_auto_fallback', False) else []


def _ibon_selection_ok(snapshot, config_dict):
    if not snapshot:
        return False
    selected = [row for row in snapshot.get('rows', []) if row.get('value') not in ('0', '')]
    allowed = _ibon_candidates(snapshot, config_dict)
    return (len(selected) == 1 and selected[0]['value'] == str(config_dict.get('ticket_number', 2)) and
            any(row['index'] == selected[0]['index'] and row['id'] == selected[0]['id'] for row in allowed))


async def _ibon_capture(tab, config_dict):
    import base64
    import hashlib
    before = await _ibon_snapshot(tab)
    if not before or not before.get('inputReady') or await _ibon_actions_paused(config_dict):
        return None
    try:
        encoded = await tab.evaluate(r'''(() => {
            const img=document.querySelector('img[src*="pic.aspx"],img[src*="captcha"],img[id*="Captcha"]');
            const canvas=img?document.createElement('canvas'):document.querySelector('canvas');
            if(!canvas||img&&(!img.complete||!img.naturalWidth))return null;
            if(img){canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;canvas.getContext('2d').drawImage(img,0,0);}
            return canvas.toDataURL('image/png').split(',')[1];
        })()''')
        image = base64.b64decode(encoded) if isinstance(encoded, str) and encoded else None
    except Exception:
        image = None
    if not image and not await _ibon_actions_paused(config_dict):
        image = await nodriver_ibon_get_captcha_image_from_shadow_dom(tab, config_dict, wait_for_render=False)
    after = await _ibon_snapshot(tab)
    if not image or not after or before['doc'] != after['doc'] or await _ibon_actions_paused(config_dict):
        return None
    return {'doc': after['doc'], 'hash': hashlib.sha256(image).hexdigest(), 'image': image}


async def _ibon_clear_rejected(tab, record):
    if await _ibon_actions_paused():
        return False
    owned = _state.get('captcha_fill', {})
    expected = dict(record, clearValue=owned.get('value') if owned.get('doc') == record.get('doc') else None)
    try:
        return bool(await tab.evaluate(_ibon_render(r'''((expected) => {
            FORM
            const saved=JSON.parse(sessionStorage.getItem(key)||'null');
            if(!saved||saved.nonce!==expected.nonce||saved.state!=='rejected')return false;
            if(doc===saved.doc&&typeof expected.clearValue==='string'&&input?.value===expected.clearValue){
                NATIVE
                setNativeInputValue(input,'');
            }
            sessionStorage.removeItem(key);delete window[Symbol.for(key)];return true;
        })(EXPECTED)''', EXPECTED=expected)))
    except Exception:
        return False


async def _ibon_submission_pending(tab, config_dict):
    if await _ibon_reset_disconnected(tab, config_dict):
        return False
    try:
        record = await tab.evaluate(r'''(() => {
            FORM
            const saved=JSON.parse(sessionStorage.getItem(key)||'null');
            const tourReady=saved?.phase==='tour_cart'&&saved.doc===doc&&/\/event\/[^/]+\/options\/?$/i.test(location.pathname)&&
                Array.from(document.querySelectorAll('button')).some(e=>visible(e)&&!e.disabled&&e.textContent.includes('確認付款方式'));
            const retry=JSON.parse(sessionStorage.getItem(key+'.retry')||'null');
            return {doc,saved,tourReady,retry,browser_now_ms:performance.timeOrigin+performance.now()};
        })()'''.replace('FORM', CONST_IBON_FORM_JS))
    except Exception:
        return True
    if not isinstance(record, dict) or not record.get('doc'):
        return True
    saved = record.get('saved')
    flow = _ibon_read_flow(tab)
    if saved is not None and (not isinstance(saved, dict) or not isinstance(saved.get('nonce'), str)):
        return True
    if record.get('tourReady'):
        return False
    if saved and await _ibon_clear_completed(tab, saved, flow):
        return False
    if not saved and flow and flow.get('handoff') is not True:
        notice = ('unconfirmed_flow', flow.get('nonce'))
        if _state.get('pending_notice') != notice:
            _state['pending_notice'] = notice
            message = ('上一筆 ibon 送出尚未確認，保留防重送狀態。請先查看購物車與訂單；'
                       '按「斷開瀏覽器」可結束本輪並解除本機鎖定，再於原分頁重新開始。')
            util.create_debug_logger(config_dict, enabled=True).log('[IBON] ' + message)
            util.write_instance_status('waiting_manual', message)
        return True
    if saved and saved.get('phase') == 'ticket_api':
        return await _ibon_api_follow_up(tab, config_dict, record['doc'], saved, flow, record.get('browser_now_ms'))
    if saved and saved.get('state') == 'rejected':
        clear = saved.get('reason') == 'stock'
        if not clear and time.monotonic() >= _state.get('rejected_check_after', 0):
            _state['rejected_check_after'] = time.monotonic() + 0.25
            frame = await _ibon_capture(tab, config_dict)
            clear = bool(frame and (frame['doc'] != saved.get('doc') or frame['hash'] != saved.get('captcha')))
        if clear and await _ibon_clear_rejected(tab, saved):
            _ibon_remove_flow(tab, saved.get('nonce'))
            _state.pop('pending_notice', None)
            _state.pop('captcha_fill', None)
            util.write_instance_status('running', '網站已明確拒絕上一筆，繼續偵測票況；尚未確認訂單完成。')
            return False
    if saved and _state.get('pending_notice') != saved.get('nonce'):
        _state['pending_notice'] = saved.get('nonce')
        message = '上一筆送出尚待網站確認，停止重送；驗證碼被明確拒絕後需先換圖再繼續。'
        util.create_debug_logger(config_dict).log('[IBON] ' + message)
        util.write_instance_status('running', message)
    retry = record.get('retry')
    cooling = isinstance(retry, dict) and isinstance(retry.get('nextAt'), (int, float)) and retry['nextAt'] > record.get('browser_now_ms', 0)
    return bool(saved or flow and flow.get('handoff') is not True or cooling)


async def _ibon_mark_rejected(tab, reason):
    try:
        await tab.evaluate(r'''((reason) => {
            FORM
            const saved=JSON.parse(sessionStorage.getItem(key)||'null'),active=window[Symbol.for(key)];
            if(!saved||!active||saved.nonce!==active.nonce||saved.doc!==doc)return;
            saved.state='rejected';saved.reason=reason;sessionStorage.setItem(key,JSON.stringify(saved));
        })(REASON)'''.replace('FORM', CONST_IBON_FORM_JS).replace('REASON', json.dumps(reason)))
    except Exception:
        pass


async def _ibon_handoff(tab, config_dict):
    live = await _ibon_snapshot(tab)
    if not live or not _ibon_is_checkout(live['url']):
        return False
    checkout_seen_at = time.perf_counter()
    flow = _ibon_read_flow(tab)
    same_origin = bool(flow and flow.get('origin') == _ibon_origin(live['url']))
    if flow and (same_origin or flow.get('origin') == _ibon_origin(live.get('referrer'))):
        flow = dict(flow, handoff=True)
        if not util.save_json(flow, _ibon_flow_path(tab)):
            return False
    try:
        cleared = await tab.evaluate(r'''((expected) => {
            FORM
            if(doc!==expected)return false;
            sessionStorage.removeItem(key);sessionStorage.removeItem(key+'.retry');
            delete window[Symbol.for(key)];return true;
        })(EXPECTED)'''.replace('FORM', CONST_IBON_FORM_JS).replace('EXPECTED', json.dumps(live['doc'])))
        if not cleared:
            return False
    except Exception:
        return False
    if flow and same_origin:
        _ibon_remove_flow(tab, flow['nonce'])
    if not _state.get('handoff_ready'):
        _state['handoff_ready'] = True
        await _ibon_timing_mark(config_dict, 'checkout_seen', '程式確認進入結帳頁（' + _ibon_page_timing(live) + '）', at=checkout_seen_at)
        await _ibon_timing_summary(config_dict)
        message = '已進入 ibon 結帳頁，請手動確認資料與付款；尚未確認訂單或付款完成。'
        util.create_debug_logger(config_dict, enabled=True).log('[IBON] ' + message)
        util.write_instance_status('waiting_manual', message)
        if config_dict.get('advanced', {}).get('play_sound', {}).get('order'):
            play_sound_while_ordering(config_dict)
        notice = dict(config_dict)
        notice['advanced'] = dict(config_dict.get('advanced', {}), discord_message=message, telegram_message=message)
        send_discord_notification(notice, 'order', 'iBon')
        send_telegram_notification(notice, 'order', 'iBon')
    return True


def _ibon_build_ocr(config_dict):
    model = create_universal_ocr(config_dict)
    return model if model is not None else create_builtin_ocr(
        beta=config_dict.get('ocr_captcha', {}).get('beta', True), ranges=0)


async def _ibon_cached_ocr(config_dict, supplied=None):
    cfg = config_dict.get('ocr_captcha', {})
    if not cfg.get('enable'):
        return None
    key = tuple(cfg.get(name) for name in ('use_universal', 'beta', 'path'))
    if _state.get('ocr_key') != key:
        first = 'ocr_key' not in _state
        _state['ocr_key'] = key
        try:
            _state['ocr_model'] = supplied if first and supplied is not None else await asyncio.to_thread(_ibon_build_ocr, config_dict)
            if hasattr(_state['ocr_model'], 'set_ranges'):
                _state['ocr_model'].set_ranges(0)
        except Exception:
            _state['ocr_model'] = None
            util.create_debug_logger(config_dict).log('[IBON] OCR 模型載入失敗，請手動處理驗證碼。')
    return _state.get('ocr_model')


def _ibon_stock_context(url):
    from urllib.parse import parse_qsl, urlsplit
    parsed = urlsplit(url or '')
    if parsed.hostname not in ('orders.ibon.com.tw', 'ticket.ibon.com.tw'):
        return None
    if not re.fullmatch(r'/application/utk02/utk020[12]_[^/]*\.aspx', parsed.path.lower()):
        return None
    params = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key = key.upper()
        if key in params:
            return None
        params[key] = value
    performance = params.get('PERFORMANCE_ID', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', performance):
        return None
    return {'performance_id': performance, 'area_id': params.get('PERFORMANCE_PRICE_AREA_ID', ''),
            'api_url': 'https://orders.ibon.com.tw/ImageServer/Data/QWARE_TICKET/images/Temp/'
                       + performance + '/1_' + performance + '_live.map'}


def _ibon_parse_stock(text, performance_id):
    from html.parser import HTMLParser

    class StockMap(HTMLParser):
        def __init__(self):
            super().__init__()
            self.areas = []
            self.seen = set()

        def handle_starttag(self, tag, attributes):
            if tag != 'area':
                return
            attributes = dict(attributes)
            send = re.search(r"Send\('(\d{4})',\s*'([^']+)',\s*'([^']+)',\s*'([^']*)'\)", attributes.get('href', ''))
            title = re.fullmatch(r'票區[:：](.*?)\s+票價[:：]([\d,]+)\s+尚餘[:：](\d+|熱賣中)', attributes.get('title', '').strip())
            if not send or not title:
                return
            page, performance, area_id, group = send.groups()
            if performance != performance_id or page not in ('0201', '0202', '0205'):
                return
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', area_id) or not re.fullmatch(r'\d{0,8}', group):
                return
            identity = (page, area_id, group)
            if identity in self.seen:
                return
            self.seen.add(identity)
            self.areas.append({'page_name': page, 'performance_id': performance, 'area_id': area_id,
                               'group_id': group, 'area_name': title[1].strip(),
                               'price': int(title[2].replace(',', '')),
                               'remaining': None if title[3] == '熱賣中' else int(title[3])})

    parser = StockMap()
    parser.feed(text)
    return parser.areas or None


def _ibon_stock_summary(areas, config_dict):
    keyword = config_dict.get('area_auto_select', {}).get('area_keyword', '').strip()
    groups = util.parse_keyword_string_to_array(keyword) or ([keyword] if keyword else [''])
    labels, seen = [], set()
    for row in areas:
        text = row['area_name'] + ' ' + str(row['price'])
        if not any(all(part in text for part in group.strip('"').split()) for group in groups):
            continue
        identity = (row['area_id'], row['area_name'], row['remaining'])
        if identity in seen:
            continue
        seen.add(identity)
        remaining = '熱賣中（數量未知）' if row['remaining'] is None else str(row['remaining'])
        excluded = '（已排除）' if util.reset_row_text_if_match_keyword_exclude(config_dict, text) else ''
        labels.append(row['area_name'] + '=' + remaining + excluded)
    return '；'.join(labels) if labels else '無關鍵字相符票區'


async def _ibon_stock_request(tab, context, config_dict, expected_doc):
    import math
    try:
        timeout = float(config_dict.get('advanced', {}).get('ibon_stock_api_timeout', 3))
    except (TypeError, ValueError):
        timeout = 3
    if not math.isfinite(timeout) or not 0.2 <= timeout <= 30:
        timeout = 3
    script = _ibon_render(r'''(async (endpoint,expected,timeout) => {
        const doc=()=>location.href+'|'+performance.timeOrigin;
        const url=new URL(endpoint);
        if(doc()!==expected||url.origin!==location.origin)return {status:0,error:'context'};
        url.searchParams.set('v',crypto.randomUUID());
        const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
        try{
            const response=await fetch(url.href,{method:'GET',credentials:'same-origin',cache:'no-store',
                redirect:'error',headers:{'X-Requested-With':'XMLHttpRequest'},signal:controller.signal});
            if(!response.ok)return {status:response.status,error:'http'};
            const text=await response.text();
            if(doc()!==expected)return {status:response.status,error:'context'};
            return text.length<=2000000?{status:response.status,text}:{status:response.status,error:'large'};
        }catch(error){return {status:0,error:error.name==='AbortError'?'timeout':'network'};}finally{clearTimeout(timer);}
    })(ENDPOINT,EXPECTED,TIMEOUT)''', ENDPOINT=context['api_url'], EXPECTED=expected_doc, TIMEOUT=timeout*1000)
    sequence = _state.get('stock_request_seq', 0) + 1
    _state['stock_request_seq'] = sequence
    debug = util.create_debug_logger(config_dict, enabled=True)
    prefix = '[IBON API #' + str(sequence) + '] 場次 ' + context['performance_id']
    debug.log(prefix + '：開始讀取票況。')
    started, areas, outcome = time.monotonic(), None, '瀏覽器讀取失敗'
    task = asyncio.create_task(tab.evaluate(script, await_promise=True))
    try:
        while not task.done():
            if not util.is_auto_refresh_enabled() or await _ibon_actions_paused(config_dict):
                outcome = '已暫停或停止自動重整，忽略本次結果'
                return None
            await asyncio.wait({task}, timeout=0.05)
        result = task.result()
        if isinstance(result, dict):
            status = result.get('status', 200 if isinstance(result.get('text'), str) else 0)
            outcome = 'HTTP ' + str(status) if status else '讀取失敗'
            error = result.get('error')
            reasons = {'context': '頁面或網域已改變，忽略結果', 'large': '回應超出大小限制',
                       'timeout': '讀取逾時', 'network': '網路讀取失敗'}
            if error:
                outcome += '，' + reasons.get(error, '網站回應失敗')
            elif isinstance(result.get('text'), str):
                areas = _ibon_parse_stock(result['text'], context['performance_id'])
                outcome += '，票況解析成功' if areas is not None else '，回應不是有效票況資料'
        return areas
    except asyncio.CancelledError:
        outcome = '讀取已中止，忽略本次結果'
        raise
    except Exception:
        outcome += '，讀取或解析失敗'
        return None
    finally:
        # Let CDP finish its read; cancelling its transaction can kill the zendriver listener.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        detail = '' if areas is None else '；' + _ibon_stock_summary(areas, config_dict)
        debug.log(prefix + '：結果 ' + outcome + '；耗時 ' + format(time.monotonic() - started, '.3f') + ' 秒' + detail)


def _ibon_stock_target(areas, config_dict):
    keyword = config_dict.get('area_auto_select', {}).get('area_keyword', '').strip()
    groups = util.parse_keyword_string_to_array(keyword) or ([keyword] if keyword else [''])
    for group in groups:
        target = util.ibon_livemap_select_area(areas, config_dict, group)
        if target:
            return target
    if config_dict.get('area_auto_fallback', False):
        return util.ibon_livemap_select_area(areas, config_dict, '')
    return None


async def _ibon_stock_area_select(tab, url, config_dict, *, retry_ticket=False):
    import math
    context = _ibon_stock_context(url)
    if not context or not config_dict.get('area_auto_select', {}).get('enable') or not util.is_auto_refresh_enabled():
        return False
    if await _ibon_actions_paused(config_dict) or _state.get('handoff_ready') or await _ibon_submission_pending(tab, config_dict):
        return True
    snapshot = await _ibon_snapshot(tab)
    if not snapshot or _ibon_stock_context(snapshot['url']) != context:
        return True
    if snapshot.get('verifying') or snapshot.get('ready') == 'loading':
        _state.pop('stock_poll', None)
        if _state.get('stock_wait') != snapshot['doc']:
            _state['stock_wait'] = snapshot['doc']
            util.create_debug_logger(config_dict, enabled=True).log('[IBON API] 等待網站頁面或瀏覽器驗證完成，暫不讀取票況或重整頁面。')
        return True
    _state.pop('stock_wait', None)
    if snapshot.get('rows') and not retry_ticket or context['area_id'] and not retry_ticket:
        return False
    if retry_ticket and (not context['area_id'] or any(str(config_dict.get('ticket_number', 2)) in row.get('options', []) for row in snapshot['rows'])):
        return False
    key = (snapshot['doc'], context['performance_id'])
    previous = _state.get('stock_poll', {})
    if previous.get('key') == key and time.monotonic() < previous.get('next', 0):
        return previous.get('valid', False)
    try:
        interval = float(config_dict.get('advanced', {}).get('auto_reload_page_interval', 1))
    except (TypeError, ValueError):
        interval = 1
    if not math.isfinite(interval) or interval <= 0:
        interval = 1
    areas = await _ibon_stock_request(tab, context, config_dict, snapshot['doc'])
    _state['stock_poll'] = {'key': key, 'next': time.monotonic() + interval, 'valid': areas is not None}
    live = await _ibon_snapshot(tab)
    if (not util.is_auto_refresh_enabled() or await _ibon_actions_paused(config_dict) or
            not live or live['doc'] != snapshot['doc'] or _state.get('handoff_ready') or
            await _ibon_submission_pending(tab, config_dict)):
        return True
    if live.get('verifying') or live.get('ready') == 'loading':
        _state.pop('stock_poll', None)
        return True
    if areas is None:
        if _state.get('stock_error') != key:
            _state['stock_error'] = key
            util.create_debug_logger(config_dict, enabled=True).log('[IBON API] 票況讀取失敗，改用網站畫面判斷；選票與下一步沿用原流程。')
        return False
    target = _ibon_stock_target(areas, config_dict)
    detected_at = time.perf_counter()
    notice = (context['performance_id'], tuple((row['area_id'], row['group_id'], row['remaining']) for row in areas),
              None if target is None else (target['area_id'], target['group_id']))
    if _state.get('stock_notice') != notice:
        _state['stock_notice'] = notice
        message = '沒有符合關鍵字、排除條件及指定張數的票區，持續讀取票況。'
        if target:
            remaining = '熱賣中，未提供精確餘票' if target['remaining'] is None else str(target['remaining']) + ' 張'
            message = target['area_name'] + '，餘票：' + remaining + '，準備選票。'
        util.create_debug_logger(config_dict, enabled=True).log('[IBON API] 場次 ' + context['performance_id'] + '：' + message)
    if target:
        # Stage 5: select the page; Stage 6/10 performs the guarded submission.
        if await _ibon_actions_paused(config_dict) or not util.is_auto_refresh_enabled():
            return True
        await _ibon_timing_start(config_dict, (target['performance_id'], target['area_id']), 'stock', at=detected_at)
        ticket_url = util.ibon_build_skip_url(target)
        if config_dict.get('ocr_captcha', {}).get('force_submit') and _ibon_api_submit_supported({'url': ticket_url}):
            if await _ibon_prefetched_ticket_api(tab, config_dict, live, ticket_url):
                return True
        await _ibon_timing_mark(config_dict, 'ticket_nav', '開始開啟選票頁')
        await navigate_page_if_enabled(tab, ticket_url)
        await _ibon_timing_mark(config_dict, 'ticket_nav_return', '選票頁開啟指令返回')
    return True


async def nodriver_ibon_ticket_number_auto_select(tab, config_dict):
    '''只接受指定張數與符合優先關鍵字的單一票種，原子核對後再填值。'''
    if await _ibon_actions_paused(config_dict) or await _ibon_submission_pending(tab, config_dict):
        return False
    for _ in range(15):
        snapshot = await _ibon_snapshot(tab)
        if snapshot and snapshot.get('rows'):
            break
        if await _ibon_actions_paused(config_dict):
            return False
        await asyncio.sleep(0.1)
    if not snapshot or not snapshot.get('rows') or await _ibon_actions_paused(config_dict):
        return False
    candidates = _ibon_candidates(snapshot, config_dict)
    if not candidates:
        notice = (snapshot['doc'], snapshot.get('area'), str(snapshot['rows']),
                  str(config_dict.get('area_auto_select')), config_dict.get('keyword_exclude'), config_dict.get('ticket_number'))
        if _state.get('selection_notice') != notice:
            _state['selection_notice'] = notice
            util.create_debug_logger(config_dict, enabled=True).log(
                '[IBON] 找不到符合區域／票種關鍵字、排除條件及指定張數的選項，等待票況或手動處理。')
        await _ibon_stock_area_select(tab, snapshot['url'], config_dict, retry_ticket=True)
        return False
    _state.pop('selection_notice', None)
    if _ibon_selection_ok(snapshot, config_dict):
        return True
    target = util.get_target_item_from_matched_list(candidates, config_dict.get('area_auto_select', {}).get('mode', CONST_FROM_TOP_TO_BOTTOM))
    if not target or await _ibon_actions_paused(config_dict):
        return False
    script = r'''((expected,target,amount) => {
        FORM
        if(doc!==expected.doc||area()!==(expected.area||'')||sessionStorage.getItem(key))return false;
        const current=rows();if(JSON.stringify(current)!==JSON.stringify(expected.rows))return false;
        const elements=Array.from(document.querySelectorAll('table.rwdtable tbody tr'));
        if(!elements.length)elements.push(...document.querySelectorAll('table.table tbody tr'));
        if(!elements.length)elements.push(...document.querySelectorAll('select#input-qty,select[id*="qty"]'));
        const desired=row=>row.index===target.index?amount:row.options.includes('0')?'0':row.options.includes('')?'':null;
        for(const row of current){
            const value=desired(row);
            if(value===null||row.value!==value&&!row.options.includes(value))return false;
        }
        const setter=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')?.set;
        for(const row of current){
            const value=desired(row);if(row.value===value)continue;
            const element=elements[row.index],select=element.matches('select')?element:element.querySelector('select.form-control-sm,select[name*="AMOUNT_DDL"]');
            if(setter)setter.call(select,value);else select.value=value;
            select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));
        }
        const selected=rows().filter(row=>row.value!==''&&row.value!=='0');
        return selected.length===1&&selected[0].index===target.index&&selected[0].value===amount;
    })(SNAPSHOT,TARGET,AMOUNT)'''
    try:
        result = await tab.evaluate(_ibon_render(script, SNAPSHOT=snapshot, TARGET=target,
                                                 AMOUNT=str(config_dict.get('ticket_number', 2))))
        verified = await _ibon_snapshot(tab)
        return bool(result and verified and verified['doc'] == snapshot['doc'] and _ibon_selection_ok(verified, config_dict))
    except Exception:
        return False

    # nodriver_ibon_get_captcha_image_from_shadow_dom moved to nodriver_common.py
    # as nodriver_get_captcha_image_from_dom_snapshot (shared by ibon and kham)

async def nodriver_ibon_keyin_captcha_code(tab, answer='', auto_submit=False, config_dict=None, captcha_frame=None):
    '''保持人工輸入，核對同一張圖片後才填寫；送出統一走防護入口。'''
    config_dict = config_dict or {}
    if await _ibon_actions_paused(config_dict) or await _ibon_submission_pending(tab, config_dict):
        return False, False
    snapshot = await _ibon_snapshot(tab)
    if not snapshot or not snapshot.get('inputReady'):
        return False, False
    if not answer or snapshot.get('value') not in ('', '驗證碼'):
        if not snapshot.get('value') and not await _ibon_actions_paused(config_dict):
            await tab.evaluate(r'''(() => { FORM if(input)input.focus(); })()'''.replace('FORM', CONST_IBON_FORM_JS))
        return True, False
    if not re.fullmatch(r'\d{4}', str(answer)):
        return True, False
    frame = await _ibon_capture(tab, config_dict)
    if not frame or captcha_frame and (frame['doc'], frame['hash']) != (captcha_frame['doc'], captcha_frame['hash']):
        return False, False
    if await _ibon_actions_paused(config_dict):
        return False, False
    try:
        filled = await tab.evaluate(_ibon_render(r'''((expected,answer) => {
            FORM
            if(doc!==expected||sessionStorage.getItem(key)||!visible(input)||input.disabled||input.value&&input.value!=='驗證碼')return false;
            NATIVE
            return setNativeInputValue(input,answer)&&input.checkValidity();
        })(EXPECTED,ANSWER)''', EXPECTED=frame['doc'], ANSWER=str(answer)))
    except Exception:
        return False, False
    if not filled:
        return False, False
    _state['captcha_fill'] = {'doc': frame['doc'], 'hash': frame['hash'], 'value': str(answer)}
    submitted = await nodriver_ibon_purchase_button_press(tab, config_dict) if auto_submit else False
    return True, submitted

async def nodriver_ibon_refresh_captcha(tab, config_dict):
    """
    Refresh ibon captcha image by calling JavaScript refreshCaptcha() function
    Returns: success (bool)
    """
    if await _ibon_actions_paused(config_dict) or await _ibon_submission_pending(tab, config_dict):
        return False
    debug = util.create_debug_logger(config_dict)

    debug.log("[CAPTCHA REFRESH] Refreshing captcha")

    ret = False
    try:
        # Call JavaScript refreshCaptcha() function
        result = await tab.evaluate('''
            (function() {
                if (typeof refreshCaptcha === 'function') {
                    refreshCaptcha();
                    return true;
                }
                return false;
            })();
        ''')

        ret = result if result else False

        debug.log(f"[CAPTCHA REFRESH] Result: {ret}")

    except Exception as exc:
        debug.log(f"[CAPTCHA REFRESH ERROR] {exc}")

    return ret

async def nodriver_ibon_auto_ocr(tab, config_dict, ocr, away_from_keyboard_enable, previous_answer):
    '''一次辨識，計算不阻塞主迴圈；網址未變不代表網站拒絕。'''
    if await _ibon_actions_paused(config_dict) or await _ibon_submission_pending(tab, config_dict):
        return False, previous_answer, False
    snapshot = await _ibon_snapshot(tab)
    if not snapshot or not _ibon_selection_ok(snapshot, config_dict):
        return False, previous_answer, False
    if not snapshot.get('hasCaptcha'):
        sent = await nodriver_ibon_purchase_button_press(tab, config_dict) if away_from_keyboard_enable else False
        return False, previous_answer, sent
    if snapshot.get('value') not in ('', '驗證碼'):
        sent = await nodriver_ibon_purchase_button_press(tab, config_dict) if away_from_keyboard_enable else False
        return False, previous_answer, sent
    if ocr is None or time.monotonic() < _state.get('next_ocr_at', 0):
        return False, previous_answer, False
    frame = await _ibon_capture(tab, config_dict)
    if not frame:
        return False, previous_answer, False
    _state['next_ocr_at'] = time.monotonic() + 0.5
    identity = (frame['doc'], frame['hash'])
    tries = _state.setdefault('ocr_tries', {})
    if tries.get(identity, 0) >= 5:
        return False, previous_answer, False
    tries[identity] = tries.get(identity, 0) + 1
    try:
        answer = await asyncio.to_thread(ocr.classification, frame['image'])
    except Exception:
        return False, previous_answer, False
    if await _ibon_actions_paused(config_dict):
        return False, previous_answer, False
    answer = ''.join(character for character in str(answer or '') if character in '0123456789')
    if len(answer) != 4:
        return False, previous_answer, False
    _, submitted = await nodriver_ibon_keyin_captcha_code(
        tab, answer, away_from_keyboard_enable, config_dict, captcha_frame=frame)
    return False, answer, submitted

async def nodriver_ibon_captcha(tab, config_dict, ocr):
    '''驗證碼僅回報是否已送出，不把無法辨識當成成功。'''
    if await _ibon_actions_paused(config_dict) or await _ibon_submission_pending(tab, config_dict):
        return False
    snapshot = await _ibon_snapshot(tab)
    if snapshot and not snapshot.get('hasCaptcha'):
        return await nodriver_ibon_purchase_button_press(tab, config_dict)
    if not config_dict.get('ocr_captcha', {}).get('enable'):
        await nodriver_ibon_keyin_captcha_code(tab, config_dict=config_dict)
        return False
    _, _, sent = await nodriver_ibon_auto_ocr(
        tab, config_dict, ocr, bool(config_dict.get('ocr_captcha', {}).get('force_submit')), None)
    return sent

def _ibon_api_submit_supported(snapshot):
    from urllib.parse import urlsplit
    context = _ibon_stock_context(snapshot.get('url'))
    return bool(context and context['area_id'] and not snapshot.get('hasCaptcha') and
                urlsplit(snapshot['url']).path.lower().endswith('/utk0201_001.aspx'))


def _ibon_timing_ms(value):
    import math
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 86400000:
        return format(value/1000, '.3f') + ' 秒'
    return '未記錄'


def _ibon_page_timing(snapshot):
    nav = snapshot.get('navigation') or {}
    return ('首位元組 ' + _ibon_timing_ms(nav.get('first_byte_ms')) + '；HTML 回應 ' +
            _ibon_timing_ms(nav.get('html_ms')) + '；DOM 就緒 ' + _ibon_timing_ms(nav.get('dom_ready_ms')))


async def _ibon_timing_start(config_dict, context, origin, *, at=None):
    sequence = _state.get('booking_timing_sequence', 0) + 1
    _state['booking_timing_sequence'] = sequence
    now = time.perf_counter() if at is None else at
    _state['booking_timing'] = {'id': sequence, 'context': context, 'origin': origin,
                                'start': now, 'last': now, 'marks': {}, 'browser': {}}
    label = '偵測到符合條件票況' if origin == 'stock' else '從選票頁開始（未包含先前找票時間）'
    await _ibon_timing_mark(config_dict, 'detected' if origin == 'stock' else 'form_start', label, at=now)


async def _ibon_timing_mark(config_dict, event, label, *, at=None):
    trace = _state.get('booking_timing')
    if not trace or event in trace['marks']:
        return
    now = time.perf_counter() if at is None else at
    trace['marks'][event] = now
    elapsed, stage = max(0, now-trace['start']), max(0, now-trace['last'])
    trace['last'] = now
    util.create_debug_logger(config_dict, enabled=True).log(
        '[IBON 耗時 #' + str(trace['id']) + '] ' + label +
        '：累計 ' + format(elapsed, '.3f') + ' 秒；距前一階段 ' + format(stage, '.3f') + ' 秒。')


async def _ibon_timing_form(config_dict, snapshot):
    context = _ibon_stock_context(snapshot['url'])
    if not context:
        return
    identity = (context['performance_id'], context['area_id'])
    trace = _state.get('booking_timing')
    if not trace or trace['context'] != identity:
        await _ibon_timing_start(config_dict, identity, 'form')
    label = '表單 HTML 可讀，未載入選票頁資源' if snapshot.get('detached') else '選票頁資料可讀'
    await _ibon_timing_mark(config_dict, 'form_ready', label + '（' + _ibon_page_timing(snapshot) + '）')


async def _ibon_timing_summary(config_dict):
    trace = _state.get('booking_timing')
    if not trace or trace.get('summarized') or 'api_dispatch' not in trace['marks'] or 'checkout_seen' not in trace['marks']:
        return
    trace['summarized'] = True
    marks, browser = trace['marks'], trace['browser']
    def span(start, end):
        return _ibon_timing_ms((marks[end]-marks[start])*1000) if start in marks and end in marks else '未記錄'
    origin = '偵測到票' if trace['origin'] == 'stock' else '選票頁起點（未含找票）'
    values = [browser.get(key) for key in ('request_ms', 'parse_ms', 'build_ms')]
    if 'response_seen' in marks and all(type(value) in (int, float) for value in values):
        browser['bridge_ms'] = max(0, (marks['response_seen']-marks['api_dispatch'])*1000-sum(values))
    notification = '回應→瀏覽器通知 ' + _ibon_timing_ms(browser.get('event_ms')) if browser.get('completion_mode') == 'event' else '回應→監控讀到 ' + _ibon_timing_ms(browser.get('monitor_ms'))
    pieces = [origin + '→API 指令 ' + _ibon_timing_ms((marks['api_dispatch']-trace['start'])*1000),
              '取得選票資料 ' + span('ticket_nav', 'form_ready'),
              '表單讀取／導覽指令 ' + span('ticket_nav', 'ticket_nav_return'),
              '指令返回→資料核對 ' + span('ticket_nav_return', 'form_ready'),
              'API 指令往返 ' + span('api_dispatch', 'api_ack'),
              '瀏覽器建立請求 ' + _ibon_timing_ms(browser.get('build_ms')),
              'API 等待（網站處理＋網路＋讀取） ' + _ibon_timing_ms(browser.get('request_ms')),
              'API 等待回應標頭 ' + _ibon_timing_ms(browser.get('headers_ms')),
              'API 回應本文讀取 ' + _ibon_timing_ms(browser.get('body_ms')),
              '回應解析 ' + _ibon_timing_ms(browser.get('parse_ms')),
              notification,
              '指令／回應傳遞與程式銜接 ' + _ibon_timing_ms(browser.get('bridge_ms')),
              '監控→開啟結帳 ' + span('response_seen', 'checkout_nav'),
              '開啟→確認結帳 ' + span('checkout_nav', 'checkout_seen'),
              origin + '→確認結帳總計 ' + _ibon_timing_ms((marks['checkout_seen']-trace['start'])*1000)]
    if trace.get('manual_wait'):
        pieces.append('本輪曾等待人工確認，總計可能包含人工或暫停時間')
    util.create_debug_logger(config_dict, enabled=True).log(
        '[IBON 耗時摘要 #' + str(trace['id']) + '] ' + '；'.join(pieces) +
        '。部分指標包含子階段或互相重疊，請以總計比較整體速度。')


async def _ibon_prepare_submit(tab, config_dict, snapshot):
    import uuid
    slot = _ibon_flow_path(tab)
    writing = _state.setdefault('receipt_writing', set())
    if slot in writing:
        return None
    writing.add(slot)
    try:
        if await _ibon_actions_paused(config_dict):
            return None
        previous = _ibon_read_flow(tab)
        tour_options = bool(re.search(r'/event/[^/]+/options/?(?:[?#]|$)', snapshot.get('url', '')))
        if previous and previous.get('handoff') is not True and not tour_options:
            return None
        nonce = uuid.uuid4().hex
        flow = {'nonce': nonce, 'origin': _ibon_origin(snapshot['url']), 'handoff': False,
                'reset_token': _ibon_reset_token()}
        task = asyncio.create_task(asyncio.to_thread(util.save_json, flow, slot))
        try:
            if not await asyncio.shield(task):
                return None
        except asyncio.CancelledError:
            await asyncio.shield(task)
            await _ibon_restore_receipt(tab, (nonce, previous))
            raise
        except (OSError, ValueError):
            return None
        if await _ibon_actions_paused(config_dict):
            await _ibon_restore_receipt(tab, (nonce, previous))
            return None
        return nonce, previous
    finally:
        writing.discard(slot)


async def _ibon_restore_receipt(tab, receipt):
    """Only an unsubmitted preparation owned by this nonce may be rolled back."""
    if not receipt:
        return
    nonce, previous = receipt
    current = _ibon_read_flow(tab)
    if not current or current.get('nonce') != nonce:
        return
    if previous:
        await asyncio.to_thread(util.save_json, previous, _ibon_flow_path(tab))
    else:
        _ibon_remove_flow(tab, nonce)


async def _ibon_cleanup_uninvoked(tab, snapshot, nonce, previous):
    try:
        live = await _ibon_snapshot(tab)
        stored = await tab.evaluate("sessionStorage.getItem('ticketsHunter.ibon.submit')")
        saved = json.loads(stored) if stored else None
        if live and live['doc'] == snapshot['doc'] and (not saved or saved.get('nonce') != nonce):
            if previous:
                util.save_json(previous, _ibon_flow_path(tab))
            else:
                _ibon_remove_flow(tab, nonce)
    except Exception:
        pass


async def _ibon_fetch_ticket_form(tab, config_dict, source, url):
    import math
    try:
        timeout = float(config_dict.get('advanced', {}).get('ibon_stock_api_timeout', 3))
    except (TypeError, ValueError):
        timeout = 3
    if not math.isfinite(timeout) or not 0.2 <= timeout <= 30:
        timeout = 3
    script = _ibon_render(CONST_IBON_FORM_FETCH_JS, ENDPOINT=url, EXPECTED=source['doc'], TIMEOUT=timeout*1000)
    task = asyncio.create_task(tab.evaluate(script, await_promise=True))
    try:
        while not task.done():
            if not util.is_auto_refresh_enabled() or await _ibon_actions_paused(config_dict):
                return None
            await asyncio.wait({task}, timeout=0.05)
        result = task.result()
        return result if isinstance(result, dict) and (result.get('detached') is True or result.get('discarded') is True) else None
    except Exception:
        return {'discarded': True}
    finally:
        # Let CDP finish its read; cancelling its transaction can kill the zendriver listener.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def _ibon_prefetched_ticket_api(tab, config_dict, source, url):
    preparing = _state.setdefault('api_preparing', {})
    slot = _ibon_flow_path(tab)
    previous = _ibon_read_flow(tab)
    if slot in preparing or previous and previous.get('handoff') is not True:
        return True
    reservation = object()
    preparing[slot] = reservation
    receipt_task = None
    submit_entered = False
    try:
        if await _ibon_actions_paused(config_dict) or not util.is_auto_refresh_enabled():
            return True
        receipt_task = asyncio.create_task(_ibon_prepare_submit(tab, config_dict, source))
        await _ibon_timing_mark(config_dict, 'ticket_nav', '開始 GET 選票表單 HTML，並行準備防重送紀錄')
        snapshot = await _ibon_fetch_ticket_form(tab, config_dict, source, url)
        received_at = time.perf_counter()
        receipt = await asyncio.shield(receipt_task)
        if not receipt or await _ibon_actions_paused(config_dict) or not util.is_auto_refresh_enabled():
            return True
        current = _ibon_read_flow(tab)
        if not current or current.get('nonce') != receipt[0] or current.get('reset_token') != _ibon_reset_token():
            return True
        if snapshot and snapshot.get('discarded'):
            return True
        if not snapshot or not snapshot.get('validForm') or not _ibon_api_submit_supported(snapshot):
            util.create_debug_logger(config_dict, enabled=True).log('[IBON API 表單] 無法確認可直接送出的表單，改為開啟選票頁。')
            return False
        await _ibon_timing_mark(config_dict, 'ticket_nav_return', '選票表單 HTML 已取得', at=received_at)
        await _ibon_timing_form(config_dict, snapshot)
        candidates = _ibon_candidates(snapshot, config_dict)
        if not candidates:
            util.create_debug_logger(config_dict, enabled=True).log('[IBON API 表單] 本輪沒有符合票種、排除條件及完整張數的選項，繼續讀取票況。')
            return True
        target = util.get_target_item_from_matched_list(candidates, config_dict.get('area_auto_select', {}).get('mode', CONST_FROM_TOP_TO_BOTTOM))
        if not target:
            return True
        await _ibon_timing_mark(config_dict, 'selection_ready', '票種與張數核對完成')
        # Final document, cached form, native submission and exact quantity checks are atomic in browser JS.
        submit_entered = True
        if await _ibon_api_submit(tab, config_dict, snapshot, receipt, target=target):
            return True
        if await _ibon_submission_pending(tab, config_dict):
            return True
        notice = _state.get('api_preflight_notice')
        return bool(notice and notice[0] == snapshot['doc'] and notice[1] in ('busy', 'not_ready'))
    finally:
        try:
            if receipt_task is not None and not submit_entered:
                receipt = await asyncio.shield(receipt_task)
                await _ibon_restore_receipt(tab, receipt)
        finally:
            if preparing.get(slot) is reservation:
                preparing.pop(slot, None)


async def _ibon_wait_submit_result(tab, config_dict, snapshot, nonce, timeout):
    script = _ibon_render(CONST_IBON_RESULT_WAIT_JS, NONCE=nonce, EXPECTED=snapshot['doc'], TIMEOUT=(timeout+.5)*1000)
    task = asyncio.create_task(tab.evaluate(script, await_promise=True))
    try:
        while not task.done():
            if await _ibon_actions_paused(config_dict):
                return None
            await asyncio.wait({task}, timeout=0.05)
        result = task.result()
        return result if isinstance(result, dict) and isinstance(result.get('saved'), dict) else None
    except Exception:
        return None
    finally:
        # Let CDP finish its read; cancelling its transaction can kill the zendriver listener.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def _ibon_api_submit(tab, config_dict, snapshot, receipt, target=None):
    import math
    nonce, previous = receipt
    if await _ibon_actions_paused(config_dict):
        await _ibon_cleanup_uninvoked(tab, snapshot, nonce, previous)
        return False
    candidates = _ibon_candidates(snapshot, config_dict)
    if target is None:
        target = next((row for row in candidates if row.get('value') == str(config_dict.get('ticket_number', 2))), None)
    if not target or target not in candidates:
        await _ibon_cleanup_uninvoked(tab, snapshot, nonce, previous)
        return False
    try:
        timeout = float(config_dict.get('advanced', {}).get('ibon_submit_api_timeout', 10))
    except (TypeError, ValueError):
        timeout = 10
    if not math.isfinite(timeout) or not 0.2 <= timeout <= 60:
        timeout = 10
    await _ibon_timing_form(config_dict, snapshot)
    dispatch_started = time.perf_counter()
    trace = _state.get('booking_timing')
    if trace:
        trace['nonce'] = nonce
    try:
        result = await tab.evaluate(_ibon_render(CONST_IBON_API_POST_JS, SNAPSHOT=snapshot,
                                                 TARGET=target, AMOUNT=str(config_dict.get('ticket_number', 2)),
                                                 ALLOW_SPLIT=bool(config_dict.get('advanced', {}).get('disable_adjacent_seat')),
                                                 NONCE=nonce, TIMEOUT=timeout*1000))
    except Exception:
        await _ibon_timing_mark(config_dict, 'api_dispatch', 'API 指令已發出，回覆不明', at=dispatch_started)
        util.create_debug_logger(config_dict, enabled=True).log('[IBON API 送出] 指令回覆不明，保留防重送狀態，請確認網站或購物車。')
        return False
    if isinstance(result, dict) and result.get('started') is True:
        await _ibon_timing_mark(config_dict, 'api_dispatch', '開始送出 API 指令', at=dispatch_started)
        if trace:
            trace['browser'].update(result.get('timing') or {})
        await _ibon_timing_mark(config_dict, 'api_ack', '瀏覽器已啟動 API 請求')
        _state.pop('api_preflight_notice', None)
        util.create_debug_logger(config_dict, enabled=True).log(
            '[IBON API 送出] 已送出指定票種與 ' + str(config_dict.get('ticket_number', 2)) +
            ' 張，等待網站確認；尚未確認訂單或付款完成。')
        completed = await _ibon_wait_submit_result(tab, config_dict, snapshot, nonce, timeout)
        if completed and completed['saved'].get('nonce') == nonce:
            await _ibon_api_follow_up(tab, config_dict, completed['doc'], completed['saved'],
                                      _ibon_read_flow(tab), completed.get('browser_now_ms'),
                                      event_driven=bool(completed.get('event_driven')))
        return True
    if isinstance(result, dict) and result.get('started') is False:
        await _ibon_cleanup_uninvoked(tab, snapshot, nonce, previous)
        reason = result.get('reason')
        notice = (snapshot['doc'], reason)
        if _state.get('api_preflight_notice') != notice:
            _state['api_preflight_notice'] = notice
            messages = {'busy': '網站原生送出仍在進行，等待網站結果。', 'not_ready': '網站表單尚未就緒，等待載入。',
                        'state': '缺少目前頁面的必要表單狀態，請重新確認頁面。',
                        'changed': '頁面或張數已改變，本次未送出。'}
            util.create_debug_logger(config_dict, enabled=True).log(
                '[IBON API 送出] ' + messages.get(reason, '表單未通過核對，本次未送出，請確認頁面。'))
    return False


async def _ibon_direct_ticket_api(tab, config_dict, snapshot):
    if await _ibon_actions_paused(config_dict):
        return
    if not snapshot.get('validForm') or snapshot.get('verifying') or snapshot.get('ready') == 'loading':
        return
    if not config_dict.get('ocr_captcha', {}).get('force_submit'):
        await nodriver_ibon_ticket_number_auto_select(tab, config_dict)
        return
    if snapshot.get('needsFreshForm'):
        context = _ibon_stock_context(snapshot['url'])
        if context:
            await _ibon_timing_start(config_dict, (context['performance_id'], context['area_id']), 'form')
        handled = await _ibon_prefetched_ticket_api(tab, config_dict, snapshot, snapshot['url'])
        if not handled:
            util.create_debug_logger(config_dict, enabled=True).log(
                '[IBON 自動恢復] 無法確認新的選票表單，本次未送出，請確認網站頁面。')
        return
    candidates = _ibon_candidates(snapshot, config_dict)
    if not candidates:
        await nodriver_ibon_ticket_number_auto_select(tab, config_dict)
        return
    target = util.get_target_item_from_matched_list(candidates, config_dict.get('area_auto_select', {}).get('mode', CONST_FROM_TOP_TO_BOTTOM))
    if not target:
        return
    await _ibon_timing_form(config_dict, snapshot)
    await _ibon_timing_mark(config_dict, 'selection_ready', '票種與張數核對完成')
    receipt = await _ibon_prepare_submit(tab, config_dict, snapshot)
    if receipt:
        await _ibon_api_submit(tab, config_dict, snapshot, receipt, target=target)


async def _ibon_api_follow_up(tab, config_dict, current_doc, saved, flow, browser_now=None, *, event_driven=False):
    if await _ibon_actions_paused(config_dict):
        return True
    result = saved.get('apiResult')
    if not isinstance(result, dict):
        return True
    nonce = saved['nonce']
    kind = result.get('kind')
    status = result.get('status')
    status_text = str(status) if type(status) is int and 0 <= status <= 599 else '未知'
    notice = (nonce, kind, status_text)
    debug = util.create_debug_logger(config_dict, enabled=True)
    trace = _state.get('booking_timing')
    if trace and trace.get('nonce') == nonce and 'response_seen' not in trace['marks']:
        for key in ('request_ms', 'headers_ms', 'body_ms', 'parse_ms'):
            trace['browser'][key] = result.get(key)
        completed = result.get('completed_at_ms')
        if type(browser_now) in (int, float) and type(completed) in (int, float):
            trace['browser']['event_ms' if event_driven else 'monitor_ms'] = max(0, browser_now-completed)
        trace['browser']['completion_mode'] = 'event' if event_driven else 'poll'
        await _ibon_timing_mark(config_dict, 'response_seen', '程式監控讀到 API 回應')
    owned = bool(flow and flow.get('nonce') == nonce and flow.get('origin') == _ibon_origin(saved.get('route')))
    if owned and kind == 'rejected' and status == 200 and result.get('reason') == 'stock' and result.get('format') == 'aspnet_delta':
        if await _ibon_recover_api_rejection(tab, config_dict, saved, flow):
            return True
        if _state.get('recovery_limit_notice') == nonce:
            return True
    path = result.get('path', '')
    confirmed = (owned and saved.get('doc') == current_doc and kind == 'checkout' and status == 200 and isinstance(path, str) and
                 re.fullmatch(r'/application/UTK02/UTK0206_\.aspx', path, re.I))
    if _state.get('api_result_notice') != notice:
        _state['api_result_notice'] = notice
        elapsed = result.get('elapsed_ms')
        duration = '；耗時 ' + format(elapsed/1000, '.3f') + ' 秒' if type(elapsed) in (int, float) and 0 <= elapsed < 3600000 else ''
        if confirmed:
            debug.log('[IBON API 送出] 回應 HTTP ' + status_text + duration + '，網站要求進入結帳頁。')
        else:
            if trace:
                trace['manual_wait'] = True
            message = 'API 已送出但結果尚未確認，停止重送；請手動查看網站或購物車。'
            formats = {'aspnet_delta': 'ASP.NET 部分更新', 'html': '完整 HTML'}
            categories = {'captcha': '驗證碼或網站驗證', 'form_state': '表單狀態驗證',
                          'session': '登入或連線狀態', 'stock': '票況或數量不足',
                          'quantity': '張數或限購條件', 'site_message': '網站提示訊息',
                          'cloudflare': 'Cloudflare 驗證'}
            diagnostic = '；回應格式 ' + formats.get(result.get('format'), '未辨識')
            if result.get('message_class') in categories:
                diagnostic += '；網站提示分類：' + categories[result['message_class']]
            debug.log('[IBON API 送出] 回應 HTTP ' + status_text + duration + diagnostic + '；' + message)
            util.write_instance_status('waiting_manual', message)
    if confirmed and _state.get('api_following_nonce') != nonce:
        if await _ibon_actions_paused(config_dict):
            return True
        _state['api_following_nonce'] = nonce
        await _ibon_timing_mark(config_dict, 'checkout_nav', '開始開啟結帳頁')
        try:
            await navigate_page_if_enabled(tab, flow['origin'] + path)
            await _ibon_timing_mark(config_dict, 'checkout_nav_return', '結帳頁開啟指令返回')
        except Exception:
            debug.log('[IBON API 送出] 開啟結帳頁的結果不明，保留防重送狀態，請手動確認。')
    return True


async def nodriver_ibon_purchase_button_press(tab, config_dict):
    '''同一分頁送出前檢查總張數，持續保存未知結果的鎖定。'''
    if not config_dict.get('ocr_captcha', {}).get('force_submit') or await _ibon_actions_paused(config_dict):
        return False
    if _state.get('handoff_ready') or await _ibon_submission_pending(tab, config_dict):
        return False
    snapshot = await _ibon_snapshot(tab)
    if not _ibon_selection_ok(snapshot, config_dict) or not snapshot.get('validForm') or _ibon_is_checkout(snapshot['url']):
        return False
    fingerprint = ''
    if snapshot.get('hasCaptcha'):
        if not re.fullmatch(r'\d{4}', snapshot.get('value', '')):
            return False
        frame = await _ibon_capture(tab, config_dict)
        filled = _state.get('captcha_fill')
        if not frame or not filled or (frame['doc'], frame['hash']) != (filled['doc'], filled['hash']):
            return False
        fingerprint = frame['hash']
    receipt = await _ibon_prepare_submit(tab, config_dict, snapshot)
    if not receipt:
        return False
    if _ibon_api_submit_supported(snapshot):
        return await _ibon_api_submit(tab, config_dict, snapshot, receipt)
    nonce, previous_flow = receipt
    script = r'''((expected,captcha,nonce) => {
        FORM
        const previous=JSON.parse(sessionStorage.getItem(key)||'null');
        const tour=/\/event\/[^/]+\/options\/?$/i.test(location.pathname);
        if(doc!==expected.doc||area()!==(expected.area||'')||JSON.stringify(rows())!==JSON.stringify(expected.rows))return false;
        if(input&&(!visible(input)||!/^\d{4}$/.test(input.value)||!input.checkValidity()))return false;
        let phase='ticket',button=null;
        if(tour){
            phase=previous?.phase==='tour_cart'?'tour_confirm':'tour_cart';
            const label=phase==='tour_cart'?'加入訂購':'確認付款方式';
            button=Array.from(document.querySelectorAll('button')).find(e=>visible(e)&&!e.disabled&&e.textContent.includes(label));
            if(previous&&(previous.phase!=='tour_cart'||previous.doc!==doc))return false;
        }else{
            if(previous)return false;
            button=document.querySelector('#ctl00_ContentPlaceHolder1_A2')||document.querySelector('a[id*="AddShopingCart"]');
        }
        if(!button&&!tour)button=Array.from(document.querySelectorAll('#ticket-wrap > a.btn,a.btn.btn-primary.btn-block')).find(e=>visible(e)&&/下一步|購買/.test(e.textContent));
        if(!visible(button)||button.disabled||button.getAttribute('aria-disabled')==='true')return false;
        if(button.closest('form')&&!button.closest('form').checkValidity())return false;
        const record={doc,route:location.href,nonce,state:'pending',captcha,phase};
        try{sessionStorage.setItem(key,JSON.stringify(record));}catch(_){return false;}
        window[Symbol.for(key)]=record;
        try{button.click();return true;}catch(_){return false;}
    })(SNAPSHOT,CAPTCHA,NONCE)'''
    try:
        invoked = bool(await tab.evaluate(_ibon_render(script, SNAPSHOT=snapshot, CAPTCHA=fingerprint, NONCE=nonce)))
        if not invoked:
            await _ibon_cleanup_uninvoked(tab, snapshot, nonce, previous_flow)
        return invoked
    except Exception:
        return False

async def nodriver_ibon_check_sold_out(tab, config_dict):
    """
    Check if the event/ticket is sold out on ibon

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary for debug settings

    Returns:
        bool: True if sold out, False otherwise
    """
    debug = util.create_debug_logger(config_dict)
    is_sold_out = False

    try:
        # Check if ticket-info div contains "已售完" text
        result = await tab.evaluate('''
            (function() {
                const ticketInfo = document.querySelector('#ticket-info');
                if (ticketInfo) {
                    const text = ticketInfo.textContent || ticketInfo.innerText;
                    return text.includes('已售完');
                }
                return false;
            })()
        ''')

        if result:
            is_sold_out = True
            debug.log("[IBON] Event is sold out")

    except Exception as e:
        debug.log(f"[IBON] Check sold out error: {e}")

    return is_sold_out

async def nodriver_ibon_wait_for_select_elements(tab, config_dict, max_wait_time=3.0):
    """
    Wait for ticket quantity select elements to appear on page.
    Prevents false sold-out detection when page hasn't fully loaded.

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary
        max_wait_time: Maximum wait time in seconds (default 3.0)

    Returns:
        int: Number of select elements found (0 if none found after timeout)
    """
    debug = util.create_debug_logger(config_dict)
    wait_interval = 0.2
    start_time = time.time()

    while time.time() - start_time < max_wait_time:
        try:
            select_count = await tab.evaluate('''
                (function() {
                    let selects = document.querySelectorAll('table.rwdtable select.form-control-sm');
                    if (selects.length === 0) {
                        selects = document.querySelectorAll('table.table select[name*="AMOUNT_DDL"]');
                    }
                    if (selects.length === 0) {
                        selects = document.querySelectorAll('select.form-control-sm');
                    }
                    return selects.length;
                })()
            ''')
            if select_count and select_count > 0:
                if debug.enabled:
                    elapsed = time.time() - start_time
                    debug.log(f"[IBON] Page loaded, found {select_count} select element(s) after {elapsed:.2f}s")
                return select_count
        except Exception:
            pass
        await asyncio.sleep(wait_interval)

    debug.log(f"[IBON] Warning: No select elements found after {max_wait_time}s wait")
    return 0

async def nodriver_ibon_check_sold_out_on_ticket_page(tab, config_dict):
    """
    Check if tickets are sold out on ibon ticket selection page
    Applicable to UTK0201_001.aspx and similar ticket quantity selection pages

    Detection methods:
    1. Check if ticket quantity dropdowns only have "0" option
    2. Check if page contains sold out messages

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary for debug settings

    Returns:
        bool: True if sold out (needs page reload), False otherwise
    """
    debug = util.create_debug_logger(config_dict)
    is_sold_out = False

    try:
        result = await tab.evaluate('''
            (function() {
                // Sold out keywords (multi-language support)
                const soldOutKeywords = ['選購一空', '已售完', 'No tickets available', 'Sold out', 'Sold Out', '空席なし', '完売した', '完売', '尚無票'];

                // Method 1: Check AMOUNT_STR spans (purchase quantity field)
                const amountSpans = document.querySelectorAll('span[id*="AMOUNT_STR"]');
                let hasAmountSoldOut = false;
                let amountSoldOutCount = 0;

                for (let span of amountSpans) {
                    const text = span.textContent.trim();
                    // Check if text matches any sold out keyword
                    if (soldOutKeywords.some(keyword => text === keyword)) {
                        hasAmountSoldOut = true;
                        amountSoldOutCount++;
                    }
                }

                // Method 2: Check PRICE_STR spans (ticket area label)
                const priceSpans = document.querySelectorAll('span[id*="PRICE_STR"]');
                let hasPriceSoldOut = false;

                for (let span of priceSpans) {
                    const text = span.textContent;
                    // Check if text contains any sold out keyword in parentheses
                    if (soldOutKeywords.some(keyword => text.includes('(' + keyword + ')'))) {
                        hasPriceSoldOut = true;
                        break;
                    }
                }

                // Method 3: Check if ticket quantity selectors only have "0" option
                let selects = document.querySelectorAll('table.rwdtable select.form-control-sm');
                if (selects.length === 0) {
                    selects = document.querySelectorAll('table.table select[name*="AMOUNT_DDL"]');
                }
                if (selects.length === 0) {
                    selects = document.querySelectorAll('select.form-control-sm');
                }

                let hasValidOptions = false;
                let selectCount = selects.length;

                for (let select of selects) {
                    for (let option of select.options) {
                        // Valid option: value not "0" or empty, and not disabled
                        if (option.value !== "0" && option.value !== "" && !option.disabled) {
                            hasValidOptions = true;
                            break;
                        }
                    }
                    if (hasValidOptions) break;
                }

                // Method 4: Check if TICKET TABLE contains sold out messages (NOT entire page)
                // Fix (2026-01-07): Only check ticket table, not entire page.
                // The area dropdown may contain "(已售完)" for OTHER areas, causing false positives.
                let hasSoldOutMessage = false;
                const ticketTable = document.querySelector('table.rwdtable, table.table');
                if (ticketTable) {
                    const tableText = ticketTable.innerText || ticketTable.textContent;
                    hasSoldOutMessage = soldOutKeywords.some(keyword => tableText.includes(keyword));
                }

                // Final determination: sold out if any method detects it
                // IMPORTANT: Only consider "no valid options" as sold-out when select elements exist
                // If selectCount === 0, the page might still be loading (not sold out)
                const noValidOptionsButSelectsExist = selectCount > 0 && !hasValidOptions;
                const isSoldOut = hasAmountSoldOut || hasPriceSoldOut || noValidOptionsButSelectsExist || hasSoldOutMessage;

                return {
                    hasAmountSoldOut: hasAmountSoldOut,
                    amountSoldOutCount: amountSoldOutCount,
                    hasPriceSoldOut: hasPriceSoldOut,
                    selectCount: selectCount,
                    hasValidOptions: hasValidOptions,
                    hasSoldOutMessage: hasSoldOutMessage,
                    isSoldOut: isSoldOut,
                    pageNotLoaded: selectCount === 0
                };
            })()
        ''')

        result = util.parse_nodriver_result(result)
        if isinstance(result, dict):
            is_sold_out = result.get('isSoldOut', False)
            page_not_loaded = result.get('pageNotLoaded', False)
            if debug.enabled:
                select_count = result.get('selectCount', 0)
                has_valid = result.get('hasValidOptions', False)
                if page_not_loaded:
                    debug.log("[IBON] Page not fully loaded, waiting...")
                elif is_sold_out:
                    debug.log("[IBON] Sold out detected, will reload")
                else:
                    debug.log(f"[IBON] Tickets available ({select_count} selects, valid={has_valid})")

    except Exception as e:
        debug.log(f"[IBON SOLD OUT CHECK] Error: {e}")

    return is_sold_out

async def nodriver_ibon_navigate_on_sold_out(tab, config_dict):
    """
    Navigate to area selection page when tickets are sold out.
    Fix (2026-01-07): Instead of just reloading the same sold-out page (causes infinite loop),
    navigate back to the area selection page where other areas might be available.

    URL Formats:
    - New format: ticket.ibon.com.tw/EventBuy/{eventId}/{sessionId}/{areaId}
      -> Navigate to: ticket.ibon.com.tw/Event/{eventId}/{sessionId}
    - Old format: orders.ibon.com.tw/application/UTK02/UTK0202_.aspx?PERFORMANCE_PRICE_AREA_ID=xxx
      -> Navigate to: orders.ibon.com.tw/application/UTK02/UTK0201_000.aspx?PERFORMANCE_ID=xxx

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary

    Returns:
        bool: True if navigation successful, False otherwise
    """
    if not util.is_auto_refresh_enabled():
        return False

    debug = util.create_debug_logger(config_dict)
    navigation_success = False

    try:
        url = tab.url if hasattr(tab, 'url') else str(tab.target.url)
        url_lower = url.lower()

        # New format: ticket.ibon.com.tw/EventBuy/{eventId}/{sessionId}/{areaId}
        if '/eventbuy/' in url_lower and 'ticket.ibon.com.tw' in url_lower:
            parts = url.split('/')
            # URL: https://ticket.ibon.com.tw/EventBuy/xxx/yyy/zzz
            # parts: ['https:', '', 'ticket.ibon.com.tw', 'EventBuy', 'eventId', 'sessionId', 'areaId']
            if len(parts) >= 7:
                # Navigate to Event page (area selection)
                event_url = '/'.join(parts[:3] + ['Event', parts[4], parts[5]])
                debug.log(f"[IBON] Sold out - navigating to area selection: {event_url}")
                await navigate_page_if_enabled(tab, event_url)
                navigation_success = True

        # Old format: orders.ibon.com.tw/application/UTK02/UTK0202_.aspx?PERFORMANCE_PRICE_AREA_ID=xxx
        # Note: Old .aspx pages require PRODUCT_ID and other parameters that are hard to reconstruct.
        # Using tab.back() is safer to return to the previous area selection page.
        elif '/utk02/utk0202_' in url_lower and 'PERFORMANCE_PRICE_AREA_ID=' in url.upper():
            debug.log("[IBON] Sold out - using tab.back() to return to area selection")
            await back_if_auto_refresh_enabled(tab)
            navigation_success = True

        # Fallback: use tab.back() if URL pattern not recognized
        if not navigation_success:
            debug.log("[IBON] Sold out - using tab.back() as fallback")
            await back_if_auto_refresh_enabled(tab)
            navigation_success = True

    except Exception as e:
        debug.log(f"[IBON] Navigation error: {e}")
        # Fallback to reload if navigation fails
        try:
            await reload_page_if_enabled(tab)
        except Exception:
            pass

    return navigation_success

async def nodriver_ibon_fill_verify_form(tab, config_dict, answer_list, fail_list,
                                          input_text_css, next_step_button_css):
    """
    ibon verification form filling (supports single/multiple input fields)

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary
        answer_list: List of answers to try
        fail_list: List of previously failed answers
        input_text_css: CSS selector for input fields
        next_step_button_css: CSS selector for submit button

    Returns:
        tuple[bool, list]: (is_answer_sent, updated fail_list)
    """
    debug = util.create_debug_logger(config_dict)
    is_answer_sent = False

    try:
        # Get all input fields and their values
        input_info_raw = await tab.evaluate(f'''
            (function() {{
                var inputs = document.querySelectorAll("{input_text_css}");
                var result = [];
                inputs.forEach(function(input) {{
                    result.push({{
                        value: input.value || ""
                    }});
                }});
                return {{
                    count: inputs.length,
                    inputs: result
                }};
            }})()
        ''')

        # Handle NoDriver result format - may return tuple (result, exception)
        input_info = input_info_raw
        if isinstance(input_info_raw, tuple) and len(input_info_raw) >= 1:
            input_info = input_info_raw[0]

        input_info = util.parse_nodriver_result(input_info)

        # Handle NoDriver error result (ExceptionDetails object or non-dict)
        if not input_info or not isinstance(input_info, dict):
            debug.log(f"[IBON VERIFY] Failed to get input info: {type(input_info)}")
            return is_answer_sent, fail_list

        form_input_count = input_info.get('count', 0)
        debug.log(f"[IBON VERIFY] Found {form_input_count} input field(s)")

        if form_input_count == 0:
            return is_answer_sent, fail_list

        # Determine multi-question mode
        is_multi_question_mode = False
        if form_input_count == 2 and len(answer_list) >= 2:
            if len(answer_list[0]) > 0 and len(answer_list[1]) > 0:
                is_multi_question_mode = True

        debug.log(f"[IBON VERIFY] Multi-question mode: {is_multi_question_mode}")
        debug.log(f"[IBON VERIFY] Answer list: {answer_list}")
        debug.log(f"[IBON VERIFY] Fail list: {fail_list}")

        if is_multi_question_mode:
            # Multi-field mode: fill answer_list[0] to first field, answer_list[1] to second
            answer_1 = answer_list[0]
            answer_2 = answer_list[1]
            # JSON encode for safe JavaScript string insertion
            answer_1_js = json.dumps(answer_1)
            answer_2_js = json.dumps(answer_2)

            # Fill both fields using JavaScript
            fill_result_raw = await tab.evaluate(f'''
                (function() {{
                    var inputs = document.querySelectorAll("{input_text_css}");
                    if (inputs.length >= 2) {{
                        var answer1 = {answer_1_js};
                        var answer2 = {answer_2_js};

                        if (inputs[0].value !== answer1) {{
                            inputs[0].value = "";
                            inputs[0].value = answer1;
                            inputs[0].dispatchEvent(new Event('input', {{ bubbles: true }}));
                            inputs[0].dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}

                        if (inputs[1].value !== answer2) {{
                            inputs[1].value = "";
                            inputs[1].value = answer2;
                            inputs[1].dispatchEvent(new Event('input', {{ bubbles: true }}));
                            inputs[1].dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}

                        return true;
                    }}
                    return false;
                }})()
            ''')

            # Handle tuple result from NoDriver
            fill_result = fill_result_raw
            if isinstance(fill_result_raw, tuple) and len(fill_result_raw) >= 1:
                fill_result = fill_result_raw[0]

            if fill_result:
                debug.log(f"[IBON VERIFY] Filled multi-field: '{answer_1}' and '{answer_2}'")

                # Click submit button
                try:
                    btn = await tab.query_selector(next_step_button_css)
                    if btn:
                        await btn.click()
                        is_answer_sent = True
                        fail_list.append(answer_1)
                        fail_list.append(answer_2)
                        debug.log(f"[IBON VERIFY] Submitted multi-field answers")
                except Exception as btn_exc:
                    debug.log(f"[IBON VERIFY] Click button error: {btn_exc}")

        else:
            # Single-field mode: find first answer not in fail_list
            inferred_answer = ""
            for answer in answer_list:
                if answer not in fail_list:
                    inferred_answer = answer
                    break

            if len(inferred_answer) > 0:
                # JSON encode for safe JavaScript string insertion
                inferred_answer_js = json.dumps(inferred_answer)
                # Fill the answer
                await tab.evaluate(f'''
                    (function() {{
                        var input = document.querySelector("{input_text_css}");
                        if (input) {{
                            input.value = "";
                            input.value = {inferred_answer_js};
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    }})()
                ''')

                debug.log(f"[IBON VERIFY] Filled single-field: '{inferred_answer}'")

                # Click submit button
                try:
                    btn = await tab.query_selector(next_step_button_css)
                    if btn:
                        await btn.click()
                        is_answer_sent = True
                        fail_list.append(inferred_answer)
                        debug.log(f"[IBON VERIFY] Submitted, attempt #{len(fail_list)}")
                except Exception as btn_exc:
                    debug.log(f"[IBON VERIFY] Click button error: {btn_exc}")
            else:
                # No answer to fill, focus the input
                debug.log("[IBON VERIFY] No answer available, focusing input")
                await tab.evaluate(f'''
                    (function() {{
                        var input = document.querySelector("{input_text_css}");
                        if (input && document.activeElement !== input) {{
                            input.focus();
                        }}
                    }})()
                ''')

        if is_answer_sent:
            await asyncio.sleep(0.3)

    except Exception as exc:
        debug.log(f"[IBON VERIFY] Error: {exc}")

    return is_answer_sent, fail_list

async def nodriver_ibon_card_vaildate(tab, config_dict):
    """
    Handle ibon cardholder-priority validation page (/Vaildate/...).
    Fills credit card prefix and submits to proceed to Event page.
    Primary source: credit_card_prefix; fallback: first token of user_guess_string.
    """
    debug = util.create_debug_logger(config_dict)

    current_url = tab.target.url if (tab and hasattr(tab, 'target') and tab.target) else ""

    # Re-entry guard: avoid re-submitting before navigation completes
    if _state.get("vaildate_submitted_url") == current_url:
        debug.log("[IBON VAILDATE] Already submitted for this URL, skipping")
        return False

    prefix = config_dict.get("contact", {}).get("credit_card_prefix", "").strip()
    if not prefix:
        guess = config_dict.get("advanced", {}).get("user_guess_string", "").strip()
        if guess:
            prefix = guess.split(",")[0].strip().strip('"')

    if not prefix:
        debug.log("[IBON VAILDATE] No credit_card_prefix configured, waiting for manual input")
        return False

    INPUT_CSS = "#content input[type='text']"
    SUBMIT_BTN_CSS = "#content a.btn"
    prefix_js = json.dumps(prefix)

    try:
        has_input = await tab.evaluate(f'''
            (function() {{
                return document.querySelector("{INPUT_CSS}") !== null;
            }})()
        ''')
        if not has_input:
            debug.log("[IBON VAILDATE] Input field not found yet")
            return False

        # Skip if already filled (page may have re-rendered)
        current_val = await tab.evaluate(f'''
            (function() {{
                var el = document.querySelector("{INPUT_CSS}");
                return el ? el.value : "";
            }})()
        ''')
        if current_val and str(current_val).strip():
            debug.log(f"[IBON VAILDATE] Input already has value, skipping")
            return False

        # Use the shared native setter so ngModel picks up the change
        fill_ok = await tab.evaluate(f'''
            (function() {{
                {CONST_NATIVE_INPUT_SETTER_JS}
                const input = document.querySelector("{INPUT_CSS}");
                if (!input) return false;
                return setNativeInputValue(input, {prefix_js});
            }})()
        ''')

        if not fill_ok:
            debug.log(f"[IBON VAILDATE] Failed to fill input")
            return False

        debug.log(f"[IBON VAILDATE] Filled prefix (length: {len(prefix)})")

        # Click via JS so Angular (click) handler fires (button is <a>, not <button>)
        clicked = await tab.evaluate(f'''
            (function() {{
                var btn = document.querySelector("{SUBMIT_BTN_CSS}");
                if (!btn) return false;
                btn.click();
                return true;
            }})()
        ''')

        if clicked:
            _state["vaildate_submitted_url"] = current_url
            debug.log("[IBON VAILDATE] Submitted, waiting for navigation")
            await asyncio.sleep(1.5)
            return True

        debug.log("[IBON VAILDATE] Submit button not found")
        return False

    except Exception as exc:
        debug.log(f"[IBON VAILDATE] Error: {exc}")
        return False

async def nodriver_ibon_verification_question(tab, fail_list, config_dict):
    """
    Handle verification question on ibon with auto-fill support

    Features:
    - Reads answers from user_guess_string (user dictionary)
    - Supports first/last N chars extraction (e.g., "phone last 3 digits")
    - Auto-guess from question text (if auto_guess_options enabled)
    - Multi-field support

    Args:
        tab: NoDriver tab object
        fail_list: List of previously failed answers
        config_dict: Configuration dictionary

    Returns:
        list: Updated fail_list
    """
    debug = util.create_debug_logger(config_dict)

    # CSS selectors for ibon verification page
    # Note: use single quotes in CSS selector to avoid breaking JavaScript string
    INPUT_CSS = "#content div.form-group input[type='text'], #content div.form-group input:not([type]), div.editor-box input[type='text'], div.editor-box input:not([type])"
    SUBMIT_BTN_CSS = '#content a.btn, div.editor-box a.btn'

    try:
        # Get question texts from all form-groups (returns array)
        question_texts_raw = await tab.evaluate('''
            (function() {
                var questions = [];
                var content = document.querySelector('#content');
                if (content) {
                    // Get all form-groups with input fields
                    var formGroups = content.querySelectorAll('div.form-group');
                    formGroups.forEach(function(formGroup) {
                        // Only process form-groups that have input fields
                        if (formGroup.querySelector('input')) {
                            var span = formGroup.querySelector('span');
                            if (span) {
                                questions.push(span.textContent || span.innerText || '');
                            }
                        }
                    });
                }
                return questions;
            })()
        ''')

        # Handle NoDriver result format - may return tuple (result, exception) or array
        question_texts = []
        raw_data = question_texts_raw

        # If it's a tuple (result, exception), extract the result
        if isinstance(raw_data, tuple) and len(raw_data) >= 1:
            raw_data = raw_data[0]

        # Parse the result into a list of strings
        if isinstance(raw_data, list):
            for q in raw_data:
                if isinstance(q, dict) and 'value' in q:
                    # NoDriver format: {'type': 'string', 'value': '...'}
                    question_texts.append(str(q['value']))
                elif isinstance(q, str) and q:
                    question_texts.append(q)
        elif isinstance(raw_data, str) and raw_data:
            question_texts = [raw_data]

        # For backward compatibility, create combined question_text
        question_text = ' '.join(question_texts).strip()

        if len(question_text) > 0:
            debug.log(f"[IBON VERIFY] Question found: {question_text}")
            debug.log(f"[IBON VERIFY] Question count: {len(question_texts)}")

            # Write question to file for debugging
            write_question_to_file(question_text)

            # Step 1: Get answers from user dictionary
            answer_list = util.get_answer_list_from_user_guess_string(config_dict, CONST_MAXBOT_ANSWER_ONLINE_FILE)

            debug.log(f"[IBON VERIFY] User dictionary answers: {answer_list}")

            # Step 2: Smart extraction for multi-field forms
            # For each question, try to extract from its corresponding answer
            if len(answer_list) > 0 and len(question_texts) > 1:
                # Multi-field mode: extract answer for each question
                extracted_answers = []
                for idx, q_text in enumerate(question_texts):
                    if idx < len(answer_list):
                        # Only try to extract from THIS position's answer
                        extracted = util.extract_answer_by_question_pattern([answer_list[idx]], q_text)
                        if extracted:
                            extracted_answers.append(extracted)
                            debug.log(f"[IBON VERIFY] Q{idx+1} extracted from answer[{idx}]: {extracted}")
                        else:
                            # No pattern match, use original answer at this position
                            extracted_answers.append(answer_list[idx])
                            debug.log(f"[IBON VERIFY] Q{idx+1} using original: {answer_list[idx]}")

                # Replace answer_list with extracted answers if we got results
                if len(extracted_answers) >= len(question_texts):
                    answer_list = extracted_answers
                    debug.log(f"[IBON VERIFY] Final answers for multi-field: {answer_list}")

            elif len(answer_list) > 0:
                # Single-field mode: original logic
                extracted_answer = util.extract_answer_by_question_pattern(answer_list, question_text)
                if extracted_answer:
                    debug.log(f"[IBON VERIFY] Extracted answer by pattern: {extracted_answer}")
                    # Prepend extracted answer to the list for priority
                    if extracted_answer not in answer_list:
                        answer_list = [extracted_answer] + answer_list

            # Step 3: If no user dictionary and auto_guess enabled, try auto-guess
            if len(answer_list) == 0:
                if config_dict["advanced"].get("auto_guess_options", False):
                    answer_list = util.get_answer_list_from_question_string(None, question_text, config_dict)
                    debug.log(f"[IBON VERIFY] Auto-guessed answers: {answer_list}")

            # Step 4: Fill the form if we have answers
            if len(answer_list) > 0:
                is_answer_sent, fail_list = await nodriver_ibon_fill_verify_form(
                    tab, config_dict, answer_list, fail_list,
                    INPUT_CSS, SUBMIT_BTN_CSS
                )
                debug.log(f"[IBON VERIFY] Answer sent: {is_answer_sent}")
            else:
                debug.log("[IBON VERIFY] No answers available, waiting for user input")
                # Focus the input field
                await tab.evaluate(f'''
                    (function() {{
                        var input = document.querySelector("{INPUT_CSS}");
                        if (input && document.activeElement !== input) {{
                            input.focus();
                        }}
                    }})()
                ''')

    except Exception as e:
        debug.log(f"[IBON] Verification question error: {e}")

    return fail_list

async def nodriver_tour_ibon_event_detail(tab, config_dict):
    """
    Handle tour.ibon.com.tw event detail page (/event/{eventId})
    Click the purchase button to proceed to options page

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary

    Returns:
        bool: True if button clicked successfully
    """
    debug = util.create_debug_logger(config_dict)
    is_button_clicked = False

    try:
        debug.log("[TOUR IBON] Event detail page detected")

        # Find and click purchase button (multiple text variations)
        button_text_list = ["立即購票", "搶票", "購票", "Buy"]

        for button_text in button_text_list:
            try:
                # Use text-based selector to find button
                result = await tab.evaluate(f'''
                    (function() {{
                        var buttons = Array.from(document.querySelectorAll('button'));
                        var targetBtn = buttons.find(btn => btn.textContent.includes("{button_text}"));
                        if (targetBtn) {{
                            targetBtn.click();
                            return true;
                        }}
                        return false;
                    }})()
                ''')

                if result:
                    is_button_clicked = True
                    debug.log(f"[TOUR IBON] Clicked button: {button_text}")
                    break
            except Exception as e:
                debug.log(f"[TOUR IBON] Button click error ({button_text}): {e}")

        if not is_button_clicked:
            debug.log("[TOUR IBON] Purchase button not found")

    except Exception as e:
        debug.log(f"[TOUR IBON] Event detail error: {e}")

    return is_button_clicked

async def nodriver_tour_ibon_options(tab, config_dict):
    """旅遊網站每個加入訂購／確認階段僅送一次，結帳頁保留手動。"""
    if await _ibon_actions_paused(config_dict):
        return False
    if not await nodriver_ibon_ticket_number_auto_select(tab, config_dict):
        return False
    return await nodriver_ibon_purchase_button_press(tab, config_dict)

async def nodriver_tour_ibon_checkout(tab, config_dict):
    """旅遊結帳頁交還使用者，不自動填寫、勾選或送出。"""
    return await _ibon_handoff(tab, config_dict)

async def nodriver_ibon_main(tab, url, config_dict, ocr, Captcha_Browser):
    # 函數開始時檢查暫停
    if await _ibon_actions_paused(config_dict):
        return False
    _ensure_state()
    if _state.get('handoff_ready'):
        return True
    if _ibon_is_checkout(url):
        return await _ibon_handoff(tab, config_dict)
    await register_ibon_alert_handler(tab, config_dict)
    if await _ibon_submission_pending(tab, config_dict):
        return False
    if config_dict.get('area_auto_select', {}).get('enable') and _ibon_api_submit_supported({'url': url}):
        snapshot = await _ibon_snapshot(tab)
        if snapshot and _ibon_api_submit_supported(snapshot):
            await _ibon_direct_ticket_api(tab, config_dict, snapshot)
            return False
    ocr = await _ibon_cached_ocr(config_dict, ocr)

    # Check if kicked to login page (Cookie/Session expired)
    debug = util.create_debug_logger(config_dict)

    # Queue-IT detection: track enter/exit time for diagnostics
    url_lower = url.lower()
    if 'queue-it.net' in url_lower:
        if _state.get("queue_it_enter_time") is None:
            import time as _time
            _state["queue_it_enter_time"] = _time.time()
            debug.log("[IBON] Queue-IT entered, waiting...")
        return False
    else:
        if _state.get("queue_it_enter_time") is not None:
            import time as _time
            elapsed = _time.time() - _state["queue_it_enter_time"]
            debug.log(f"[IBON] Queue-IT passed (waited {elapsed:.1f}s)")
            _state["queue_it_enter_time"] = None

    is_login_page = False
    target_url = None

    # Detect login page patterns
    # Support both patterns: /huiwan/loginhuiwan and /huiwan//loginhuiwan (with double slash)
    if 'huiwan.ibon.com.tw' in url_lower and 'loginhuiwan' in url_lower:
        is_login_page = True
        # Extract target URL from query parameter
        if 'targeturl=' in url.lower():
            try:
                import urllib.parse
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query)
                if 'targeturl' in params:
                    target_url = params['targeturl'][0]
            except:
                pass
        if debug.enabled:
            debug.log(f"[IBON LOGIN] Detected login page redirect")
            if target_url:
                debug.log(f"[IBON LOGIN] Target URL: {target_url}")

    # If kicked to login page, re-login and redirect back
    if is_login_page:
        debug.log("[IBON LOGIN] Re-authenticating with cookie...")

        # Re-execute login process (get driver from tab.browser)
        driver = getattr(tab, 'browser', None)
        login_result = await nodriver_ibon_login(tab, config_dict, driver)

        if debug.enabled:
            if login_result['success']:
                debug.log("[IBON LOGIN] Re-authentication successful")
            else:
                debug.log(f"[IBON LOGIN] Re-authentication failed: {login_result.get('error', 'Unknown error')}")

        # Navigate to ticket.ibon.com.tw homepage so the server can process the
        # cookie and establish a session. Do NOT navigate to target_url (/login)
        # directly -- that SSO endpoint redirects back to huiwan when there is
        # no existing session, causing an infinite redirect loop.
        ibon_base = "https://ticket.ibon.com.tw/"
        debug.log(f"[IBON LOGIN] Navigating to ibon base URL to activate cookie session")
        try:
            await navigate_page_if_enabled(tab, ibon_base)
            await asyncio.sleep(random.uniform(1.5, 2.0))
            await dismiss_pending_ibon_dialog(tab, config_dict)
        except Exception as e:
            debug.log(f"[IBON LOGIN] Navigation to ibon base failed: {e}")

        return False  # Don't quit bot, continue monitoring

    home_url_list = ['https://ticket.ibon.com.tw/'
    ,'https://ticket.ibon.com.tw/index/entertainment'
    ]
    for each_url in home_url_list:
        if each_url == url.lower():
            if config_dict["ocr_captcha"]["enable"]:
                # TODO:
                #set_non_browser_cookies(driver, url, Captcha_Browser)
                pass
            break

    # Auto-redirect if kicked back to homepage (防止被踢回首頁)
    # Pattern: Homepage → Target page redirection
    # - If homepage config is set to a specific page (ActivityInfo/orders/Event), redirect back when kicked to homepage
    # - If homepage config is homepage itself, skip redirect (normal behavior)
    is_kicked_to_homepage = False
    normalized_url = url.lower().rstrip('/')
    if normalized_url == 'https://ticket.ibon.com.tw' or normalized_url == 'https://ticket.ibon.com.tw/index/entertainment':
        is_kicked_to_homepage = True

    if is_kicked_to_homepage:
        config_homepage = config_dict["homepage"]
        # Only redirect if user wants to be on a specific page (not homepage itself)
        # Support all ibon page types: ActivityInfo, orders.ibon.com.tw, Event, EventBuy, etc.
        normalized_homepage = config_homepage.lower().rstrip('/')
        is_homepage_same_as_current = (
            normalized_homepage == 'https://ticket.ibon.com.tw' or
            normalized_homepage == 'https://ticket.ibon.com.tw/index/entertainment'
        )
        should_redirect = not is_homepage_same_as_current

        if should_redirect:
            current_time = time.time()
            last_redirect_time = _state.get("last_homepage_redirect_time", 0)
            redirect_interval = config_dict["advanced"].get("auto_reload_page_interval", 3)
            if redirect_interval <= 0:
                redirect_interval = 3

            if current_time - last_redirect_time > redirect_interval:
                debug.log(f"[IBON] Detected kicked back to homepage: {url}")
                debug.log(f"[IBON] Redirecting to config homepage: {config_homepage}")
                try:
                    _state["last_homepage_redirect_time"] = current_time
                    await navigate_page_if_enabled(tab, config_homepage)
                    await asyncio.sleep(2)
                    debug.log(f"[IBON] Successfully redirected to: {config_homepage}")
                except Exception as redirect_exc:
                    debug.log(f"[IBON] Redirect failed: {redirect_exc}")

            return False

    # tour.ibon.com.tw URL routing
    # https://tour.ibon.com.tw/event/{eventId}
    # https://tour.ibon.com.tw/event/{eventId}/options
    # https://tour.ibon.com.tw/event/{eventId}/checkout
    if 'tour.ibon.com.tw' in url.lower() and '/event/' in url.lower():
        url_parts = url.split('/')

        # Event detail page: /event/{eventId}
        if len(url_parts) == 5:
            await nodriver_tour_ibon_event_detail(tab, config_dict)

        # Options or checkout page: /event/{eventId}/options or /event/{eventId}/checkout
        elif len(url_parts) == 6:
            if '/options' in url.lower():
                await nodriver_tour_ibon_options(tab, config_dict)
            elif '/checkout' in url.lower():
                await nodriver_tour_ibon_checkout(tab, config_dict)

    is_match_target_feature = False

    #PS: ibon some utk is upper case, some is lower.
    if not is_match_target_feature:
        #https://ticket.ibon.com.tw/ActivityInfo/Details/0000?pattern=entertainment
        if '/activityinfo/details/' in url.lower():
            is_event_page = False
            if len(url.split('/'))==6:
                is_event_page = True

            if is_event_page:
                if config_dict["date_auto_select"]["enable"]:
                    is_match_target_feature = True
                    is_date_assign_by_bot = await nodriver_ibon_date_auto_select(tab, config_dict)

                    # If date selection failed, reload page
                    if not is_date_assign_by_bot:
                        debug = util.create_debug_logger(config_dict)
                        debug.log("[IBON DATE] Date selection failed, reloading page...")

                        auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                        if auto_reload_interval > 0:
                            await sleep_before_auto_refresh(auto_reload_interval)

                        try:
                            await reload_page_if_enabled(tab)
                        except:
                            pass

    if 'ibon.com.tw/error.html?' in url.lower():
        try:
            await back_if_auto_refresh_enabled(tab)
        except Exception as exc:
            pass

    is_enter_verify_mode = False
    if not is_match_target_feature:
        # validation question url:
        # https://orders.ibon.com.tw/application/UTK02/UTK0201_0.aspx?rn=1180872370&PERFORMANCE_ID=B04M7XZT&PRODUCT_ID=B04KS88E&SHOW_PLACE_MAP=True
        is_event_page = False
        if '/UTK02/UTK0201_0.' in url.upper():
            if '.aspx?' in url.lower():
                if 'rn=' in url.lower():
                    if 'PERFORMANCE_ID=' in url.upper():
                        if "PRODUCT_ID=" in url.upper():
                            is_event_page = True

        if is_event_page:
            is_enter_verify_mode = True
            _state["fail_list"] = await nodriver_ibon_verification_question(tab, _state["fail_list"], config_dict)
            is_match_target_feature = True

    if not is_match_target_feature:
        # Cardholder-priority validation page: ticket.ibon.com.tw/Vaildate/{eventId}/{sessionId}/{activityId}
        # (Note: "Vaildate" is ibon's own typo in the URL)
        if 'ticket.ibon.com.tw' in url_lower and '/vaildate/' in url_lower:
            is_enter_verify_mode = True
            await nodriver_ibon_card_vaildate(tab, config_dict)
            is_match_target_feature = True
        elif _state.get("vaildate_submitted_url") is not None:
            _state["vaildate_submitted_url"] = None

    if not is_enter_verify_mode:
        _state["fail_list"] = []

    if not is_match_target_feature:
        # https://orders.ibon.com.tw/application/UTK02/UTK0201_000.aspx?PERFORMANCE_ID=0000
        # https://orders.ibon.com.tw/application/UTK02/UTK0201_000.aspx?rn=1111&PERFORMANCE_ID=2222&PRODUCT_ID=BBBB
        # https://orders.ibon.com.tw/application/UTK02/UTK0201_001.aspx?PERFORMANCE_ID=2222&GROUP_ID=4&PERFORMANCE_PRICE_AREA_ID=3333

        is_event_page = False
        if '/UTK02/UTK0201_' in url.upper():
            if '.aspx?' in url.lower():
                if 'PERFORMANCE_ID=' in url.upper():
                    if len(url.split('/'))==6:
                        is_event_page = True

        if '/UTK02/UTK0202_' in url.upper():
            if '.aspx?' in url.lower():
                if 'PERFORMANCE_ID=' in url.upper():
                    if len(url.split('/'))==6:
                        is_event_page = True

        if is_event_page:
            # Check if area is already selected (avoid duplicate selection causing double tickets)
            is_area_already_selected = 'PERFORMANCE_PRICE_AREA_ID=' in url.upper()

            if config_dict["area_auto_select"]["enable"] and not is_area_already_selected:
                is_match_target_feature = True
                area_keyword = config_dict["area_auto_select"]["area_keyword"].strip()

                # Stage 5: poll the site's stock endpoint without reloading the area page.
                if await _ibon_stock_area_select(tab, url, config_dict):
                    return False

                is_need_refresh = False
                is_price_assign_by_bot = False

                if len(area_keyword) > 0:
                    area_keyword_array = []
                    try:
                        import json
                        area_keyword_array = json.loads("["+ area_keyword +"]")
                    except Exception as exc:
                        area_keyword_array = []

                    for area_keyword_item in area_keyword_array:
                        is_need_refresh, is_price_assign_by_bot = await nodriver_ibon_area_auto_select(tab, config_dict, area_keyword_item)
                        if not is_need_refresh:
                            break

                    # Fallback: If all keyword groups failed, check area_auto_fallback setting
                    if not is_price_assign_by_bot:
                        if is_need_refresh:
                            area_auto_fallback = config_dict.get("area_auto_fallback", False)
                            if area_auto_fallback:
                                # Feature 003: Fallback enabled - use auto_select_mode without keyword
                                debug = util.create_debug_logger(config_dict)
                                auto_select_mode = config_dict["area_auto_select"]["mode"]
                                debug.log(f"[IBON AREA] All keyword groups failed, area_auto_fallback=true")
                                debug.log(f"[IBON AREA] Falling back to auto_select_mode: {auto_select_mode}")
                                is_need_refresh, is_price_assign_by_bot = await nodriver_ibon_area_auto_select(tab, config_dict, "")
                            else:
                                # Feature 003: Fallback disabled - strict mode (no selection, will reload)
                                debug = util.create_debug_logger(config_dict)
                                debug.log(f"[IBON AREA] All keyword groups failed, area_auto_fallback=false")
                                debug.log(f"[IBON AREA] No area selected, will reload page and retry")
                                # Keep is_price_assign_by_bot=False and is_need_refresh=True
                                # This will trigger page reload in the outer loop
                else:
                    # empty keyword, match all.
                    is_need_refresh, is_price_assign_by_bot = await nodriver_ibon_area_auto_select(tab, config_dict, area_keyword)

                if is_need_refresh:
                    debug = util.create_debug_logger(config_dict)
                    debug.log("[IBON ORDERS] No available areas found, page reload required")

                    auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                    if auto_reload_interval > 0:
                        await sleep_before_auto_refresh(auto_reload_interval)

                    try:
                        await reload_page_if_enabled(tab)
                    except Exception as exc:
                        pass

                # Check if we need to handle ticket number and captcha (UTK0201_001 page)
                is_do_ibon_performance_with_ticket_number = False

                # UTK0201_001 page: has PERFORMANCE_PRICE_AREA_ID parameter
                if 'PERFORMANCE_PRICE_AREA_ID=' in url.upper():
                    is_do_ibon_performance_with_ticket_number = True

                # UTK0201_000 page: check if ticket selection appears on same page
                if 'PRODUCT_ID=' in url.upper():
                    if not is_price_assign_by_bot:
                        # This case shows captcha and ticket-number on the same page
                        # Check if ticket number dropdown exists
                        try:
                            selector = 'table.table > tbody > tr > td > select'
                            form_select = await tab.query_selector(selector)
                            if form_select:
                                is_do_ibon_performance_with_ticket_number = True
                        except Exception as exc:
                            pass

                if is_do_ibon_performance_with_ticket_number:
                    # Wait for page to load before checking sold-out (prevents false detection)
                    debug = util.create_debug_logger(config_dict)
                    await nodriver_ibon_wait_for_select_elements(tab, config_dict)

                    # Step 0: Check if tickets are sold out FIRST (before OCR)
                    is_sold_out_detected = await nodriver_ibon_check_sold_out_on_ticket_page(tab, config_dict)

                    if is_sold_out_detected:
                        debug.log("[IBON] All tickets sold out, navigating to area selection...")

                        # Fix (2026-01-07): Navigate to area selection instead of reload (prevents infinite loop)
                        await nodriver_ibon_navigate_on_sold_out(tab, config_dict)

                        # Wait before next check (FR-061: auto_reload_page_interval)
                        auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                        if auto_reload_interval > 0:
                            debug.log(f"[AUTO RELOAD] Waiting {auto_reload_interval} seconds before next check...")
                            await sleep_before_auto_refresh(auto_reload_interval)

                        return False  # Return to main loop to continue monitoring

                    # Livemap fast-path area verified: has tickets, clear the attempt tracker

                    # Step 1: Handle non-adjacent seat checkbox
                    if config_dict["advanced"]["disable_adjacent_seat"]:
                        try:
                            await nodriver_ibon_allow_not_adjacent_seat(tab, config_dict)
                        except Exception as exc:
                            debug.log(f"[IBON] Checkbox error: {exc}")

                    # Step 2: Assign ticket number FIRST (before captcha)
                    # Fix (2026-01-07): Captcha auto_submit checks if ticket number is selected.
                    # Must select ticket number first, then process captcha.
                    is_ticket_number_assigned = False
                    max_retries = 3

                    try:
                        for attempt in range(1, max_retries + 1):
                            is_ticket_number_assigned = await nodriver_ibon_ticket_number_auto_select(tab, config_dict)

                            if is_ticket_number_assigned:
                                if attempt > 1:
                                    debug.log(f"[TICKET RETRY] Success after {attempt} attempt(s)")
                                break

                            if attempt < max_retries:
                                delay = 0.2 * (2 ** (attempt - 1))
                                debug.log(f"[TICKET RETRY] Attempt {attempt}/{max_retries} failed, waiting {delay}s (exponential backoff)")
                                await asyncio_sleep_with_pause_check(delay, config_dict)

                        if is_ticket_number_assigned:
                            # Wait for iBon to process ticket number change
                            await asyncio.sleep(random.uniform(0.15, 0.25))
                    except Exception as exc:
                        debug.log(f"[IBON] Ticket number error: {exc}")

                    # Step 3: Handle captcha (after ticket number is selected)
                    is_captcha_sent = False
                    try:
                        is_captcha_sent = await nodriver_ibon_captcha(tab, config_dict, ocr)
                    except Exception as exc:
                        debug.log(f"[IBON] Captcha error: {exc}")

                    # Step 3.5: Final check if tickets are sold out (backup check)
                    if not is_ticket_number_assigned:
                        is_sold_out_detected = await nodriver_ibon_check_sold_out_on_ticket_page(tab, config_dict)

                        if is_sold_out_detected:
                            debug.log("[IBON] All tickets sold out, navigating to area selection...")

                            # Fix (2026-01-07): Navigate to area selection instead of reload (prevents infinite loop)
                            await nodriver_ibon_navigate_on_sold_out(tab, config_dict)

                            # Wait before next check (FR-061: auto_reload_page_interval)
                            auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                            if auto_reload_interval > 0:
                                debug.log(f"[AUTO RELOAD] Waiting {auto_reload_interval} seconds before next check...")
                                await sleep_before_auto_refresh(auto_reload_interval)

                            return False  # Return to main loop to continue monitoring

                    # Step 4: Submit purchase
                    if is_ticket_number_assigned and is_captcha_sent:
                        try:
                            click_ret = is_captcha_sent  # CAPTCHA handler already used the guarded submit path.
                            if click_ret:
                                # Play "ticket" sound when attempting to enter checkout (found ticket)
                                if not _state.get("played_sound_ticket", False):
                                    if config_dict["advanced"]["play_sound"]["ticket"]:
                                        play_sound_while_ordering(config_dict)
                                    _state["played_sound_ticket"] = True
                        except Exception as exc:
                            debug = util.create_debug_logger(config_dict)
                            debug.log(f"[IBON] Submit button error: {exc}")

            # Handle case when area is already selected (direct access to UTK0201_001 page)
            elif is_area_already_selected:
                is_match_target_feature = True
                debug = util.create_debug_logger(config_dict)

                debug.log("[IBON] Area already selected, checking ticket availability...")

                # Check if we need to handle ticket number and captcha (UTK0201_001 page)
                is_do_ibon_performance_with_ticket_number = False

                # UTK0201_001 page: has PERFORMANCE_PRICE_AREA_ID parameter
                if 'PERFORMANCE_PRICE_AREA_ID=' in url.upper():
                    is_do_ibon_performance_with_ticket_number = True

                if is_do_ibon_performance_with_ticket_number:
                    # Wait for page to load before checking sold-out (prevents false detection)
                    await nodriver_ibon_wait_for_select_elements(tab, config_dict)

                    # Step 0: Check if tickets are sold out FIRST (before OCR)
                    is_sold_out_detected = await nodriver_ibon_check_sold_out_on_ticket_page(tab, config_dict)

                    if is_sold_out_detected:
                        debug.log("[IBON] All tickets sold out, navigating to area selection...")

                        # Fix (2026-01-07): Navigate to area selection instead of reload (prevents infinite loop)
                        await nodriver_ibon_navigate_on_sold_out(tab, config_dict)

                        # Wait before next check (FR-061: auto_reload_page_interval)
                        auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                        if auto_reload_interval > 0:
                            debug.log(f"[AUTO RELOAD] Waiting {auto_reload_interval} seconds before next check...")
                            await sleep_before_auto_refresh(auto_reload_interval)

                        return False  # Return to main loop to continue monitoring

                    # Livemap fast-path area verified: has tickets, clear the attempt tracker

                    # Step 1: Handle non-adjacent seat checkbox
                    if config_dict["advanced"]["disable_adjacent_seat"]:
                        try:
                            await nodriver_ibon_allow_not_adjacent_seat(tab, config_dict)
                        except Exception as exc:
                            debug.log(f"[IBON] Checkbox error: {exc}")

                    # Step 2: Handle captcha (only executed when tickets are available)
                    is_captcha_sent = False
                    try:
                        is_captcha_sent = await nodriver_ibon_captcha(tab, config_dict, ocr)
                    except Exception as exc:
                        debug.log(f"[IBON] Captcha error: {exc}")

                    # Step 3: Assign ticket number with retry
                    is_ticket_number_assigned = False
                    max_retries = 3

                    try:
                        for attempt in range(1, max_retries + 1):
                            is_ticket_number_assigned = await nodriver_ibon_ticket_number_auto_select(tab, config_dict)

                            if is_ticket_number_assigned:
                                if attempt > 1:
                                    debug.log(f"[TICKET RETRY] Success after {attempt} attempt(s)")
                                break

                            if attempt < max_retries:
                                delay = 0.2 * (2 ** (attempt - 1))
                                debug.log(f"[TICKET RETRY] Attempt {attempt}/{max_retries} failed, waiting {delay}s")
                                await asyncio_sleep_with_pause_check(delay, config_dict)

                        if is_ticket_number_assigned:
                            # Wait for iBon to process ticket number change
                            await asyncio.sleep(random.uniform(0.15, 0.25))
                    except Exception as exc:
                        debug.log(f"[IBON] Ticket number error: {exc}")

                    # Step 3.5: Final check if tickets are sold out (backup check)
                    if not is_ticket_number_assigned:
                        is_sold_out_detected = await nodriver_ibon_check_sold_out_on_ticket_page(tab, config_dict)

                        if is_sold_out_detected:
                            debug.log("[IBON] All tickets sold out, navigating to area selection...")

                            # Fix (2026-01-07): Navigate to area selection instead of reload (prevents infinite loop)
                            await nodriver_ibon_navigate_on_sold_out(tab, config_dict)

                            # Wait before next check (FR-061: auto_reload_page_interval)
                            auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                            if auto_reload_interval > 0:
                                debug.log(f"[AUTO RELOAD] Waiting {auto_reload_interval} seconds before next check...")
                                await sleep_before_auto_refresh(auto_reload_interval)

                            return False  # Return to main loop to continue monitoring

                    # Step 4: Submit purchase
                    if is_ticket_number_assigned and is_captcha_sent:
                        try:
                            click_ret = is_captcha_sent  # CAPTCHA handler already used the guarded submit path.
                            if click_ret:
                                # Play "ticket" sound when attempting to enter checkout (found ticket)
                                if not _state.get("played_sound_ticket", False):
                                    if config_dict["advanced"]["play_sound"]["ticket"]:
                                        play_sound_while_ordering(config_dict)
                                    _state["played_sound_ticket"] = True
                        except Exception as exc:
                            debug.log(f"[IBON] Submit button error: {exc}")

    # New ibon Event page format (Angular SPA): https://ticket.ibon.com.tw/Event/{eventId}/{sessionId}[/{activityId}]
    if not is_match_target_feature:
        is_new_event_page = False
        if 'ticket.ibon.com.tw' in url.lower() and '/event/' in url.lower():
            url_parts = url.split('/')
            # URL format: https://ticket.ibon.com.tw/Event/B09QY340/B09VO5KQ
            # Split result: ['https:', '', 'ticket.ibon.com.tw', 'Event', 'B09QY340', 'B09VO5KQ']
            # URL format (with activity ID): https://ticket.ibon.com.tw/Event/B09QY340/B09VO5KQ/39142
            # Split result: ['https:', '', 'ticket.ibon.com.tw', 'Event', 'B09QY340', 'B09VO5KQ', '39142']
            if len(url_parts) >= 6:
                is_new_event_page = True

        if is_new_event_page:
            if config_dict["area_auto_select"]["enable"]:
                is_match_target_feature = True
                area_keyword = config_dict["area_auto_select"]["area_keyword"].strip()

                is_need_refresh = False
                is_price_assign_by_bot = False

                if len(area_keyword) > 0:
                    area_keyword_array = []
                    try:
                        import json
                        area_keyword_array = json.loads("["+ area_keyword +"]")
                    except Exception as exc:
                        area_keyword_array = []

                    for area_keyword_item in area_keyword_array:
                        is_need_refresh, is_price_assign_by_bot = await nodriver_ibon_event_area_auto_select(tab, config_dict, area_keyword_item)
                        if not is_need_refresh:
                            break

                    # Fallback: If all keyword groups failed, check area_auto_fallback setting
                    if not is_price_assign_by_bot:
                        if is_need_refresh:
                            area_auto_fallback = config_dict.get("area_auto_fallback", False)
                            if area_auto_fallback:
                                # Feature 003: Fallback enabled - use auto_select_mode without keyword
                                debug = util.create_debug_logger(config_dict)
                                auto_select_mode = config_dict["area_auto_select"]["mode"]
                                debug.log(f"[IBON EVENT] All keyword groups failed, area_auto_fallback=true")
                                debug.log(f"[IBON EVENT] Falling back to auto_select_mode: {auto_select_mode}")
                                is_need_refresh, is_price_assign_by_bot = await nodriver_ibon_event_area_auto_select(tab, config_dict, "")
                            else:
                                # Feature 003: Fallback disabled - strict mode (no selection, will reload)
                                debug = util.create_debug_logger(config_dict)
                                debug.log(f"[IBON EVENT] All keyword groups failed, area_auto_fallback=false")
                                debug.log(f"[IBON EVENT] No area selected, will reload page and retry")
                                # Keep is_price_assign_by_bot=False and is_need_refresh=True
                                # This will trigger page reload in the outer loop
                else:
                    # empty keyword, match all.
                    is_need_refresh, is_price_assign_by_bot = await nodriver_ibon_event_area_auto_select(tab, config_dict, area_keyword)

                debug = util.create_debug_logger(config_dict)
                debug.log(f"[NEW EVENT] Area selection result - is_price_assign_by_bot: {is_price_assign_by_bot}, is_need_refresh: {is_need_refresh}")

                # Auto-reload if no available ticket areas found
                if is_need_refresh:
                    debug.log("[NEW EVENT] No available ticket areas found, page reload required")

                    # Use auto_reload_page_interval setting
                    auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                    if auto_reload_interval > 0:
                        await sleep_before_auto_refresh(auto_reload_interval)

                    try:
                        await reload_page_if_enabled(tab)
                        debug.log("[NEW EVENT] Page reloaded successfully")
                    except Exception as reload_exc:
                        debug.log(f"[NEW EVENT] Page reload failed: {reload_exc}")

            is_match_target_feature = True

    # New ibon EventBuy page format: https://ticket.ibon.com.tw/EventBuy/{eventId}/{sessionId}/{areaId}[/{activityId}]
    if not is_match_target_feature:
        is_new_eventbuy_page = False
        if 'ticket.ibon.com.tw' in url.lower() and '/eventbuy/' in url.lower():
            url_parts = url.split('/')
            # URL format: https://ticket.ibon.com.tw/EventBuy/B09QY340/B09VO5KQ/B09VO6K0
            # Split result: ['https:', '', 'ticket.ibon.com.tw', 'EventBuy', 'eventId', 'sessionId', 'areaId']
            # URL format (with activity ID): https://ticket.ibon.com.tw/EventBuy/B09QY340/B09VO5KQ/B09VSQGL/39142
            # Split result: ['https:', '', 'ticket.ibon.com.tw', 'EventBuy', 'B09QY340', 'B09VO5KQ', 'B09VSQGL', '39142']
            if len(url_parts) >= 7:
                is_new_eventbuy_page = True

        if is_new_eventbuy_page:
            is_match_target_feature = True
            debug = util.create_debug_logger(config_dict)

            debug.log("[NEW EVENTBUY] Processing EventBuy page")

            # Check disable_adjacent_seat
            if config_dict["advanced"]["disable_adjacent_seat"]:
                is_finish_checkbox_click = await nodriver_check_checkbox(tab, '.asp-checkbox > input[type="checkbox"]:not(:checked)')

            # Step 1: Assign ticket number first with retry (exponential backoff)
            is_ticket_number_assigned = False
            max_retries = 3
            debug = util.create_debug_logger(config_dict)

            for attempt in range(1, max_retries + 1):
                is_ticket_number_assigned = await nodriver_ibon_ticket_number_auto_select(tab, config_dict)

                if is_ticket_number_assigned:
                    if attempt > 1:
                        debug.log(f"[TICKET RETRY] Success after {attempt} attempt(s)")
                    break

                if attempt < max_retries:
                    delay = 0.2 * (2 ** (attempt - 1))
                    debug.log(f"[TICKET RETRY] Attempt {attempt}/{max_retries} failed, waiting {delay}s (exponential backoff)")
                    await asyncio_sleep_with_pause_check(delay, config_dict)

            if is_ticket_number_assigned:
                # Wait for iBon to process ticket number change
                await asyncio.sleep(random.uniform(0.15, 0.25))

            # Step 2: Handle captcha after ticket number is selected
            is_captcha_sent = False
            if is_ticket_number_assigned:
                debug.log("[NEW EVENTBUY] Ticket number assigned, proceeding to captcha")

                # Extract model name from URL for captcha
                domain_name = url.split('/')[2]
                # For EventBuy, use sessionId as model name
                model_name = url.split('/')[5] if len(url.split('/')) > 5 else 'EventBuy'
                if len(model_name) > 7:
                    model_name = model_name[:7]
                captcha_url = '/pic.aspx?TYPE=%s' % (model_name)

                # Set cookies for Captcha_Browser if needed
                if not Captcha_Browser is None:
                    Captcha_Browser.set_domain(domain_name, captcha_url=captcha_url)

                # Call ibon captcha handler (handles both OCR and manual mode)
                is_captcha_sent = await nodriver_ibon_captcha(tab, config_dict, ocr)

            # Step 3: Click purchase button if everything is ready
            if is_ticket_number_assigned:
                if is_captcha_sent:
                    debug.log("[NEW EVENTBUY] Clicking purchase button")

                    click_ret = is_captcha_sent  # CAPTCHA handler already used the guarded submit path.

                    # Play sound if button clicked successfully
                    if click_ret:
                        # Play "ticket" sound when attempting to enter checkout (found ticket)
                        if not _state.get("played_sound_ticket", False):
                            if config_dict["advanced"]["play_sound"]["ticket"]:
                                play_sound_while_ordering(config_dict)
                            _state["played_sound_ticket"] = True
                        debug.log("[NEW EVENTBUY] Purchase button clicked successfully")
            else:
                # Check if sold out: try event-level check first, then per-zone ticket page check
                is_sold_out = await nodriver_ibon_check_sold_out(tab, config_dict)
                if not is_sold_out:
                    is_sold_out = await nodriver_ibon_check_sold_out_on_ticket_page(tab, config_dict)
                if is_sold_out:
                    debug.log("[NEW EVENTBUY] Sold out detected, going back and refreshing")
                    try:
                        await back_if_auto_refresh_enabled(tab)
                        await reload_page_if_enabled(tab)
                    except Exception as exc:
                        debug.log(f"[NEW EVENTBUY] Back/reload failed: {exc}")

    if not is_match_target_feature:
        # https://orders.ibon.com.tw/application/UTK02/UTK0201_000.aspx?PERFORMANCE_ID=0000
        # https://orders.ibon.com.tw/application/UTK02/UTK0201_000.aspx?rn=1111&PERFORMANCE_ID=2222&PRODUCT_ID=BBBB
        # https://orders.ibon.com.tw/application/UTK02/UTK0201_001.aspx?PERFORMANCE_ID=2222&GROUP_ID=4&PERFORMANCE_PRICE_AREA_ID=3333

        is_event_page = False
        if '/UTK02/UTK0201_' in url.upper():
            if '.aspx?' in url.lower():
                if 'PERFORMANCE_ID=' in url.upper():
                    if len(url.split('/'))==6:
                        is_event_page = True

        if '/UTK02/UTK0202_' in url.upper():
            if '.aspx?' in url.lower():
                if 'PERFORMANCE_ID=' in url.upper():
                    if len(url.split('/'))==6:
                        is_event_page = True

        if is_event_page:
            if config_dict["area_auto_select"]["enable"]:
                select_query = "tr.disbled"
                # TODO:
                #clean_tag_by_selector(driver,select_query)
                
                select_query = "tr.sold-out"
                # TODO:
                #clean_tag_by_selector(driver,select_query)

                is_do_ibon_performance_with_ticket_number = False

                # Check if area is already selected (avoid duplicate selection causing double tickets)
                is_area_already_selected = 'PERFORMANCE_PRICE_AREA_ID=' in url.upper()

                if 'PRODUCT_ID=' in url.upper() and not is_area_already_selected:
                    # step 1: select area.
                    is_price_assign_by_bot = False
                    debug = util.create_debug_logger(config_dict)

                    # Call area selection function (simplified version for testing)
                    # TODO: Implement nodriver_ibon_performance() wrapper with OR logic
                    area_keyword = config_dict["area_auto_select"]["area_keyword"].strip()
                    is_need_refresh, is_price_assign_by_bot = await nodriver_ibon_area_auto_select(tab, config_dict, area_keyword)

                    debug.log(f"Area selection result - is_price_assign_by_bot: {is_price_assign_by_bot}, is_need_refresh: {is_need_refresh}")

                    # Auto-reload if no available ticket areas found
                    if is_need_refresh:
                        debug.log("[IBON AREA] No available ticket areas found, page reload required")

                        # Use auto_reload_page_interval setting
                        auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                        if auto_reload_interval > 0:
                            await sleep_before_auto_refresh(auto_reload_interval)

                        try:
                            await reload_page_if_enabled(tab)
                            debug.log("[IBON AREA] Page reloaded successfully")
                        except Exception as reload_exc:
                            debug.log(f"[IBON AREA] Page reload failed: {reload_exc}")

                    if not is_price_assign_by_bot:
                        # this case show captcha and ticket-number in this page.
                        # TODO:
                        #if ibon_ticket_number_appear(driver, config_dict):
                        #    is_do_ibon_performance_with_ticket_number = True
                        pass

                # Old ibon format handling
                if 'PERFORMANCE_PRICE_AREA_ID=' in url.upper():
                    is_do_ibon_performance_with_ticket_number = True

                if is_do_ibon_performance_with_ticket_number:
                    if config_dict["advanced"]["disable_adjacent_seat"]:
                        # TODO:
                        is_finish_checkbox_click = await nodriver_check_checkbox(tab, '.asp-checkbox > input[type="checkbox"]:not(:checked)')

                    # Step 1: Assign ticket number first with retry (exponential backoff)
                    is_match_target_feature = True
                    is_ticket_number_assigned = False
                    max_retries = 3
                    debug = util.create_debug_logger(config_dict)

                    for attempt in range(1, max_retries + 1):
                        is_ticket_number_assigned = await nodriver_ibon_ticket_number_auto_select(tab, config_dict)

                        if is_ticket_number_assigned:
                            if attempt > 1:
                                debug.log(f"[TICKET RETRY] Success after {attempt} attempt(s)")
                            break

                        if attempt < max_retries:
                            delay = 0.2 * (2 ** (attempt - 1))
                            debug.log(f"[TICKET RETRY] Attempt {attempt}/{max_retries} failed, waiting {delay}s (exponential backoff)")
                            await asyncio_sleep_with_pause_check(delay, config_dict)

                    if is_ticket_number_assigned:
                        # Wait for iBon to process ticket number change
                        await asyncio.sleep(random.uniform(0.15, 0.25))

                    # Step 2: Handle captcha after ticket number is selected
                    is_captcha_sent = False
                    if is_ticket_number_assigned:
                        domain_name = url.split('/')[2]
                        model_name = url.split('/')[5]
                        if len(model_name) > 7:
                            model_name=model_name[:7]
                        captcha_url = '/pic.aspx?TYPE=%s' % (model_name)

                        # Set cookies for Captcha_Browser if needed
                        if not Captcha_Browser is None:
                            Captcha_Browser.set_domain(domain_name, captcha_url=captcha_url)

                        # Call ibon captcha handler (handles both OCR and manual mode)
                        is_captcha_sent = await nodriver_ibon_captcha(tab, config_dict, ocr)
                    
                    #print("is_ticket_number_assigned:", is_ticket_number_assigned)
                    if is_ticket_number_assigned:
                        if is_captcha_sent:
                            click_ret = is_captcha_sent  # CAPTCHA handler already used the guarded submit path.

                            # only this case: "ticket number CHANGED by bot" and "cpatcha sent" to play sound!
                            if click_ret:
                                # Play "ticket" sound when attempting to enter checkout (found ticket)
                                if not _state.get("played_sound_ticket", False):
                                    if config_dict["advanced"]["play_sound"]["ticket"]:
                                        play_sound_while_ordering(config_dict)
                                    _state["played_sound_ticket"] = True
                    else:
                        is_sold_out = await nodriver_ibon_check_sold_out(tab, config_dict)
                        if is_sold_out:
                            debug.log("[IBON] is_sold_out, go back and refresh.")
                            # plan-A
                            #is_button_clicked = press_button(tab, By.CSS_SELECTOR, 'a.btn.btn-primary')
                            # plan-B, easy and better than plan-A
                            try:
                                await back_if_auto_refresh_enabled(tab)
                                await reload_page_if_enabled(tab)
                            except Exception as exc:
                                pass

    if not is_match_target_feature:
        #https://orders.ibon.com.tw/application/UTK02/UTK0206_.aspx
        is_event_page = False
        if '/UTK02/UTK020' in url.upper():
            if '.aspx' in url.lower():
                if len(url.split('/'))==6:
                    is_event_page = True

        # ignore "pay money" step.
        if '/UTK02/UTK0207_.ASPX' in url.upper():
            is_event_page = False

        if is_event_page:
            if is_event_page:
                is_match_target_feature = True
                is_finish_checkbox_click = await nodriver_ibon_ticket_agree(tab)
                if is_finish_checkbox_click:
                    is_name_based = False
                    try:
                        html_body = await tab.get_content()
                        #print("html_body:",len(html_body))
                        if html_body:
                            if len(html_body) > 1024:
                                if '實名制' in html_body:
                                    is_name_based = True
                    except Exception as exc:
                        #print(exc)
                        pass

                    if not is_name_based:
                        is_button_clicked = await nodriver_press_button(tab, 'a.btn.btn-pink.continue')

    return tab

