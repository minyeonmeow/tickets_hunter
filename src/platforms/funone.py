#!/usr/bin/env python3
#encoding=utf-8
"""platforms/funone.py -- FunOne Tickets platform (tickets.funone.io)."""

import asyncio
import base64
import json
import re
import time

try:
    import ddddocr
except Exception:
    ddddocr = None

import util
from nodriver_common import evaluate_refresh_if_enabled, sleep_before_auto_refresh
from nodriver_common import navigate_page_if_enabled, reload_page_if_enabled
from nodriver_common import (
    check_and_handle_pause,
    check_and_handle_quit,
    CONST_NATIVE_INPUT_SETTER_JS,
    create_ocr_for_platform,
    nodriver_current_url_safe,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
)


__all__ = [
    "nodriver_funone_inject_cookie",
    "nodriver_funone_check_login_status",
    "nodriver_funone_verify_login",
    "nodriver_funone_close_popup",
    "nodriver_funone_area_auto_select",
    "nodriver_funone_check_sold_out",
    "nodriver_funone_click_refresh_button",
    "nodriver_funone_assign_ticket_number",
    "nodriver_funone_captcha_handler",
    "nodriver_funone_reload_captcha",
    "nodriver_funone_ocr_captcha",
    "nodriver_funone_detect_step",
    "nodriver_funone_ticket_agree",
    "nodriver_funone_order_submit",
    "nodriver_funone_error_handler",
    "nodriver_funone_main",
]

# Log unresolved submission after this interval; never retry only because time elapsed.
CONST_FUNONE_SUBMIT_COOLDOWN = 5.0

_state = {}


def _is_past_ticket_selection(url):
    """Past ticket selection - leave form submission and payment to the user.

    Leave buyer details and payment to the user; never refresh these routes.
    """
    if not url:
        return False
    return any(marker in url for marker in (
        '/purchase_fill_form/',
        '/purchase_checkout',
        '/purchase_completed',
        '/purchase_waiting_jump/',
    ))


async def nodriver_funone_inject_cookie(tab, config_dict):
    """
    Inject FunOne session cookie using CDP network.set_cookie

    Args:
        tab: NoDriver tab
        config_dict: Configuration dictionary

    Returns:
        bool: True if injection successful
    """
    from zendriver import cdp

    debug = util.create_debug_logger(config_dict)
    funone_session_cookie = config_dict["accounts"].get("funone_session_cookie", "").strip()

    if len(funone_session_cookie) == 0:
        debug.log("[FUNONE] No session cookie configured")
        return False

    try:
        # Inject ticket_session cookie
        await tab.send(cdp.network.set_cookie(
            name="ticket_session",
            value=funone_session_cookie,
            domain="tickets.funone.io",
            path="/",
            secure=False,
            http_only=True
        ))

        debug.log("[FUNONE] Session cookie injected successfully")
        return True

    except Exception as exc:
        debug.log(f"[FUNONE] Cookie injection failed: {exc}")
        return False

async def nodriver_funone_check_login_status(tab):
    """
    Check if user is logged in by verifying ticket_session cookie exists

    Returns:
        bool: True if logged in (cookie exists and valid)
    """
    try:
        # Cookie verification - check if ticket_session cookie exists
        cookies = await tab.browser.cookies.get_all()
        session_cookie = next((c for c in cookies if c.name == 'ticket_session'), None)
        if session_cookie and len(session_cookie.value) > 10:
            return True
        return False

    except Exception as exc:
        return False

async def nodriver_funone_verify_login(tab, config_dict):
    """
    Verify login status (cookie is already injected in goto_homepage)

    Returns:
        bool: True if logged in
    """
    debug = util.create_debug_logger(config_dict)

    is_logged_in = await nodriver_funone_check_login_status(tab)

    # Only print when login status changes (reduce repetitive messages)
    if is_logged_in:
        if _state.get("last_login_status") != True:
            debug.log("[FUNONE] Login status verified - logged in")
            _state["last_login_status"] = True
        return True

    if _state.get("last_login_status") != False:
        debug.log("[FUNONE] Not logged in - waiting for manual OTP login")
        _state["last_login_status"] = False
    return False

async def nodriver_funone_close_popup(tab):
    """
    Close cookie consent, login modal, and announcement popups

    Returns:
        bool: True if any popup was closed
    """
    closed_any = False

    try:
        # Close common popups
        close_popup_js = '''
        (function() {
            let closed = 0;

            // Priority 0: Age confirmation modal (must click confirm, NOT close/return)
            const ageModal = document.querySelector('.activity_aged_18_limit_modal');
            if (ageModal) {
                const confirmBtn = ageModal.querySelector('.btn-primary');
                if (confirmBtn) {
                    confirmBtn.click();
                    return 1;
                }
            }

            // Priority 1: FunOne login modal (close button)
            const loginModalClose = document.querySelector('button.modal_close');
            if (loginModalClose) {
                const style = window.getComputedStyle(loginModalClose);
                if (style.display !== 'none' && style.visibility !== 'hidden') {
                    loginModalClose.click();
                    closed++;
                    return closed;
                }
            }

            // Priority 2: FunOne login modal (return button as fallback)
            const modalAction = document.querySelector('.modal_action');
            if (modalAction) {
                const buttons = modalAction.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = (btn.textContent || '').trim();
                    if (text.includes('返回') || text.toLowerCase().includes('back') || text.toLowerCase().includes('cancel')) {
                        btn.click();
                        closed++;
                        return closed;
                    }
                }
            }

            // Priority 3: Generic modal close buttons
            const closeIcons = document.querySelectorAll('[class*="close"], [aria-label="close"]');
            for (const icon of closeIcons) {
                const style = window.getComputedStyle(icon);
                if (style.display !== 'none' && style.visibility !== 'hidden') {
                    icon.click();
                    closed++;
                    return closed;
                }
            }

            // Priority 4: Cookie consent banner (FunOne specific)
            // Look for "同意" button in cookie context
            const allButtons = document.querySelectorAll('button');
            for (const btn of allButtons) {
                const text = (btn.textContent || '').trim();
                const style = window.getComputedStyle(btn);
                if ((text === '同意' || text.toLowerCase() === 'agree' || text.toLowerCase() === 'accept') &&
                    style.display !== 'none' && style.visibility !== 'hidden') {
                    // Verify it's in a cookie/consent context
                    const parent = btn.closest('div');
                    if (parent && (parent.textContent.includes('Cookie') || parent.textContent.includes('cookie'))) {
                        btn.click();
                        closed++;
                        return closed;
                    }
                }
            }

            // Priority 5: Generic cookie consent (fallback)
            const cookieButtons = document.querySelectorAll('button, a');
            for (const btn of cookieButtons) {
                const text = (btn.textContent || '').toLowerCase();
                if (text.includes('accept') || text.includes('got it') || text.includes('ok')) {
                    if (btn.closest('.cookie') || btn.closest('[class*="consent"]') || btn.closest('[class*="popup"]')) {
                        btn.click();
                        closed++;
                    }
                }
            }

            return closed;
        })()
        '''
        result = await tab.evaluate(close_popup_js)
        closed_any = result > 0

    except Exception as exc:
        pass

    return closed_any


