#encoding=utf-8
# =============================================================================
# TixCraft + Ticketmaster Platform Module
# Extracted from nodriver_tixcraft.py during modularization (Phase 1)
# Contains: tixcraft.com, indievox.com, ticketmaster.* family
# =============================================================================

import asyncio
import base64
import json
import os
import random
import re
import time
import traceback
from urllib.parse import urlsplit, urljoin

import tixcraft_api as ticket_http


from zendriver import cdp

import util
from nodriver_common import navigate_page_if_enabled, reload_page_if_enabled, sleep_before_auto_refresh
from nodriver_common import (
    check_and_handle_pause,
    check_and_handle_quit,
    sleep_with_pause_check,
    convert_remote_object,
    nodriver_check_checkbox,
    nodriver_check_checkbox_enhanced,
    nodriver_current_url,
    nodriver_get_text_by_selector,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    write_question_to_file,
    CONST_MAXBOT_ANSWER_ONLINE_FILE,
    CONST_MAXBOT_INT28_FILE,
    CONST_OCR_CAPTCH_IMAGE_SOURCE_CANVAS,
    CONST_OCR_CAPTCH_IMAGE_SOURCE_NON_BROWSER,
    CONST_NATIVE_INPUT_SETTER_JS,
)

__all__ = [
    "nodriver_tixcraft_home_close_window",
    "nodriver_tixcraft_redirect",
    "nodriver_ticketmaster_parse_zone_info",
    "get_ticketmaster_target_area",
    "nodriver_ticketmaster_get_ticketPriceList",
    "nodriver_ticketmaster_date_auto_select",
    "nodriver_ticketmaster_area_auto_select",
    "nodriver_ticketmaster_assign_ticket_number",
    "nodriver_ticketmaster_captcha",
    "nodriver_ticketmaster_promo",
    "nodriver_tixcraft_verify",
    "nodriver_fill_verify_form",
    "nodriver_tixcraft_input_check_code",
    "nodriver_tixcraft_date_auto_select",
    "nodriver_tixcraft_area_auto_select",
    "nodriver_get_tixcraft_target_area",
    "nodriver_ticket_number_select_fill",
    "nodriver_tixcraft_assign_ticket_number",
    "nodriver_tixcraft_ticket_main_agree",
    "nodriver_tixcraft_ticket_main",
    "nodriver_tixcraft_keyin_captcha_code",
    "nodriver_tixcraft_toast",
    "nodriver_tixcraft_reload_captcha",
    "nodriver_tixcraft_get_ocr_answer",
    "nodriver_tixcraft_has_captcha",
    "nodriver_tixcraft_submit_without_captcha",
    "nodriver_tixcraft_auto_ocr",
    "nodriver_tixcraft_ticket_main_ocr",
    "nodriver_tixcraft_main",
    "nodriver_tixcraft_disconnect",
    "nodriver_ticketmaster_check_ip_block",
]

# Module-level state (replaces global tixcraft_dict)
_state = {}


def _tixcraft_runtime_guards():
    return {key: _state[key] for key in ('tixcraft_submit_pending', 'tixcraft_submit_generation') if key in _state}


def _tixcraft_reset_runtime_state(pending=None):
    """Reset run-local UI state while retaining uncertain command guards."""
    _state.clear()
    if pending:
        _state.update({key: pending[key] for key in ('tixcraft_submit_pending', 'tixcraft_submit_generation') if key in pending})

# Seconds the Ticketmaster check-captcha handler waits for the captcha input:
# the URL flips before the form renders, and nodriver_tixcraft_auto_ocr reads a
# missing input as "no captcha" and ends the OCR loop on the first pass.
CONST_TICKETMASTER_CAPTCHA_WAIT_SEC = 6.0


# Keywords that identify serial-number / membership-code / promo-code style
# verify prompts -- i.e. prompts the discount_code setting can actually answer.
# Used as a guard for the discount_code fallback in nodriver_tixcraft_input_check_code
# to avoid wasting an attempt on unrelated questions (math, common knowledge, etc.).
# Match is case-insensitive substring on question_text; covers zh-TW/zh-CN/en/ja/ko.
_SERIAL_CODE_QUESTION_KEYWORDS = (
    "會員",        # zh-TW: member (會員)
    "会员",        # zh-CN: member (会员)
    "序號",        # zh-TW: serial (序號)
    "序号",        # zh-CN: serial (序号)
    "編號",        # zh-TW: ID/number (編號)
    "编号",        # zh-CN: ID/number (编号)
    "員編",        # zh-TW: member ID (員編)
    "会員番号",  # ja: member number (会員番号)
    "シリアル",  # ja: serial (シリアル)
    "회원",        # ko: member (회원)
    "멤버십",  # ko: membership (멤버십)
    "membership",
    "member id",
    "member no",
    "serial",
    "weverse",
    # Promo / discount code prompts (Ticketmaster #promoBox and TixCraft
    # discount gates). These read the same discount_code setting as the
    # member/serial prompts above, so they belong on the same allow list.
    "優惠碼",      # zh-TW: promo code
    "优惠码",      # zh-CN: promo code
    "折扣碼",      # zh-TW: discount code
    "折扣码",      # zh-CN: discount code
    "兌換碼",      # zh-TW: redemption code
    "兑换码",      # zh-CN: redemption code
    "promo",       # also covers "promotion" / "promotional code"
    "discount",
    "coupon",
)

_TIXCRAFT_SOFT_BLOCK_SCOPE_HOSTS = (
    "tixcraft.com",
    "teamear.com",
    "indievox.com",
)


def _is_serial_code_question(question_text):
    if not question_text:
        return False
    text_lower = question_text.lower()
    for kw in _SERIAL_CODE_QUESTION_KEYWORDS:
        if kw.lower() in text_lower:
            return True
    return False


def _should_leave_dialog_to_user(current_url):
    """True when a JS dialog on this page must NOT be answered automatically.

    Checkout dialogs are the user's decision, not the bot's. cdp.page
    .handle_java_script_dialog(accept=True) is the OK button of a confirm(),
    so auto-answering the checkout page's "Are you sure you want to cancel
    your order?" actually cancels the order (issue #378).
    """
    return '/ticket/checkout' in (current_url or '')


def _is_tixcraft_soft_block_scope(url):
    url_lower = (url or "").lower()
    return any(host in url_lower for host in _TIXCRAFT_SOFT_BLOCK_SCOPE_HOSTS)


def _parse_tixcraft_soft_block_delay(config_dict):
    advanced = (config_dict or {}).get("advanced", {})
    raw_value = advanced.get("tixcraft_soft_block_delay", "")

    if raw_value is None:
        return None

    text = str(raw_value).strip()
    if not text:
        return None

    try:
        delay_seconds = int(text)
    except (TypeError, ValueError):
        return None

    if delay_seconds <= 0:
        return None

    return min(delay_seconds, 600)


def _resolve_soft_block_wait_seconds(config_dict, scope_url, default_wait_seconds=None):
    custom_delay = _parse_tixcraft_soft_block_delay(config_dict)
    if custom_delay is not None and _is_tixcraft_soft_block_scope(scope_url):
        return custom_delay, True

    if default_wait_seconds is None:
        default_wait_seconds = random.randint(240, 420)

    return default_wait_seconds, False


def _process_queue_it_state(url, state, current_time):
    """Update queue-it tracking state and decide whether the main loop should pause.

    Returns:
        (should_pause: bool, elapsed_seconds: float | None)
        - should_pause=True means the caller must short-circuit the main loop
          (we're still inside *.queue-it.net).
        - elapsed_seconds is set to the wait duration when this call detects
          the queue has been passed; otherwise None.
    """
    url_lower = (url or "").lower()
    if 'queue-it.net' in url_lower:
        if state.get("queue_it_enter_time") is None:
            state["queue_it_enter_time"] = current_time
        return True, None
    if state.get("queue_it_enter_time") is not None:
        elapsed = current_time - state["queue_it_enter_time"]
        state["queue_it_enter_time"] = None
        return False, elapsed
    return False, None


def _is_page_reload_enabled(config_dict, debug, log_tag):
    """True when auto_reload_page_interval allows a polling reload (issue #396).

    The settings help defines 0 as "disable auto reload", but the date and
    area selection sites, which reload to retry when nothing was selected,
    only skipped the wait before tab.reload(), so 0 still reloaded the page,
    on most of them once per 50ms main-loop pass. A value <= 0 now skips
    those polling reloads. Recovery reloads (TixCraft ticket page, issue
    #174; Ticketmaster after 30 retries, issue #378) do not use this check
    and keep their original behavior.
    The skip is logged once per call site. A site's mark is cleared the next
    time that same site runs with an interval above 0, so a change between
    two values <= 0 is not logged again. The mark is only set while verbose
    is on, so a site that skipped while verbose was off still logs the first
    time verbose is on.
    """
    if not util.is_auto_refresh_enabled():
        return False

    interval = config_dict.get("advanced", {}).get("auto_reload_page_interval", 0)
    try:
        is_enabled = interval > 0
    except TypeError:
        # Non-numeric value (e.g. a hand-edited string): the Ticketmaster
        # date and area sites swallowed this TypeError inside try/except and
        # did not reload, while the TixCraft date and area sites raised it.
        # Treat it as reload disabled at every polling reload site.
        is_enabled = False
    logged_tags = _state.setdefault("reload_disabled_logged_tags", set())
    if is_enabled:
        logged_tags.discard(log_tag)
        return True
    if debug.enabled and log_tag not in logged_tags:
        debug.log(f"[{log_tag}] auto_reload_page_interval={interval!r}, page reload disabled")
        logged_tags.add(log_tag)
    return False


async def nodriver_tixcraft_home_close_window(tab):
    if _state.get('cookie_accepted'):
        return
    try:
        accept_all_cookies_btn = await tab.query_selector('#onetrust-accept-btn-handler')
        if accept_all_cookies_btn:
            await accept_all_cookies_btn.click()
            _state['cookie_accepted'] = True
    except:
        pass

async def nodriver_tixcraft_redirect(tab, url):
    ret = False
    game_name = ""
    url_split = url.split("/")
    if len(url_split) >= 6:
        # Strip URL fragment/query: tixcraft detail pages carry a tab anchor
        # (e.g. .../activity/detail/26_fireball#intro). Without this the fragment
        # leaks into game_name -> the buy-link selector never matches -> the bot
        # never redirects to /activity/game/ and stalls on the detail page.
        game_name = url_split[5].split("#")[0].split("?")[0]
    if len(game_name) > 0:
        if "/activity/detail/%s" % (game_name,) in url:
            # Issue #278: detail pages are server-side rendered. Until the sale
            # opens there is no anchor to /activity/game/{id}. Redirecting
            # blindly drops the user on an empty gameList page and makes manual
            # F5 look broken. Only redirect once the buy link actually exists.
            selector = 'a[href*="/activity/game/%s"]' % game_name
            js = 'document.querySelector(%s) !== null' % json.dumps(selector)
            has_buy_link = False
            try:
                has_buy_link = await tab.evaluate(js)
            except Exception:
                has_buy_link = False
            if not has_buy_link:
                return ret
            entry_url = url.replace("/activity/detail/", "/activity/game/").split("#")[0]
            print("redirect to new url:", entry_url)
            try:
                await navigate_page_if_enabled(tab, entry_url)
                # 等待日期列表出現，確保頁面載入完成
                try:
                    await tab.wait_for('#gameList > table > tbody > tr', timeout=5)
                except:
                    pass  # timeout 沒關係，讓後續邏輯處理
                ret = True
            except Exception as exec1:
                pass
    return ret

# ============================================
# Ticketmaster.com NoDriver Platform Migration
# ============================================
# Foundation Functions (T004-T007)
#

# T004: Parse zone_info JSON from #mapSelectArea
async def nodriver_ticketmaster_parse_zone_info(tab, config_dict):
    """
    Parse zone_info JavaScript variable from #mapSelectArea element.
    Returns: zone_info dict or None if parsing fails
    """
    debug = util.create_debug_logger(config_dict)

    zone_info = None

    # Try method 1: String extraction from innerHTML (get_attribute('innerHTML') returns None
    # because innerHTML is a DOM property, not an HTML attribute — use evaluate instead)
    try:
        mapSelectArea_html = await tab.evaluate(
            "document.getElementById('mapSelectArea')?.innerHTML || ''"
        )

        tag_start = "var zone ="
        tag_end = "fieldImageType"
        if mapSelectArea_html and tag_start in mapSelectArea_html and tag_end in mapSelectArea_html:
            zone_string = mapSelectArea_html.split(tag_start)[1]
            zone_string = zone_string.split(tag_end)[0]
            zone_string = zone_string.strip().rstrip('\n,')

            import json
            zone_info = json.loads(zone_string)
            if debug.enabled:
                debug.log(f"[TICKETMASTER ZONE] Parsed zone_info via string extraction ({len(zone_info)} zones)")
                if len(zone_info) > 0:
                    sample_id = list(zone_info.keys())[0]
                    sample = zone_info[sample_id]
                    if isinstance(sample, dict) and "groupName" in sample:
                        debug.log(f"[TICKETMASTER ZONE] Sample zone '{sample_id}' groupName: {sample['groupName']}")
            return zone_info

    except Exception as exc:
        debug.log(f"[TICKETMASTER ZONE] String extraction failed: {exc}")

    # Try method 2: Direct JavaScript evaluation (fallback)
    try:
        result = await tab.evaluate('''
            (function() {
                // Check if zone variable exists in global scope
                if (typeof zone !== 'undefined') {
                    // IMPORTANT: Use JSON.parse(JSON.stringify()) to serialize RemoteObject to plain JSON
                    // Without this, NoDriver returns CDP RemoteObject format:
                    // {"type": "object", "value": [["key", {"type": "string", "value": "..."}]]}
                    // Instead of standard JSON: {"key": "value"}
                    try {
                        return JSON.parse(JSON.stringify(zone));
                    } catch(e) {
                        console.error('Zone serialization failed:', e);
                        return zone;  // Fallback to RemoteObject if serialization fails
                    }
                }

                // Fallback: Extract from #mapSelectArea innerHTML
                const el = document.querySelector('#mapSelectArea');
                if (!el) return null;

                const html = el.innerHTML;
                const match = html.match(/var zone = ({[\\s\\S]*?});/);
                if (!match) return null;

                try {
                    return JSON.parse(match[1]);
                } catch(e) {
                    console.error('JSON parse failed:', e);
                    return null;
                }
            })();
        ''')

        if result:
            # Convert RemoteObject to standard Python types
            zone_info = convert_remote_object(result)

            if debug.enabled:
                zone_type = "dict" if isinstance(zone_info, dict) else "list"
                debug.log(f"[TICKETMASTER ZONE] Successfully parsed zone_info ({len(zone_info)} zones, type: {zone_type})")
                debug.log(f"[TICKETMASTER ZONE] RemoteObject converted to standard format")

                # Print detailed structure for debugging
                if len(zone_info) > 0:
                    try:
                        # Print sample zone keys (BEFORE json.dumps to avoid serialization issues)
                        if isinstance(zone_info, list):
                            sample = zone_info[0]
                            sample_id = "index_0"

                            # Diagnostic for List format
                            debug.log(f"[TICKETMASTER ZONE] List first item type: {type(sample)}")

                            if isinstance(sample, dict):
                                sample_keys = list(sample.keys())[:10]  # First 10 keys
                                debug.log(f"[TICKETMASTER ZONE] List item keys (first 10): {sample_keys}")

                                # Check for zone_id fields
                                zone_id_field = None
                                for field in ["sectionCode", "id", "zoneId", "areaNo"]:
                                    if field in sample:
                                        zone_id_field = field
                                        debug.log(f"[TICKETMASTER ZONE] Found zone_id field: '{field}' = '{sample.get(field)}'")
                                        break

                                if not zone_id_field:
                                    debug.log(f"[TICKETMASTER ZONE] WARNING: No zone_id field found (sectionCode, id, zoneId, areaNo)")

                            elif isinstance(sample, (list, tuple)):
                                debug.log(f"[TICKETMASTER ZONE] List item is tuple/list with {len(sample)} elements")
                                if len(sample) > 0:
                                    debug.log(f"[TICKETMASTER ZONE] First element type: {type(sample[0])}")
                                    if isinstance(sample[0], str):
                                        debug.log(f"[TICKETMASTER ZONE] First element (zone_id): {sample[0]}")
                                if len(sample) > 1:
                                    debug.log(f"[TICKETMASTER ZONE] Second element type: {type(sample[1])}")
                                    zone_data = sample[1]
                                    if isinstance(zone_data, dict):
                                        # Check if conversion successful
                                        if "groupName" in zone_data:
                                            debug.log(f"[TICKETMASTER ZONE] [OK] groupName found: {zone_data.get('groupName')}")
                                        elif "type" in zone_data and "value" in zone_data:
                                            debug.log(f"[TICKETMASTER ZONE] [FAIL] Still RemoteObject format (has 'type' and 'value' keys)")
                                            # Try to convert again
                                            zone_data = convert_remote_object(zone_data)
                                            # Update in the list
                                            sample[1] = zone_data
                                            zone_info[0] = sample
                                            if "groupName" in zone_data:
                                                debug.log(f"[TICKETMASTER ZONE] [OK] After re-conversion, groupName found: {zone_data.get('groupName')}")
                                        else:
                                            debug.log(f"[TICKETMASTER ZONE] zone_data keys: {list(zone_data.keys())[:10]}")
                            else:
                                debug.log(f"[TICKETMASTER ZONE] WARNING: Unknown list item format")
                        else:
                            # Dict format
                            sample_id = list(zone_info.keys())[0]
                            sample = zone_info[sample_id]
                            debug.log(f"[TICKETMASTER ZONE] Sample zone_id: {sample_id}")

                        sample_keys = list(sample.keys()) if isinstance(sample, dict) else []
                        if sample_keys:
                            debug.log(f"[TICKETMASTER ZONE] Sample structure keys: {sample_keys[:10]}")  # First 10 keys
                    except Exception as diag_exc:
                        debug.log(f"[TICKETMASTER ZONE] Diagnostic logging failed: {diag_exc}")

    except Exception as exc:
        debug.log(f"[TICKETMASTER ZONE] JavaScript evaluation failed: {exc}")

    return zone_info

# T005: Get target area from zone_info (Pure function - no DOM access)
def get_ticketmaster_target_area(config_dict, area_keyword_item, zone_info):
    """
    Match areas from zone_info based on keyword.
    Returns: (is_need_refresh, matched_blocks)
    """
    debug = util.create_debug_logger(config_dict)

    area_auto_select_mode = config_dict.get("area_auto_select", {}).get("mode", "from top to bottom")

    is_need_refresh = False
    matched_blocks = []

    if not zone_info or len(zone_info) == 0:
        return True, None

    # Normalize zone_info to uniform iteration format
    # Dict format: {"zone_id": {...}} → [("zone_id", {...}), ...]
    # List format (3 types):
    #   Type A: [{"sectionCode": "field_C1_B", ...}, {...}] → extract sectionCode as zone_id
    #   Type B: [["field_C1_B", {...}], ...] → unpack tuple/list
    #   Type C: [(zone_id, {...}), ...] → already in correct format

    if isinstance(zone_info, dict):
        # Dict format: standard case
        zone_items = list(zone_info.items())
    elif isinstance(zone_info, list):
        # List format: need to detect which type
        if len(zone_info) == 0:
            zone_items = []
        else:
            first_item = zone_info[0]

            if isinstance(first_item, dict):
                # Type A: List of dicts - extract zone_id from dict
                zone_items = []
                for z in zone_info:
                    if not isinstance(z, dict):
                        continue
                    zone_id = z.get("sectionCode") or z.get("id") or z.get("zoneId") or z.get("areaNo")
                    zone_items.append((zone_id, z))

            elif isinstance(first_item, (list, tuple)) and len(first_item) >= 2:
                # Type B: List of [id, data] pairs
                zone_items = []
                for item in zone_info:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        zone_id = item[0]
                        zone_data = item[1]
                        # Convert RemoteObject if needed
                        if isinstance(zone_data, dict) and "type" in zone_data and "value" in zone_data:
                            zone_data = convert_remote_object(zone_data)
                        zone_items.append((zone_id, zone_data))

            else:
                # Unknown format - fallback to old logic
                debug.log(f"[TICKETMASTER AREA] Unknown zone_info list format, first item type: {type(first_item)}")
                zone_items = [(None, z) for z in zone_info]
    else:
        # Unexpected type
        debug.log(f"[TICKETMASTER AREA] Unexpected zone_info type: {type(zone_info)}")
        zone_items = []

    for zone_id, zone_data in zone_items:
        # Validate zone_data is dict-like (has .get() method)
        if not hasattr(zone_data, 'get'):
            debug.log(f"[TICKETMASTER AREA] zone_data is not dict-like: {type(zone_data)}, skipping")
            continue

        # Fallback: extract zone_id if still None
        if zone_id is None:
            zone_id = zone_data.get("sectionCode") or zone_data.get("id") or zone_data.get("zoneId") or zone_data.get("areaNo")

        row_is_enabled = zone_data.get("areaStatus") != "UNAVAILABLE"

        if not row_is_enabled:
            continue

        # Build row text from zone info
        row_text = ""
        group_name = ""
        description = ""
        try:
            group_name = zone_data.get("groupName", "")
            description = zone_data.get("description", "")
            row_text = group_name + " " + description
            if "price" in zone_data and len(zone_data["price"]) > 0:
                row_text += " " + zone_data["price"][0].get("ticketPrice", "")
        except:
            pass

        if debug.enabled:
            # Show human-readable zone info instead of just zone_id
            display_name = f"{group_name} {description}".strip() if group_name or description else zone_id
            debug.log(f"[TICKETMASTER AREA] Processing zone: {zone_id} ({display_name})")

        if not row_text.strip():
            continue

        # Check exclude keywords
        if util.reset_row_text_if_match_keyword_exclude(config_dict, row_text):
            continue

        # Format and match keywords
        row_text = util.format_keyword_string(row_text)

        is_append_this_row = False
        if area_keyword_item:
            # Must match all keywords (AND logic)
            area_keyword_array = area_keyword_item.split(' ')

            # Word boundary matching function
            import re
            def word_boundary_match(keyword, text):
                """
                Match keyword with word boundary awareness.
                - Single char keywords (like 'I') require word boundaries
                - Multi-char keywords use substring match for flexibility
                """
                formatted_kw = util.format_keyword_string(keyword)
                if len(formatted_kw) <= 2:
                    # Short keywords need word boundary to avoid false positives
                    # e.g., 'I' should not match 'CIRCLE'
                    pattern = r'\b' + re.escape(formatted_kw) + r'\b'
                    return bool(re.search(pattern, text, re.IGNORECASE))
                else:
                    # Longer keywords use substring match
                    return formatted_kw in text

            # Detailed AND logic matching with PASS/FAIL logs
            if debug.enabled:
                keyword_results = []
                for kw in area_keyword_array:
                    match_result = word_boundary_match(kw, row_text)
                    status = "PASS" if match_result else "FAIL"
                    keyword_results.append(f"'{kw}':{status}")

                all_matched = all(
                    word_boundary_match(kw, row_text)
                    for kw in area_keyword_array
                )
                overall_status = "MATCHED" if all_matched else "REJECTED"
                debug.log(f"[TICKETMASTER AREA] AND Match: {zone_id} [{', '.join(keyword_results)}] -> {overall_status}")

            is_append_this_row = all(
                word_boundary_match(kw, row_text)
                for kw in area_keyword_array
            )
        else:
            # No keyword = match all
            is_append_this_row = True

        if is_append_this_row:
            matched_blocks.append(zone_id)

            if area_auto_select_mode == "from top to bottom":
                # Only need first match
                break

    if len(matched_blocks) == 0:
        matched_blocks = None
        is_need_refresh = True

    if matched_blocks:
        debug.log(f"[TICKETMASTER AREA] Matched {len(matched_blocks)} areas: {matched_blocks}")

    return is_need_refresh, matched_blocks