async def _funone_actions_paused(config_dict=None):
    return await check_and_handle_pause(config_dict) or await check_and_handle_quit(config_dict)


async def nodriver_funone_area_auto_select(tab, url, config_dict):
    """
    Auto-select ticket type on ticket selection page

    Returns:
        bool: True if ticket type selected
    """
    if await _funone_actions_paused(config_dict):
        return False
    debug = util.create_debug_logger(config_dict)

    # Get area keyword from config
    area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "")
    auto_select_mode = config_dict.get("area_auto_select", {}).get("mode", "random")
    keyword_exclude = config_dict.get("keyword_exclude", "")
    area_auto_fallback = config_dict.get("area_auto_fallback", False)

    # Only print area selection config once
    area_config_key = f"{area_keyword}_{auto_select_mode}"
    if _state.get("last_area_config") != area_config_key:
        debug.log(f"[FUNONE] Area selection - keyword: '{area_keyword}', mode: {auto_select_mode}")
        _state["last_area_config"] = area_config_key

    try:
        # Get all ticket areas (FunOne uses div.zone_box elements)
        # IMPORTANT: Only look for .zone_box elements, do NOT fallback to generic buttons
        # Generic button search would incorrectly match ticket type names on quantity selection pages
        get_tickets_js = '''
        (function() {
            const tickets = [];

            // Only FunOne-specific zone_box elements (area selection page)
            const zoneBoxes = document.querySelectorAll('.zone_box');
            if (zoneBoxes.length > 0) {
                for (const box of zoneBoxes) {
                    // Use innerText for more reliable text extraction (rendered text)
                    const fullText = (box.innerText || box.textContent || '').trim().replace(/\\s+/g, ' ');
                    const isDisabled = box.classList.contains('disabled');

                    // Get zone name - try multiple methods
                    let name = '';
                    const zoneName = box.querySelector('.zone_name');
                    if (zoneName) {
                        name = (zoneName.innerText || zoneName.textContent || '').trim();
                    }
                    // Fallback: use first line of fullText
                    if (!name && fullText) {
                        name = fullText.split(/[\\n\\r]|\\u70ed|\\u5df2/)[0].trim();
                    }

                    tickets.push({
                        text: name,
                        fullText: fullText,
                        index: tickets.length,
                        disabled: isDisabled,
                        type: 'zone_box'
                    });
                }
            }

            // No fallback - if no zone_box found, return empty array
            // This page is likely a ticket quantity selection page, not area selection
            return tickets;
        })()
        '''
        tickets = await tab.evaluate(get_tickets_js)

        # Convert CDP format to Python dict if needed
        # NoDriver may return CDP format: {'type': 'object', 'value': [['key', {'type': 'string', 'value': 'val'}], ...]}
        def parse_cdp_value(item):
            if isinstance(item, dict) and 'type' in item and 'value' in item:
                if item['type'] == 'object' and isinstance(item['value'], list):
                    # Convert [['key', {'type': 'string', 'value': 'val'}], ...] to {'key': 'val', ...}
                    result = {}
                    for pair in item['value']:
                        if isinstance(pair, list) and len(pair) == 2:
                            key, val_obj = pair
                            if isinstance(val_obj, dict) and 'value' in val_obj:
                                result[key] = val_obj['value']
                            else:
                                result[key] = val_obj
                    return result
                else:
                    return item['value']
            return item

        if tickets:
            tickets = [parse_cdp_value(t) for t in tickets]

        if not tickets or len(tickets) == 0:
            # Only print "No ticket types found" if we previously found some
            if _state.get("last_area_count", -1) != 0:
                debug.log("[FUNONE] No ticket types found")
                _state["last_area_count"] = 0
            return False

        # Only print ticket count when it changes
        if _state.get("last_area_count") != len(tickets):
            debug.log(f"[FUNONE] Found {len(tickets)} ticket types")
            _state["last_area_count"] = len(tickets)

        # Filter out disabled (sold out) tickets first
        available_tickets = [t for t in tickets if not t.get('disabled', False)]
        if len(available_tickets) != len(tickets):
            sold_out_count = len(tickets) - len(available_tickets)
            debug.log(f"[FUNONE] {sold_out_count} ticket types sold out, {len(available_tickets)} available")

        if len(available_tickets) == 0:
            debug.log("[FUNONE] All ticket types are sold out")
            return False

        tickets = available_tickets

        # Apply exclude keywords first (using standard util function)
        if keyword_exclude and len(keyword_exclude) > 0:
            filtered_tickets = []
            for ticket in tickets:
                # Use text first, fallback to fullText for keyword matching
                ticket_text = ticket.get('text', '') or ticket.get('fullText', '')
                if ticket_text and util.reset_row_text_if_match_keyword_exclude(config_dict, ticket_text):
                    debug.log(f"[FUNONE] Excluding ticket '{ticket_text[:50]}'")
                else:
                    filtered_tickets.append(ticket)
            tickets = filtered_tickets

        if len(tickets) == 0:
            debug.log("[FUNONE] All tickets filtered out by exclude keywords")
            return False

        # Use the same keyword priority and AND matching as quantity selection.
        for ticket in tickets:
            ticket["text"] = ticket.get("text") or ticket.get("fullText", "")
        tickets = _funone_matching_rows(tickets, config_dict)
        if not tickets:
            return False
        target_index = util.get_target_index_by_mode(len(tickets), auto_select_mode)

        # Click the selected ticket type
        original_index = tickets[target_index].get('index', target_index)
        ticket_type = tickets[target_index].get('type', 'button')
        ticket_name = tickets[target_index].get('text', '') or tickets[target_index].get('fullText', '')

        debug.log(f"[FUNONE] Clicking area '{ticket_name}' (index: {original_index}, type: {ticket_type})")

        click_ticket_js = f"""
        (() => {{
            const box = document.querySelectorAll('.zone_box')[{int(original_index)}];
            if (!box || box.classList.contains('disabled') ||
                box.getClientRects().length === 0) return false;
            const label = box.querySelector('.zone_name') || box;
            const text = (label.innerText || label.textContent || '').trim();
            if (!text.includes({json.dumps(ticket_name)})) return false;
            box.click();
            return true;
        }})()
        """
        if await _funone_actions_paused(config_dict):
            return False
        clicked = await tab.evaluate(click_ticket_js)

        if clicked:
            debug.log(f"[FUNONE] Ticket type {target_index} clicked")

        return clicked

    except Exception as exc:
        debug.log(f"[FUNONE] Area selection error: {exc}")
        return False

async def nodriver_funone_check_sold_out(tab, config_dict):
    """
    Check if all tickets are sold out on purchase_choose_ticket_no_map page

    Returns:
        tuple: (is_sold_out: bool, remaining_count: int, ticket_info: list)
    """
    debug = util.create_debug_logger(config_dict)
    ticket_number = config_dict.get("ticket_number", 2)
    area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "")

    # Parse keywords
    keywords = []
    if area_keyword:
        clean_keyword = area_keyword.replace('"', '').replace("'", '')
        keywords = [k.strip() for k in clean_keyword.split(',') if k.strip()]

    try:
        check_sold_out_js = f'''
        (function() {{
            const keywords = {keywords};
            const ticketInfo = [];
            let totalRemaining = 0;
            let matchedRemaining = 0;
            let hasMatchedTicket = false;
            let allSoldOut = true;
            let hasPurchaseButton = false;
            let purchaseButtonDisabled = true;

            // Use TreeWalker to find text nodes containing remaining count
            // This avoids duplicate counting from nested elements
            const pattern = /(?:剩餘|remaining)\\s*(\\d+)/i;
            const processedRows = new Set();

            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                null,
                false
            );

            let node;
            while (node = walker.nextNode()) {{
                const text = node.textContent || '';
                const remainingMatch = text.match(pattern);

                if (remainingMatch) {{
                    const remaining = parseInt(remainingMatch[1]);

                    // Find the ticket row container to get context
                    let container = node.parentElement;
                    if (container) {{
                        container = container.closest('div[class*="ticket"], div[class*="row"], tr, li') || container.parentElement;
                    }}

                    // Use container reference to avoid duplicate processing
                    if (container && processedRows.has(container)) {{
                        continue;
                    }}
                    if (container) {{
                        processedRows.add(container);
                    }}

                    const rowText = container ? container.textContent : text;

                    // Check if this matches any keyword
                    let matched = keywords.length === 0;
                    for (const kw of keywords) {{
                        if (kw && rowText.toLowerCase().includes(kw.toLowerCase())) {{
                            matched = true;
                            break;
                        }}
                    }}

                    ticketInfo.push({{
                        remaining: remaining,
                        text: rowText.substring(0, 100),
                        matched: matched
                    }});

                    totalRemaining += remaining;
                    if (matched) {{
                        matchedRemaining += remaining;
                        hasMatchedTicket = true;
                    }}
                    if (remaining > 0) {{
                        allSoldOut = false;
                    }}
                }}
            }}

            // Check +/- buttons status
            // FUNONE uses SVG icons for +/- buttons, need to check btn-tertiary class
            const buttons = document.querySelectorAll('button');
            let allButtonsDisabled = true;

            for (const btn of buttons) {{
                const btnText = btn.textContent.trim();
                const classList = Array.from(btn.classList);

                // Check for +/- buttons (btn-tertiary, but not refresh button)
                if (classList.includes('btn-tertiary') && !classList.includes('round_info_refresh')) {{
                    // Check if this is a plus button by examining SVG path
                    const svg = btn.querySelector('svg');
                    if (svg) {{
                        const path = svg.querySelector('path');
                        if (path) {{
                            const d = path.getAttribute('d') || '';
                            // Plus button path starts with 'M11 13H' (has vertical line)
                            // Minus button path starts with 'M6 13C' (only horizontal line)
                            if (d.startsWith('M11')) {{
                                // This is a plus button
                                if (!btn.disabled) {{
                                    allButtonsDisabled = false;
                                }}
                            }}
                        }}
                    }}
                }}

                // Check for purchase/submit button (Chinese and English)
                if (btnText.includes('立即購買') || btnText.includes('Purchase') ||
                    btnText.includes('Confirm') || btnText.includes('submit')) {{
                    hasPurchaseButton = true;
                    purchaseButtonDisabled = btn.disabled;
                }}
            }}

            // If no remaining info found, try alternative detection via buttons
            if (ticketInfo.length === 0) {{
                const inputs = document.querySelectorAll('input[type="text"], input[type="number"]');
                for (const input of inputs) {{
                    const parent = input.parentElement;
                    if (parent) {{
                        const plusBtn = parent.querySelector('button:not(:disabled)');
                        if (plusBtn) {{
                            allSoldOut = false;
                        }}
                    }}
                }}
            }}

            // Determine if sold out
            const isSoldOut = (ticketInfo.length > 0 && allSoldOut) ||
                              (allButtonsDisabled && hasPurchaseButton && purchaseButtonDisabled);

            return {{
                isSoldOut: isSoldOut,
                totalRemaining: totalRemaining,
                matchedRemaining: hasMatchedTicket ? matchedRemaining : totalRemaining,
                hasMatchedTicket: hasMatchedTicket,
                ticketInfo: ticketInfo,
                allButtonsDisabled: allButtonsDisabled,
                purchaseButtonDisabled: purchaseButtonDisabled
            }};
        }})()
        '''
        result = await tab.evaluate(check_sold_out_js)
        result = util.parse_nodriver_result(result)

        if result and isinstance(result, dict):
            is_sold_out = result.get('isSoldOut', False)
            remaining = result.get('matchedRemaining', 0) if result.get('hasMatchedTicket') else result.get('totalRemaining', 0)
            ticket_info = result.get('ticketInfo', [])

            if debug.enabled:
                if is_sold_out:
                    debug.log("[FUNONE] All tickets sold out")

            return (is_sold_out, remaining, ticket_info)

        return (False, 0, [])

    except Exception as exc:
        debug.log(f"[FUNONE] Check sold out error: {exc}")
        return (False, 0, [])