# T006: Get ticket price list (wait for page load)
async def nodriver_ticketmaster_get_ticketPriceList(tab, config_dict):
    """
    Wait for ticketPriceList to load and return the table element.
    Uses official NoDriver API (stable, recommended approach).
    Returns: table element or None

    References:
    - Fixed based on famiticket_nodriver_fixes.md (Phase 4: NoDriver Official API Migration)
    - Issue: tab.evaluate() returns None due to JavaScript Context failure
    - Solution: Use tab.wait_for() and tab.query_selector() instead
    """
    debug = util.create_debug_logger(config_dict)

    try:
        # Phase 1: Wait for mapContainer (basic page load)
        await tab.wait_for(selector='#mapContainer', timeout=5)

        # Ensure DOM references are synchronized (official recommendation)
        await tab

        debug.log("[TICKETMASTER TICKET] mapContainer found")

        # Phase 2: Wait for loading to finish (check if loadingmap disappears)
        max_wait = 10  # 10 seconds max
        for i in range(max_wait):
            loading = await tab.query_selector('#loadingmap')
            if not loading:
                if i > 0:
                    debug.log(f"[TICKETMASTER TICKET] Loading finished after {i}s")
                break
            await tab.sleep(1)
        else:
            # Timeout after 10 seconds
            debug.log("[TICKETMASTER TICKET] Loading timeout after 10s")

        # Phase 3: Try to find ticketPriceList
        table_element = await tab.query_selector('#ticketPriceList')

        if table_element:
            debug.log("[TICKETMASTER TICKET] Found ticketPriceList table")
            return table_element
        else:
            debug.log("[TICKETMASTER TICKET] ticketPriceList not found, will use zone_info")
            return None

    except asyncio.TimeoutError:
        debug.log("[TICKETMASTER TICKET] Timeout waiting for mapContainer")
        return None
    except Exception as e:
        debug.log(f"[TICKETMASTER TICKET] Error: {e}")
        return None

# ============================================
# User Story 1: Date Auto Select (T009)
# ============================================

async def nodriver_ticketmaster_date_auto_select(tab, config_dict):
    """
    Automatically select event date on Ticketmaster artist page.
    Returns: True if date was clicked, False otherwise
    """
    debug = util.create_debug_logger(config_dict)

    # Read config
    auto_select_mode = config_dict.get("date_auto_select", {}).get("mode", "from top to bottom")
    date_keyword = config_dict.get("date_auto_select", {}).get("date_keyword", "").strip()
    pass_date_is_sold_out_enable = config_dict.get("tixcraft", {}).get("pass_date_is_sold_out", False)
    # The key only exists under "tixcraft" (settings.py get_default_config); every
    # other platform reads it from there too. Reading "kktix" made this always
    # False, so a Ticketmaster page never auto-reloaded while waiting for the
    # sale to open (issue #378).
    auto_reload_coming_soon_page_enable = (config_dict.get("tixcraft", {}).get("auto_reload_coming_soon_page", False)) and util.is_auto_refresh_enabled()

    sold_out_text_list = ["Sold out", "No tickets available"]
    find_ticket_text_list = ['Find tickets', 'See Tickets']

    # Query date list
    # Ticketmaster.sg uses a table structure: #gameList tbody tr
    # Wait for dynamic content to load (max 5 seconds)
    area_list = None
    max_attempts = 10
    for attempt in range(max_attempts):
        try:
            area_list = await tab.query_selector_all('#gameList tbody tr')
            if area_list and len(area_list) > 0:
                debug.log(f"[TICKETMASTER DATE] Found date list after {attempt * 0.5}s")
                break
            await asyncio.sleep(0.5)
        except Exception as exc:
            if attempt == 0:
                debug.log(f"[TICKETMASTER DATE] Waiting for date list to load... ({exc})")
            await asyncio.sleep(0.5)

    if not area_list:
        debug.log(f"[TICKETMASTER DATE] Failed to find date list after {max_attempts * 0.5}s")
        return False

    matched_blocks = None
    formated_area_list = []

    if not area_list or len(area_list) == 0:
        debug.log("[TICKETMASTER DATE] No dates found on page")
        return False

    debug.log(f"[TICKETMASTER DATE] Found {len(area_list)} date blocks")

    # Filter date blocks
    for row in area_list:
        try:
            row_html = await row.get_html()
            row_text = util.remove_html_tags(row_html)
        except:
            break

        if not row_text:
            continue

        row_is_enabled = False

        # Must contain "See Tickets"
        for text_item in find_ticket_text_list:
            if text_item in row_text:
                row_is_enabled = True
                break

        # Check sold out
        if row_is_enabled and pass_date_is_sold_out_enable:
            for sold_out_item in sold_out_text_list:
                if sold_out_item in row_text:
                    row_is_enabled = False
                    debug.log(f"[TICKETMASTER DATE] Skipping sold out event: {row_text[:60]}...")
                    break

        if row_is_enabled:
            formated_area_list.append(row)

    debug.log(f"[TICKETMASTER DATE] {len(formated_area_list)} available dates after filtering")

    # Get date_auto_fallback setting (default: False = strict mode)
    date_auto_fallback = config_dict.get('date_auto_fallback', False)

    # Build text list for keyword matching
    formated_area_list_text = []
    for row in formated_area_list:
        try:
            row_html = await row.get_html()
            row_text = util.remove_html_tags(row_html)
            formated_area_list_text.append(row_text)
        except:
            formated_area_list_text.append("")

    # T004-T008: Early return pattern (Feature 003)
    if not date_keyword:
        matched_blocks = formated_area_list
        debug.log(f"[TICKETMASTER DATE KEYWORD] No keyword specified, using all {len(formated_area_list)} dates")
    else:
        # Early return pattern - iterate keywords in priority order
        matched_blocks = []
        target_row_found = False
        keyword_matched_index = -1

        try:
            import json
            import re
            keyword_array = json.loads("[" + date_keyword + "]")

            # T005: Start checking keywords log
            debug.log(f"[TICKETMASTER DATE KEYWORD] Start checking keywords in order: {keyword_array}")
            debug.log(f"[TICKETMASTER DATE KEYWORD] Total keyword groups: {len(keyword_array)}")
            debug.log(f"[TICKETMASTER DATE KEYWORD] Checking against {len(formated_area_list_text)} available dates...")

            # Iterate keywords in priority order (early return)
            for keyword_index, keyword_item_set in enumerate(keyword_array):
                debug.log(f"[TICKETMASTER DATE KEYWORD] Checking keyword #{keyword_index + 1}: {keyword_item_set}")

                # Check all rows for this keyword
                for i, row_text in enumerate(formated_area_list_text):
                    normalized_row_text = re.sub(r'\s+', ' ', row_text)
                    is_match = False

                    if isinstance(keyword_item_set, str):
                        # OR logic: single keyword
                        normalized_keyword = re.sub(r'\s+', ' ', keyword_item_set)
                        is_match = normalized_keyword in normalized_row_text
                    elif isinstance(keyword_item_set, list):
                        # AND logic: all keywords must match
                        normalized_keywords = [re.sub(r'\s+', ' ', kw) for kw in keyword_item_set]
                        match_results = [kw in normalized_row_text for kw in normalized_keywords]
                        is_match = all(match_results)

                        # Detailed AND logic log
                        if debug.enabled:
                            result_strs = [f"'{kw}':{('PASS' if r else 'FAIL')}" for kw, r in zip(keyword_item_set, match_results)]
                            overall = "MATCHED" if is_match else "REJECTED"
                            debug.log(f"[TICKETMASTER DATE KEYWORD] AND Match: [{', '.join(result_strs)}] -> {overall}")

                    if is_match:
                        # T006: Keyword matched - IMMEDIATELY select and stop
                        matched_blocks = [formated_area_list[i]]
                        target_row_found = True
                        keyword_matched_index = keyword_index
                        debug.log(f"[TICKETMASTER DATE KEYWORD] Keyword #{keyword_index + 1} matched: '{keyword_item_set}'")
                        debug.log(f"[TICKETMASTER DATE SELECT] Selected date: {row_text[:80]} (keyword match)")
                        break  # Early Return - stop checking other rows

                if target_row_found:
                    # EARLY RETURN: Stop checking further keywords
                    break

            # T007: All keywords failed log
            if not target_row_found:
                debug.log(f"[TICKETMASTER DATE KEYWORD] All keywords failed to match")

        except Exception as e:
            debug.log(f"[TICKETMASTER DATE KEYWORD] Parsing error: {e}")
            matched_blocks = []

    # Match result summary
    debug.log(f"[TICKETMASTER DATE KEYWORD] ========================================")
    debug.log(f"[TICKETMASTER DATE KEYWORD] Match Summary:")
    debug.log(f"[TICKETMASTER DATE KEYWORD]   Total dates available: {len(formated_area_list) if formated_area_list else 0}")
    debug.log(f"[TICKETMASTER DATE KEYWORD]   Total dates matched: {len(matched_blocks) if matched_blocks else 0}")
    debug.log(f"[TICKETMASTER DATE KEYWORD] ========================================")

    # T018-T020: Conditional fallback based on date_auto_fallback switch
    if matched_blocks is not None and len(matched_blocks) == 0 and date_keyword and formated_area_list is not None and len(formated_area_list) > 0:
        if date_auto_fallback:
            # T018: Fallback enabled - use auto_select_mode
            debug.log(f"[TICKETMASTER DATE FALLBACK] date_auto_fallback=true, triggering auto fallback")
            debug.log(f"[TICKETMASTER DATE FALLBACK] Selecting available date based on date_select_order='{auto_select_mode}'")
            matched_blocks = formated_area_list
        else:
            # T019: Fallback disabled - strict mode
            debug.log(f"[TICKETMASTER DATE FALLBACK] date_auto_fallback=false, fallback is disabled")
            debug.log(f"[TICKETMASTER DATE SELECT] No date selected, will reload page and retry")

    # Select target
    if formated_area_list is None or len(formated_area_list) == 0:
        target_area = None
    elif matched_blocks is None or len(matched_blocks) == 0:
        target_area = None
    else:
        target_area = util.get_target_item_from_matched_list(matched_blocks, auto_select_mode)

    is_date_clicked = False
    if target_area:
        try:
            # Click "See Tickets" link
            link_element = await target_area.query_selector('a')
            if link_element:
                # Snapshot the tabs that already exist, so only a tab spawned by
                # THIS click is closed afterwards. Closing tabs[1:] wholesale
                # also killed tabs the user had opened themselves to queue other
                # dates (issue #378).
                known_target_ids = set()
                try:
                    for known_tab in tab.browser.tabs:
                        known_id = getattr(getattr(known_tab, "target", None), "target_id", None)
                        if known_id:
                            known_target_ids.add(known_id)
                except Exception:
                    pass

                await link_element.click()
                is_date_clicked = True
                debug.log("[TICKETMASTER DATE] Clicked 'See Tickets' link")

                # Handle new tab (close only what this click opened)
                await tab.sleep(0.3)
                for extra_tab in list(tab.browser.tabs):
                    extra_id = getattr(getattr(extra_tab, "target", None), "target_id", None)
                    if not extra_id or extra_id in known_target_ids:
                        continue
                    try:
                        await extra_tab.close()
                        debug.log("[TICKETMASTER DATE] Closed the tab opened by the click")
                    except Exception:
                        pass
                    await tab.sleep(0.2)

        except Exception as exc:
            debug.log(f"[TICKETMASTER DATE] Failed to click link: {exc}")

    # Auto reload if no match
    if auto_reload_coming_soon_page_enable and not is_date_clicked and len(formated_area_list) == 0 and _is_page_reload_enabled(config_dict, debug, "TICKETMASTER DATE"):
        debug.log("[TICKETMASTER DATE] No dates available, reloading page...")
        try:
            # Honor the user's refresh interval, the same way the area-select
            # path does. The main loop spins every 50ms, so reloading without a
            # delay here means 2-3 page loads per second while waiting for a
            # sale to open -- exactly what trips Ticketmaster's EPS block
            # (issue #378). sleep_with_pause_check keeps the pause button
            # responsive during the wait.
            reload_interval = config_dict.get("advanced", {}).get("auto_reload_page_interval", 0)
            if reload_interval > 0:
                await sleep_with_pause_check(tab, reload_interval, config_dict)
            await reload_page_if_enabled(tab)
            await tab.sleep(0.3)
        except:
            pass

    return is_date_clicked

# ============================================
# User Story 2: Area Auto Select (T012)
# ============================================

async def nodriver_ticketmaster_area_auto_select(tab, config_dict, zone_info):
    """
    Automatically select seat area on Ticketmaster ticket page.
    """
    debug = util.create_debug_logger(config_dict)

    area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "").strip()

    debug.log(f"[TICKETMASTER AREA] area_keyword: {area_keyword}")

    is_need_refresh = False
    matched_blocks = None

    # Get area_auto_fallback setting (default: False = strict mode)
    area_auto_fallback = config_dict.get("area_auto_fallback", False)

    if area_keyword:
        area_keyword_array = util.parse_keyword_string_to_array(area_keyword)

        debug.log(f"[TICKETMASTER AREA] Parsed keyword groups: {area_keyword_array}")

        # Early Return Pattern: Try each keyword group with priority
        for idx, area_keyword_item in enumerate(area_keyword_array):
            debug.log(f"[TICKETMASTER AREA] Trying keyword group {idx + 1}/{len(area_keyword_array)}: '{area_keyword_item}'")

            is_need_refresh, matched_blocks = get_ticketmaster_target_area(config_dict, area_keyword_item, zone_info)

            if not is_need_refresh and matched_blocks:
                # Found match - Early Return
                debug.log(f"[TICKETMASTER AREA] Early Return: keyword group {idx + 1} matched {len(matched_blocks)} area(s)")
                break
            else:
                debug.log(f"[TICKETMASTER AREA] Keyword group {idx + 1} had no matches, trying next...")

        # Conditional fallback: only match all if area_auto_fallback is enabled
        if is_need_refresh:
            if area_auto_fallback:
                debug.log("[TICKETMASTER AREA] Fallback enabled: selecting from all available areas")
                is_need_refresh, matched_blocks = get_ticketmaster_target_area(config_dict, "", zone_info)
            else:
                debug.log("[TICKETMASTER AREA] Strict mode: no keyword match, will refresh page")
                # Keep is_need_refresh = True, matched_blocks = None
    else:
        # Empty keyword = match all
        is_need_refresh, matched_blocks = get_ticketmaster_target_area(config_dict, "", zone_info)

    # Select target
    auto_select_mode = config_dict.get("area_auto_select", {}).get("mode", "from top to bottom")
    target_area = util.get_target_item_from_matched_list(matched_blocks, auto_select_mode)

    if target_area:
        try:
            # Execute JavaScript to select area
            click_area_javascript = f'areaTicket("{target_area}", "map");'
            debug.log(f"[TICKETMASTER AREA] Executing: {click_area_javascript}")

            await tab.evaluate(click_area_javascript)

            # Wait for AJAX to load ticketPriceList (areaTicket executes AJAX request)
            max_wait = 5  # 5 seconds max
            for i in range(max_wait):
                await tab.sleep(1)

                # Check if ticketPriceList has loaded
                price_list = await tab.query_selector('#ticketPriceList')
                if price_list:
                    debug.log(f"[TICKETMASTER AREA] ticketPriceList loaded after {i+1}s")
                    break
            else:
                debug.log("[TICKETMASTER AREA] Timeout waiting for ticketPriceList (5s)")

            debug.log(f"[TICKETMASTER AREA] Selected zone: {target_area}")

        except Exception as exc:
            debug.log(f"[TICKETMASTER AREA] Failed to execute JavaScript: {exc}")

    # Auto refresh if needed (only when keyword is specified but no match)
    if is_need_refresh:
        # Check if area_keyword is empty (empty = should match all areas)
        area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "").strip()

        if area_keyword and _is_page_reload_enabled(config_dict, debug, "TICKETMASTER AREA"):
            # Keyword specified but no match → might need to wait for availability
            debug.log("[TICKETMASTER AREA] No areas matched keyword, reloading page...")
            try:
                if config_dict.get("advanced", {}).get("auto_reload_page_interval", 0) > 0:
                    await sleep_before_auto_refresh(config_dict["advanced"]["auto_reload_page_interval"])
                await reload_page_if_enabled(tab)
            except:
                pass
        elif not area_keyword:
            # No keyword but no areas → likely a data parsing issue, don't reload
            debug.log("[TICKETMASTER AREA] No areas available (possible zone_info parsing issue)")
            # Let next function (assign_ticket_number) handle it

# ============================================
# User Story 3: Ticket Number Assignment (T015-T016)
# ============================================

async def nodriver_ticketmaster_assign_ticket_number(tab, config_dict):
    """
    Automatically set ticket number on Ticketmaster ticket page.
    Caller is responsible for area selection before calling this function.
    Returns True if ticket number was set, False otherwise.
    """
    debug = util.create_debug_logger(config_dict)

    # Get ticket price list (area must already be selected by caller)
    table_select = await nodriver_ticketmaster_get_ticketPriceList(tab, config_dict)
    if not table_select:
        return False

    # Find select element
    select_element = None
    try:
        select_element = await table_select.query_selector('select')
    except Exception as exc:
        debug.log(f"[TICKETMASTER TICKET] Failed to find select: {exc}")
        return False

    if not select_element:
        debug.log("[TICKETMASTER TICKET] No select element found")
        return False

    # Update element to sync attributes
    try:
        await select_element.update()
    except:
        pass

    # Check if element is enabled
    try:
        select_attrs = select_element.attrs or {}
        is_disabled = 'disabled' in select_attrs
        if is_disabled:
            debug.log("[TICKETMASTER TICKET] Select element is disabled")
            return False
    except Exception as exc:
        debug.log(f"[TICKETMASTER TICKET] Failed to check disabled status: {exc}")
        pass

    # Ticketmaster shares the TixCraft backend but has its own DOM, so it needs
    # its own allow_less_tickets handling (the TixCraft path lives in
    # nodriver_ticket_number_select_fill).
    ticket_number = str(config_dict.get("ticket_number", 1))
    allow_less_tickets = config_dict.get("tixcraft", {}).get("allow_less_tickets", False)

    # Check current value (zendriver evaluate returns Python values directly)
    select_attrs = select_element.attrs or {}
    selector_id = select_attrs.get('id')
    current_value = None
    if selector_id:
        try:
            current_value = await tab.evaluate(f'''
                (function() {{
                    const selectEl = document.getElementById('{selector_id}');
                    if (selectEl && selectEl.selectedIndex >= 0) {{
                        return selectEl.options[selectEl.selectedIndex].text;
                    }}
                    return null;
                }})();
            ''')
        except:
            pass

    if current_value and current_value != "0" and current_value.isnumeric():
        # A leftover value from an earlier round (or from the site itself) must
        # still satisfy the target, otherwise the order is submitted with the
        # wrong count. Only accept fewer tickets when the user opted in.
        current_count = int(current_value)
        target_count = int(ticket_number)
        is_acceptable = (current_count == target_count) or (
            allow_less_tickets and 0 < current_count < target_count
        )
        if is_acceptable:
            debug.log(f"[TICKETMASTER TICKET] Ticket number already set to: {current_value}")
            try:
                auto_mode_button = await tab.query_selector('#autoMode')
                if auto_mode_button:
                    await auto_mode_button.click()
                    debug.log("[TICKETMASTER TICKET] Clicked #autoMode button")
            except:
                pass
            return True
        debug.log(f"[TICKETMASTER TICKET] Existing ticket number {current_value} "
                  f"does not satisfy target {ticket_number}, re-selecting")

    try:
        # Get select element ID for JavaScript manipulation
        select_attrs = select_element.attrs or {}
        selector_id = select_attrs.get('id')
        if not selector_id:
            debug.log("[TICKETMASTER TICKET] Select element has no id attribute")
            return False

        # Dump all option texts for debugging
        option_texts = await tab.evaluate(f'''
            (function(elementId) {{
                const selectEl = document.getElementById(elementId);
                if (!selectEl) return [];
                return Array.from(selectEl.options).map(o => o.text + '|' + o.value);
            }})('{selector_id}');
        ''')
        debug.log(f"[TICKETMASTER TICKET] Available options: {option_texts}")

        # Use JavaScript to set select value. Without allow_less_tickets the exact
        # count is mandatory; with it, fall back to the largest count BELOW the
        # target (never above it -- that would buy more than the user asked for).
        result = await tab.evaluate(f'''
            (function(elementId, targetText, targetCount, allowLess) {{
                const selectEl = document.getElementById(elementId);
                if (!selectEl) {{
                    return {{ success: false, error: "Element not found" }};
                }}
                const options = selectEl.options;
                // Try exact text match first
                for (let i = 0; i < options.length; i++) {{
                    if (options[i].text === targetText) {{
                        selectEl.selectedIndex = i;
                        selectEl.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return {{ success: true, value: options[i].value, selected: options[i].text }};
                    }}
                }}
                if (!allowLess) {{
                    return {{ success: false, error: "Exact ticket count unavailable" }};
                }}
                // Fallback: largest available count below the target
                let maxIdx = -1;
                let maxVal = 0;
                for (let i = 0; i < options.length; i++) {{
                    const v = parseInt(options[i].value);
                    if (!isNaN(v) && v > 0 && v < targetCount && v > maxVal) {{
                        maxVal = v;
                        maxIdx = i;
                    }}
                }}
                if (maxIdx >= 0) {{
                    selectEl.selectedIndex = maxIdx;
                    selectEl.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return {{ success: true, value: options[maxIdx].value, selected: options[maxIdx].text, fallback: true }};
                }}
                return {{ success: false, error: "Option not found" }};
            }})({json.dumps(selector_id)}, {json.dumps(ticket_number)}, {json.dumps(int(ticket_number))}, {json.dumps(bool(allow_less_tickets))});
        ''')

        if result and result.get('success'):
            selected = result.get('selected', ticket_number)
            if result.get('fallback'):
                debug.log(f"[TICKETMASTER TICKET] Exact '{ticket_number}' not found, "
                          f"allow_less_tickets enabled, selected: {selected}")
            else:
                debug.log(f"[TICKETMASTER TICKET] Set ticket number to: {selected}")

            # Click autoMode button
            await tab.sleep(0.1)
            try:
                auto_mode_button = await tab.query_selector('#autoMode')
                if auto_mode_button:
                    await auto_mode_button.click()
                    debug.log("[TICKETMASTER TICKET] Clicked #autoMode button")
            except:
                pass
            return True
        else:
            debug.log(f"[TICKETMASTER TICKET] Failed to set ticket number: {result.get('error') if result else 'no result'}")
            return False

    except Exception as exc:
        debug.log(f"[TICKETMASTER TICKET] Exception setting ticket number: {exc}")
        return False

# ============================================
# User Story 4: Captcha Handling (T019)
# ============================================

async def _wait_for_captcha_input(tab):
    """True once #TicketForm_verifyCode exists, within the wait limit.

    Polls on wall-clock time, so pressing pause mid-wait neither shortens it
    nor gives up early; the OCR loop handles pause. Each poll catches its own
    error: a CDP error while the form is still rendering means "not yet".
    tab.wait_for re-raises those and would give up on the first one.
    """
    deadline = time.monotonic() + CONST_TICKETMASTER_CAPTCHA_WAIT_SEC
    while True:
        try:
            if await tab.query_selector('#TicketForm_verifyCode'):
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        await tab.sleep(0.5)


async def nodriver_ticketmaster_captcha(tab, config_dict, ocr, captcha_browser):
    """
    Handle captcha on Ticketmaster check-captcha page.
    Returns: True if captcha was handled, False otherwise
    """
    debug = util.create_debug_logger(config_dict)

    # Check agree checkbox
    for _ in range(2):
        is_checked = await nodriver_check_checkbox(tab, '#TicketForm_agree')
        if is_checked:
            debug.log("[TICKETMASTER CAPTCHA] Checked TicketForm_agree")
            break

    # Alert state tracked by event handler
    alert_state = {"detected": False, "message": ""}

    async def on_captcha_alert(event: cdp.page.JavascriptDialogOpening):
        if not isinstance(event, cdp.page.JavascriptDialogOpening):
            return
        alert_state["detected"] = True
        alert_state["message"] = event.message
        debug.log(f"[TICKETMASTER CAPTCHA] Alert event: '{event.message[:60]}'")

        # This handler is registered on the check-captcha page but never removed
        # (zendriver's tab.remove_handlers drops EVERY handler of the type,
        # including the global one), so it stays alive for the rest of the
        # session. Without the guards below it answered OK to the checkout
        # page's "cancel your order?" confirm and cancelled the order (#378).
        if os.path.exists(util.get_instance_state_path(CONST_MAXBOT_INT28_FILE)):
            return
        # tab.target.url is the CDP-cached value; js_dumps would hang here
        # because an open dialog blocks JavaScript execution.
        current_url = tab.target.url if hasattr(tab, 'target') and tab.target else ""
        if _should_leave_dialog_to_user(current_url):
            debug.log("[TICKETMASTER CAPTCHA] Alert on checkout page, NOT auto-dismissing")
            return

        # Dismiss the alert immediately to prevent blocking
        try:
            await tab.send(cdp.page.handle_java_script_dialog(accept=True))
            debug.log("[TICKETMASTER CAPTCHA] Alert auto-dismissed by handler")
        except:
            pass

    # Register handler for this captcha session
    tab.add_handler(cdp.page.JavascriptDialogOpening, on_captcha_alert)

    # Handle captcha
    if not config_dict.get("ocr_captcha", {}).get("enable", False):
        # OCR disabled - manual input
        await nodriver_tixcraft_keyin_captcha_code(tab, answer="", auto_submit=False, config_dict=config_dict)
        return False
    else:
        # OCR enabled - auto recognition
        previous_answer = None
        current_url = tab.target.url
        fail_count = 0
        total_fail_count = 0

        # The caller marks this URL done once we return, so an input that has
        # not rendered yet must be waited for here, not skipped.
        if not await _wait_for_captcha_input(tab):
            print("[TICKETMASTER CAPTCHA] Captcha input did not appear. "
                  "Please enter the captcha manually.")
            return False

        # Once the input is there, give the captcha image a moment to draw.
        await asyncio.sleep(random.uniform(0.5, 1.0))

        for redo_ocr in range(99):
            try:
                alert_state["detected"] = False  # Reset before each attempt

                away_from_keyboard_enable = config_dict.get("ocr_captcha", {}).get("force_submit", False)
                ocr_captcha_image_source = config_dict.get("ocr_captcha", {}).get("image_source", "canvas")
                domain_name = tab.target.url.split('/')[2]

                # Call tixcraft_auto_ocr for captcha recognition
                is_need_redo_ocr, previous_answer, is_form_submitted = await nodriver_tixcraft_auto_ocr(
                    tab, config_dict, ocr, away_from_keyboard_enable, previous_answer,
                    captcha_browser, ocr_captcha_image_source, domain_name
                )

                if is_form_submitted:
                    debug.log("[TICKETMASTER CAPTCHA] Form submitted")

                    # Poll for alert event (max 2 seconds)
                    for wait_i in range(10):
                        await asyncio.sleep(0.2)
                        if alert_state["detected"]:
                            break

                    debug.log(f"[TICKETMASTER CAPTCHA] alert_state={alert_state}")

                    error_detected = alert_state["detected"]

                    # If alert was detected (already dismissed by handler), retry OCR
                    if error_detected:
                        debug.log("[TICKETMASTER CAPTCHA] Captcha error detected, retrying...")

                        await asyncio.sleep(0.3)
                        await nodriver_tixcraft_reload_captcha(tab, domain_name)
                        previous_answer = None
                        fail_count = 0
                        total_fail_count += 1

                        if total_fail_count >= 15:
                            print("[TICKETMASTER CAPTCHA] Failed 15 times. Manual input required.")
                            await nodriver_tixcraft_keyin_captcha_code(tab, config_dict=config_dict)
                            break

                        await asyncio.sleep(random.uniform(0.5, 1.0))
                        continue

                    # Check for Ticketmaster custom error modal (not native alert)
                    # The modal shows "The verification code that you entered is incorrect"
                    try:
                        # Check for modal overlay or dialog
                        modal_result = await tab.evaluate('''
                            (function() {
                                // Check for visible modal or alert dialog
                                const modals = document.querySelectorAll('.modal, .alert, [role="dialog"], [role="alertdialog"]');
                                for (const modal of modals) {
                                    if (modal.offsetParent !== null || getComputedStyle(modal).display !== 'none') {
                                        return {
                                            found: true,
                                            text: modal.innerText || modal.textContent
                                        };
                                    }
                                }
                                // Also check for any visible buttons that might be confirm/OK
                                const buttons = document.querySelectorAll('button');
                                for (const btn of buttons) {
                                    const text = btn.innerText || btn.textContent;
                                    if ((text.includes('確定') || text.includes('OK') || text.includes('Try again')) &&
                                        btn.offsetParent !== null) {
                                        return {
                                            found: true,
                                            buttonSelector: 'button'
                                        };
                                    }
                                }
                                return { found: false };
                            })();
                        ''')

                        # Handle CDP RemoteObject format (may return as list or dict)
                        modal_content = None
                        if modal_result:
                            if isinstance(modal_result, dict):
                                modal_content = modal_result
                            elif isinstance(modal_result, list) and len(modal_result) > 0:
                                # CDP sometimes returns [{'type': 'object', 'value': {...}}]
                                first_item = modal_result[0]
                                if isinstance(first_item, dict):
                                    if 'value' in first_item:
                                        modal_content = first_item.get('value', {})
                                    else:
                                        modal_content = first_item

                        if modal_content and isinstance(modal_content, dict) and modal_content.get('found'):
                            error_detected = True
                            debug.log(f"[TICKETMASTER CAPTCHA] Error modal detected")

                            # Try to click confirm/OK button to dismiss modal
                            dismiss_result = await tab.evaluate('''
                                (function() {
                                    // Find and click confirm button
                                    const buttons = document.querySelectorAll('button');
                                    for (const btn of buttons) {
                                        const text = btn.innerText || btn.textContent;
                                        if (text.includes('確定') || text.includes('OK') || text.includes('Try again')) {
                                            btn.click();
                                            return true;
                                        }
                                    }
                                    // Try to find any primary button
                                    const primaryBtn = document.querySelector('.btn-primary, [type="button"]');
                                    if (primaryBtn) {
                                        primaryBtn.click();
                                        return true;
                                    }
                                    return false;
                                })();
                            ''')

                            # Handle CDP RemoteObject format
                            dismiss_success = False
                            if dismiss_result is True:
                                dismiss_success = True
                            elif isinstance(dismiss_result, list) and len(dismiss_result) > 0:
                                dismiss_success = dismiss_result[0] is True or dismiss_result[0] == True

                            if debug.enabled:
                                if dismiss_success:
                                    debug.log("[TICKETMASTER CAPTCHA] Error modal dismissed, will retry OCR")
                                else:
                                    debug.log("[TICKETMASTER CAPTCHA] Could not dismiss modal")

                            # Reset state for retry
                            await asyncio.sleep(0.3)

                            # Reload captcha for new image
                            await nodriver_tixcraft_reload_captcha(tab, domain_name)
                            previous_answer = None
                            fail_count = 0
                            total_fail_count += 1

                            # Check retry limit
                            if total_fail_count >= 15:
                                print("[TICKETMASTER CAPTCHA] OCR failed 15 times after error modal. Please enter captcha manually.")
                                await nodriver_tixcraft_keyin_captcha_code(tab, config_dict=config_dict)
                                break

                            await asyncio.sleep(random.uniform(0.5, 1.0))
                            continue  # Retry OCR

                    except Exception as modal_exc:
                        debug.log(f"[TICKETMASTER CAPTCHA] Error checking modal: {modal_exc}")

                    # No error modal detected, form submitted successfully
                    if not error_detected:
                        break

                if not away_from_keyboard_enable:
                    break

                if not is_need_redo_ocr:
                    break

                # Track failures and handle retry limits
                fail_count += 1
                total_fail_count += 1

                debug.log(f"[TICKETMASTER CAPTCHA] Fail count: {fail_count}, Total fails: {total_fail_count}")

                # Check if total failures reached 15, switch to manual input mode
                if total_fail_count >= 15:
                    print("[TICKETMASTER CAPTCHA] OCR failed 15 times. Please enter captcha manually.")
                    await nodriver_tixcraft_keyin_captcha_code(tab, config_dict=config_dict)
                    break

                # Refresh captcha after 3 consecutive failures with same answer
                if fail_count >= 3:
                    debug.log("[TICKETMASTER CAPTCHA] 3 consecutive failures, reloading captcha...")
                    await nodriver_tixcraft_reload_captcha(tab, domain_name)
                    fail_count = 0
                    previous_answer = None  # Reset to allow fresh OCR
                    await asyncio.sleep(random.uniform(0.8, 1.2))  # Wait for new captcha to load
                else:
                    # Wait between retries to allow canvas to fully load
                    await asyncio.sleep(random.uniform(0.3, 0.5))

                # Check if URL changed
                new_url = tab.target.url
                if new_url != current_url:
                    debug.log("[TICKETMASTER CAPTCHA] URL changed, stopping OCR loop")
                    break

            except Exception as exc:
                debug.log(f"[TICKETMASTER CAPTCHA] OCR error: {exc}")
                break

        return True

async def nodriver_ticketmaster_promo(tab, config_dict, fail_list):
    question_selector = '#promoBox'
    return await nodriver_tixcraft_input_check_code(tab, config_dict, fail_list, question_selector)

async def nodriver_tixcraft_verify(tab, config_dict, fail_list):
    question_selector = '.zone-verify'
    return await nodriver_tixcraft_input_check_code(tab, config_dict, fail_list, question_selector)

async def nodriver_fill_verify_form(tab, config_dict, inferred_answer_string, fail_list, input_text_css, next_step_button_css, submit_by_enter, check_input_interval):
    """
    NoDriver version of fill_common_verify_form for TixCraft verification.

    Fills verification form input and submits the answer.

    Args:
        tab: NoDriver tab object
        config_dict: Configuration dictionary
        inferred_answer_string: Answer to fill in
        fail_list: List of failed answers
        input_text_css: CSS selector for input field
        next_step_button_css: CSS selector for submit button (optional)
        submit_by_enter: Whether to submit by pressing Enter
        check_input_interval: Interval to wait when no answer

    Returns:
        tuple[bool, list]: (is_answer_sent, updated fail_list)
    """
    debug = util.create_debug_logger(config_dict)

    is_answer_sent = False

    try:
        # Check if input field exists and get current value
        input_info = await tab.evaluate(f'''
            (function() {{
                var input = document.querySelector("{input_text_css}");
                if (input) {{
                    return {{
                        exists: true,
                        value: input.value || ""
                    }};
                }}
                return {{ exists: false, value: "" }};
            }})()
        ''')
        input_info = util.parse_nodriver_result(input_info)

        if not input_info or not input_info.get('exists', False):
            debug.log("[VERIFY FORM] Input field not found:", input_text_css)
            return is_answer_sent, fail_list

        inputed_value = input_info.get('value', '')

        debug.log(f"[VERIFY FORM] Current input value: '{inputed_value}'")
        debug.log(f"[VERIFY FORM] Answer to fill: '{inferred_answer_string}'")

        if len(inferred_answer_string) > 0:
            # Fill the answer if different from current value
            if inputed_value != inferred_answer_string:
                # Clear and fill using JavaScript
                await tab.evaluate(f'''
                    (function() {{
                        var input = document.querySelector("{input_text_css}");
                        if (input) {{
                            input.value = "";
                            input.value = {json.dumps(inferred_answer_string)};
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}
                    }})()
                ''')

                debug.log(f"[VERIFY FORM] Filled answer: {inferred_answer_string}")

            # Submit the form
            is_button_clicked = False

            if submit_by_enter:
                # Submit by pressing Enter
                await tab.evaluate(f'''
                    (function() {{
                        var input = document.querySelector("{input_text_css}");
                        if (input) {{
                            var event = new KeyboardEvent('keydown', {{
                                key: 'Enter',
                                code: 'Enter',
                                keyCode: 13,
                                which: 13,
                                bubbles: true
                            }});
                            input.dispatchEvent(event);

                            // Also try form submit
                            var form = input.closest('form');
                            if (form) {{
                                form.submit();
                            }}
                        }}
                    }})()
                ''')
                is_button_clicked = True
                debug.log("[VERIFY FORM] Submitted by Enter key")
            elif len(next_step_button_css) > 0:
                # Click the submit button
                try:
                    btn = await tab.query_selector(next_step_button_css)
                    if btn:
                        await btn.click()
                        is_button_clicked = True
                        debug.log(f"[VERIFY FORM] Clicked submit button: {next_step_button_css}")
                except Exception as btn_exc:
                    debug.log(f"[VERIFY FORM] Failed to click button: {btn_exc}")

            if is_button_clicked:
                is_answer_sent = True
                fail_list.append(inferred_answer_string)
                debug.log(f"[VERIFY FORM] Answer sent, attempt #{len(fail_list)}")

                # Wait and check for alert
                await asyncio.sleep(0.3)
        else:
            # No answer to fill, just focus the input
            if len(inputed_value) == 0:
                await tab.evaluate(f'''
                    (function() {{
                        var input = document.querySelector("{input_text_css}");
                        if (input && document.activeElement !== input) {{
                            input.focus();
                        }}
                    }})()
                ''')
                await asyncio.sleep(check_input_interval)
                debug.log("[VERIFY FORM] No answer, focused input field")

    except Exception as exc:
        debug.log(f"[VERIFY FORM] Error: {exc}")

    return is_answer_sent, fail_list

async def nodriver_tixcraft_input_check_code(tab, config_dict, fail_list, question_selector):
    debug = util.create_debug_logger(config_dict)

    answer_list = []

    question_text = await nodriver_get_text_by_selector(tab, question_selector, 'innerText')
    if len(question_text) > 0:
        write_question_to_file(question_text)

        answer_list = util.get_answer_list_from_user_guess_string(config_dict, CONST_MAXBOT_ANSWER_ONLINE_FILE)
        if len(answer_list)==0:
            if config_dict["advanced"]["auto_guess_options"]:
                # Note: guess_tixcraft_question() doesn't use the driver parameter
                answer_list = util.guess_tixcraft_question(None, question_text, config_dict)

        # Fallback: use discount_code as final answer when user_guess_string is empty
        # and auto_guess_options yielded no result. Covers serial-number style prompts
        # (e.g. Weverse Presale MY MEMBERSHIP) and promo-code gates (Ticketmaster
        # #promoBox) where users naturally fill the discount code field rather than
        # the answer dictionary.
        # Guard: only trigger for member/serial/promo style prompts so the
        # discount_code is not wasted on unrelated questions (math, trivia, etc.).
        if len(answer_list)==0:
            discount_code_fallback = (config_dict["advanced"].get("discount_code") or "").strip()
            if discount_code_fallback and _is_serial_code_question(question_text):
                debug.log("[VERIFY] Using discount_code as serial-code fallback answer")
                answer_list = [discount_code_fallback]

        inferred_answer_string = ""
        for answer_item in answer_list:
            if not answer_item in fail_list:
                inferred_answer_string = answer_item
                break

        debug.log("inferred_answer_string:", inferred_answer_string)
        debug.log("answer_list:", answer_list)

        # PS: auto-focus() when empty inferred_answer_string with empty inputed text value.
        input_text_css = "input[name='checkCode']"
        next_step_button_css = "button.btn.btn-primary"
        submit_by_enter = False
        check_input_interval = 0.2
        is_answer_sent, fail_list = await nodriver_fill_verify_form(tab, config_dict, inferred_answer_string, fail_list, input_text_css, next_step_button_css, submit_by_enter, check_input_interval)

    return fail_list

async def _tixcraft_action_allowed(tab, config_dict, document_id):
    """副作用前確認未暫停、未結束且仍是原文件。"""
    if await _tixcraft_actions_paused(config_dict) or not document_id:
        return False
    return await _tixcraft_document_id(tab) == document_id


async def _tixcraft_reload_after_wait(tab, config_dict, document_id):
    """等待期間可中止，手動 F5 或換頁後不再覆蓋新畫面。"""
    if not util.is_auto_refresh_enabled() or await _tixcraft_actions_paused(config_dict):
        return False
    try:
        interval = max(0.0, float(config_dict.get('advanced', {}).get('auto_reload_page_interval', 1)))
        if not interval < float('inf'):
            return False
        if await _tixcraft_sleep(interval, config_dict):
            return False
        if not await _tixcraft_action_allowed(tab, config_dict, document_id):
            return False
        if await _tixcraft_submission_pending(tab, config_dict):
            return False
        return await reload_page_if_enabled(tab)
    except Exception:
        return False