async def nodriver_funone_click_refresh_button(tab, config_dict):
    """Refresh stock using only an identified ticket-refresh control."""
    if not util.is_auto_refresh_enabled():
        return False

    try:
        if _is_past_ticket_selection(await nodriver_current_url_safe(tab)):
            return False
        script = r"""
        (() => {
            const visible = e => e.getClientRects().length > 0 &&
                getComputedStyle(e).visibility !== 'hidden';
            const candidates = document.querySelectorAll(
                'button.round_info_refresh, button, [role="button"]');
            for (const btn of candidates) {
                const text = (btn.textContent || '').trim();
                const refresh = btn.matches('.round_info_refresh') ||
                    /即時票務|更新票況|重新整理|刷新|instant.*update|refresh.*ticket|ticket.*refresh/i.test(text);
                if (refresh && visible(btn) && !btn.disabled &&
                    btn.getAttribute('aria-disabled') !== 'true') {
                    btn.click();
                    return true;
                }
            }
            return false;
        })()
        """
        return bool(util.parse_nodriver_result(await evaluate_refresh_if_enabled(tab, script)))
    except Exception as exc:
        util.create_debug_logger(config_dict).log(f"[FUNONE] Stock refresh failed: {exc}")
        return False


async def _funone_quantity_rows(tab):
    """Read quantity controls without confusing numeric form fields for tickets."""
    script = r"""
    (() => {
        const result = [];
        const visible = e => e.getClientRects().length > 0 &&
            getComputedStyle(e).visibility !== 'hidden';
        for (const input of document.querySelectorAll('input')) {
            if (!visible(input) || !/^\d+$/.test(input.value)) continue;
            const parent = input.parentElement;
            if (!parent) continue;
            const children = Array.from(parent.children);
            const pos = children.indexOf(input);
            const plus = children.slice(pos + 1).find(e => e.tagName === 'BUTTON');
            const minus = children.slice(0, pos).reverse().find(e => e.tagName === 'BUTTON');
            if (!plus || !minus) continue;
            let row = parent;
            for (let depth = 0; row && depth < 5; depth++, row = row.parentElement) {
                const text = (row.innerText || row.textContent || '').trim();
                // A parent containing several quantity boxes is not a ticket row.
                if (row.querySelectorAll('input').length > 1) break;
                if (/TWD|NT\$|\d[\d,]*\s*元/i.test(text)) {
                    result.push({index: Array.from(document.querySelectorAll('input')).indexOf(input),
                        text, value: Number(input.value), canAdd: !plus.disabled,
                        canRemove: !minus.disabled});
                    break;
                }
            }
        }
        return result;
    })()
    """
    return util.parse_nodriver_result(await tab.evaluate(script)) or []


def _funone_matching_rows(rows, config_dict):
    """Apply keyword priority, exclusions and explicit fallback consistently."""
    rows = [r for r in rows if not util.reset_row_text_if_match_keyword_exclude(
        config_dict, r["text"])]
    keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "")
    if keyword:
        keywords = util.parse_keyword_string_to_array(keyword)
        if not keywords:
            keywords = [k.strip() for k in keyword.split(";") if k.strip()]
        for group in keywords:
            matched = [r for r in rows if all(
                word.casefold() in r["text"].casefold() for word in group.split())]
            if matched:
                return matched
        if not config_dict.get("area_auto_fallback", False):
            return []
    return rows


async def nodriver_funone_assign_ticket_number(tab, config_dict):
    """Change one ticket row, then verify actual quantities on a later read."""
    if await _funone_actions_paused(config_dict):
        return False
    try:
        if _is_past_ticket_selection(await nodriver_current_url_safe(tab)):
            return False
        seat_status = util.parse_nodriver_result(await tab.evaluate(r"""
        (() => {
            const seats = Array.from(document.querySelectorAll('.seat'));
            return {hasMap: seats.some(e => e.getClientRects().length > 0),
                selected: seats.filter(e => e.classList.contains('selected')).length};
        })()
        """))
        if isinstance(seat_status, dict) and seat_status.get("hasMap"):
            ready = seat_status.get("selected") == config_dict.get("ticket_number", 2)
            _state["qty_fail_reason"] = "" if ready else "waiting_manual_seats"
            return ready
        rows = await _funone_quantity_rows(tab)
        target_qty = config_dict.get("ticket_number", 2)
        if not isinstance(target_qty, int) or target_qty < 1:
            return False
        eligible = [r for r in rows if r["canAdd"] or r["value"] >= target_qty]
        matches = _funone_matching_rows(eligible, config_dict)
        if not matches:
            _state["qty_fail_reason"] = "no_match"
            return False
        available = [r for r in matches if r["canAdd"] or r["value"] >= target_qty]
        if not available:
            _state["qty_fail_reason"] = "sold_out"
            return False
        # Keep the selected row stable across loop iterations, including random mode.
        chosen = next((r for r in available if r["value"] > 0), None)
        if chosen is None:
            mode = config_dict.get("area_auto_select", {}).get("mode", "random")
            chosen = available[util.get_target_index_by_mode(len(available), mode)]
        if any(r["value"] > 0 and r["index"] != chosen["index"] for r in rows):
            _state["qty_fail_reason"] = "other_ticket_selected"
            return False
        if chosen["value"] == target_qty:
            _state["qty_fail_reason"] = ""
            return True
        action = "add" if chosen["value"] < target_qty else "remove"
        if not chosen["canAdd" if action == "add" else "canRemove"]:
            _state["qty_fail_reason"] = "insufficient_stock"
            return False
        # One click per iteration gives Vue and stock-limit checks time to update.
        script = r"""
        ((index, action) => {
            const input = document.querySelectorAll('input')[index];
            if (!input || !input.parentElement) return false;
            const children = Array.from(input.parentElement.children);
            const pos = children.indexOf(input);
            const buttons = action === 'add' ? children.slice(pos + 1) :
                children.slice(0, pos).reverse();
            const button = buttons.find(e => e.tagName === 'BUTTON');
            if (!button || button.disabled) return false;
            button.click();
            return true;
        })(INDEX, ACTION)
        """
        if await _funone_actions_paused(config_dict):
            return False
        await tab.evaluate(script.replace("INDEX", json.dumps(chosen["index"])).replace(
            "ACTION", json.dumps(action)))
        _state["qty_fail_reason"] = "updating"
        return False
    except Exception as exc:
        _state["qty_fail_reason"] = "error"
        util.create_debug_logger(config_dict).log(f"[FUNONE] Quantity check failed: {exc}")
        return False