async def nodriver_tixcraft_date_auto_select(tab, url, config_dict, domain_name):
    if await _tixcraft_actions_paused(config_dict):
        return False
    document_id = await _tixcraft_document_id(tab)
    debug = util.create_debug_logger(config_dict)

    # Issue #188: Check sold out cooldown before proceeding
    if _state and _state.get("sold_out_cooldown_until", 0) > time.time():
        remaining = _state["sold_out_cooldown_until"] - time.time()
        debug.log(f"[DATE SELECT] Sold out cooldown active, waiting {remaining:.1f}s...")
        if await _tixcraft_sleep(remaining, config_dict):
            return False
        _state["sold_out_cooldown_until"] = 0  # Reset after waiting

    # T003: Check main switch (defensive programming)
    if not config_dict["date_auto_select"]["enable"]:
        debug.log("[DATE SELECT] Main switch is disabled, skipping date selection")
        return False

    # read config
    auto_select_mode = config_dict["date_auto_select"]["mode"]
    date_keyword = config_dict["date_auto_select"]["date_keyword"].strip()
    date_auto_fallback = config_dict.get('date_auto_fallback', False)  # T017: Safe access for new field (default: strict mode)
    pass_date_is_sold_out_enable = config_dict["tixcraft"]["pass_date_is_sold_out"]
    auto_reload_coming_soon_page_enable = (config_dict["tixcraft"]["auto_reload_coming_soon_page"]) and util.is_auto_refresh_enabled()

    sold_out_text_list = ["選購一空","已售完","No tickets available","Sold out","空席なし","完売した"]
    find_ticket_text_list = ['立即訂購','Find tickets', 'Start ordering','お申込みへ進む']

    game_name = ""
    if "/activity/game/" in url:
        url_split = url.split("/")
        if len(url_split) >= 6:
            game_name = url_split[5]

    check_game_detail = "/activity/game/%s" % (game_name,) in url

    area_list = None
    if check_game_detail:
        # 智慧等待：等待日期列表出現
        # 注意：從 /activity/detail/ redirect 過來時，redirect 函數已經等待過了
        # 這裡再等待一次是為了處理直接進入 /activity/game/ 頁面的情況
        try:
            await tab.wait_for('#gameList > table > tbody > tr', timeout=3)
        except:
            pass  # timeout 沒關係，繼續嘗試讀取

        try:
            area_list = await tab.query_selector_all('#gameList > table > tbody > tr')
        except:
            pass

    # Language detection for coming soon
    is_coming_soon = False
    coming_soon_conditions = {
        'en-US': [' day(s)', ' hrs.',' min',' sec',' till sale starts!','0',':','/'],
        'zh-TW': ['開賣','剩餘',' 天',' 小時',' 分鐘',' 秒','0',':','/','20'],
        'ja': ['発売開始', ' 日', ' 時間',' 分',' 秒','0',':','/','20']
    }

    if 'html_lang' not in _state:
        try:
            _state['html_lang'] = await tab.evaluate('document.documentElement.lang') or 'en-US'
        except:
            _state['html_lang'] = 'en-US'
    html_lang = _state['html_lang']

    coming_soon_condictions_list = coming_soon_conditions.get(html_lang, coming_soon_conditions['en-US'])

    matched_blocks = None
    formated_area_list = None

    if area_list and len(area_list) > 0:
        formated_area_list = []
        formated_area_list_text = []
        # Batch fetch all row HTML in one CDP round-trip (~9-16x faster than sequential get_html())
        all_row_htmls = None
        try:
            all_row_htmls = await tab.evaluate(
                "Array.from(document.querySelectorAll('#gameList > table > tbody > tr')).map(r => r.outerHTML)"
            )
        except:
            pass
        for i, row in enumerate(area_list):
            try:
                if all_row_htmls and i < len(all_row_htmls):
                    row_html = all_row_htmls[i]
                else:
                    row_html = await row.get_html()
                row_text = util.remove_html_tags(row_html)
            except:
                break

            if row_text and not util.reset_row_text_if_match_keyword_exclude(config_dict, row_text):
                # Check coming soon
                if all(cond in row_text for cond in coming_soon_condictions_list):
                    is_coming_soon = True
                    debug.log(f"[DATE SELECT] Detected coming soon countdown")
                    if auto_reload_coming_soon_page_enable:
                        debug.log(f"[DATE SELECT] auto_reload_coming_soon_page=true, will reload and retry")
                        break
                    else:
                        # Skip this row (don't add to formated_area_list)
                        continue

                # Check if row has ticket text
                row_is_enabled = any(text in row_text for text in find_ticket_text_list)

                # Check sold out
                if row_is_enabled and pass_date_is_sold_out_enable:
                    for sold_out_item in sold_out_text_list:
                        if sold_out_item in row_text[-(len(sold_out_item)+5):]:
                            row_is_enabled = False
                            # 移除：售完訊息過度詳細
                            break

                if row_is_enabled:
                    formated_area_list.append(row)
                    formated_area_list_text.append(row_text)
                    # 移除：可用場次訊息過度詳細

        # T004-T008: NEW LOGIC - Early return pattern (Feature 003)
        # Keyword priority matching: first match wins and stops immediately
        if not date_keyword:
            matched_blocks = formated_area_list
            debug.log(f"[DATE KEYWORD] No keyword specified, using all {len(formated_area_list)} dates")
        else:
            # NEW: Early return pattern - iterate keywords in order
            matched_blocks = []
            target_row_found = False
            keyword_matched_index = -1

            try:
                import json
                import re
                keyword_array = json.loads("[" + date_keyword + "]")

                # T005: Start checking keywords log
                debug.log(f"[DATE KEYWORD] Start checking keywords in order: {keyword_array}")
                debug.log(f"[DATE KEYWORD] Total keyword groups: {len(keyword_array)}")
                debug.log(f"[DATE KEYWORD] Checking against {len(formated_area_list_text)} available dates...")

                # NEW: Iterate keywords in priority order (early return)
                for keyword_index, keyword_item_set in enumerate(keyword_array):
                    debug.log(f"[DATE KEYWORD] Checking keyword #{keyword_index + 1}: {keyword_item_set}")

                    # Check all rows for this keyword
                    for i, row_text in enumerate(formated_area_list_text):
                        normalized_row_text = re.sub(r'\s+', ' ', row_text)
                        is_match = False

                        if isinstance(keyword_item_set, str):
                            # OR logic: single keyword
                            normalized_keyword = re.sub(r'\s+', ' ', keyword_item_set)
                            is_match = ticket_http.date_matches(normalized_keyword, normalized_row_text)
                        elif isinstance(keyword_item_set, list):
                            # AND logic: all keywords must match
                            normalized_keywords = [re.sub(r'\s+', ' ', kw) for kw in keyword_item_set]
                            match_results = [ticket_http.date_matches(kw, normalized_row_text) for kw in normalized_keywords]
                            is_match = all(match_results)

                        if is_match:
                            # T006: Keyword matched log - IMMEDIATELY select and stop
                            matched_blocks = [formated_area_list[i]]
                            target_row_found = True
                            keyword_matched_index = keyword_index
                            debug.log(f"[DATE KEYWORD] Keyword #{keyword_index + 1} matched: '{keyword_item_set}'")
                            debug.log(f"[DATE SELECT] Selected date: {row_text[:80]} (keyword match)")
                            break

                    if target_row_found:
                        # EARLY RETURN: Stop checking further keywords
                        break

                # T007: All keywords failed log
                if not target_row_found:
                    debug.log(f"[DATE KEYWORD] All keywords failed to match")

            except Exception as e:
                debug.log(f"[DATE KEYWORD] Parsing error: {e}")
                # On error, use mode selection
                matched_blocks = []

    # T018-T020: NEW - Conditional fallback based on date_auto_fallback switch
    if matched_blocks is not None and len(matched_blocks) == 0 and date_keyword and formated_area_list is not None and len(formated_area_list) > 0:
        if date_auto_fallback:
            # T018: Fallback enabled - use auto_select_mode
            debug.log(f"[DATE FALLBACK] date_auto_fallback=true, triggering auto fallback")
            debug.log(f"[DATE FALLBACK] Selecting available date based on date_select_order='{auto_select_mode}'")
            matched_blocks = formated_area_list
        else:
            # T019: Fallback disabled - strict mode (no selection, but still reload)
            debug.log(f"[DATE FALLBACK] date_auto_fallback=false, fallback is disabled")
            debug.log(f"[DATE SELECT] No date selected, will reload page and retry")
            # Don't return - let reload logic execute below
            # matched_blocks remains None (no selection will be made)

    # T020: Handle case when formated_area_list is empty or None (all options excluded or sold out)
    if formated_area_list is None or len(formated_area_list) == 0:
        debug.log(f"[DATE FALLBACK] No available options after exclusion")
        debug.log(f"[DATE SELECT] Will reload page and retry")
        # Don't return - let reload logic execute at function end
        is_date_clicked = False
        target_area = None  # Skip selection when no options available
    elif matched_blocks is None or len(matched_blocks) == 0:
        # matched_blocks is None when fallback=false and keyword didn't match
        target_area = None
        is_date_clicked = False
    else:
        target_area = util.get_target_item_from_matched_list(matched_blocks, auto_select_mode)

    if debug.enabled:
        if target_area and matched_blocks:
            # Find which index was selected
            try:
                target_index = matched_blocks.index(target_area) if target_area in matched_blocks else -1
                debug.log(f"[DATE SELECT] Auto-select mode: {auto_select_mode}")
                debug.log(f"[DATE SELECT] Selected target: #{target_index + 1}/{len(matched_blocks)}")
            except:
                debug.log(f"[DATE SELECT] Auto-select mode: {auto_select_mode}")
                debug.log(f"[DATE SELECT] Target selected from {len(matched_blocks)} matched dates")
        elif not matched_blocks or len(matched_blocks) == 0:
            debug.log(f"[DATE SELECT] No target selected (matched_blocks is empty)")

    is_date_clicked = False

    # 移除：內部選擇細節過度詳細

    if target_area:
        # Priority: button with data-href (tixcraft/indievox) > regular link > regular button
        # IMPORTANT: Search within target_area, not the whole page
        click_method_used = None
        try:
            debug.log("[DATE SELECT] Trying button[data-href] method within target_area...")

            # Method 1: button[data-href] within target_area (tixcraft/indievox specific)
            # 使用 NoDriver Element API 取得 data-href
            button_with_href = await target_area.query_selector('button[data-href]')
            data_href = None
            if button_with_href:
                # 更新元素以確保屬性載入
                await button_with_href.update()
                button_attrs = button_with_href.attrs or {}
                data_href = button_attrs.get('data-href')

                if debug.enabled:
                    if data_href:
                        debug.log(f"[DATE SELECT] button[data-href] found in target_area: {data_href}")
                    else:
                        debug.log("[DATE SELECT] button[data-href] found but no href value")

                if data_href:
                    debug.log("[DATE SELECT] Navigating via button[data-href]...")
                    if not await _tixcraft_action_allowed(tab, config_dict, document_id):
                        return False
                    await navigate_page_if_enabled(tab, data_href)
                    is_date_clicked = True
                    click_method_used = "button[data-href]"
                    debug.log("[DATE SELECT] Successfully navigated via button[data-href]")
            else:
                debug.log("[DATE SELECT] No button[data-href] in target_area, will try fallback methods")
        except Exception as e:
            debug.log(f"[DATE SELECT] button[data-href] method failed: {e}")

        # Method 2: regular link or button click
        if not is_date_clicked:
            try:
                debug.log("[DATE SELECT] Trying link <a[href]> method within target_area...")

                # Try link first (ticketmaster, etc)
                link = await target_area.query_selector('a[href]')
                if link:
                    debug.log("[DATE SELECT] Link found in target_area, clicking...")
                    if not await _tixcraft_action_allowed(tab, config_dict, document_id):
                        return False
                    await link.click()
                    is_date_clicked = True
                    click_method_used = "link <a[href]>"
                    debug.log("[DATE SELECT] Successfully clicked via link")
                else:
                    debug.log("[DATE SELECT] No link found, trying regular button within target_area...")

                    # Try regular button
                    button = await target_area.query_selector('button')
                    if button:
                        debug.log("[DATE SELECT] Regular button found in target_area, clicking...")
                        if not await _tixcraft_action_allowed(tab, config_dict, document_id):
                            return False
                        await button.click()
                        is_date_clicked = True
                        click_method_used = "regular button"
                        debug.log("[DATE SELECT] Successfully clicked via regular button")
                    else:
                        debug.log("[DATE SELECT] No clickable element found in target_area")
            except Exception as e:
                debug.log(f"[DATE SELECT] Click error: {e}")

        # Final summary
        if debug.enabled:
            if is_date_clicked and click_method_used:
                debug.log(f"[DATE SELECT] ========================================")
                debug.log(f"[DATE SELECT] Date selection completed successfully")
                debug.log(f"[DATE SELECT] Method used: {click_method_used}")
                debug.log(f"[DATE SELECT] ========================================")
            elif not is_date_clicked:
                debug.log(f"[DATE SELECT] ========================================")
                debug.log("[DATE SELECT] All click methods failed")
                debug.log(f"[DATE SELECT] ========================================")

    # Auto refresh if no date was selected (for strict mode or sold out scenarios)
    if not is_date_clicked and _is_page_reload_enabled(config_dict, debug, "DATE SELECT"):
        await _tixcraft_reload_after_wait(tab, config_dict, document_id)

    return is_date_clicked

async def nodriver_tixcraft_area_auto_select(tab, url, config_dict):
    # 函數開始時檢查暫停
    if await _tixcraft_actions_paused(config_dict):
        return False
    document_id = await _tixcraft_document_id(tab)

    debug = util.create_debug_logger(config_dict)

    # T010: Check main switch (defensive programming)
    if not config_dict["area_auto_select"]["enable"]:
        debug.log("[AREA SELECT] Main switch is disabled, skipping area selection")
        return False

    import json

    area_keyword = config_dict["area_auto_select"]["area_keyword"].strip()
    auto_select_mode = config_dict["area_auto_select"]["mode"]
    area_auto_fallback = config_dict.get('area_auto_fallback', False)  # T021: Safe access for new field

    try:
        el = await tab.query_selector('.zone')
    except:
        return

    if not el:
        return

    # Batch pre-fetch: one JS call to get all area text and font data
    area_list_cache = None
    area_text_cache = None
    try:
        area_list_cache = await el.query_selector_all('a')
        area_text_cache = await tab.evaluate("""
            Array.from(document.querySelectorAll('.zone a')).map(a => ({
                text: a.innerText.trim(),
                fontText: a.querySelector('font')?.textContent?.trim() ?? ''
            }))
        """)
        if area_list_cache and area_text_cache and len(area_list_cache) != len(area_text_cache):
            area_text_cache = None
        if area_text_cache:
            debug.log(f"[AREA KEYWORD] Batch pre-fetch: {len(area_text_cache)} areas cached")
    except:
        area_list_cache = None
        area_text_cache = None

    is_need_refresh = False
    matched_blocks = None

    if area_keyword:
        # Parse keywords using JSON to avoid splitting keywords containing commas (e.g., "5,600")
        # Format: "\"keyword1\",\"keyword2\"" → ['keyword1', 'keyword2']
        # Supports OR logic - iterates through keywords until match found
        area_keyword_array = util.parse_keyword_string_to_array(area_keyword)
        area_keyword_array = list(dict.fromkeys(area_keyword_array))

        # T012: Start checking keywords log
        debug.log(f"[AREA KEYWORD] Start checking keywords in order: {area_keyword_array}")
        debug.log(f"[AREA KEYWORD] Total keyword groups: {len(area_keyword_array)}")

        # T011: Early return pattern - iterate keywords in priority order
        keyword_matched = False
        for keyword_index, area_keyword_item in enumerate(area_keyword_array):
            debug.log(f"[AREA KEYWORD] Checking keyword #{keyword_index + 1}: {area_keyword_item}")

            is_need_refresh, matched_blocks = await nodriver_get_tixcraft_target_area(
                el, config_dict, area_keyword_item,
                area_list_cache=area_list_cache, area_text_cache=area_text_cache)

            if not is_need_refresh:
                # T013: Keyword matched log
                keyword_matched = True
                debug.log(f"[AREA KEYWORD] Keyword #{keyword_index + 1} matched: '{area_keyword_item}'")
                break

        # T014: All keywords failed log
        if not keyword_matched:
            debug.log(f"[AREA KEYWORD] All keywords failed to match")

        # T022-T024: NEW - Conditional fallback based on area_auto_fallback switch
        is_fallback_selection = False  # Track selection type for logging
        if is_need_refresh and matched_blocks is None:
            if area_auto_fallback:
                # T022: Fallback enabled - use auto_select_mode without keyword
                debug.log(f"[AREA FALLBACK] area_auto_fallback=true, triggering auto fallback")
                debug.log(f"[AREA FALLBACK] Selecting available area based on area_select_order='{auto_select_mode}'")
                is_need_refresh, matched_blocks = await nodriver_get_tixcraft_target_area(
                    el, config_dict, "",
                    area_list_cache=area_list_cache, area_text_cache=area_text_cache)
                is_fallback_selection = True  # Mark as fallback selection
            else:
                # T023: Fallback disabled - strict mode (no selection, but still reload)
                debug.log(f"[AREA FALLBACK] area_auto_fallback=false, fallback is disabled")
                debug.log(f"[AREA SELECT] No area selected, will reload page and retry")
                # Don't return - let reload logic execute below
                # matched_blocks remains None (no selection will be made)
                # is_need_refresh remains True (will trigger reload)
    else:
        is_need_refresh, matched_blocks = await nodriver_get_tixcraft_target_area(
            el, config_dict, "",
            area_list_cache=area_list_cache, area_text_cache=area_text_cache)
        # No keyword specified, treat as mode-based selection (similar to fallback)
        if not area_keyword:
            is_fallback_selection = True

    # T024: Handle case when matched_blocks is empty or None (all options excluded or sold out)
    if matched_blocks is None or len(matched_blocks) == 0:
        debug.log(f"[AREA FALLBACK] No available options after exclusion")
        debug.log(f"[AREA SELECT] Will reload page and retry")
        # Don't return - let reload logic execute below
        is_need_refresh = True  # Ensure reload will happen
        target_area = None  # Skip selection when no options available
    else:
        target_area = util.get_target_item_from_matched_list(matched_blocks, auto_select_mode)
    if target_area:
        # T013: Log selected area with selection type
        if debug.enabled:
            try:
                # Element.text is a property and there is no inner_text attribute;
                # awaiting either raised and was swallowed, so this line never logged.
                area_html = await target_area.get_html()
                area_text = util.remove_html_tags(area_html)
                area_text = area_text[:80] if area_text else "Unknown"
                selection_type = "fallback" if is_fallback_selection else "keyword match"
                debug.log(f"[AREA SELECT] Selected area: {area_text} ({selection_type})")
            except:
                pass  # If text extraction fails, skip logging

        try:
            if not await _tixcraft_action_allowed(tab, config_dict, document_id):
                return False
            await target_area.click()
        except:
            try:
                if not await _tixcraft_action_allowed(tab, config_dict, document_id):
                    return False
                # Element.apply, not Element.evaluate: zendriver defines
                # evaluate on Tab only and Element.__getattr__ returns None for
                # unknown names, so .evaluate(...) is a TypeError.
                await target_area.apply('(el) => el.click()')
            except:
                pass

    # Auto refresh if needed (simple wait mode, consistent with TicketPlus/iBon/FamiTicket)
    if is_need_refresh and _is_page_reload_enabled(config_dict, debug, "AREA SELECT"):
        await _tixcraft_reload_after_wait(tab, config_dict, document_id)

async def nodriver_get_tixcraft_target_area(el, config_dict, area_keyword_item,
                                            area_list_cache=None, area_text_cache=None):
    area_auto_select_mode = config_dict["area_auto_select"]["mode"]
    debug = util.create_debug_logger(config_dict)
    is_need_refresh = False
    matched_blocks = None

    # Display keyword information
    if debug.enabled:
        debug.log(f"[AREA KEYWORD] ========================================")
        if area_keyword_item:
            keyword_parts = area_keyword_item.split(' ')
            debug.log(f"[AREA KEYWORD] Raw input: '{area_keyword_item}'")
            debug.log(f"[AREA KEYWORD] Parsed (AND logic): {keyword_parts}")
            debug.log(f"[AREA KEYWORD] Total sub-keywords: {len(keyword_parts)}")
            debug.log(f"[AREA KEYWORD] Auto-select mode: {area_auto_select_mode}")
        else:
            debug.log(f"[AREA KEYWORD] No keyword specified, matching all areas")
            debug.log(f"[AREA KEYWORD] Auto-select mode: {area_auto_select_mode}")

    if not el:
        debug.log(f"[AREA KEYWORD] Element is None, cannot select area")
        return True, None

    if area_list_cache is not None:
        area_list = area_list_cache
    else:
        try:
            area_list = await el.query_selector_all('a')
        except:
            debug.log(f"[AREA KEYWORD] Failed to query area list")
            return True, None

    if not area_list or len(area_list) == 0:
        debug.log(f"[AREA KEYWORD] No areas found")
        return True, None

    debug.log(f"[AREA KEYWORD] Found {len(area_list)} area(s) to check")
    debug.log(f"[AREA KEYWORD] ========================================")

    matched_blocks = []
    area_index = 0
    for row in area_list:
        area_index += 1

        if area_text_cache is not None:
            row_text = area_text_cache[area_index - 1].get('text', '')
        else:
            try:
                row_html = await row.get_html()
                row_text = util.remove_html_tags(row_html)
            except:
                debug.log(f"[AREA KEYWORD] [{area_index}] Failed to get row content")
                break

        if not row_text or util.reset_row_text_if_match_keyword_exclude(config_dict, row_text):
            debug.log(f"[AREA KEYWORD] [{area_index}] Excluded by keyword_exclude")
            continue

        debug.log(f"[AREA KEYWORD] [{area_index}/{len(area_list)}] Checking: {row_text[:80]}...")

        row_text = util.format_keyword_string(row_text)

        # Check keyword match
        if area_keyword_item:
            keyword_parts = area_keyword_item.split(' ')

            debug.log(f"[AREA KEYWORD]   Matching AND keywords: {keyword_parts}")

            # Check each keyword individually for detailed feedback
            match_results = {}
            for kw in keyword_parts:
                formatted_kw = util.format_keyword_string(kw)
                kw_match = formatted_kw in row_text
                match_results[kw] = kw_match

                if debug.enabled:
                    status = "PASS" if kw_match else "FAIL"
                    debug.log(f"[AREA KEYWORD]     {status} '{kw}': {kw_match}")

            is_match = all(match_results.values())

            if debug.enabled:
                if is_match:
                    debug.log(f"[AREA KEYWORD]   All AND keywords matched")
                else:
                    debug.log(f"[AREA KEYWORD]   AND logic failed")

            if not is_match:
                continue
        else:
            debug.log(f"[AREA KEYWORD]   No keyword filter, accepting this area")

        # Check seat availability for multiple tickets
        allow_less_tickets = config_dict.get("tixcraft", {}).get("allow_less_tickets", False)
        if config_dict["ticket_number"] > 1 and not allow_less_tickets:
            try:
                if area_text_cache is not None:
                    font_text = area_text_cache[area_index - 1].get('fontText', '')
                else:
                    font_text = ''
                    font_el = await row.query_selector('font')
                    if font_el:
                        # Element.apply, not Element.evaluate (None on an
                        # Element; see nodriver_tixcraft_area_auto_select).
                        font_text = await font_el.apply('(el) => el.textContent') or ''
                if font_text:
                    font_text = font_text.strip()

                    debug.log(f"[AREA KEYWORD]   Checking seats: {font_text}")

                    # The count is embedded in localized text, never a bare
                    # number: "剩餘 1" (zh) / "1 seat(s) remaining" (en). The old
                    # "@1@".."@9@" substring test therefore never matched and
                    # this guard silently passed every area through (#358).
                    # A status word instead of a count (熱賣中 / Available) means
                    # the page is not telling us how many are left, so the area
                    # is kept - the ticket-number page is the next line of
                    # defence.
                    seats_match = re.search(r'\d+', font_text)
                    if seats_match:
                        remaining = int(seats_match.group())
                        if remaining < config_dict["ticket_number"]:
                            debug.log(f"[AREA KEYWORD]   Insufficient seats (need {config_dict['ticket_number']}, only {remaining} available)")
                            continue
                        debug.log(f"[AREA KEYWORD]   Sufficient seats available ({remaining})")
                    else:
                        debug.log(f"[AREA KEYWORD]   No seat count in '{font_text}', keeping this area")
            except:
                pass

        matched_blocks.append(row)

        debug.log(f"[AREA KEYWORD]   → Area added to matched list (total: {len(matched_blocks)})")

        if area_auto_select_mode == util.CONST_FROM_TOP_TO_BOTTOM:
            debug.log(f"[AREA KEYWORD]   Mode is '{area_auto_select_mode}', stopping at first match")
            break

    if not matched_blocks:
        is_need_refresh = True
        matched_blocks = None

    return is_need_refresh, matched_blocks

def _tixcraft_ticket_config_signature(config_dict):
    """只保存選票設定，不包含登入或驗證資料。"""
    return json.dumps({
        'target': config_dict.get('ticket_number', 1),
        'allow_less': config_dict.get('tixcraft', {}).get('allow_less_tickets', False),
        'area': config_dict.get('area_auto_select', {}),
        'exclude': config_dict.get('keyword_exclude', ''),
        'fallback': config_dict.get('area_auto_fallback', False),
    }, sort_keys=True, ensure_ascii=False)


async def _tixcraft_ticket_snapshot(tab):
    """一次讀取票種欄位；不把其他表單選單算成票券。"""
    try:
        result = await tab.evaluate(r'''(() => {
            const sold=['選購一空','已售完','Sold out','No tickets available','空席なし','完売した'];
            const rows=Array.from(document.querySelectorAll('select[id^="TicketForm_ticketPrice_"]')).map(s=>{
                const row=s.closest('tr'),label=row?.querySelector('h4')||row?.querySelector('td.fcBlue');
                const validValue=s.value===''||/^\d+$/.test(s.value);
                return {id:s.id,name:(label?.textContent||'').trim(),value:validValue?Number(s.value):null,
                    validValue,disabled:s.matches(':disabled'),options:Array.from(s.options).filter(o=>
                        !o.disabled&&!o.closest('optgroup')?.disabled&&/^\d+$/.test(o.value)&&Number(o.value)>0&&
                        !sold.includes((o.textContent||'').trim())).map(o=>Number(o.value))};
            });
            return {document_id:location.href+'|'+performance.timeOrigin,rows,
                area_name:(document.querySelector('.select-area span')?.textContent||'').trim()};
        })()''')
        return result if isinstance(result, dict) and isinstance(result.get('rows'), list) else None
    except Exception:
        return None


def _tixcraft_ticket_count_ok(count, target, allow_less):
    return (isinstance(count, (int, float)) and not isinstance(count, bool)
            and 0 < count < float('inf') and count == int(count)
            and (count == target or (allow_less and count < target)))


def _tixcraft_ticket_state_valid(snapshot, record):
    if not snapshot or not record or snapshot.get('document_id') != record.get('document_id'):
        return False
    rows = snapshot['rows']
    selected = [row for row in rows if row.get('id') == record.get('select_id')]
    if len(selected) != 1 or len({row.get('id') for row in rows}) != len(rows):
        return False
    chosen = selected[0]
    count = chosen.get('value')
    if (chosen.get('disabled') or not _tixcraft_ticket_count_ok(count, record.get('target', 0), record.get('allow_less', False))
            or count != record.get('count') or count not in chosen.get('options', [])):
        return False
    return all(row.get('validValue') and (row.get('value') == count if row is chosen else row.get('value') == 0)
               for row in rows)


async def _tixcraft_ticket_selection_valid(tab, config_dict):
    """唯讀核對文件、目標票種、目前設定及全表張數。"""
    record = _state.get('tixcraft_ticket_selection')
    if not record or record.get('config_signature') != _tixcraft_ticket_config_signature(config_dict):
        return False
    snapshot = await _tixcraft_ticket_snapshot(tab)
    return _tixcraft_ticket_state_valid(snapshot, record)


async def _tixcraft_fill_generic_ticket_select(select_obj, target, allow_less):
    """相容其他家族的單一已知選單，不掃描或變更其他表單欄位。"""
    if select_obj is None:
        return False
    try:
        result = await select_obj.apply(r'''async (select) => {
            if(select.tagName!=='SELECT'||!select.isConnected||select.matches(':disabled'))return false;
            const target=TARGET,allowLess=ALLOW_LESS;
            const sold=['選購一空','已售完','Sold out','No tickets available','空席なし','完売した'];
            const options=Array.from(select.options).filter(o=>!o.disabled&&!o.closest('optgroup')?.disabled&&
                /^\d+$/.test(o.value)&&Number(o.value)>0&&!sold.includes((o.textContent||'').trim()));
            let option=options.find(o=>Number(o.value)===target);
            if(!option&&allowLess)option=options.filter(o=>Number(o.value)<target).sort((a,b)=>Number(b.value)-Number(a.value))[0];
            if(!option)return false;
            const wanted=option.value;
            if(select.value!==wanted){select.value=wanted;select.dispatchEvent(new Event('change',{bubbles:true}));}
            await Promise.resolve();
            return select.isConnected&&!select.matches(':disabled')&&select.value===wanted&&
                select.selectedOptions.length===1&&!select.selectedOptions[0].disabled&&
                !select.selectedOptions[0].closest('optgroup')?.disabled;
        }'''.replace('TARGET', json.dumps(target)).replace('ALLOW_LESS', json.dumps(bool(allow_less))), await_promise=True)
        return result is True
    except Exception:
        return False


async def nodriver_ticket_number_select_fill(tab, select_obj, ticket_number, select_id=None, allow_less_tickets=False):
    """僅設定指定票種，清除其他已選票種並讀回驗證，未知結果不算成功。"""
    if await _tixcraft_actions_paused():
        return False
    try:
        target = int(ticket_number)
        if isinstance(ticket_number, bool) or target <= 0 or float(ticket_number) != target:
            return False
        if not select_id and select_obj is not None:
            await select_obj.update()
            select_id = (select_obj.attrs or {}).get('id')
        if not isinstance(select_id, str) or not select_id.startswith('TicketForm_ticketPrice_'):
            if select_obj is None and isinstance(select_id, str):
                select_obj = await tab.query_selector('select[id=' + json.dumps(select_id) + ']')
            return await _tixcraft_fill_generic_ticket_select(select_obj, target, allow_less_tickets)
        record = _state.get('tixcraft_ticket_selection', {})
        expected = record.get('document_id') if record.get('select_id') == select_id else None
        result = await tab.evaluate(r'''(async (id,target,allowLess,expected) => {
            const documentId=location.href+'|'+performance.timeOrigin;
            if(expected&&expected!==documentId)return {success:false};
            const selector='select[id^="TicketForm_ticketPrice_"]';
            const fields=()=>Array.from(document.querySelectorAll(selector));
            let rows=fields(),chosen=rows.filter(s=>s.id===id);
            if(chosen.length!==1||new Set(rows.map(s=>s.id)).size!==rows.length)return {success:false};
            const select=chosen[0],sold=['選購一空','已售完','Sold out','No tickets available','空席なし','完売した'];
            if(select.matches(':disabled'))return {success:false};
            const options=Array.from(select.options).filter(o=>!o.disabled&&!o.closest('optgroup')?.disabled&&
                /^\d+$/.test(o.value)&&Number(o.value)>0&&!sold.includes((o.textContent||'').trim()));
            let option=options.find(o=>Number(o.value)===target);
            if(!option&&allowLess)option=options.filter(o=>Number(o.value)<target).sort((a,b)=>Number(b.value)-Number(a.value))[0];
            if(!option)return {success:false};
            for(const other of rows){
                if(other===select||other.value==='0'||other.value==='')continue;
                if(!/^\d+$/.test(other.value)||!Array.from(other.options).some(o=>o.value==='0'))return {success:false};
                other.value='0';other.dispatchEvent(new Event('change',{bubbles:true}));
                await Promise.resolve();
                if(!other.isConnected||other.value!=='0')return {success:false};
            }
            if(!select.isConnected||select.matches(':disabled')||option.disabled||!option.isConnected)return {success:false};
            const wanted=option.value;
            if(select.value!==wanted){select.value=wanted;select.dispatchEvent(new Event('change',{bubbles:true}));}
            await Promise.resolve();
            rows=fields();
            const selected=Array.from(select.selectedOptions);
            const success=location.href+'|'+performance.timeOrigin===documentId&&select.isConnected&&
                !select.matches(':disabled')&&rows.filter(s=>s.id===id).length===1&&rows.includes(select)&&
                select.value===wanted&&selected.length===1&&!selected[0].disabled&&
                !selected[0].closest('optgroup')?.disabled&&rows.every(s=>s===select||s.value==='0'||s.value==='');
            return {success,document_id:documentId,count:Number(select.value)};
        })(SELECT_ID, TARGET, ALLOW_LESS, EXPECTED)'''.replace('SELECT_ID', json.dumps(select_id)).replace(
            'TARGET', json.dumps(target)).replace('ALLOW_LESS', json.dumps(bool(allow_less_tickets))).replace(
            'EXPECTED', json.dumps(expected)), await_promise=True)
        if not isinstance(result, dict) or result.get('success') is not True:
            return False
        if record.get('select_id') == select_id and record.get('document_id') == result.get('document_id'):
            record['count'] = result['count']
        else:
            record = {'document_id': result['document_id'], 'select_id': select_id,
                      'target': target, 'allow_less': bool(allow_less_tickets), 'count': result['count']}
        return _tixcraft_ticket_state_valid(await _tixcraft_ticket_snapshot(tab), record)
    except Exception:
        return False


def _tixcraft_ticket_candidates(snapshot, config_dict):
    raw_target = config_dict.get('ticket_number', 1)
    target = int(raw_target)
    if isinstance(raw_target, bool) or target <= 0 or float(raw_target) != target:
        return [], []
    allow_less = config_dict.get('tixcraft', {}).get('allow_less_tickets', False)
    eligible, candidates = [], []
    for row in snapshot['rows']:
        name = row.get('name', '')
        if row.get('disabled') or not row.get('options'):
            continue
        if name and util.reset_row_text_if_match_keyword_exclude(config_dict, name):
            continue
        eligible.append(row)
        counts = [count for count in row['options'] if _tixcraft_ticket_count_ok(count, target, allow_less)]
        if counts:
            candidates.append({**row, 'count': max(counts)})
    return eligible, candidates


def _tixcraft_pick_ticket_candidate(eligible, candidates, config_dict):
    keywords = util.parse_keyword_string_to_array(config_dict['area_auto_select']['area_keyword'].strip())
    for group in keywords:
        if not isinstance(group, str):
            continue
        parts = [util.format_keyword_string(word) for word in group.split(' ')]
        for candidate in candidates:
            if all(word in util.format_keyword_string(candidate.get('name', '')) for word in parts):
                return candidate
    # 單票種頁的區域關鍵字常不等於票種名，沿用原本直接選取的行為。
    if len(eligible) == 1 and len(candidates) == 1:
        return candidates[0]
    if keywords and not config_dict.get('area_auto_fallback', False):
        return None
    return util.get_target_item_from_matched_list(candidates, config_dict['area_auto_select']['mode'])


async def nodriver_tixcraft_assign_ticket_number(tab, config_dict):
    """先依可購張數篩選，再依原關鍵字優先順序選票種。"""
    if await check_and_handle_pause(config_dict):
        return False, None, None
    debug = util.create_debug_logger(config_dict)
    _state.pop('tixcraft_ticket_selection', None)
    snapshot = await _tixcraft_ticket_snapshot(tab)
    if not snapshot or not snapshot['rows']:
        return False, None, None
    if urlsplit(snapshot['document_id'].split('|', 1)[0]).hostname in ('tixcraft.com', 'www.tixcraft.com'):
        if not await _tixcraft_apply_area_update(config_dict) or not await _tixcraft_verify_current_area(tab, config_dict, snapshot):
            return False, None, None
    try:
        eligible, candidates = _tixcraft_ticket_candidates(snapshot, config_dict)
        chosen = _tixcraft_pick_ticket_candidate(eligible, candidates, config_dict)
        if not chosen:
            debug.log('[TICKET SELECT] 沒有可滿足票數與關鍵字的票種。')
            return False, None, None
        select_id = chosen['id']
        select_obj = await tab.query_selector('select[id=' + json.dumps(select_id) + ']')
        if not select_obj:
            return False, None, None
        target = int(config_dict.get('ticket_number', 1))
        allow_less = config_dict.get('tixcraft', {}).get('allow_less_tickets', False)
        count = chosen['value'] if _tixcraft_ticket_count_ok(chosen.get('value'), target, allow_less) else chosen['count']
        record = {'document_id': snapshot['document_id'], 'select_id': select_id, 'target': target,
                  'allow_less': bool(allow_less), 'count': count,
                  'config_signature': _tixcraft_ticket_config_signature(config_dict)}
        _state['tixcraft_ticket_selection'] = record
        assigned = _tixcraft_ticket_state_valid(snapshot, record)
        debug.log(f'[TICKET SELECT] 選定票種：{chosen.get("name", "")}，目標 {target} 張，已核對：{assigned}')
        return assigned, select_obj, select_id
    except Exception:
        return False, None, None


async def nodriver_tixcraft_ticket_main_agree(tab, config_dict):
    debug = util.create_debug_logger(config_dict)
    for i in range(3):
        if await _tixcraft_actions_paused(config_dict):
            return False
        if await nodriver_check_checkbox_enhanced(tab, '#TicketForm_agree', config_dict):
            return True
        if await _tixcraft_sleep(0.3, config_dict):
            return False
    debug.log('[TIXCRAFT] 尚未確認購票條款已勾選，暫不送出。')
    return False

async def nodriver_tixcraft_ticket_main(tab, config_dict, ocr, Captcha_Browser, domain_name):
    """每輪以實際文件及全表張數確認狀態，不沿用同網址的完成旗標。"""
    if await _tixcraft_actions_paused(config_dict):
        return False
    if await _tixcraft_submission_pending(tab, config_dict):
        return False
    debug = util.create_debug_logger(config_dict)
    document_id = await _tixcraft_document_id(tab)
    if not document_id:
        return False
    ticket_number = str(config_dict["ticket_number"])
    allow_less_tickets = config_dict.get("tixcraft", {}).get("allow_less_tickets", False)
    if not await _tixcraft_ticket_selection_valid(tab, config_dict):
        assigned, select_obj, select_id = await nodriver_tixcraft_assign_ticket_number(tab, config_dict)
        if not assigned and select_obj is not None:
            await nodriver_ticket_number_select_fill(
                tab, select_obj, ticket_number, select_id, allow_less_tickets=allow_less_tickets)
        if not await _tixcraft_action_allowed(tab, config_dict, document_id):
            return False
        if not await _tixcraft_ticket_selection_valid(tab, config_dict):
            debug.log('[TIXCRAFT] 張數未通過全表核對，暫不送出。')
            await _tixcraft_reload_after_wait(tab, config_dict, document_id)
            return False
    if not await _tixcraft_action_allowed(tab, config_dict, document_id):
        return False
    if not await nodriver_tixcraft_ticket_main_agree(tab, config_dict):
        return False
    if await _tixcraft_ticket_selection_valid(tab, config_dict):
        await nodriver_tixcraft_ticket_main_ocr(tab, config_dict, ocr, Captcha_Browser, domain_name)


def _tixcraft_http_log(config, message):
    util.create_debug_logger(config, enabled=True).log('[TIXCRAFT HTTP] ' + message)


def _tixcraft_http_time(config, phase):
    clock = _state.setdefault('http_timing', {'start': time.monotonic(), 'last': time.monotonic()})
    now = time.monotonic()
    _tixcraft_http_log(config, '%s：累計 %.3f 秒，本階段 %.3f 秒。' % (phase, now-clock['start'], now-clock['last']))
    clock['last'] = now


def _tixcraft_http_choose(rows, config, date=False):
    if date:
        eligible = [row for row in rows if not (row['sold'] and config.get('tixcraft', {}).get('pass_date_is_sold_out', True))]
    else:
        amount = int(config.get('ticket_number', 1))
        less = config.get('tixcraft', {}).get('allow_less_tickets', False)
        eligible = [row for row in rows if not row['sold'] and (row['count'] is None or row['count'] >= amount or less and row['count'] > 0)]
    eligible = [row for row in eligible if not util.reset_row_text_if_match_keyword_exclude(config, row['name'])]
    section = config['date_auto_select' if date else 'area_auto_select']
    raw = section.get('date_keyword' if date else 'area_keyword', '').strip()
    groups = util.parse_keyword_string_to_array(raw) if raw else ['']
    for group in groups:
        if date and isinstance(group, list) and all(isinstance(word, str) for word in group):
            matched = [row for row in eligible if all(ticket_http.date_matches(word, row['name']) for word in group)]
            if matched:
                return util.get_target_item_from_matched_list(matched, section['mode'])
            continue
        if not isinstance(group, str):
            continue
        if date:
            matched = [row for row in eligible if ticket_http.date_matches(group, row['name'])]
        else:
            words = [util.format_keyword_string(word) for word in group.split()]
            matched = [row for row in eligible if all(word in util.format_keyword_string(row['name']) for word in words)]
        if matched:
            return util.get_target_item_from_matched_list(matched, section['mode'])
    if config.get('date_auto_fallback' if date else 'area_auto_fallback', False):
        return util.get_target_item_from_matched_list(eligible, section['mode'])
    return None


def _tixcraft_http_drain(task):
    if not task.cancelled():
        task.exception()


def _tixcraft_http_area_diagnostics(rows, config):
    amount = int(config.get('ticket_number', 1))
    less = config.get('tixcraft', {}).get('allow_less_tickets', False)
    raw = config.get('area_auto_select', {}).get('area_keyword', '').strip()
    groups = util.parse_keyword_string_to_array(raw) if raw else ['']
    if not rows:
        return '可選票區 0 個：本次未解析出可點選票區，無法據此取得全場精確餘票數。'
    details = []
    for row in rows:
        name, count = row['name'], row['count']
        if row['sold']:
            reason = '網站標示售完'
        elif count is not None and count < amount and not (less and count > 0):
            reason = '張數不足，需 %s 張' % amount
        elif util.reset_row_text_if_match_keyword_exclude(config, name):
            reason = '符合排除字'
        elif not any(isinstance(group, str) and all(util.format_keyword_string(word) in util.format_keyword_string(name)
                     for word in group.split()) for group in groups):
            reason = '未匹配區域關鍵字'
        else:
            reason = '符合篩選條件'
        stock = '網站未提供精確張數' if count is None else '餘額 %s 張' % count
        details.append('%s〔%s；%s〕' % (name, stock, reason))
    return '可選票區 %s 個；%s' % (len(rows), '；'.join(details))


def _tixcraft_http_expiry(config):
    try:
        timeout = float(config.get('advanced', {}).get('tixcraft_http_dispatch_timeout', 1))
    except (TypeError, ValueError):
        timeout = 1
    if not .05 <= timeout <= 5:
        timeout = 1
    return int((time.time() + timeout) * 1000)


async def _tixcraft_watch_entry_response(tab, target, frame_id):
    """Reuse the document GET caused by the real entry click; no second GET."""
    capture = {'tab': tab, 'url': target, 'frame': frame_id, 'future': asyncio.get_running_loop().create_future()}
    capture['future'].add_done_callback(_tixcraft_http_drain)
    _state['http_entry_capture'] = capture
    async def response(event, *_):
        if (isinstance(event, cdp.network.ResponseReceived) and _state.get('http_entry_capture') is capture and
                event.response.url == target and event.frame_id == frame_id and event.type_ == cdp.network.ResourceType.DOCUMENT):
            capture.update(request=event.request_id, loader=str(event.loader_id), status=int(event.response.status))
    async def finished(event, *_):
        if (not isinstance(event, cdp.network.LoadingFinished) or _state.get('http_entry_capture') is not capture or
                event.request_id != capture.get('request') or capture['future'].done()):
            return
        try:
            body, encoded = await tab.send(cdp.network.get_response_body(event.request_id))
            if encoded:
                body = base64.b64decode(body).decode('utf8', errors='replace')
            value = (capture['status'], body if len(body.encode('utf8')) <= ticket_http.CONST_HTML_LIMIT else None)
        except Exception:
            value = (0, None)
        if not capture['future'].done():
            capture['future'].set_result(value)
    async def loading(event, *_):
        if (isinstance(event, cdp.page.FrameStartedLoading) and _state.get('http_entry_capture') is capture and
                event.frame_id == frame_id):
            capture['started'] = True
    tab.add_handler(cdp.network.ResponseReceived, response)
    tab.add_handler(cdp.network.LoadingFinished, finished)
    tab.add_handler(cdp.page.FrameStartedLoading, loading)
    await tab.send(cdp.page.enable())
    await tab.send(cdp.network.enable())
    return capture


async def _tixcraft_click_date_entry(tab, config, flow):
    capture = _state.get('http_entry_capture')
    if flow.get('entry_logged') and capture and capture.get('started'):
        return True
    if time.monotonic() < flow.get('entry_due', 0) or await _tixcraft_actions_paused(config):
        return True
    target = flow['entry']['url']
    if not flow.get('entry_watch'):
        await _tixcraft_watch_entry_response(tab, target, flow['frame'])
        flow['entry_watch'] = True
    result = await tab.evaluate(r'''(() => {
        const p=PARAMETERS;
        if(location.href!==p.source||Date.now()>p.expires)return false;
        const nodes=document.querySelectorAll('#gameList button[data-href],#gameList a[href],a[href],button[data-href]');
        for(const button of nodes){
            const href=button.getAttribute('data-href')||button.getAttribute('href');
            if(!href||new URL(href,location.href).href!==p.target)continue;
            if(button.matches(':disabled,.disabled')||button.getAttribute('aria-disabled')==='true'||!button.getClientRects().length)continue;
            button.click();return true;
        }
        return false;
    })()'''.replace('PARAMETERS', json.dumps({'source': flow['source'], 'target': target, 'expires': _tixcraft_http_expiry(config)})))
    flow['entry_due'] = time.monotonic() + 1
    if result is True and not flow.get('entry_logged'):
        flow['entry_logged'] = True
        _tixcraft_http_log(config, '已點場次入口，等待正確場次載入並同步讀取該次回應。')
    return True


def _tixcraft_start_catalog_read(tab, url, flow):
    capture = _state.get('http_entry_capture')
    if (not flow['reads'] and capture and capture['tab'] is tab and capture['url'] == url and
            capture.get('loader', flow['identity'][1]) == flow['identity'][1] and not capture.get('consumed')):
        flow['task'] = capture['future']
        capture['consumed'] = True
    else:
        read = ticket_http.current_html(tab) if not util.is_auto_refresh_enabled() and flow['endpoint'] == url else ticket_http.read_html(tab, flow['endpoint'], flow['frame'])
        flow['task'] = asyncio.create_task(read)
    flow['task'].add_done_callback(_tixcraft_http_drain)
    flow['read_started'] = time.monotonic()


async def _tixcraft_http_catalog(tab, url, config):
    parts = urlsplit(url).path.strip('/').split('/')
    if parts[:2] == ['activity', 'detail']:
        return False  # Retain the existing activity-detail entry handling.
    is_date = parts[:2] in (['activity', 'game'], ['activity', 'detail'])
    if not is_date and parts[:2] != ['ticket', 'area']:
        return False
    if is_date and not config.get('date_auto_select', {}).get('enable', True):
        return False
    if not is_date and not config.get('area_auto_select', {}).get('enable', True):
        return False
    signature = json.dumps({key: config.get(key) for key in ('date_auto_select', 'area_auto_select', 'date_auto_fallback',
                           'area_auto_fallback', 'ticket_number', 'keyword_exclude', 'tixcraft')}, sort_keys=True)
    flow = _state.get('http_catalog')
    if not flow or flow['source'] != url or flow['signature'] != signature or flow['tab'] is not tab:
        # Old GETs are read-only. Let their replies drain: cancelling a pending
        # CDP command on the same socket can break Zendriver's response listener.
        identity = await ticket_http.page_identity(tab)
        if identity[0] != url:
            return True
        endpoint = urljoin(url, '/activity/game/' + parts[2]) if is_date else url
        flow = {'source': url, 'signature': signature, 'identity': identity[:2], 'frame': identity[2],
                'tab': tab, 'endpoint': endpoint, 'date': is_date, 'task': None,
                'due': _state.get('http_next_read', 0), 'reads': 0,
                'interval': float(config.get('advanced', {}).get('auto_reload_page_interval', 1))}
        _state['http_catalog'] = flow
        _state.setdefault('http_timing', {'start': time.monotonic(), 'last': time.monotonic()})
    elif time.monotonic() >= flow.get('identity_due', 0):
        flow['identity_due'] = time.monotonic() + .25
        if (await ticket_http.page_identity(tab))[:2] != flow['identity']:
            _state.pop('http_catalog', None)
            return True  # A manual F5 is a new document, even with the same URL.
    interval = max(0, float(config.get('advanced', {}).get('auto_reload_page_interval', 1)))
    if interval != flow['interval']:
        _tixcraft_http_log(config, '重整間隔已即時套用：%s → %s 秒。' % (flow['interval'], interval))
        flow['interval'] = interval
        if not flow.get('forbidden_retry'):
            flow['due'] = flow.get('completed', time.monotonic()) + interval
    if flow.get('entry'):
        return await _tixcraft_click_date_entry(tab, config, flow)
    if flow.get('blocked') or time.monotonic() < flow['due']:
        return True
    if flow['task'] is None:
        if flow['reads'] and (not util.is_auto_refresh_enabled() or not _is_page_reload_enabled(config, util.create_debug_logger(config), 'TIXCRAFT HTTP') or
                              flow['date'] and not config.get('tixcraft', {}).get('auto_reload_coming_soon_page', True)):
            return True
        _tixcraft_start_catalog_read(tab, url, flow)
        return True
    if not flow['task'].done():
        return True
    try:
        status, html = flow['task'].result()
    except Exception:
        status, html = 0, None
    flow['task'] = None
    flow['reads'] += 1
    flow['completed'] = time.monotonic()
    _tixcraft_http_log(config, '讀取%s HTTP %s，耗時 %.3f 秒。' % ('場次' if flow['date'] else '票況', status, time.monotonic()-flow['read_started']))
    flow.pop('forbidden_retry', None)
    if status == 403:
        minimum = max(1, int(float(config.get('advanced', {}).get('tixcraft_http_forbidden_retry_interval', 5))))
        maximum = max(minimum, int(float(config.get('advanced', {}).get('tixcraft_http_forbidden_retry_interval_max', 10))))
        delay = random.randint(minimum, maximum)
        flow.update(forbidden_retry=True, due=flow['completed'] + delay)
        _tixcraft_http_log(config, '網站拒絕票況請求（HTTP 403），等待 %s 秒後重試；可暫停或在瀏覽器處理網站驗證。' % delay)
        util.write_instance_status('running', '票況 HTTP 403，等待 %s 秒後重試。' % delay)
        return True
    if status in (401, 429):
        flow['blocked'] = True
        _tixcraft_http_log(config, '網站拒絕票況請求（HTTP %s），已停止輪詢；請在瀏覽器處理驗證或登入，完成後手動 F5 重新確認。' % status)
        util.write_instance_status('waiting_manual', '票況請求未獲放行，請在瀏覽器確認登入或網站驗證；暫不重試。')
        return True
    endpoint_parts = urlsplit(flow['endpoint']).path.strip('/').split('/')
    rows = (ticket_http.parse_sessions(html, url, parts[2]) if flow['date'] else
            ticket_http.parse_areas(html, url, endpoint_parts[2], endpoint_parts[3])) if html else None
    if rows is None:
        _state.pop('http_catalog', None)
        return False  # No POST attempted: the existing page flow remains available.
    chosen = _tixcraft_http_choose(rows, config, date=flow['date'])
    if chosen:
        _tixcraft_http_log(config, ('選定場次：' if flow['date'] else '偵測到票區：') + chosen['name'])
        if flow['date']:
            flow['entry'] = chosen
            return await _tixcraft_click_date_entry(tab, config, flow)
        if await _tixcraft_actions_paused(config):
            return True
        if (await ticket_http.page_identity(tab))[:2] != flow['identity']:
            _state.pop('http_catalog', None)
            return True
        _tixcraft_http_time(config, '偵測到有票，直接進入選票頁')
        _state.update(http_ticket_navigation_started=time.monotonic(), http_ticket_navigation_url=chosen['url'])
        _state['http_selected_area'] = {'url': chosen['url'], 'row': dict(chosen), 'preferences': _tixcraft_area_preferences(config)}
        await tab.get(chosen['url'])
        _state.pop('http_catalog', None)
        return True
    flow['due'] = time.monotonic() + max(0, float(config.get('advanced', {}).get('auto_reload_page_interval', 1)))
    if not flow['date']:
        _tixcraft_http_log(config, _tixcraft_http_area_diagnostics(rows, config))
    _tixcraft_http_log(config, '目前沒有符合日期／區域關鍵字及張數的選項，依自動重整間隔讀取票況。')
    return True