async def _funone_read_captcha(tab):
    """Distinguish missing/loading CAPTCHA from a fully rendered input."""
    script = r"""
    (() => {
        const visible = e => e && e.getClientRects().length > 0 &&
            getComputedStyle(e).visibility !== 'hidden';
        const image = document.querySelector('img[alt="vCode"], img.captcha_img, img[src*="captcha"]');
        const inputs = Array.from(document.querySelectorAll('input'));
        const input = inputs.find(e => visible(e) &&
            (/captcha|驗證/i.test([e.name, e.id, e.placeholder].join(' ')) ||
                e.closest('.captcha_form')));
        const src = image ? image.getAttribute('src') || '' : '';
        const value = input ? input.value.trim() : '';
        const maximum = input && input.maxLength > 0 ? input.maxLength : 6;
        const minimum = input && input.minLength > 0 ? input.minLength :
            (maximum < 6 ? maximum : 4);
        const errors = Array.from(document.querySelectorAll(
            '.captcha_form .input_error.show, .captcha_form .error, [role="alert"]'));
        return {hasCaptcha: !!(image || input), image: src,
            ready: !!(input && (!image || (visible(image) && image.complete &&
                image.naturalWidth > 0))),
            value, valid: !!(input && value.length >= minimum &&
                value.length <= maximum && /^[a-z0-9]+$/i.test(value) && input.checkValidity()),
            invalid: !!(input && input.getAttribute('aria-invalid') === 'true') ||
                errors.some(e => visible(e) &&
                    /驗證碼.*(錯|失敗)|captcha.*(incorrect|invalid|wrong)|incorrect.*captcha/i.test(e.textContent)),
            index: input ? inputs.indexOf(input) : -1, minimum, maximum};
    })()
    """
    result = util.parse_nodriver_result(await tab.evaluate(script))
    return result if isinstance(result, dict) else None


async def nodriver_funone_captcha_handler(tab, config_dict, require_captcha=False):
    """Wait for rendering; retry per image without locking out later CAPTCHA loads."""
    if await _funone_actions_paused(config_dict):
        return False
    debug = util.create_debug_logger(config_dict)
    try:
        info = await _funone_read_captcha(tab)
        if info is None:
            return False
        if not info.get("hasCaptcha"):
            return not require_captcha
        if not info.get("ready"):
            return False
        image = info.get("image", "")
        if await _funone_actions_paused(config_dict):
            return False
        signature = (image, info.get("value", ""))
        last_filled = _state.get("captcha_filled_signature", (None, None))
        if not info.get("invalid"):
            _state["captcha_error_handled"] = False
        if info.get("invalid") and not _state.get("captcha_error_handled") and (
                signature == last_filled or
                (not info.get("value") and image == last_filled[0])):
            if _state.get("ocr_retry_count", 0) >= 5:
                _state["ocr_exhausted_image"] = image
                return False
            if image != _state.get("captcha_reload_from"):
                if await nodriver_funone_reload_captcha(tab):
                    _state["captcha_reload_from"] = image
                    _state["captcha_error_handled"] = True
            return False
        if info.get("value"):
            if info.get("valid"):
                _state["captcha_filled_signature"] = signature
                return True
            # Let a person finish typing; do not overwrite partial manual input.
            return False
        if image == _state.get("captcha_reload_from"):
            return False
        if not config_dict.get("ocr_captcha", {}).get("enable", False):
            return False
        if not image.startswith("data:image"):
            return False
        if _state.get("ocr_exhausted_image") == image:
            return False
        if _state.get("ocr_exhausted_image") and _state["ocr_exhausted_image"] != image:
            _state["ocr_retry_count"] = 0
            _state.pop("ocr_exhausted_image", None)
        if _state.get("ocr_retry_count", 0) >= 5:
            _state["ocr_exhausted_image"] = image
            return False
        if time.monotonic() < _state.get("next_ocr_time", 0):
            return False
        _state["next_ocr_time"] = time.monotonic() + 0.5
        _state["ocr_retry_count"] = _state.get("ocr_retry_count", 0) + 1
        if await nodriver_funone_ocr_captcha(tab, config_dict, image):
            if _state["ocr_retry_count"] >= 5:
                _state["ocr_exhausted_image"] = image
            return True
        if await _funone_actions_paused(config_dict):
            return False
        if _state["ocr_retry_count"] >= 5:
            _state["ocr_exhausted_image"] = image
            debug.log("[FUNONE] OCR retries exhausted; enter CAPTCHA or refresh it manually.")
        else:
            if await nodriver_funone_reload_captcha(tab):
                _state["captcha_reload_from"] = image
        return False
    except Exception as exc:
        debug.log(f"[FUNONE] CAPTCHA processing failed: {exc}")
        return False


async def nodriver_funone_reload_captcha(tab):
    """Click the captcha reload button to get a new image"""
    if await _funone_actions_paused(None):
        return False
    try:
        reload_js = '''
        (function() {
            // FunOne reload button: button.captcha_reload_btn or button next to img[alt="vCode"]
            let btn = document.querySelector('button.captcha_reload_btn');
            if (!btn) {
                const img = document.querySelector('img[alt="vCode"]');
                if (img) {
                    const sibling = img.nextElementSibling;
                    if (sibling && sibling.tagName === 'BUTTON') btn = sibling;
                    if (!btn && img.parentElement) {
                        btn = img.parentElement.querySelector('button');
                    }
                }
            }
            if (btn) {
                btn.click();
                return true;
            }
            return false;
        })()
        '''
        result = await tab.evaluate(reload_js)
        result = util.parse_nodriver_result(result)
        return bool(result)
    except Exception:
        return False