async def _tixcraft_http_snapshot(tab, config, ocr):
    script = ticket_http.CONST_SNAPSHOT_JS.replace('RESET_LOGIC', CONST_TIXCRAFT_DISCONNECT_RESET_JS).replace(
        'RESET_DATA', json.dumps(_tixcraft_disconnect_parameters())).replace('CAPTCHA_LOGIC', CONST_TIXCRAFT_CAPTCHA_ID_JS).replace(
        'NEED_IMAGE', json.dumps(bool(ocr and config.get('ocr_captcha', {}).get('enable'))))
    value = await tab.evaluate(script)
    if not isinstance(value, dict) or not value.get('document_id'):
        return None
    if value.get('released'):
        await _tixcraft_finish_disconnect(config)
    return value


async def _tixcraft_experimental_queue(tab, config, snapshot, api):
    route = snapshot['document_id'].split('|', 1)[0]
    queue = _state.setdefault('http_queue', {})
    if queue.get('nonce') != api['nonce'] or queue.get('document') != snapshot['document_id']:
        queue.clear()
        queue.update(nonce=api['nonce'], document=snapshot['document_id'], handled=0)
        util.write_instance_status('running', 'API 實驗：網站排隊中，依網站指定間隔查詢。')
        _tixcraft_http_time(config, 'API 實驗：選票已確認進入排隊')
    if api.get('check') is not None and api.get('check_seq', 0) > queue['handled']:
        queue['handled'] = api['check_seq']
        result = ticket_http.check_result(api['check'], route)
        if result is None:
            queue['blocked'] = True
            util.write_instance_status('waiting_manual', '排隊回應無法確認，保留防重送；請查看瀏覽器。')
        elif result['state'] == 'ready':
            if not await _tixcraft_actions_paused(config) and (await ticket_http.page_identity(tab))[0] == route:
                await tab.get(result['url'])
            return True
        else:
            _tixcraft_http_log(config, 'API 實驗：網站要求等待 %s 秒，依回覆間隔查詢。' % result['delay'])
    remaining = (api.get('check_due_ms', 0)-api.get('browser_now_ms', 0))/1000
    if queue.get('blocked') or api.get('check_inflight') or remaining > 0 or await _tixcraft_actions_paused(config):
        return True
    await tab.evaluate(ticket_http.CONST_CHECK_JS.replace('PARAMETERS', json.dumps(
        {'nonce': api['nonce'], 'expires': _tixcraft_http_expiry(config)})))
    return True


async def _tixcraft_http_pending(tab, config, snapshot):
    """Keep native submission protection; website alone owns queue requests."""
    api = snapshot.get('api')
    if not api and snapshot.get('pending_state') == 'rejected':
        nonce = snapshot.get('pending_nonce')
        if nonce != _state.get('http_rejected_nonce'):
            _state['http_rejected_nonce'] = nonce
            _state['http_captcha_failures'] = _state.get('http_captcha_failures', 0) + 1
        if _state.get('http_captcha_failures', 0) >= max(1, int(config.get('advanced', {}).get('tixcraft_http_captcha_retries', 15))):
            await _tixcraft_submission_pending(tab, config)
            util.write_instance_status('waiting_manual', '驗證碼已達重試上限，請手動處理。')
            return True
    if api and _state.get('http_submit_experiment', False):
        route = snapshot['document_id'].split('|', 1)[0]
        if api.get('instance_id') == util.get_instance_id() and api.get('route') == route and isinstance(api.get('nonce'), str):
            if api['state'] == 'queued':
                return await _tixcraft_experimental_queue(tab, config, snapshot, api)
            if api['state'] == 'checkout' and not await _tixcraft_actions_paused(config):
                await tab.get(urljoin(route, '/ticket/checkout'))
                return True
            if api['state'] == 'posting' and api.get('active'):
                return True
            if api['state'] == 'rejected':
                if _state.get('http_rejected_nonce') != api['nonce']:
                    _state['http_rejected_nonce'] = api['nonce']
                    failures = _state.get('http_captcha_failures', 0) + 1
                    _state['http_captcha_failures'] = failures
                limit = max(1, int(config.get('advanced', {}).get('tixcraft_http_captcha_retries', 15)))
                if _state.get('http_captcha_failures', 0) >= limit:
                    _tixcraft_http_wait(config, snapshot, '驗證碼已達重試上限，請手動處理。', 'waiting_manual')
                    return True
                await _tixcraft_submission_pending(tab, config)
                return True
    if not await _tixcraft_submission_pending(tab, config):
        return True
    if api:
        _tixcraft_http_response_notice(config, api)
    return True


def _tixcraft_http_response_notice(config, api):
    identity = tuple(api.get(key) for key in ('nonce', 'state', 'status', 'response_reason', 'response_path'))
    if _state.get('http_response_notice') == identity:
        return
    _state['http_response_notice'] = identity
    reasons = {'login_redirect': '回應導向登入頁，請確認登入狀態',
               'same_ticket_page': '回應仍是選票頁，尚未確認原因', 'http_error': '網站拒絕或處理失敗',
               'network_error': '網路或回應讀取失敗', 'unexpected_redirect': '回應不是預期的排隊或結帳頁'}
    reason = reasons.get(api.get('response_reason'), '送出結果尚未確認')
    message = 'API 送出回應：狀態 %s，HTTP %s，%s；保留防重送，請檢查瀏覽器與訂單。' % (api.get('state', 'unknown'), api.get('status', '未知'), reason)
    _tixcraft_http_log(config, message)
    util.write_instance_status('waiting_manual', message)


def _tixcraft_area_preferences(config):
    return json.dumps({key: config.get(key) for key in ('area_auto_select', 'area_auto_fallback', 'keyword_exclude', 'ticket_number')}, sort_keys=True)


async def _tixcraft_apply_area_update(config):
    path = config.get('_runtime_config_path')
    if path:
        try:
            stat = os.stat(path)
            stamp = (path, stat.st_mtime_ns, stat.st_size)
            if _state.get('http_area_config_stamp') != stamp:
                fresh = await asyncio.to_thread(util.load_json_file, path)
                if not isinstance(fresh, dict):
                    raise ValueError('invalid settings')
                for key in ('area_auto_select', 'area_auto_fallback', 'keyword_exclude', 'ticket_number'):
                    if key in fresh:
                        config[key] = fresh[key]
                _state['http_area_config_stamp'] = stamp
        except Exception:
            _tixcraft_http_wait(config, None, '設定更新尚未讀取完成，暫不選票或送出；稍後重新確認。')
            return False
    signature = _tixcraft_area_preferences(config)
    if _state.get('http_area_preferences_notice') != signature:
        _state['http_area_preferences_notice'] = signature
        _tixcraft_http_log(config, '區域設定已套用：關鍵字 %s；自動回退 %s。' % (
            config.get('area_auto_select', {}).get('area_keyword', '') or '不限', '開啟' if config.get('area_auto_fallback', False) else '關閉'))
    return True


async def _tixcraft_verify_current_area(tab, config, snapshot):
    if not config.get('area_auto_select', {}).get('enable', True):
        return True
    route = snapshot['document_id'].split('|', 1)[0]
    selected = _state.get('http_selected_area', {})
    known = selected.get('url') == route
    signature = _tixcraft_area_preferences(config)
    if known and selected.get('preferences') == signature:
        return True  # The catalog already checked all keyword priorities.
    name = snapshot.get('area_name', '').strip()
    labels = [r.get('name', '') for r in snapshot['rows'] if not r.get('disabled') and
              not util.reset_row_text_if_match_keyword_exclude(config, r.get('name', ''))]
    row = {'name': name + ' ' + ' '.join(labels), 'url': route, 'count': None, 'sold': False}
    strict = dict(config, area_auto_fallback=False)
    raw = config.get('area_auto_select', {}).get('area_keyword', '').strip()
    if not known and name and (not raw or _tixcraft_http_choose([row], strict)):
        _state['http_selected_area'] = {'url': route, 'row': row, 'preferences': signature}
        return True
    if await _tixcraft_actions_paused(config) or await _tixcraft_submission_pending(tab, config):
        return False
    if (await ticket_http.page_identity(tab))[0] != route:
        return False
    _tixcraft_http_log(config, '重新核對票區：%s；目前票區不符或區域設定已更新，返回票區列表依新設定重選。' % (name or selected.get('row', {}).get('name', '無法確認')))
    _state.pop('http_catalog', None)
    _state.pop('http_ocr', None)
    _state.pop('http_image_retry_due', None)
    parts = urlsplit(route).path.strip('/').split('/')
    await tab.get(urljoin(route, '/ticket/area/' + parts[2] + '/' + parts[3]))
    return False


def _tixcraft_http_wait(config, snapshot, reason, state='running'):
    identity = ((snapshot or {}).get('document_id'), reason, state)
    if _state.get('http_wait_notice') != identity:
        _state['http_wait_notice'] = identity
        _tixcraft_http_log(config, reason)
        util.write_instance_status(state, reason)


async def _tixcraft_refresh_unreadable_captcha(tab, config, snapshot):
    """Refresh an invalid OCR challenge once; never submit or clear order guards."""
    if await _tixcraft_actions_paused(config):
        return
    identity = (snapshot['document_id'], snapshot['captcha'])
    refresh = _state.get('http_invalid_refresh')
    if not refresh or refresh['identity'] != identity:
        refresh = {'identity': identity, 'requested': False}
        _state['http_invalid_refresh'] = refresh
        _state['http_captcha_failures'] = _state.get('http_captcha_failures', 0) + 1
    limit = max(1, int(config.get('advanced', {}).get('tixcraft_http_captcha_retries', 15)))
    if _state['http_captcha_failures'] >= limit:
        _tixcraft_http_wait(config, snapshot, '驗證碼辨識已達重試上限，尚未送出；請手動輸入或換圖。', 'waiting_manual')
        return
    if refresh['requested']:
        return
    parameters = {'document': snapshot['document_id'], 'captcha': snapshot['captcha'], 'expires': _tixcraft_http_expiry(config)}
    result = await tab.evaluate(r'''(() => {
        CAPTCHA_LOGIC
        const p=PARAMETERS,key='ticketsHunter.tixcraft.submit';
        if(Date.now()>p.expires||location.href+'|'+performance.timeOrigin!==p.document||captchaId()!==p.captcha)return 'changed';
        if(window[Symbol.for(key)]||sessionStorage.getItem(key))return 'pending';
        const verify=document.querySelector('#TicketForm_verifyCode');
        if(verify?.value)return 'manual';
        if(typeof jQuery==='undefined')return 'manual';
        const img=jQuery('#TicketForm_verifyCode-image');
        if(!img.length||typeof img.yiiCaptcha!=='function')return 'manual';
        img.yiiCaptcha('refresh');return 'requested';
    })()'''.replace('CAPTCHA_LOGIC', CONST_TIXCRAFT_CAPTCHA_ID_JS).replace('PARAMETERS', json.dumps(parameters)))
    if result in ('requested', 'manual', 'pending'):
        refresh['requested'] = True
    if result == 'requested':
        _tixcraft_http_wait(config, snapshot, 'OCR 結果不是四碼，已要求更新驗證碼；等待新圖後重新辨識，尚未送出。')
    elif result == 'manual':
        _tixcraft_http_wait(config, snapshot, 'OCR 結果不是四碼，目前無法自動換圖或已有手動輸入；請手動處理，尚未送出。', 'waiting_manual')


def _tixcraft_http_phase(config, snapshot, stage):
    identity = (snapshot['document_id'], snapshot.get('captcha'))
    timing = _state.get('http_detail_timing')
    if not timing or timing['identity'] != identity:
        start = _state.get('http_ticket_navigation_started', time.monotonic()) if snapshot['document_id'].split('|', 1)[0] == _state.get('http_ticket_navigation_url') else time.monotonic()
        timing = {'identity': identity, 'start': start, 'marks': {}}
        _state['http_detail_timing'] = timing
    if stage not in timing['marks']:
        timing['marks'][stage] = now = time.monotonic()
        _tixcraft_http_log(config, '細部計時：%s，距進入選票頁指令 %.3f 秒。' % (stage, now - timing['start']))


def _tixcraft_http_form_parameters(config, snapshot, chosen, answer, submit):
    return {'document': snapshot['document_id'], 'captcha': snapshot['captcha'], 'id': chosen['id'],
            'count': chosen['count'], 'target': int(config.get('ticket_number', 1)),
            'allow_less': bool(config.get('tixcraft', {}).get('allow_less_tickets', False)),
            'answer': answer, 'require_captcha': snapshot['has_captcha'], 'submit': submit,
            'generation': util.get_browser_session_generation(), 'instance': util.get_instance_id(),
            'expires': _tixcraft_http_expiry(config)}


async def _tixcraft_parallel_ocr(tab, config, snapshot, chosen, ocr):
    async def recognize():
        _tixcraft_http_phase(config, snapshot, 'OCR 開始')
        value = await asyncio.to_thread(ocr.classification, base64.b64decode(snapshot['image']))
        _tixcraft_http_phase(config, snapshot, 'OCR 返回')
        return value
    async def prepare():
        if await _tixcraft_actions_paused(config):
            return {'state': 'paused'}
        _tixcraft_http_phase(config, snapshot, '張數與條款準備開始')
        parameters = _tixcraft_http_form_parameters(config, snapshot, chosen, None, False)
        script = (ticket_http.CONST_FORM_PREPARE_JS + '\n})()').replace(
            'CAPTCHA_LOGIC', CONST_TIXCRAFT_CAPTCHA_ID_JS).replace('PARAMETERS', json.dumps(parameters))
        result = await tab.evaluate(script)
        _tixcraft_http_phase(config, snapshot, '張數與條款準備返回')
        return result
    # Drain both operations even if one fails; never cancel an in-flight CDP command.
    value, prepared = await asyncio.gather(recognize(), prepare(), return_exceptions=True)
    for result in (value, prepared):
        if isinstance(result, BaseException):
            raise result
    return value, isinstance(prepared, dict) and prepared.get('state') == 'prepared'


async def _tixcraft_http_recognize(tab, config, snapshot, chosen, ocr, experiment):
    cached = _state.get('http_ocr')
    identity = (snapshot['document_id'], snapshot['captcha'])
    if cached and cached[0] == identity:
        return cached[1], True
    started = time.monotonic()
    _tixcraft_http_log(config, '驗證碼圖片已就緒，開始 OCR 辨識。')
    if experiment:
        value, prepared = await _tixcraft_parallel_ocr(tab, config, snapshot, chosen, ocr)
    else:
        _tixcraft_http_phase(config, snapshot, 'OCR 開始')
        value = await asyncio.to_thread(ocr.classification, base64.b64decode(snapshot['image']))
        _tixcraft_http_phase(config, snapshot, 'OCR 返回')
        prepared = True
    answer = value.strip() if isinstance(value, str) else ''
    _state['http_ocr'] = (identity, answer)
    _tixcraft_http_log(config, 'OCR 與必要準備已返回，耗時 %.3f 秒；取得 %s 碼，需要 4 碼。' % (time.monotonic()-started, len(answer)))
    return answer, prepared


async def _tixcraft_http_ticket(tab, config, ocr):
    experiment = _state.setdefault('http_submit_experiment', config.get('advanced', {}).get('tixcraft_api_submit_experiment') is True)
    if time.monotonic() < _state.get('http_image_retry_due', 0):
        return True
    snapshot = await _tixcraft_http_snapshot(tab, config, ocr)
    if not snapshot:
        _tixcraft_http_wait(config, snapshot, '等待選票頁可讀取，尚未送出。')
        return True
    if snapshot.get('blocked'):
        util.write_instance_status('waiting_manual', '網站正在阻擋或驗證連線，停止送出，請先在瀏覽器處理網站驗證。')
        return True
    if snapshot.get('pending'):
        return await _tixcraft_http_pending(tab, config, snapshot)
    if _state.get('tixcraft_submit_pending'):
        _tixcraft_pending_notice(config, ('python', _state['tixcraft_submit_pending']), 'python')
        return True
    if _state.get('http_soldout_document') == snapshot['document_id']:
        return True
    if not snapshot.get('supported') or not snapshot.get('rows'):
        _tixcraft_http_wait(config, snapshot, '等待選票表單載入，尚未送出。')
        return True
    if not await _tixcraft_verify_current_area(tab, config, snapshot):
        return True
    preferences = _tixcraft_area_preferences(config)
    _tixcraft_http_phase(config, snapshot, '選票表單已讀取')
    if not snapshot.get('captcha_ready'):
        _state['http_image_retry_due'] = time.monotonic() + max(.01, float(config.get('advanced', {}).get('tixcraft_http_image_retry_interval', .2)))
        _tixcraft_http_wait(config, snapshot, '等待驗證碼圖片載入完成，尚未辨識或送出。')
        return True  # Partial document/unfinished captcha is not sold out; never F5 here.
    _tixcraft_http_phase(config, snapshot, '驗證碼圖片已載入')
    eligible, candidates = _tixcraft_ticket_candidates(snapshot, config)
    chosen = _tixcraft_pick_ticket_candidate(eligible, candidates, config)
    if chosen is None:
        if snapshot.get('ready') == 'loading':
            return True
        if util.is_auto_refresh_enabled() and await _tixcraft_action_allowed(tab, config, snapshot['document_id']):
            # Return to the known area catalog; subsequent updates only read HTML.
            parts = urlsplit(snapshot['document_id'].split('|', 1)[0]).path.split('/')
            _state['http_next_read'] = time.monotonic() + max(0, float(config.get('advanced', {}).get('auto_reload_page_interval', 1)))
            await tab.get(urljoin(snapshot['document_id'].split('|', 1)[0], '/ticket/area/'+parts[3]+'/'+parts[4]))
        return True
    answer = None
    manual_only = _state.get('http_captcha_failures', 0) >= max(1, int(config.get('advanced', {}).get('tixcraft_http_captcha_retries', 15)))
    if snapshot['has_captcha'] and not manual_only and not snapshot.get('captcha_filled') and config.get('ocr_captcha', {}).get('enable') and ocr is not None:
        if not snapshot.get('image'):
            _state['http_image_retry_due'] = time.monotonic() + max(.01, float(config.get('advanced', {}).get('tixcraft_http_image_retry_interval', .2)))
            _tixcraft_http_wait(config, snapshot, '驗證碼圖片已載入但無法取得辨識影像，持續重新確認；尚未送出。')
            return True
        answer, prepared = await _tixcraft_http_recognize(tab, config, snapshot, chosen, ocr, experiment)
        if len(answer) != 4:
            await _tixcraft_refresh_unreadable_captcha(tab, config, snapshot)
            return True
        if not prepared:
            _tixcraft_http_wait(config, snapshot, '表單準備尚未完成，重新確認張數與條款；尚未送出。')
            return True
    elif snapshot['has_captcha'] and not snapshot.get('captcha_filled'):
        _tixcraft_http_wait(config, snapshot, '等待手動輸入驗證碼：自動辨識未啟用、不可用或已達重試上限；尚未送出。')
    if not await _tixcraft_apply_area_update(config) or preferences != _tixcraft_area_preferences(config):
        return True
    if await _tixcraft_actions_paused(config):
        return True
    parameters = _tixcraft_http_form_parameters(config, snapshot, chosen, answer,
        bool(config.get('ocr_captcha', {}).get('force_submit', False)) and not manual_only)
    if parameters['submit']:
        _state['tixcraft_submit_pending'] = snapshot['document_id']
        _state['tixcraft_submit_generation'] = parameters['generation']
    template = ticket_http.CONST_EXPERIMENTAL_SEND_JS if experiment else ticket_http.CONST_SEND_JS
    script = template.replace('CAPTCHA_LOGIC', CONST_TIXCRAFT_CAPTCHA_ID_JS).replace('PARAMETERS', json.dumps(parameters))
    _tixcraft_http_phase(config, snapshot, '最終核對與送出開始')
    result = await tab.evaluate(script)
    _tixcraft_http_phase(config, snapshot, '最終核對與送出指令返回')
    stage = result.get('state') if isinstance(result, dict) else 'unknown'
    if stage in ('changed', 'not_ready', 'prepared', 'unsupported', 'expired'):
        _state.pop('tixcraft_submit_pending', None)
        _state.pop('tixcraft_submit_generation', None)
    if stage == 'expired' and _state.get('http_expired_notice') != snapshot['document_id']:
        _state['http_expired_notice'] = snapshot['document_id']
        _tixcraft_http_log(config, '瀏覽器執行延遲，本次尚未發出的請求已作廢；重新確認暫停與表單後才會嘗試。')
    if stage == 'unsupported' and _state.get('http_unsupported_notice') != snapshot['document_id']:
        _state['http_unsupported_notice'] = snapshot['document_id']
        util.write_instance_status('waiting_manual', '目前選票表單不符合已確認的 HTTP 格式，尚未送出；請查看瀏覽器或切回原本頁面流程。')
    if stage == 'invoked':
        phase = 'API 實驗已送出選票' if experiment else '已點原本送出按鈕'
        _tixcraft_http_time(config, phase + '（%s 張）' % chosen['count'])
        util.write_instance_status('running', '已點選票送出按鈕，等待網站導向與排隊；尚未確認購票完成。')
    return True


async def _tixcraft_http_main(tab, url, config, ocr):
    parsed = urlsplit(url)
    if parsed.hostname not in ('tixcraft.com', 'www.tixcraft.com') or not config.get('advanced', {}).get('tixcraft_http_api', True):
        return False
    if not await _tixcraft_apply_area_update(config):
        return True
    _state.setdefault('http_submit_experiment', config.get('advanced', {}).get('tixcraft_api_submit_experiment') is True)
    _state.setdefault('http_timing', {'start': time.monotonic(), 'last': time.monotonic()})
    if parsed.path.startswith('/ticket/ticket/'):
        return await _tixcraft_http_ticket(tab, config, ocr)
    return await _tixcraft_http_catalog(tab, url, config)

async def _tixcraft_actions_paused(config_dict=None):
    """每個可變更瀏覽器狀態的步驟都重新查暫停及結束。"""
    return await check_and_handle_pause(config_dict) or await check_and_handle_quit(config_dict)


async def _tixcraft_sleep(seconds, config_dict=None):
    deadline = time.monotonic() + max(0, float(seconds))
    while time.monotonic() < deadline:
        if await _tixcraft_actions_paused(config_dict):
            return True
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return await _tixcraft_actions_paused(config_dict)


async def _tixcraft_document_id(tab):
    try:
        value = await tab.evaluate("location.href + '|' + performance.timeOrigin")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


CONST_TIXCRAFT_CAPTCHA_ID_JS = r'''
function captchaId(){
    const img=document.querySelector('#TicketForm_verifyCode-image');
    if(!img||!img.complete||!img.naturalWidth)return '';
    let hash=0;
    if(typeof jQuery!=='undefined'){
        const data=jQuery('body').data('yiiCaptcha/ticket/captcha');
        if(data&&data[0])hash=data[0];
    }
    return (img.currentSrc||img.src||'')+'|'+hash;
}
'''