async def nodriver_funone_ocr_captcha(tab, config_dict, base64_data):
    """Honor model settings and verify that the same image is still displayed."""
    if await _funone_actions_paused(config_dict):
        return False
    debug = util.create_debug_logger(config_dict)
    try:
        config_key = json.dumps(config_dict.get("ocr_captcha", {}), sort_keys=True)
        if _state.get("ocr_config_key") != config_key:
            ocr = await asyncio.to_thread(create_ocr_for_platform, config_dict)
            if ocr is None:
                if ddddocr is None:
                    return False
                ocr = await asyncio.to_thread(ddddocr.DdddOcr, show_ad=False, beta=False)
                ocr.set_ranges(6)
            _state["ocr_instance"] = ocr
            _state["ocr_config_key"] = config_key
        if await _funone_actions_paused(config_dict):
            return False
        image_bytes = base64.b64decode(base64_data.split(",", 1)[-1])
        answer = await asyncio.to_thread(_state["ocr_instance"].classification, image_bytes)
        if await _funone_actions_paused(config_dict):
            return False
        answer = re.sub(r"[^A-Za-z0-9]", "", answer or "")
        info = await _funone_read_captcha(tab)
        if not info or not info.get("ready") or info.get("image") != base64_data:
            return False
        if info.get("value"):
            return bool(info.get("valid"))
        if not info["minimum"] <= len(answer) <= info["maximum"]:
            return False
        script = r"""
        ((index, answer, image) => {
            const img = document.querySelector('img[alt="vCode"], img.captcha_img, img[src*="captcha"]');
            const input = document.querySelectorAll('input')[index];
            if (!img || img.getAttribute('src') !== image || !input || input.value) return false;
            NATIVE_SETTER
            return setNativeInputValue(input, answer);
        })(INDEX, ANSWER, IMAGE)
        """
        script = script.replace("NATIVE_SETTER", CONST_NATIVE_INPUT_SETTER_JS)
        script = script.replace("INDEX", json.dumps(info["index"])).replace(
            "ANSWER", json.dumps(answer)).replace("IMAGE", json.dumps(base64_data))
        if await _funone_actions_paused(config_dict):
            return False
        if not await tab.evaluate(script):
            return False
        verified = await _funone_read_captcha(tab)
        if not verified or verified.get("image") != base64_data or not verified.get("valid"):
            return False
        _state["captcha_filled_signature"] = (base64_data, answer)
        debug.log("[FUNONE] CAPTCHA filled; awaiting website validation.")
        return True
    except Exception as exc:
        debug.log(f"[FUNONE] OCR failed: {exc}")
        return False


async def nodriver_funone_detect_step(tab):
    """
    Detect which step of the ticket flow we're on.

    Returns:
        int: Step number (1=Area, 2=Quantity, 3=Form, 4=Payment, 5=Complete, 0=Unknown)
    """
    try:
        detect_step_js = '''
        (function() {
            const url = window.location.href;
            const bodyText = document.body.textContent || '';

            const explicitStep = new URL(url).searchParams.get('step');
            if (url.includes('purchase_choose_ticket') && /^[12]$/.test(explicitStep || '')) {
                return Number(explicitStep);
            }
            // Priority 1: Check URL patterns (most reliable)
            if (url.includes('purchase_choose_ticket_no_map') || url.includes('purchase_choose_ticket')) {
                // Ticket type/quantity selection page
                return 1;
            }
            if (url.includes('purchase_fill_form')) {
                // Form filling page
                return 3;
            }
            if (url.includes('purchase_checkout')) {
                // Payment/checkout page
                return 4;
            }

            // Priority 2: Check URL for step parameter
            const stepMatch = url.match(/[?&]step=(\d+)/);
            if (stepMatch) {
                return parseInt(stepMatch[1]);
            }

            // Priority 3: Check for specific DOM patterns
            // Step 1: Area/Ticket selection
            const zoneBoxes = document.querySelectorAll('.zone_box');
            if (zoneBoxes.length > 0) {
                return 1;
            }

            // Step 1/2: Ticket type selection - has +/- buttons for quantity
            // Look for ticket price/type layout with quantity controls
            const plusBtns = document.querySelectorAll('button');
            let hasPlusMinusBtn = false;
            for (const btn of plusBtns) {
                const text = btn.textContent.trim();
                if (text === '+' || text === '-') {
                    hasPlusMinusBtn = true;
                    break;
                }
            }
            // Check for ticket selection page (has textbox for quantity input)
            const qtyInputs = document.querySelectorAll('input[type="text"][value="0"]');
            if (hasPlusMinusBtn && qtyInputs.length > 0) {
                // This is ticket selection page, not form filling
                return 1;
            }
            if (hasPlusMinusBtn && (bodyText.includes('張數') || bodyText.includes('票種'))) {
                return 2;
            }

            // Step 3: Form filling - must have actual form inputs (not just quantity inputs)
            const formInputs = document.querySelectorAll('input[type="text"]:not([value="0"]), input[type="email"], input[type="tel"]');
            if (formInputs.length >= 3 && bodyText.includes('填寫')) {
                return 3;
            }

            // Step 4: Payment page - must have specific payment elements
            const hasPaymentForm = document.querySelector('input[name*="credit"], input[name*="card"], select[name*="payment"]');
            if (hasPaymentForm || (bodyText.includes('付款方式') && bodyText.includes('信用卡'))) {
                return 4;
            }

            // Step 5: Complete
            if (bodyText.includes('購票完成') || bodyText.includes('訂單成功')) {
                return 5;
            }

            return 0; // Unknown
        })()
        '''
        step = await tab.evaluate(detect_step_js)
        step = util.parse_nodriver_result(step)
        return int(step) if step else 0

    except Exception as exc:
        return 0

async def nodriver_funone_ticket_agree(tab):
    """
    Check agreement checkboxes

    Returns:
        bool: True if agreements checked
    """
    if await _funone_actions_paused(None):
        return False
    try:
        check_agree_js = '''
        (function() {
            let checked = 0;

            // FunOne-specific: custom div.checkbox elements
            // Structure: <div class="checkbox_block active"> <div class="checkbox"><svg>...</svg></div> </div>
            // The parent checkbox_block gets 'active' class when checked
            const customCheckboxes = document.querySelectorAll('.checkbox_block .checkbox, div.checkbox');
            for (const cb of customCheckboxes) {
                // Check if not already checked
                // FunOne: parent element gets 'active' class when checked
                const parent = cb.parentElement;
                const parentActive = parent && parent.classList.contains('active');
                const selfChecked = cb.classList.contains('checked') || cb.classList.contains('active');

                if (!parentActive && !selfChecked) {
                    cb.click();
                    checked++;
                }
            }

            if (checked > 0) {
                return checked;
            }

            // Standard input checkboxes
            const checkboxes = document.querySelectorAll('input[type="checkbox"]');

            for (const cb of checkboxes) {
                if (!cb.checked) {
                    // Try multiple ways to find the associated text
                    const label = cb.closest('label') || document.querySelector(`label[for="${cb.id}"]`);
                    let text = label ? label.textContent : '';

                    // If no label text, check parent/sibling elements
                    if (!text && cb.parentElement) {
                        text = cb.parentElement.textContent || '';
                    }

                    // FunOne specific: check for nearby text about terms/agreement
                    const nearbyText = cb.closest('div')?.textContent || '';

                    // Check if it's an agreement checkbox
                    if (text.includes('同意') || text.includes('agree') || text.includes('條款') ||
                        text.includes('terms') || text.includes('規則') || text.includes('閱讀') ||
                        nearbyText.includes('同意') || nearbyText.includes('服務條款')) {
                        cb.click(); // Use click instead of just setting checked for better event handling
                        checked++;
                    }
                }
            }

            // If no checkbox found with text, just click any unchecked checkbox
            if (checked === 0) {
                for (const cb of checkboxes) {
                    if (!cb.checked) {
                        cb.click();
                        checked++;
                        break; // Only click first unchecked checkbox
                    }
                }
            }

            return checked;
        })()
        '''
        result = await tab.evaluate(check_agree_js)
        return result >= 0

    except Exception as exc:
        return False

async def _funone_submission_pending(tab, url, config_dict):
    """Require a navigation, stage change or explicit failure before another submit."""
    if not _state.get("submission_pending"):
        return False
    if url and url != _state.get("order_submitted_url"):
        _state["submission_pending"] = False
        return False
    try:
        step = await nodriver_funone_detect_step(tab)
        if step and step != _state.get("order_submitted_step"):
            _state["submission_pending"] = False
            return False
        failure = util.parse_nodriver_result(await tab.evaluate(r"""
        (() => {
            const items = document.querySelectorAll(
                '[role="alert"], .modal, .modal_container, .notice.show, .error');
            for (const e of items) {
                if (!e.getClientRects().length || getComputedStyle(e).visibility === 'hidden') continue;
                if (/驗證碼.*(錯|失敗)|captcha.*(wrong|invalid|incorrect)/i.test(e.textContent)) return 'captcha';
                if (/票券不足|已售完|已售罄|數量不足|購買失敗|座位.*(失敗|已被)|sold out|insufficient/i.test(e.textContent)) return 'stock';
            }
            return false;
        })()
        """))
        if failure:
            _state["submission_pending"] = False
            if failure == "captcha":
                info = await _funone_read_captcha(tab)
                if info and info.get("image"):
                    _state["captcha_reload_from"] = info["image"]
                    await nodriver_funone_reload_captcha(tab)
            await nodriver_funone_close_popup(tab)
            return False
    except Exception:
        # A missing response is not evidence that a click failed.
        return True
    if not _state.get("submit_wait_logged") and time.monotonic() - _state.get(
            "order_submitted_time", 0) >= CONST_FUNONE_SUBMIT_COOLDOWN:
        util.create_debug_logger(config_dict).log(
            "[FUNONE] Waiting for website response; automatic resubmission is blocked.")
        _state["submit_wait_logged"] = True
    return True


async def nodriver_funone_order_submit(tab, config_dict, selection_verified=False):
    """Submit a verified selection once; do not treat a click as order success."""
    if await _funone_actions_paused(config_dict):
        return False
    delay = util.scheduled_start_delay(config_dict.get("refresh_datetime", ""))
    if delay > 0:
        if not _state.get("scheduled_wait_logged"):
            util.create_debug_logger(config_dict, enabled=True).log(
                "[FUNONE] 預選已準備，等待指定開始時間及網站購買按鈕開放。")
            _state["scheduled_wait_logged"] = True
        return False
    debug = util.create_debug_logger(config_dict)
    current_url = await nodriver_current_url_safe(tab)
    if not current_url or _is_past_ticket_selection(current_url):
        return False
    if await _funone_submission_pending(tab, current_url, config_dict):
        return False
    if not selection_verified:
        if not await nodriver_funone_assign_ticket_number(tab, config_dict):
            return False
        if not await nodriver_funone_captcha_handler(tab, config_dict):
            return False
    if not config_dict.get("ocr_captcha", {}).get("force_submit", True):
        return False
    script = r"""
    (() => {
        for (const btn of document.querySelectorAll('button, input[type="submit"]')) {
            const text = (btn.textContent || btn.value || '').trim();
            if (!/^(立即購買|確認購買|購買|下一步|確認|送出|Purchase|Buy\s+now|Confirm|Submit|Next)$/i.test(text)) continue;
            const style = getComputedStyle(btn);
            if (btn.getClientRects().length && style.visibility !== 'hidden' &&
                style.display !== 'none' && !btn.disabled &&
                btn.getAttribute('aria-disabled') !== 'true') {
                btn.click();
                return {clicked:true};
            }
        }
        return {clicked:false};
    })()
    """
    if await _funone_actions_paused(config_dict):
        return False
    # Stamp before CDP: a timeout after a real click must not trigger another click.
    _state.update(submission_pending=True, order_submitted_url=current_url,
                  order_submitted_time=time.monotonic(),
                  order_submitted_step=await nodriver_funone_detect_step(tab),
                  submit_wait_logged=False)
    try:
        result = util.parse_nodriver_result(await tab.evaluate(script))
        if isinstance(result, dict) and result.get("clicked") is False:
            _state["submission_pending"] = False
            return False
        if isinstance(result, dict) and result.get("clicked"):
            debug.log("[FUNONE] Purchase clicked; waiting for website confirmation.")
            return True
    except Exception as exc:
        debug.log(f"[FUNONE] Submit result uncertain; waiting for website: {exc}")
    return False


async def nodriver_funone_error_handler(tab, error, config_dict):
    """
    Handle various error types

    Returns:
        bool: True if error was handled
    """
    if not util.is_auto_refresh_enabled():
        return False

    debug = util.create_debug_logger(config_dict)

    error_str = str(error).lower()

    if 'timeout' in error_str:
        debug.log("[FUNONE] Timeout error, reloading page...")
        try:
            await reload_page_if_enabled(tab)
            return True
        except:
            pass

    if 'network' in error_str or 'connection' in error_str:
        debug.log("[FUNONE] Network error, waiting and retrying...")
        await tab.sleep(2)
        try:
            await reload_page_if_enabled(tab)
            return True
        except:
            pass

    return False

async def _funone_document_state(tab):
    """Read browser-owned document identity; both F5 and reload create a new one."""
    try:
        result = util.parse_nodriver_result(await tab.evaluate(r"""
        (() => ({documentId: performance.timeOrigin, url: location.href,
            ready: document.readyState !== 'loading'}))()
        """))
        if (isinstance(result, dict) and
                isinstance(result.get("documentId"), (int, float)) and
                result["documentId"] > 0 and isinstance(result.get("url"), str)):
            return result
    except Exception:
        pass  # Navigation temporarily destroys the JS execution context.
    return None