CONST_TIXCRAFT_DISCONNECT_RESET_JS = r'''
const resetSubmission=({generation,force,pythonStale,instance})=>{
    if(!generation&&!force)return {cleared:false};
    const key='ticketsHunter.tixcraft.submit',active=window[Symbol.for(key)];
    let saved=null,corrupt=false;try{saved=JSON.parse(sessionStorage.getItem(key)||'null');}catch(_){corrupt=true;}
    if(saved&&(typeof saved!=='object'||typeof saved.nonce!=='string'))corrupt=true;
    const foreign=record=>record?.instanceId&&record.instanceId!==instance;
    if(!force&&(foreign(saved)||foreign(active)))return {cleared:false};
    const stale=!!generation&&(saved&&saved.resetToken!==generation||active&&active.resetToken!==generation);
    if(!force&&(!stale&&!pythonStale||corrupt))return {cleared:false};
    if(!force&&!stale&&(saved||active))return {cleared:false};
    if(!saved&&!active&&!corrupt&&!pythonStale)return {cleared:false};
    if(active?.transport==='http'&&Array.isArray(active.buttons)){
        for(const item of active.buttons)if(item.button?.isConnected)item.button.disabled=item.disabled;
    }
    sessionStorage.removeItem(key);delete window[Symbol.for(key)];return {cleared:true};
};
'''


def _tixcraft_disconnect_parameters(force=False):
    generation = util.get_browser_session_generation()
    stale = bool(_state.get('tixcraft_submit_pending') and
                 (force or generation and generation != _state.get('tixcraft_submit_generation')))
    return {'generation': generation, 'force': force, 'pythonStale': stale, 'instance': util.get_instance_id()}


async def _tixcraft_finish_disconnect(config_dict):
    for key in ('tixcraft_submit_pending', 'tixcraft_submit_generation', 'tixcraft_pending_notice',
                'tixcraft_ticket_selection', 'tixcraft_manual_captcha', 'captcha_rejected_notice'):
        _state.pop(key, None)
    _state['captcha_alert_detected'] = False
    _state['ocr_completed_url'] = ''
    message = '已依「斷開瀏覽器」解除本輪本機送出鎖定，可在原分頁重新開始；已送達網站的請求無法撤回，訂單結果仍以網站為準。'
    util.create_debug_logger(config_dict, enabled=True).log('[TIXCRAFT] ' + message)


async def nodriver_tixcraft_disconnect(tab, config_dict=None):
    """Explicit disconnect ends this run without clicking, navigating or cancelling an order."""
    if tab is None:
        return
    urls = str(getattr(getattr(tab, 'target', None), 'url', '') or '') + ' ' + str((config_dict or {}).get('homepage', ''))
    if not _state.get('tixcraft_submit_pending') and not any(
            domain in urls for domain in ('tixcraft.com', 'indievox.com', 'ticketmaster.')):
        return
    try:
        cleared = await tab.evaluate(r'''(() => {
            RESET_LOGIC
            return resetSubmission(RESET_DATA).cleared;
        })()'''.replace('RESET_LOGIC', CONST_TIXCRAFT_DISCONNECT_RESET_JS).replace(
            'RESET_DATA', json.dumps(_tixcraft_disconnect_parameters(force=True))))
    except Exception:
        return
    if cleared is True:
        await _tixcraft_finish_disconnect(config_dict)


async def _tixcraft_mark_submission_rejected(tab):
    """只在明確驗證碼錯誤視窗已關閉後標記，換碼前仍保持鎖定。"""
    try:
        await tab.evaluate(r'''(() => {
            const key='ticketsHunter.tixcraft.submit',r=window[Symbol.for(key)];
            const doc=location.href+'|'+performance.timeOrigin;
            const saved=JSON.parse(sessionStorage.getItem(key)||'null');
            if(!saved||saved.route!==location.href)return;
            if(r&&(r.documentId!==doc||saved.nonce!==r.nonce))return;
            if(r)r.state='rejected';saved.state='rejected';sessionStorage.setItem(key,JSON.stringify(saved));
        })()''')
    except Exception:
        pass


async def _tixcraft_submission_handoff(tab):
    """已到實際結帳頁才交接；清除提交鎖不代表訂單付款完成。"""
    try:
        cleared = await tab.evaluate(r'''(() => {
            if(!/^\/ticket\/checkout(?:\/|$)/i.test(location.pathname))return false;
            const key='ticketsHunter.tixcraft.submit';
            sessionStorage.removeItem(key);delete window[Symbol.for(key)];return true;
        })()''')
    except Exception:
        return False
    if cleared:
        _state.pop('tixcraft_submit_pending', None)
        _state.pop('tixcraft_pending_notice', None)
    return bool(cleared)


async def _tixcraft_mark_submission_failed(tab):
    """明確售完且視窗已關閉後，僅解除同場次同分頁的上一筆鎖。"""
    try:
        cleared = await tab.evaluate(r'''(() => {
            const key='ticketsHunter.tixcraft.submit',r=window[Symbol.for(key)];
            const saved=JSON.parse(sessionStorage.getItem(key)||'null');
            if(!saved||r&&r.nonce!==saved.nonce)return false;
            const previous=new URL(saved.route),parts=location.pathname.split('/').filter(Boolean);
            const old=previous.pathname.split('/').filter(Boolean);
            if(previous.origin!==location.origin||old[0]!=='ticket'||old[1]!=='ticket'||
               parts[0]!=='ticket'||!['ticket','area'].includes(parts[1])||
               parts[2]!==old[2]||parts[3]!==old[3])return false;
            sessionStorage.removeItem(key);delete window[Symbol.for(key)];return true;
        })()''')
    except Exception:
        return False
    if cleared:
        _state.pop('tixcraft_submit_pending', None)
        _state.pop('tixcraft_pending_notice', None)
        util.write_instance_status('running', '網站已明確回覆票券售完，重新偵測票況。')
    return bool(cleared)


def _tixcraft_pending_notice(config_dict, identity, source='saved'):
    if _state.get('tixcraft_pending_notice') == identity:
        return
    _state['tixcraft_pending_notice'] = identity
    if source == 'read_failure':
        message = '暫時無法讀取頁面與送出狀態，正在重新確認，暫不送出；這不代表已經送出購票。'
    else:
        origins = {'saved': '瀏覽器保留的紀錄', 'active': '本頁送出紀錄', 'python': '程式保留的待確認指令'}
        message = ('上一筆送出尚待網站確認（' + origins.get(source, '待確認紀錄') +
                   '），暫不重送；請確認排隊或訂單結果。如要結束本輪，可按「斷開瀏覽器」後在原分頁重新開始。')
    util.create_debug_logger(config_dict).log('[TIXCRAFT] ' + message)
    util.write_instance_status('running' if source == 'read_failure' else 'waiting_manual', message)


async def _tixcraft_submission_pending(tab, config_dict=None):
    """結果未知時跨重連與 F5 保留鎖定，不能只靠時間或 URL 猜測。"""
    try:
        result = await tab.evaluate(r'''(() => {
            RESET_LOGIC
            const reset=resetSubmission(RESET_DATA);
            CAPTCHA_ID
            const doc=location.href+'|'+performance.timeOrigin,key='ticketsHunter.tixcraft.submit';
            const r=window[Symbol.for(key)],saved=JSON.parse(sessionStorage.getItem(key)||'null');
            const captcha=captchaId();
            const rejected=!!saved&&saved.state==='rejected';
            const changedCaptcha=r?.documentId===doc&&r.state==='rejected'&&r.captcha&&captcha&&r.captcha!==captcha;
            const newRejectedDocument=rejected&&saved.documentId!==doc&&!!captcha;
            if(rejected&&(changedCaptcha||newRejectedDocument)){
                sessionStorage.removeItem(key);delete window[Symbol.for(key)];
                return {documentId:doc,pending:false,released:true};
            }
            return {documentId:doc,pending:!!saved||!!r,rejected,nonce:saved?.nonce,
                source:saved?'saved':r?'active':null,resetToken:saved?.resetToken??r?.resetToken??null,
                released:reset.cleared,releaseReason:reset.cleared?'disconnect':null};
        })()'''.replace('RESET_LOGIC', CONST_TIXCRAFT_DISCONNECT_RESET_JS).replace(
            'RESET_DATA', json.dumps(_tixcraft_disconnect_parameters())).replace('CAPTCHA_ID', CONST_TIXCRAFT_CAPTCHA_ID_JS))
    except Exception:
        _tixcraft_pending_notice(config_dict, ('read_failure', _state.get('tixcraft_submit_pending')), 'read_failure')
        return True
    if not isinstance(result, dict) or not result.get('documentId'):
        _tixcraft_pending_notice(config_dict, ('read_failure', _state.get('tixcraft_submit_pending')), 'read_failure')
        return True
    doc = result['documentId']
    if result.get('released'):
        if result.get('releaseReason') == 'disconnect':
            await _tixcraft_finish_disconnect(config_dict)
        else:
            _state.pop('tixcraft_submit_pending', None)
            _state.pop('tixcraft_submit_generation', None)
            _state['captcha_alert_detected'] = False
            _state['ocr_completed_url'] = ''
        util.write_instance_status('running', '上一輪狀態已解除，繼續偵測與核對選票。')
    pending = result.get('pending') is True or bool(_state.get('tixcraft_submit_pending'))
    if pending:
        _state.setdefault('tixcraft_submit_pending', doc)
        if result.get('source'):
            _state['tixcraft_submit_generation'] = result.get('resetToken')
        _tixcraft_pending_notice(config_dict, (doc, result.get('source')), result.get('source') or 'python')
        if result.get('rejected'):
            await _tixcraft_refresh_rejected(tab, config_dict, doc, result.get('nonce'))
    else:
        if _state.pop('tixcraft_pending_notice', None) and not result.get('released'):
            util.write_instance_status('running', '網站狀態已確認，繼續偵測與選票。')
    return pending


async def _tixcraft_refresh_rejected(tab, config_dict, document_id, nonce):
    """已明確拒絕的同一筆只換圖一次；未知結果不能走此路徑。"""
    if await _tixcraft_actions_paused(config_dict):
        return
    ocr_settings = (config_dict or {}).get('ocr_captcha', {})
    force = bool(ocr_settings.get('force_submit') and ocr_settings.get('enable'))
    state = 'manual'
    if force:
        try:
            state = await tab.evaluate(r'''(() => {
                CAPTCHA_ID
                const key='ticketsHunter.tixcraft.submit',r=window[Symbol.for(key)];
                const saved=JSON.parse(sessionStorage.getItem(key)||'null');
                if(location.href+'|'+performance.timeOrigin!==EXPECTED||!r||r.documentId!==EXPECTED||
                   r.nonce!==NONCE||saved?.nonce!==NONCE||saved.state!=='rejected'||r.state!=='rejected')return 'stale';
                if(r.refreshRequested)return 'waiting';
                if(!captchaId()||captchaId()!==r.captcha)return 'waiting';
                if(typeof jQuery==='undefined')return 'manual';
                const img=jQuery('#TicketForm_verifyCode-image');
                if(!img.length||typeof img.yiiCaptcha!=='function')return 'manual';
                r.refreshRequested=true;img.yiiCaptcha('refresh');return 'started';
            })()'''.replace('CAPTCHA_ID', CONST_TIXCRAFT_CAPTCHA_ID_JS).replace(
                'EXPECTED', json.dumps(document_id)).replace('NONCE', json.dumps(nonce)))
        except Exception:
            state = 'manual'
    if state == 'manual' and _state.get('captcha_rejected_notice') != nonce:
        _state['captcha_rejected_notice'] = nonce
        message = '網站已拒絕驗證碼，請手動點擊驗證碼換圖；確認新圖後才會解除送出防護。'
        util.create_debug_logger(config_dict).log('[TIXCRAFT] ' + message)
        util.write_instance_status('running', message)


async def _tixcraft_submit_form(tab, config_dict, require_captcha=False, expected_captcha=None):
    """瀏覽器端以同一動作核對欄位、取得鎖定與觸發原生送出。"""
    if await _tixcraft_actions_paused(config_dict) or await _tixcraft_submission_pending(tab, config_dict):
        return False
    document_id = await _tixcraft_document_id(tab)
    if not document_id:
        return False
    if urlsplit(document_id.split('|', 1)[0]).hostname in ('tixcraft.com', 'www.tixcraft.com'):
        if not await _tixcraft_apply_area_update(config_dict):
            return False
    is_ticketmaster = 'ticketmaster.' in document_id.split('|', 1)[0]
    if not is_ticketmaster and not await _tixcraft_ticket_selection_valid(tab, config_dict):
        _forget_ticket_assigned(document_id.split('|', 1)[0])
        return False
    if await _tixcraft_actions_paused(config_dict):
        return False
    selection = _state.get('tixcraft_ticket_selection', {})
    _state['tixcraft_submit_pending'] = document_id
    generation = util.get_browser_session_generation()
    _state['tixcraft_submit_generation'] = generation
    script = r'''(() => {
        CAPTCHA_ID
        const doc=location.href+'|'+performance.timeOrigin,key=Symbol.for('ticketsHunter.tixcraft.submit');
        if(doc!==EXPECTED)return {state:'changed'};
        if(window[key]||sessionStorage.getItem('ticketsHunter.tixcraft.submit'))return {state:'pending'};
        const verify=document.querySelector('#TicketForm_verifyCode'),agree=document.querySelector('#TicketForm_agree');
        const form=document.querySelector('#form-ticket-ticket')||verify?.form;
        const rows=Array.from(document.querySelectorAll('select[id^="TicketForm_ticketPrice_"]'));
        const positive=rows.filter(s=>Number(s.value)>0);
        const total=positive.reduce((n,s)=>n+Number(s.value),0);
        const countOk=TICKETMASTER||(positive.length===1&&new Set(rows.map(s=>s.id)).size===rows.length&&
            rows.every(s=>s.value===''||/^\d+$/.test(s.value))&&positive[0].id===SELECT_ID&&total===COUNT&&
            positive.every(s=>!s.matches(':disabled')&&Array.from(s.options).some(o=>
                o.value===s.value&&!o.disabled&&!o.closest('optgroup')?.disabled))&&
            Number.isInteger(total)&&(ALLOW_LESS?(total>0&&total<=AMOUNT):total===AMOUNT));
        if(!countOk||!agree?.checked||(CAPTCHA&&(!verify||verify.disabled||verify.value.length!==4)))
            return {state:'not_ready'};
        if(CHECK_CHALLENGE&&captchaId()!==CHECK_CHALLENGE)return {state:'not_ready'};
        if(!form||!form.checkValidity())return {state:'not_ready'};
        const button=form.querySelector('button[type="submit"],button:not([type]),input[type="submit"]');
        if(!button||button.matches(':disabled'))return {state:'not_ready'};
        const nonce=globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36);
        const record={documentId:doc,state:'invoked',captcha:captchaId(),nonce,resetToken:RESET_TOKEN,instanceId:INSTANCE_ID};
        try{sessionStorage.setItem('ticketsHunter.tixcraft.submit',JSON.stringify({documentId:doc,route:location.href,nonce:record.nonce,state:record.state,resetToken:record.resetToken,instanceId:record.instanceId}));}
        catch(_){return {state:'not_ready'};}
        window[key]=record;
        try{button.click();return {state:'invoked'};}catch(_){return {state:'unknown'};}
    })()'''.replace('INSTANCE_ID', json.dumps(util.get_instance_id())).replace('RESET_TOKEN', json.dumps(generation)).replace('CAPTCHA_ID', CONST_TIXCRAFT_CAPTCHA_ID_JS).replace('EXPECTED', json.dumps(document_id)).replace(
        'TICKETMASTER', json.dumps(is_ticketmaster)).replace('ALLOW_LESS', json.dumps(bool(
            config_dict.get('tixcraft', {}).get('allow_less_tickets', False)))).replace(
        'AMOUNT', json.dumps(config_dict.get('ticket_number', 2))).replace(
        'CHECK_CHALLENGE', json.dumps(expected_captcha)).replace('CAPTCHA', json.dumps(require_captcha)).replace(
        'SELECT_ID', json.dumps(selection.get('select_id'))).replace('COUNT', json.dumps(selection.get('count')))
    try:
        result = await tab.evaluate(script)
    except Exception:
        return False  # 回應遺失時保留 Python 鎖定，不猜測剛才沒有送出。
    state = result.get('state') if isinstance(result, dict) else 'unknown'
    if state in ('not_ready', 'changed'):
        _state.pop('tixcraft_submit_pending', None)
        _state.pop('tixcraft_submit_generation', None)
    return state in ('invoked', 'pending')


async def nodriver_tixcraft_keyin_captcha_code(tab, answer="", auto_submit=False, config_dict=None, captcha_snapshot=None):
    """輸入前與送出前均核對暫停，保留原生欄位事件及網站驗證。"""
    if await _tixcraft_actions_paused(config_dict) or await _tixcraft_submission_pending(tab, config_dict):
        return False, False
    config_dict = config_dict or {}
    try:
        form_verifyCode = await tab.query_selector('#TicketForm_verifyCode')
        visible = await tab.evaluate("(() => {const e=document.querySelector('#TicketForm_verifyCode');return !!e&&!e.disabled&&e.offsetParent!==null})()")
        if not form_verifyCode or not visible:
            return False, False
        current = await _tixcraft_captcha_snapshot(tab)
        if not current or (captcha_snapshot is not None and current != captcha_snapshot):
            return False, False
        manual_key = (current['documentId'], current.get('captcha'),
                      bool(config_dict.get('ocr_captcha', {}).get('enable')))
        if not auto_submit and _state.get('tixcraft_manual_captcha') == manual_key:
            return True, False
        if not answer:
            if await _tixcraft_actions_paused(config_dict):
                return False, False
            await form_verifyCode.click()
            _state['tixcraft_manual_captcha'] = manual_key
            return True, False
        captcha_before = current
        if await _tixcraft_actions_paused(config_dict):
            return False, False
        filled = await form_verifyCode.apply('''(el) => {
            NATIVE_SETTER
            return setNativeInputValue(el, ANSWER);
        }'''.replace('NATIVE_SETTER', CONST_NATIVE_INPUT_SETTER_JS).replace('ANSWER', json.dumps(str(answer))))
        if filled and not auto_submit:
            _state['tixcraft_manual_captcha'] = manual_key
        if not filled or await _tixcraft_actions_paused(config_dict):
            return False, False
        if auto_submit:
            await nodriver_check_checkbox_enhanced(tab, '#TicketForm_agree', config_dict)
            return False, await _tixcraft_submit_form(
                tab, config_dict, require_captcha=True,
                expected_captcha=captcha_before.get('captcha') if captcha_before else None)
        await tab.evaluate("document.querySelector('#TicketForm_verifyCode')?.select()")
        await nodriver_tixcraft_toast(tab, '驗證碼已填入，請檢查後手動送出。')
        _state['tixcraft_manual_captcha'] = manual_key
        return True, False
    except Exception:
        return False, False

async def nodriver_tixcraft_toast(tab, message):
    """顯示提示訊息"""
    try:
        await tab.evaluate(f'''
            (function() {{
                const toast = document.querySelector('p.remark-word');
                if (toast) {{
                    toast.innerHTML = '{message}';
                }}
            }})();
        ''')
    except Exception as exc:
        pass

async def nodriver_get_yii_captcha_hash(tab):
    """Read Yii2 captcha hash1 from body data (stored after refresh).
    Returns hash1 (int) or 0 if not yet available (first page load)."""
    try:
        result = await tab.evaluate('''
            (function() {
                if (typeof jQuery === "undefined") return 0;
                var data = jQuery("body").data("yiiCaptcha/ticket/captcha");
                return (data && data[0]) ? data[0] : 0;
            })()
        ''')
        return int(result) if result else 0
    except Exception:
        return 0


async def nodriver_tixcraft_reload_captcha(tab, domain_name, config_dict=None):
    """由原生 Yii 換圖，等待可被暫停／結束中斷。"""
    if await _tixcraft_actions_paused(config_dict) or await _tixcraft_submission_pending(tab, config_dict):
        return False
    try:
        before = await _tixcraft_captcha_snapshot(tab)
        if await _tixcraft_actions_paused(config_dict):
            return False
        started = await tab.evaluate('''(() => {
            if(typeof jQuery==='undefined')return false;
            const img=jQuery('#TicketForm_verifyCode-image');
            if(!img.length||typeof img.yiiCaptcha!=='function')return false;
            img.yiiCaptcha('refresh');return true;
        })()''')
        if not started:
            return False
        for _ in range(20):
            if await _tixcraft_sleep(0.1, config_dict):
                return False
            after = await _tixcraft_captcha_snapshot(tab)
            if not after or not before or after['documentId'] != before['documentId']:
                return False
            if after.get('captcha') and after['captcha'] != before.get('captcha'):
                return True
    except Exception:
        pass
    return False

async def _tixcraft_captcha_snapshot(tab):
    try:
        result = await tab.evaluate(r'''(() => {
            CAPTCHA_ID
            return {documentId:location.href+'|'+performance.timeOrigin,captcha:captchaId()};
        })()'''.replace('CAPTCHA_ID', CONST_TIXCRAFT_CAPTCHA_ID_JS))
        return result if isinstance(result, dict) and result.get('documentId') else None
    except Exception:
        return None


async def nodriver_tixcraft_get_ocr_answer(tab, ocr, ocr_captcha_image_source, Captcha_Browser, domain_name, config_dict=None):
    """讀圖等待可中斷；暫停後不再啟動備援驗證碼請求。"""
    if ocr is None or await _tixcraft_actions_paused(config_dict):
        return None
    img_base64 = None
    document_id = await _tixcraft_document_id(tab)
    if not document_id:
        return None
    if ocr_captcha_image_source == CONST_OCR_CAPTCH_IMAGE_SOURCE_CANVAS:
        for _ in range(30):
            if await _tixcraft_actions_paused(config_dict):
                return None
            try:
                result = await tab.evaluate(r'''(() => {
                    if(location.href+'|'+performance.timeOrigin!==EXPECTED)return null;
                    const img=document.querySelector('#TicketForm_verifyCode-image');
                    if(!img?.complete||!img.naturalWidth||!img.naturalHeight)return null;
                    const canvas=document.createElement('canvas');
                    canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;
                    canvas.getContext('2d').drawImage(img,0,0);return canvas.toDataURL();
                })()'''.replace('EXPECTED', json.dumps(document_id)))
                if isinstance(result, str) and ',' in result:
                    img_base64 = base64.b64decode(result.split(',', 1)[1])
                    break
            except Exception:
                break
            if await _tixcraft_sleep(0.1, config_dict):
                return None
    if img_base64 is None and Captcha_Browser is not None:
        if await _tixcraft_actions_paused(config_dict) or await _tixcraft_document_id(tab) != document_id:
            return None
        try:
            img_base64 = base64.b64decode(await asyncio.to_thread(Captcha_Browser.request_captcha))
        except Exception:
            return None
    if img_base64 is None or await _tixcraft_actions_paused(config_dict):
        return None
    try:
        return await asyncio.to_thread(ocr.classification, img_base64)
    except Exception:
        return None

async def nodriver_tixcraft_auto_ocr(tab, config_dict, ocr, away_from_keyboard_enable,
                                     previous_answer, Captcha_Browser,
                                     ocr_captcha_image_source, domain_name):
    """每輪讀圖、填寫及換圖之前確認本輪仍可執行。"""
    if await _tixcraft_actions_paused(config_dict) or await _tixcraft_submission_pending(tab, config_dict):
        return False, previous_answer, False
    debug = util.create_debug_logger(config_dict)
    if ocr is None or await tab.query_selector('#TicketForm_verifyCode') is None:
        return False, previous_answer, False
    before = await _tixcraft_captcha_snapshot(tab)
    answer = await nodriver_tixcraft_get_ocr_answer(tab, ocr, ocr_captcha_image_source,
                                                  Captcha_Browser, domain_name, config_dict=config_dict)
    if await _tixcraft_actions_paused(config_dict):
        return False, previous_answer, False
    after = await _tixcraft_captcha_snapshot(tab)
    if not before or not after or before != after:
        return True, previous_answer, False
    if answer is None:
        if away_from_keyboard_enable:
            await _tixcraft_sleep(0.1, config_dict)
            return True, previous_answer, False
        await nodriver_tixcraft_keyin_captcha_code(tab, config_dict=config_dict)
        return False, previous_answer, False
    answer = str(answer).strip()
    if len(answer) != 4:
        if not away_from_keyboard_enable:
            await nodriver_tixcraft_keyin_captcha_code(tab, config_dict=config_dict)
            return False, previous_answer, False
        if answer != previous_answer:
            await nodriver_tixcraft_reload_captcha(tab, domain_name, config_dict)
        return True, answer, False
    if away_from_keyboard_enable:
        hash1 = await nodriver_get_yii_captcha_hash(tab)
        if hash1 > 0 and not util.yii_captcha_verify(answer, hash1):
            candidates = util.yii_captcha_edit1(answer, hash1)
            if not candidates:
                await nodriver_tixcraft_reload_captcha(tab, domain_name, config_dict)
                return True, previous_answer, False
            answer = candidates[0]
    if await _tixcraft_actions_paused(config_dict):
        return False, previous_answer, False
    _, submitted = await nodriver_tixcraft_keyin_captcha_code(
        tab, answer=answer, auto_submit=away_from_keyboard_enable, config_dict=config_dict,
        captcha_snapshot=before)
    return False, answer, submitted

async def nodriver_tixcraft_has_captcha(tab):
    """True when this ticket page actually shows an image captcha.

    Some events use invisible reCAPTCHA and have no #TicketForm_verifyCode,
    which every captcha path here assumes exists. A detection failure answers
    True: guessing False on a captcha page would submit an empty answer.
    """
    try:
        result = await tab.evaluate('''
            (function() {
                return !!(document.querySelector('#TicketForm_verifyCode') ||
                          document.querySelector('#TicketForm_verifyCode-image'));
            })();
        ''')
        return result is not False
    except Exception:
        return True


def _forget_ticket_assigned(current_url):
    """Drop the "ticket count already set" marks nodriver_tixcraft_ticket_main
    keeps for this URL, so the next pass assigns the count again."""
    prefix = f"ticket_assigned_{current_url}_"
    for key in [k for k in _state if k.startswith(prefix)]:
        del _state[key]


async def nodriver_tixcraft_submit_without_captcha(tab, config_dict):
    """無圖片驗證碼仍遵守網站按鈕驗證、全表張數與持續送出鎖。"""
    if await _tixcraft_actions_paused(config_dict) or await _tixcraft_submission_pending(tab, config_dict):
        return False
    await nodriver_check_checkbox_enhanced(tab, '#TicketForm_agree', config_dict)
    if await _tixcraft_actions_paused(config_dict) or not config_dict.get('ocr_captcha', {}).get('force_submit', False):
        return False
    return await _tixcraft_submit_form(tab, config_dict)


async def nodriver_tixcraft_ticket_main_ocr(tab, config_dict, ocr, Captcha_Browser, domain_name):
    """本輪重試有上限，所有等待與送出都保留暫停／結束檢查。"""
    if await _tixcraft_actions_paused(config_dict) or await _tixcraft_submission_pending(tab, config_dict):
        return
    document_id = await _tixcraft_document_id(tab)
    if not document_id:
        return
    has_captcha = await nodriver_tixcraft_has_captcha(tab)
    if await _tixcraft_actions_paused(config_dict):
        return
    if not has_captcha:
        await nodriver_tixcraft_submit_without_captcha(tab, config_dict)
        return
    if not config_dict['ocr_captcha']['enable'] or not config_dict['ocr_captcha']['force_submit']:
        current = await _tixcraft_captcha_snapshot(tab)
        manual_key = (current['documentId'], current.get('captcha'),
                      bool(config_dict['ocr_captcha']['enable'])) if current else None
        if manual_key and _state.get('tixcraft_manual_captcha') == manual_key:
            return
    if not config_dict['ocr_captcha']['enable']:
        await nodriver_tixcraft_keyin_captcha_code(tab, config_dict=config_dict)
        return
    force_submit = config_dict['ocr_captcha']['force_submit']
    previous_answer = None
    for attempt in range(5):
        if await _tixcraft_actions_paused(config_dict) or await _tixcraft_document_id(tab) != document_id:
            return
        redo, previous_answer, submitted = await nodriver_tixcraft_auto_ocr(
            tab, config_dict, ocr, force_submit, previous_answer, Captcha_Browser,
            config_dict['ocr_captcha']['image_source'], domain_name)
        if submitted:
            _state['ocr_completed_document'] = document_id
            return
        if not force_submit or not redo:
            return
        if attempt == 4:
            util.create_debug_logger(config_dict).log('[TIXCRAFT] 本輪辨識未成功，請手動輸入驗證碼。')
            await nodriver_tixcraft_keyin_captcha_code(tab, config_dict=config_dict)
            return
        if attempt == 2 and await _tixcraft_sleep(2.5, config_dict):
            return

async def nodriver_ticketmaster_check_ip_block(tab, config_dict, current_url=""):
    """Detect PerimeterX EPS block page on tixcraft/ticketmaster domains.

    When blocked, waits 4-7 minutes (random) then navigates back to original URL.
    Returns True if blocked (caller should skip normal processing), False otherwise.
    """
    debug = util.create_debug_logger(config_dict)

    # Still within previous block wait period
    block_until = _state.get("ip_block_until", 0)
    if block_until > 0 and time.time() < block_until:
        remaining = int(block_until - time.time())
        debug.log(f"[EPS BLOCK] Still waiting for block to expire, {remaining}s remaining")
        await sleep_with_pause_check(tab, 5, config_dict)
        return True

    try:
        result_json = await tab.evaluate('''
            (function() {
                try {
                    if (typeof action !== "undefined" && action === "block" && typeof rr !== "undefined") {
                        return JSON.stringify({
                            blocked: true,
                            rr: rr || "",
                            client_ip: typeof client_ip !== "undefined" ? client_ip : ""
                        });
                    }
                } catch(e) {}
                return JSON.stringify({blocked: false, rr: "", client_ip: ""});
            })()
        ''')

        if not result_json:
            return False

        result = json.loads(result_json)
        if not result.get("blocked", False):
            return False

        original_url = result.get("rr", "")
        client_ip = result.get("client_ip", "unknown")
        scope_url = original_url or current_url
        wait_seconds, is_custom_delay = _resolve_soft_block_wait_seconds(config_dict, scope_url)

        if is_custom_delay:
            debug.log(
                f"[EPS BLOCK] IP blocked (IP: {client_ip}), using custom TixCraft soft-block delay: {wait_seconds}s"
            )
        else:
            debug.log(
                f"[EPS BLOCK] IP blocked (IP: {client_ip}), using default delay: {wait_seconds}s before retry"
            )
        _state["ip_block_until"] = time.time() + wait_seconds

        waited = 0
        while waited < wait_seconds:
            if await check_and_handle_pause(config_dict):
                return True
            chunk = min(10, wait_seconds - waited)
            await asyncio.sleep(chunk)
            waited += chunk
            remaining = wait_seconds - waited
            if remaining > 0 and waited % 60 == 0:
                debug.log(f"[EPS BLOCK] Waiting for block to expire, {remaining}s remaining")

        _state["ip_block_until"] = 0

        if original_url:
            debug.log(f"[EPS BLOCK] Block expired, navigating back to: {original_url}")
            try:
                await navigate_page_if_enabled(tab, original_url)
            except Exception:
                pass

        return True

    except Exception as exc:
        debug.log(f"[EPS BLOCK] Error checking block status: {exc}")
        return False


async def _tixcraft_checkout_handoff(tab, config_dict):
    """到結帳頁只交接，不操作付款方式、結帳表單或訂單取消。"""
    if not await _tixcraft_submission_handoff(tab):
        return False
    if _state.get('handoff_ready'):
        return True
    _state['handoff_ready'] = True
    message = '[TIXCRAFT] 已進入結帳頁，請手動確認資料與付款；尚未確認訂單或付款完成。'
    util.write_instance_status('waiting_manual', message)
    util.create_debug_logger(enabled=True).log(message)
    advanced = dict(config_dict.get('advanced', {}))
    notification_config = dict(config_dict, advanced=advanced)
    for key in ('discord_message', 'telegram_message'):
        custom = advanced.get(key, '').strip()
        advanced[key] = message + ('\n' + custom if custom else '')
    if advanced.get('play_sound', {}).get('order'):
        play_sound_while_ordering(config_dict)
    send_discord_notification(notification_config, 'order', 'TixCraft')
    send_telegram_notification(notification_config, 'order', 'TixCraft')
    return True


async def nodriver_tixcraft_main(tab, url, config_dict, ocr, Captcha_Browser):
    # 函數開始時檢查暫停
    if await _tixcraft_actions_paused(config_dict):
        return False

    if _state.get('handoff_ready'):
        return True
    if url.split('?', 1)[0].rstrip('/').endswith('/ticket/checkout'):
        return await _tixcraft_checkout_handoff(tab, config_dict)

    debug = util.create_debug_logger(config_dict)

    # Global alert handler - auto-dismiss all alerts (sold out, errors, etc.)
    # Handles alerts that appear after page navigation (e.g., area selection redirects)
    # Reference: KHAM platform implementation (Line 10681-10697)
    async def handle_global_alert(event):
        if not isinstance(event, cdp.page.JavascriptDialogOpening):
            return
        # Skip alert handling when bot is paused (let user handle manually)
        if _state.get('alert_handler_tab') is not tab or await _tixcraft_actions_paused(config_dict):
            return
        # IMPORTANT: Use tab.target.url (cached) instead of nodriver_current_url (js_dumps)
        # When alert dialog is open, JavaScript execution is blocked, causing js_dumps to hang
        current_url = tab.target.url if hasattr(tab, 'target') and tab.target else ""

        if _should_leave_dialog_to_user(current_url):
            debug.log(f"[GLOBAL ALERT] Alert on checkout page, NOT auto-dismissing: '{event.message}'")
            return

        debug.log(f"[GLOBAL ALERT] Alert detected: '{event.message}'")

        # Track captcha error alerts for retry logic
        is_captcha_error = False
        captcha_error_keywords = ['驗證碼錯誤', '驗證碼不正確', '驗證碼輸入錯誤',
                                  'incorrect verification code', 'invalid verification code',
                                  'wrong verification code', 'incorrect captcha', 'invalid captcha',
                                  'captcha is incorrect', 'verification code is incorrect']
        alert_message_lower = event.message.lower()
        for keyword in captcha_error_keywords:
            if keyword in alert_message_lower:
                is_captcha_error = True
                break

        if is_captcha_error:
            _state["captcha_alert_detected"] = True
            debug.log('[TIXCRAFT] 網站回報驗證碼錯誤，待確認新驗證碼後才允許重試。')

        # Issue #188: Detect sold out alerts to add cooldown delay
        sold_out_keywords = ['售完', '已售完', '選購一空', 'sold out', 'no tickets']
        is_sold_out_alert = any(kw in alert_message_lower for kw in sold_out_keywords)

        # Dismiss the alert - try multiple times with small delays
        dismiss_success = False
        for attempt in range(3):
            try:
                if await _tixcraft_actions_paused(config_dict):
                    return
                await tab.send(cdp.page.handle_java_script_dialog(accept=True))
                dismiss_success = True
                debug.log(f"[GLOBAL ALERT] Alert dismissed (attempt {attempt + 1})")
                break
            except Exception as dismiss_exc:
                error_msg = str(dismiss_exc)
                # CDP -32602 means no dialog is showing (already dismissed by another handler or user)
                if "No dialog is showing" in error_msg or "-32602" in error_msg:
                    dismiss_success = True  # Consider it handled
                    debug.log("[GLOBAL ALERT] Dialog already dismissed")
                    break  # No need to retry
                if attempt < 2:
                    await asyncio.sleep(0.1)  # Small delay before retry
                else:
                    debug.log(f"[GLOBAL ALERT] Failed to dismiss alert: {dismiss_exc}")

        if is_captcha_error and dismiss_success:
            await _tixcraft_mark_submission_rejected(tab)
        elif is_sold_out_alert and dismiss_success:
            await _tixcraft_mark_submission_failed(tab)

        # Issue #188: Set cooldown timestamp instead of async sleep (event handler doesn't block main loop)
        if is_sold_out_alert and dismiss_success:
            interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
            if interval > 0:
                cooldown_until = time.time() + interval
                _state["sold_out_cooldown_until"] = cooldown_until
                debug.log(f"[GLOBAL ALERT] Sold out detected, setting cooldown for {interval}s")

    if not _state.get('initialized'):
        defaults = {
            "fail_list": [],
            "fail_promo_list": [],
            "start_time": None,
            "done_time": None,
            "elapsed_time": None,
            "is_popup_checkout": False,
            "area_retry_count": 0,
            "played_sound_ticket": False,
            "played_sound_order": False,
            "alert_handler_registered": False,
            "captcha_alert_detected": False,
            "captcha_submit_until": 0,
            "ocr_completed_url": "",
            "last_homepage_redirect_time": 0,
            "sold_out_cooldown_until": 0,
            "printed_completed": False,
            "ticketmaster_phase": "area_select",
            "ticketmaster_captcha_processed_url": "",
            "ip_block_until": 0,
            "queue_it_enter_time": None,
        }
        for key, value in defaults.items():
            _state.setdefault(key, value)
        _state['initialized'] = True

    # Register global alert handler (remains active throughout session)
    # Only register once to prevent infinite loop
    if _state.get('alert_handler_tab') is not tab:
        try:
            tab.add_handler(cdp.page.JavascriptDialogOpening, handle_global_alert)
            _state["alert_handler_registered"] = True
            _state['alert_handler_tab'] = tab
            debug.log(f"[GLOBAL ALERT] Global alert handler registered")
        except Exception as handler_exc:
            debug.log(f"[GLOBAL ALERT] Failed to register alert handler: {handler_exc}")

    try:
        if await _tixcraft_http_main(tab, url, config_dict, ocr):
            return False  # True is reserved for an actual checkout handoff.
    except Exception as exc:
        _tixcraft_http_log(config_dict, '本輪讀取尚未完成（%s），保留送出保護；不因錯誤重送。' % type(exc).__name__)
        if urlsplit(url).path.startswith('/ticket/ticket/'):
            return False

    # Queue-it virtual waiting room detection (TixCraft + Ticketmaster SG)
    # Pattern: ported from platforms/ibon.py — URL-based, customerId-agnostic.
    # Both family platforms route high-traffic users to *.queue-it.net before EPS evaluates.
    was_in_queue = _state.get("queue_it_enter_time") is not None
    should_pause, elapsed = _process_queue_it_state(url, _state, time.time())
    if should_pause:
        if not was_in_queue:
            debug.log("[TIXCRAFT] Queue-IT entered, waiting...")
        return False
    if elapsed is not None:
        debug.log(f"[TIXCRAFT] Queue-IT passed (waited {elapsed:.1f}s)")

    await nodriver_tixcraft_home_close_window(tab)

    # EPS block detection for TixCraft family and Ticketmaster domains (Issue #289)
    if _is_tixcraft_soft_block_scope(url) or 'ticketmaster' in url:
        if await nodriver_ticketmaster_check_ip_block(tab, config_dict, current_url=url):
            return False

    # special case for same event re-open, redirect to user's homepage.
    # Add cooldown to prevent infinite redirect loop when area page is unavailable
    # Match homepage URLs: tixcraft.com, tixcraft.com/, tixcraft.com/activity
    is_tixcraft_home = url in ['https://tixcraft.com', 'https://tixcraft.com/', 'https://tixcraft.com/activity']
    if is_tixcraft_home:
        homepage = config_dict["homepage"]
        # Only redirect if homepage is not the platform root itself (avoid infinite loop)
        homepage_is_root = homepage.rstrip('/') in ['https://tixcraft.com', 'https://tixcraft.com/activity']
        if not homepage_is_root:
            current_time = time.time()
            last_redirect_time = _state.get("last_homepage_redirect_time", 0)
            # Use auto_reload_page_interval from settings, default to 3 seconds
            redirect_interval = config_dict["advanced"].get("auto_reload_page_interval", 3)
            if redirect_interval <= 0:
                redirect_interval = 3  # Minimum 3 seconds to prevent rapid loop

            if current_time - last_redirect_time > redirect_interval:
                try:
                    _state["last_homepage_redirect_time"] = current_time
                    await navigate_page_if_enabled(tab, homepage)
                except Exception:
                    pass

    # Ticketmaster embeds the date list (#gameList) directly in the detail page
    # and has no /activity/game/ URL at all, so the TixCraft detail->game
    # redirect can never fire there. The Ticketmaster date handler below claims
    # the detail page instead (issue #378).
    if "/activity/detail/" in url and 'ticketmaster' not in url:
        _state["start_time"] = time.time()
        is_redirected = await nodriver_tixcraft_redirect(tab, url)

    is_date_selected = False
    # Check if this is a Ticketmaster page before using TixCraft logic
    if "/activity/game/" in url and 'ticketmaster' not in url:
        _state["start_time"] = time.time()
        if config_dict["date_auto_select"]["enable"]:
            domain_name = url.split('/')[2]
            is_date_selected = await nodriver_tixcraft_date_auto_select(tab, url, config_dict, domain_name)

    # T010: Ticketmaster date selection integration (User Story 1)
    # Support both URL formats:
    # - /artist/{artist_id} (artist listing page)
    # - /activity/game/{event_id} (event date listing page from /activity/detail redirect)
    is_ticketmaster_date_page = (
        'ticketmaster' in url and
        (('/artist/' in url and len(url.split('/'))==6) or
         ('/activity/game/' in url) or
         ('/activity/detail/' in url))
    )

    if is_ticketmaster_date_page:
        _state["start_time"] = time.time()
        if config_dict["date_auto_select"]["enable"]:
            debug.log("[TICKETMASTER] Detected Ticketmaster date page, calling date auto select")
            domain_name = url.split('/')[2]
            # Call Ticketmaster date auto select
            is_date_selected = await nodriver_ticketmaster_date_auto_select(tab, config_dict)
            if debug.enabled:
                if is_date_selected:
                    debug.log("[TICKETMASTER] Date selection completed")
                else:
                    debug.log("[TICKETMASTER] Date selection failed or no match")

    # choose area
    if '/ticket/area/' in url:
        domain_name = url.split('/')[2]
        if config_dict["area_auto_select"]["enable"]:
            if not 'ticketmaster' in domain_name:
                # for tixcraft
                await nodriver_tixcraft_area_auto_select(tab, url, config_dict)

                _state["area_retry_count"]+=1
                #print("count:", _state["area_retry_count"])
                if _state["area_retry_count"] >= (60 * 15):
                    # Cool-down
                    _state["area_retry_count"] = 0
                    await asyncio.sleep(5)
            else:
                # T013+T017: Ticketmaster area + ticket number (phase-based state machine)
                # Ticketmaster uses AJAX on same URL: area selection -> ticketPriceList loads -> set ticket number
                # Phase: area_select -> wait_ticket -> done (reset when URL leaves /ticket/area/)
                phase = _state.get("ticketmaster_phase", "area_select")

                if phase == "area_select":
                    zone_info = await nodriver_ticketmaster_parse_zone_info(tab, config_dict)
                    if zone_info:
                        await nodriver_ticketmaster_area_auto_select(tab, config_dict, zone_info)
                        _state["ticketmaster_phase"] = "wait_ticket"
                        _state["area_retry_count"] = 0
                    else:
                        _state["area_retry_count"] += 1

                elif phase == "wait_ticket":
                    is_assigned = await nodriver_ticketmaster_assign_ticket_number(tab, config_dict)
                    if is_assigned:
                        _state["fail_promo_list"] = await nodriver_ticketmaster_promo(tab, config_dict, _state["fail_promo_list"])
                        _state["ticketmaster_phase"] = "done"
                        _state["area_retry_count"] = 0
                    else:
                        _state["area_retry_count"] += 1
                        if _state["area_retry_count"] >= 30:
                            # Either ticketPriceList never loaded, or the wanted
                            # ticket count is unavailable and allow_less_tickets
                            # is off. Re-selecting the area alone is not enough:
                            # with an empty area_keyword the area step matches
                            # everything and never reloads, so the phase machine
                            # would spin on a stale page forever and miss any
                            # returned ticket. Reload (throttled) like the
                            # TixCraft ticket page does (issue #378 / #174).
                            debug.log("[TICKETMASTER] Ticket assignment failed after 30 retries, reloading page")
                            _state["ticketmaster_phase"] = "area_select"
                            _state["area_retry_count"] = 0
                            try:
                                reload_interval = config_dict.get("advanced", {}).get("auto_reload_page_interval", 0)
                                if reload_interval > 0:
                                    await sleep_with_pause_check(tab, reload_interval, config_dict)
                                await reload_page_if_enabled(tab)
                            except Exception:
                                pass

                # phase == "done": no-op, wait for POST navigation to /ticket/ticket/
    else:
        _state["fail_promo_list"] = []
        _state["area_retry_count"] = 0
        _state["ticketmaster_phase"] = "area_select"

    # T020: Ticketmaster captcha integration (User Story 4)
    # https://ticketmaster.sg/ticket/check-captcha/23_blackpink/954/5/75
    if '/ticket/check-captcha/' in url:
        domain_name = url.split('/')[2]
        if 'ticketmaster' in domain_name:
            # Check if we already processed this captcha page (avoid repeated execution)
            ticketmaster_captcha_processed = _state.get("ticketmaster_captcha_processed_url", "")
            if ticketmaster_captcha_processed != url:
                # Call Ticketmaster captcha handler
                await nodriver_ticketmaster_captcha(tab, config_dict, ocr, Captcha_Browser)
                # Mark this URL as processed
                _state["ticketmaster_captcha_processed_url"] = url
    else:
        # Reset captcha processed state when leaving captcha page
        _state["ticketmaster_captcha_processed_url"] = ""

    if '/ticket/verify/' in url:
        # Tixcraft verify handler (already implemented)
        _state["fail_list"] = await nodriver_tixcraft_verify(tab, config_dict, _state["fail_list"])
    else:
        _state["fail_list"] = []

    # main app, to select ticket number.
    if '/ticket/ticket/' in url:
        domain_name = url.split('/')[2]
        await nodriver_tixcraft_ticket_main(tab, config_dict, ocr, Captcha_Browser, domain_name)
        _state["done_time"] = time.time()

        if not _state["played_sound_ticket"]:
            if config_dict["advanced"]["play_sound"]["ticket"]:
                play_sound_while_ordering(config_dict)
        _state["played_sound_ticket"] = True
    else:
        _state["played_sound_ticket"] = False

    if '/ticket/order' in url:
        _state["done_time"] = time.time()

    return False