def _funone_reset_page_state(url, document_id=None):
    """Reset per-page retry/notification state without discarding the OCR model."""
    same_document = document_id is None or document_id == _state.get("flow_document_id")
    if _state.get("flow_url") == url and same_document:
        return
    previous = _state.get("flow_url")
    _state["flow_url"] = url
    if document_id is not None:
        _state["flow_document_id"] = document_id
    for key in ("captcha_reload_from", "captcha_filled_signature", "ocr_exhausted_image",
                "handoff_notified", "captcha_ready_logged", "next_ocr_time",
                "captcha_error_handled", "scheduled_wait_logged"):
        _state.pop(key, None)
    _state.update(ocr_retry_count=0, qty_fail_reason="")
    if previous is not None:
        _state["submission_pending"] = False


async def _funone_refresh_stock(tab, config_dict):
    """Recheck the live route after waiting, before any refresh or reload."""
    if not util.is_auto_refresh_enabled():
        return False
    if util.scheduled_start_delay(config_dict.get("refresh_datetime", "")) > 0:
        return False

    document_id = _state.get("flow_document_id")
    interval = config_dict.get("advanced", {}).get("auto_reload_page_interval", 5)
    try:
        interval = max(0.5, float(interval))
    except (ValueError, TypeError):
        interval = 5
    if not await sleep_before_auto_refresh(interval) or await _funone_actions_paused(config_dict):
        return
    live_url = await nodriver_current_url_safe(tab)
    if not live_url or _is_past_ticket_selection(live_url) or _state.get("submission_pending"):
        return
    document = await _funone_document_state(tab)
    if (not document or not document.get("ready") or document["url"] != live_url or
            (document_id is not None and document["documentId"] != document_id)):
        # A manual reload may have just revealed tickets; let the main loop
        # inspect this document instead of overwriting it with another reload.
        return
    if not await nodriver_funone_click_refresh_button(tab, config_dict):
        # Navigation can happen during the refresh-button lookup too.
        live_url = await nodriver_current_url_safe(tab)
        latest = await _funone_document_state(tab)
        if (live_url and not _is_past_ticket_selection(live_url) and latest and
                latest.get("ready") and latest["url"] == live_url and
                latest["documentId"] == document["documentId"]):
            await reload_page_if_enabled(tab)


async def _funone_handle_ticket_flow(tab, url, config_dict):
    if await _funone_submission_pending(tab, url, config_dict):
        return
    step = await nodriver_funone_detect_step(tab)
    _state["last_step"] = step
    if step not in (1, 2):
        return
    qty_ready = await nodriver_funone_assign_ticket_number(tab, config_dict)
    if not qty_ready:
        if (_state.get("qty_fail_reason") == "no_match" and step == 1 and
                config_dict.get("area_auto_select", {}).get("enable", True)):
            if await nodriver_funone_area_auto_select(tab, url, config_dict):
                return
        if _state.get("qty_fail_reason") in ("sold_out", "insufficient_stock"):
            await _funone_refresh_stock(tab, config_dict)
        return
    if not await nodriver_funone_ticket_agree(tab):
        return
    captcha_ready = await nodriver_funone_captcha_handler(tab, config_dict)
    if captcha_ready:
        # Quantity and CAPTCHA were verified in this iteration; avoid duplicate CDP reads.
        await nodriver_funone_order_submit(tab, config_dict, selection_verified=True)


async def _funone_handle_form(tab, config_dict):
    """Only fill CAPTCHA on the data page; leave order creation/payment to the user."""
    if await _funone_actions_paused(config_dict):
        return
    ready = await nodriver_funone_captcha_handler(tab, config_dict, require_captcha=True)
    info = await _funone_read_captcha(tab)
    if not info or not info.get("ready") or await _funone_actions_paused(config_dict):
        return
    debug = util.create_debug_logger(config_dict)
    if ready and not _state.get("captcha_ready_logged"):
        debug.log("[FUNONE] CAPTCHA filled. Review buyer details and submit manually.")
        _state["captcha_ready_logged"] = True
    elif not ready:
        _state["captcha_ready_logged"] = False
    if _state.get("handoff_notified"):
        return
    message = "[FunOne] 已進入購票資料確認頁，請檢查資料與驗證碼並手動送出；訂單尚未確認成立。"
    notification_config = dict(config_dict)
    advanced = dict(config_dict.get("advanced", {}))
    for key in ("discord_message", "telegram_message"):
        custom = advanced.get(key, "").strip()
        advanced[key] = message + ("\n" + custom if custom else "")
    notification_config["advanced"] = advanced
    if advanced.get("play_sound", {}).get("order"):
        play_sound_while_ordering(config_dict)
    send_discord_notification(notification_config, "order", "FunOne")
    send_telegram_notification(notification_config, "order", "FunOne")
    debug.log("[FUNONE] Data confirmation page reached; order/payment require manual completion.")
    _state["handoff_notified"] = True


async def nodriver_funone_main(tab, url, config_dict):
    """Route FunOne stages, keeping selected tickets and final checkout separate."""
    if await check_and_handle_pause(config_dict):
        return tab
    document = await _funone_document_state(tab)
    if not document or not document.get("ready") or document["url"] != url:
        return tab
    _funone_reset_page_state(url, document["documentId"])
    if "/activity/activity_detail/" in url:
        return tab
    if "/purchase_fill_form/" in url:
        await _funone_handle_form(tab, config_dict)
        return tab
    if "/purchase_checkout" in url or "/purchase_completed" in url:
        return tab
    if "/purchase_waiting_jump/" in url:
        return tab
    if _state.get("submission_pending"):
        if await _funone_submission_pending(tab, url, config_dict):
            return tab
    await nodriver_funone_close_popup(tab)
    if not await nodriver_funone_verify_login(tab, config_dict):
        return tab
    if "/purchase_choose_ticket" in url:
        await _funone_handle_ticket_flow(tab, url, config_dict)
    elif url.rstrip("/") == "https://tickets.funone.io":
        homepage = config_dict.get("homepage", url)
        if homepage.rstrip("/") != url.rstrip("/"):
            now = time.monotonic()
            if now - _state.get("last_homepage_redirect_time", 0) > 3:
                _state["last_homepage_redirect_time"] = now
                await navigate_page_if_enabled(tab, homepage)
    return tab

