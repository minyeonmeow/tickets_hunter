#!/usr/bin/env python3
#encoding=utf-8
"""platforms/ticketplus.py -- TicketPlus platform (ticketplus.com.tw)."""

import asyncio
import json
import random
import time
import traceback
from urllib.parse import urlsplit

from zendriver import cdp

import util
from nodriver_common import click_refresh_if_enabled
from nodriver_common import navigate_page_if_enabled, reload_page_if_enabled, sleep_before_auto_refresh
from nodriver_common import (
    check_and_handle_pause,
    check_and_handle_quit,
    evaluate_with_pause_check,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    CONST_NATIVE_INPUT_SETTER_JS,
)


__all__ = [
    "nodriver_ticketplus_detect_layout_style",
    "nodriver_ticketplus_account_sign_in",
    "nodriver_ticketplus_is_signin",
    "nodriver_ticketplus_account_auto_fill",
    "nodriver_ticketplus_date_auto_select",
    "nodriver_ticketplus_unified_select",
    "nodriver_ticketplus_click_next_button_unified",
    "nodriver_ticketplus_ticket_agree",
    "nodriver_ticketplus_accept_realname_card",
    "nodriver_ticketplus_accept_other_activity",
    "nodriver_ticketplus_accept_order_fail",
    "nodriver_ticketplus_check_queue_status",
    "nodriver_ticketplus_confirm",
    "nodriver_ticketplus_order",
    "nodriver_ticketplus_wait_for_vue_ready",
    "nodriver_ticketplus_check_next_button",
    "nodriver_ticketplus_order_exclusive_code",
    "nodriver_ticketplus_main",
]

_state = {}


def _ticketplus_runtime_guards():
    return {key: _state[key] for key in
            ('submit_pending', 'seat_next_pending', 'quantity_pending', 'selected_ticket_row')
            if key in _state}


def _ticketplus_reset_runtime_state(pending=None):
    """Reset a run while retaining uncertain actions on the same document."""
    _state.clear()
    _state.update(fail_list=[], is_popup_confirm=False, is_ticket_assigned=False,
                  start_time=None, done_time=None, elapsed_time=None,
                  signin_form_filled=False, purchase_completed=False)
    if pending:
        _state.update({key: pending[key] for key in
                      ('submit_pending', 'seat_next_pending', 'quantity_pending', 'selected_ticket_row')
                      if key in pending})

# After the next button is clicked the site does one of three things: navigate to
# the confirmation page, open a failure dialog, or enter its in-page queue. Poll
# for all three instead of sleeping blind for a fixed window (#389).
CONST_TICKETPLUS_SUBMIT_WAIT_MAX = 10.0
CONST_TICKETPLUS_SUBMIT_POLL_INTERVAL = 0.3

# URL checks read cached browser state; full DOM scans use separate cadences.
CONST_TICKETPLUS_QUEUE_URL_POLL_INTERVAL = 0.05
CONST_TICKETPLUS_QUEUE_FAILURE_POLL_INTERVAL = 0.5

# Absolute fuse on queue monitoring. The queue keywords include wording as
# generic as "processing" and "please wait", so any future dialog carrying one
# can strand the loop the same way the failure popup did (#389). While stranded
# the main loop never runs. Stop and pause are now checked inside this
# monitor as well; keep the time limit as a final fallback.
CONST_TICKETPLUS_QUEUE_MONITOR_MAX = 600.0

# Order-failure wording. Shared by the dismissal scan and the queue check so the
# two can never disagree about the same dialog: they used to carry separate
# lists, and a popup matched by one but not the other was dismissed as a failure
# while simultaneously being counted as a queue.
#
# Deliberately excludes bare "system busy" wording: queue pages say the same
# thing ("system busy, please wait"), and a false positive reloads the page and
# throws away the queue position. The eight below have no queue reading.
CONST_TICKETPLUS_FAILURE_KEYWORDS = [
    "購票失敗",
    "您選擇的票種已售完",
    "已售完",
    "別人搶先一步",
    "已無可配座位",
    "本活動有限制購票總張數",
    "已被購買",
    "無法購票",
]

# Wording that marks the in-page queue.
CONST_TICKETPLUS_QUEUE_KEYWORDS = [
    "排隊購票中",
    "請稍候",
    "請別離開頁面",
    "請勿離開",
    "請勿關閉網頁",
    "同時使用多個裝置",
    "視窗購票",
    "正在處理",
    "處理中",
]

# Button labels that dismiss a failure dialog.
CONST_TICKETPLUS_DISMISS_BUTTON_TEXTS = [
    "我知道了",
    "知道了",
    "確定",
    "OK",
    "Ok",
]

# How long to keep re-checking a next button that exists but is still disabled.
# Vuetify unlocks it in the same tick the ticket count is written, so this only
# has to cover mount jitter; the caller has its own budget for what happens
# after submit, and a longer wait here just delays the next main-loop pass.
CONST_TICKETPLUS_NEXT_BUTTON_WAIT = 2.0
CONST_TICKETPLUS_NEXT_BUTTON_POLL = 0.05

# Locate and click the next button. Every selector here must be valid CSS:
# document.querySelector throws a DOMException on anything else, and zendriver
# turns that into a ProtocolException that aborts the whole script -- which is
# why the label match walks the buttons instead of using jQuery's :contains().
CONST_TICKETPLUS_NEXT_BUTTON_JS = '''
    (function() {
        const selectors = [
            'button.nextBtn:not(.disabledBtn):not(.v-btn--disabled)',
            '.order-footer button.nextBtn:not(.disabledBtn)',
            '.order-footer .v-btn--has-bg:not(.v-btn--disabled):not(.disabledBtn)',
            '.nextBtn:not([disabled])'
        ];
        const labels = ['下一步', 'Next'];

        const isEnabled = (el) => !!el && !el.disabled &&
            !el.classList.contains('v-btn--disabled') &&
            !el.classList.contains('disabledBtn');

        let foundDisabled = false;

        for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (!el) continue;
            if (isEnabled(el)) {
                el.click();
                return { clicked: true, buttonText: (el.textContent || '').trim() };
            }
            foundDisabled = true;
        }

        for (const button of document.querySelectorAll('button')) {
            const text = (button.textContent || '').trim();
            for (const label of labels) {
                if (text.indexOf(label) === -1) continue;
                if (isEnabled(button)) {
                    button.click();
                    return { clicked: true, buttonText: text };
                }
                foundDisabled = true;
            }
        }

        return { clicked: false, foundDisabled: foundDisabled };
    })();
'''


# Session rows on the activity page. Shared by the collect pass and the click
# pass of the date selector so both agree on what index N refers to; the two
# passes must never drift apart or the allowed-index list would point at the
# wrong rows.
# TicketPlus 2026-05 revamp: a Vue sub-component wrapper div now sits between
# #buyTicket and .sesstion-item, hence the descendant combinator and the
# div.row.pa-4 fallback for the older layout.
_TICKETPLUS_SESSION_CONTAINERS_JS = '''
    let sessionContainers = Array.from(document.querySelectorAll('div#buyTicket div.sesstion-item'))
        .filter(c => c.querySelector('button.nextBtn'));

    if (sessionContainers.length === 0) {
        sessionContainers = Array.from(document.querySelectorAll('div#buyTicket div.row.pa-4'))
            .filter(c => c.querySelector('button.nextBtn'));
    }
'''


def _ticketplus_filter_excluded(config_dict, items, text_of):
    """Drop items whose text matches keyword_exclude.

    Keeps keyword matching and fallback on one clean candidate list, so an
    excluded session can never be auto-picked (same invariant as cityline's
    _cityline_collect_available_areas). Exclusion is decided in Python via
    util so TicketPlus matches every other platform's semantics -- full-width
    space normalization and space-separated AND logic included.
    """
    return [
        item for item in items
        if not util.reset_row_text_if_match_keyword_exclude(config_dict, text_of(item))
    ]


def _get_status():
    """Return current ticketplus status for main loop (Approach B)."""
    return {
        # Reaching a confirmation page is not evidence of a completed order.
        "purchase_completed": False,
        "is_ticket_assigned": _state.get("is_ticket_assigned", False),
        "handoff_ready": _state.get("handoff_ready", False),
        "stage": _state.get("stage", "selecting"),
    }


def _ticketplus_path_segment_count(url):
    """Path segment count with the trailing slash normalized away.

    The site 302-redirects activity URLs to a trailing-slash form (#308),
    which would inflate split('/') by one and break page-type routing.
    """
    return len(url.rstrip('/').split('/'))


async def nodriver_ticketplus_detect_layout_style(tab, config_dict=None):
    """Detect TicketPlus page layout style.

    Returns:
        dict: {
            'style': int,      # 0: unknown, 1: style_1 (expansion), 2: style_2 (simple), 3: style_3 (new Vue.js)
            'found': bool,     # whether next button found
            'button_enabled': bool  # whether button is enabled
        }
    """
    try:
        result = await evaluate_with_pause_check(tab, '''
            (function() {
                console.log("=== Layout Detection Started ===");

                // Check for row layout ticket structure (Page3 feature)
                const rowTickets = document.querySelectorAll('.row.py-1.py-md-4.rwd-margin.no-gutters.text-title');
                const expansionPanels = document.querySelectorAll('.v-expansion-panels .v-expansion-panel');

                console.log("Row ticket element count:", rowTickets.length);
                console.log("Expansion Panel element count:", expansionPanels.length);

                // If row tickets exist and no expansion panels, prioritize style 3 (Page3)
                if (rowTickets.length > 0 && expansionPanels.length === 0) {
                    const style3Button = document.querySelector("div.order-footer > div.container > div.row > div.col-sm-3.col-4 > button.nextBtn") ||
                                       document.querySelector("button.nextBtn");
                    if (style3Button) {
                        console.log("Confirmed as Page3 (Style 3) - Row layout");
                        return {
                            style: 3,
                            found: true,
                            button_enabled: style3Button.disabled === false,
                            button_class: style3Button.className,
                            debug_info: "Page3 row layout detected"
                        };
                    }
                }

                // style_3: new Vue.js layout (general check)
                const style3Button = document.querySelector("div.order-footer > div.container > div.row > div.col-sm-3.col-4 > button.nextBtn");
                if (style3Button) {
                    console.log("Found Style 3 button");
                    return {
                        style: 3,
                        found: true,
                        button_enabled: style3Button.disabled === false,
                        button_class: style3Button.className,
                        debug_info: "Standard style 3 button"
                    };
                }

                // style_2: new layout (simple)
                const style2Button = document.querySelector("div.order-footer > div.container > div.row > div > button.nextBtn");
                if (style2Button) {
                    console.log("Found Style 2 button");
                    return {
                        style: 2,
                        found: true,
                        button_enabled: style2Button.disabled === false,
                        button_class: style2Button.className,
                        debug_info: "Standard style 2 button"
                    };
                }

                // style_1: old layout (expansion) - only when expansion panels exist
                if (expansionPanels.length > 0) {
                    const style1Button = document.querySelector("div.order-footer > div.container > div.row > div > div.row > div > button.nextBtn");
                    if (style1Button) {
                        console.log("Found Style 1 button (expansion panel type)");
                        return {
                            style: 1,
                            found: true,
                            button_enabled: style1Button.disabled === false,
                            button_class: style1Button.className,
                            debug_info: "Expansion panel layout"
                        };
                    }
                }

                // Generic button search (fallback)
                const anyButton = document.querySelector("button.nextBtn");
                if (anyButton) {
                    console.log("Found generic nextBtn button, determining style based on content structure");
                    if (rowTickets.length > 0) {
                        return {
                            style: 3,
                            found: true,
                            button_enabled: anyButton.disabled === false,
                            button_class: anyButton.className,
                            debug_info: "Generic button + row structure = style 3"
                        };
                    }
                    if (expansionPanels.length > 0) {
                        return {
                            style: 1,
                            found: true,
                            button_enabled: anyButton.disabled === false,
                            button_class: anyButton.className,
                            debug_info: "Generic button + expansion panels = style 1"
                        };
                    }
                }

                console.log("Unable to detect layout style");
                return {
                    style: 0,
                    found: false,
                    button_enabled: false,
                    button_class: "",
                    debug_info: "No layout detected"
                };
            })();
        ''')

        if result is None:
            return {'style': 0, 'found': False, 'button_enabled': False, 'paused': True}

        result = util.parse_nodriver_result(result)

        return result if isinstance(result, dict) else {
            'style': 0, 'found': False, 'button_enabled': False
        }

    except Exception as exc:
        return {'style': 0, 'found': False, 'button_enabled': False, 'error': str(exc)}


async def nodriver_ticketplus_account_sign_in(tab, config_dict):
    debug = util.create_debug_logger(config_dict)
    debug.log("[TICKETPLUS SIGNIN] nodriver_ticketplus_account_sign_in")
    is_filled_form = False
    is_submited = False

    ticketplus_account = config_dict["accounts"]["ticketplus_account"]
    ticketplus_password = config_dict["accounts"]["ticketplus_password"].strip()

    # manually keyin verify code.
    country_code = ""
    try:
        my_css_selector = 'input[placeholder="\u5340\u78bc"]'
        el_country = await tab.query_selector(my_css_selector)
        if el_country:
            country_code = await el_country.apply('function (element) { return element.value; } ')
            debug.log(f"[TICKETPLUS SIGNIN] country_code: {country_code}")
    except Exception as exc:
        debug.log(f"[TICKETPLUS SIGNIN] country code error: {exc}")

    is_account_assigned = False
    try:
        my_css_selector = 'input[placeholder="\u624b\u6a5f\u865f\u78bc *"]'
        el_account = await tab.query_selector(my_css_selector)
        if el_account:
            await el_account.click()
            await el_account.apply('function (element) {element.value = ""; } ')
            await el_account.send_keys(ticketplus_account);
            is_account_assigned = True
    except Exception as exc:
        debug.log(f"[TICKETPLUS SIGNIN] account input error: {exc}")

    if is_account_assigned:
        try:
            my_css_selector = 'input[type="password"]'
            el_password = await tab.query_selector(my_css_selector)
            if el_password:
                debug.log("[TICKETPLUS SIGNIN] Entering password...")
                await el_password.click()
                await el_password.apply('function (element) {element.value = ""; } ')
                await el_password.send_keys(ticketplus_password);
                await asyncio.sleep(random.uniform(0.1, 0.3))
                is_filled_form = True

                if country_code=="+886":
                    # only this case to auto sumbmit.
                    debug.log("[TICKETPLUS SIGNIN] press enter")
                    await tab.send(cdp.input_.dispatch_key_event("keyDown", code="Enter", key="Enter", text="\r", windows_virtual_key_code=13))
                    await tab.send(cdp.input_.dispatch_key_event("keyUp", code="Enter", key="Enter", text="\r", windows_virtual_key_code=13))
                    await asyncio.sleep(random.uniform(0.8, 1.2))
                    # PS: ticketplus country field may not located at your target country.
                    is_submited = True
        except Exception as exc:
            debug.log(f"[TICKETPLUS SIGNIN] password input error: {exc}")
            pass

    return is_filled_form, is_submited


async def nodriver_ticketplus_is_signin(tab):
    is_user_signin = False
    try:
        cookies  = await tab.browser.cookies.get_all()
        for cookie in cookies:
            if cookie.name=='user':
                if '%22account%22:%22' in cookie.value:
                    is_user_signin = True
        cookies = None
    except Exception as exc:
        pass

    return is_user_signin


async def nodriver_ticketplus_account_auto_fill(tab, config_dict):
    # auto fill account info.
    debug = util.create_debug_logger(config_dict)
    is_user_signin = False
    if len(config_dict["accounts"]["ticketplus_account"]) > 0:
        is_user_signin = await nodriver_ticketplus_is_signin(tab)
        if not is_user_signin:
            await asyncio.sleep(0.1)
            if not _state.get("signin_form_filled", False):
                is_sign_in_btn_pressed = False
                try:
                    # full screen mode.
                    my_css_selector = 'button.v-btn > span.v-btn__content > i.mdi-account'
                    sign_in_btn = await tab.query_selector(my_css_selector)
                    if sign_in_btn:
                        await sign_in_btn.click()
                        is_sign_in_btn_pressed = True
                        await asyncio.sleep(0.2)
                except Exception as exc:
                    debug.log(f"[TICKETPLUS AUTOFILL] sign-in button click error: {exc}")
                    pass

                if not is_sign_in_btn_pressed:
                    action_btns = None
                    try:
                        my_css_selector = 'div.px-4.py-3.drawerItem.cursor-pointer'
                        action_btns = await tab.query_selector_all(my_css_selector)
                    except Exception as exc:
                        debug.log(f"[TICKETPLUS AUTOFILL] drawer items query error: {exc}")
                        pass
                    if action_btns:
                        debug.log(f"[TICKETPLUS AUTOFILL] action buttons len: {len(action_btns)}")
                        if len(action_btns) >= 4:
                            try:
                                await action_btns[3].click()
                            except Exception as exc:
                                debug.log(f"[TICKETPLUS AUTOFILL] action button click error: {exc}")
                                pass

                is_filled_form, is_submited = await nodriver_ticketplus_account_sign_in(tab, config_dict)
                if is_filled_form:
                    _state["signin_form_filled"] = True

    return is_user_signin


async def _ticketplus_click_refresh_button(tab, debug):
    """Click float-btn refresh button for partial DOM update; return True if clicked."""
    if not util.is_auto_refresh_enabled():
        return False

    try:
        btn = await tab.query_selector('button.float-btn')
        if btn:
            await click_refresh_if_enabled(btn)
            await asyncio.sleep(0.3)
            debug.log("[REFRESH] Clicked update button (partial refresh)")
            return True
    except Exception:
        pass
    return False


async def nodriver_ticketplus_date_auto_select(tab, config_dict):
    """TicketPlus date auto selection."""
    debug = util.create_debug_logger(config_dict)

    auto_select_mode = config_dict["date_auto_select"]["mode"]
    date_keyword = config_dict["date_auto_select"]["date_keyword"].strip()
    date_auto_fallback = config_dict.get('date_auto_fallback', False)
    pass_date_is_sold_out_enable = config_dict["tixcraft"]["pass_date_is_sold_out"]
    auto_reload_coming_soon_page_enable = (config_dict["tixcraft"]["auto_reload_coming_soon_page"]) and util.is_auto_refresh_enabled()

    debug.log("date_auto_select_mode:", auto_select_mode)
    debug.log("date_keyword:", date_keyword)

    area_list = None
    try:
        # TicketPlus 2026-05 revamp: a Vue sub-component wrapper div now sits between
        # #buyTicket and .sesstion-item, so a direct-child combinator matches nothing.
        # Use a descendant combinator (still matches the old direct-child layout too).
        # The session list renders ~1-2s after load; judging it empty earlier
        # reloads the page before it ever renders (blank-page reload loop).
        for _ in range(15):
            area_list = await tab.query_selector_all('div#buyTicket div.sesstion-item')
            if area_list:
                break
            await tab.sleep(0.2)
        if not area_list:
            debug.log("date list still empty after 3s wait")
    except Exception as exc:
        debug.log("find #buyTicket fail:", exc)

    find_ticket_text_list = ['>\u7acb\u5373\u8cfc', '\u5c1a\u672a\u958b\u8ce3']
    sold_out_text_list = ['\u92b7\u552e\u4e00\u7a7a']

    formated_area_list = None
    is_vue_ready = True

    if area_list and len(area_list) > 0:
        debug.log("date_list_count:", len(area_list))

        formated_area_list = []
        for row in area_list:
            row_text = ""
            row_html = ""
            try:
                row_html = await row.get_html()
                row_text = util.remove_html_tags(row_html)
            except Exception as exc:
                debug.log("Date item processing failed:", exc)
                break

            if len(row_text) > 0:
                if util.reset_row_text_if_match_keyword_exclude(config_dict, row_text):
                    row_text = ""

            if len(row_text) > 0:
                if '<div class="v-progress-circular__info"></div>' in row_html:
                    is_vue_ready = False
                    break

            if len(row_text) > 0:
                row_is_enabled = False
                for text_item in find_ticket_text_list:
                    if text_item in row_html:
                        row_is_enabled = True
                        break

                if row_is_enabled and pass_date_is_sold_out_enable:
                    for sold_out_item in sold_out_text_list:
                        if sold_out_item in row_text:
                            row_is_enabled = False
                            debug.log(f"match sold out text: {sold_out_item}, skip this row.")
                            break

                if row_is_enabled:
                    formated_area_list.append(row)

        debug.log("formated_area_list count:", len(formated_area_list))
    else:
        debug.log("date date-time-position is None or empty")

    is_date_clicked = False
    if is_vue_ready and formated_area_list and len(formated_area_list) > 0:
        try:
            original_keyword = config_dict["date_auto_select"]["date_keyword"].strip()

            # Primary: read sessionId from Vue data layer, navigate directly
            # Avoids clicking loading-state placeholder containers
            vue_data = await tab.evaluate('''
                (function() {
                    const el = document.querySelector('.eventClass');
                    if (!el || !el.__vue__) return { ready: false };
                    const sessions = el.__vue__.$data.sessions || [];
                    const loaded = sessions.filter(function(s) { return s.loadingStatusFinished; });
                    return {
                        ready: loaded.length > 0,
                        sessions: loaded.map(function(s) {
                            return { sessionId: s.sessionId, date: s.date || '', name: s.name || '' };
                        })
                    };
                })();
            ''')

            if isinstance(vue_data, dict) and vue_data.get('ready') and vue_data.get('sessions'):
                # Exclude before matching so the sessions[0] fallback below can
                # never land on an excluded session.
                sessions = _ticketplus_filter_excluded(
                    config_dict,
                    vue_data['sessions'],
                    lambda s: (s.get('date', '') + ' ' + s.get('name', '')),
                )
                excluded_count = len(vue_data['sessions']) - len(sessions)
                if excluded_count:
                    debug.log(f"[TicketPlus DATE] Excluded {excluded_count} session(s) by keyword_exclude")

                target_session = None
                try:
                    kw_array = json.loads("[" + original_keyword + "]")
                except Exception:
                    kw_array = [original_keyword.strip('"').strip("'").strip()]

                for s in sessions:
                    session_text = (s.get('date', '') + ' ' + s.get('name', '')).lower()
                    for kw_item in kw_array:
                        sub_kws = [k.strip() for k in kw_item.split(' ') if k.strip()]
                        if all(k.lower() in session_text for k in sub_kws):
                            target_session = s
                            break
                    if target_session:
                        break

                if not target_session and date_auto_fallback and sessions:
                    debug.log("[TicketPlus DATE FALLBACK] Vue data fallback: using first loaded session")
                    target_session = sessions[0]

                if target_session:
                    session_id = target_session['sessionId']
                    # Known honeypot sessionId: TicketPlus API returns fake data to detected bots
                    KNOWN_FAKE_SESSION_IDS = ['c18900a1d5f295218fe60b982d7ece96']
                    if session_id in KNOWN_FAKE_SESSION_IDS:
                        debug.log(f"[TicketPlus DATE] WARNING: API returned known fake session data (anti-bot honeypot detected). sessionId={session_id}")
                        debug.log("[TicketPlus DATE] Bot may be flagged by TicketPlus. Skipping navigation to avoid invalid order page.")
                    else:
                        current_url = tab.url if hasattr(tab, 'url') else ''
                        if not current_url:
                            current_url = await tab.evaluate('window.location.href')
                        event_id = current_url.split('/activity/')[-1].split('/')[0].split('?')[0]
                        order_url = 'https://ticketplus.com.tw/order/' + event_id + '/' + session_id
                        debug.log(f"[TicketPlus DATE] Vue data: date={target_session.get('date', '')} sessionId={session_id}")
                        await navigate_page_if_enabled(tab, order_url)
                        is_date_clicked = True

        except Exception as exc:
            debug.log(f"[TicketPlus DATE] Vue data navigation failed: {exc}")

    if not is_date_clicked and is_vue_ready and formated_area_list and len(formated_area_list) > 0:
        try:
            original_keyword = config_dict["date_auto_select"]["date_keyword"].strip()

            # Collect first, decide exclusion in Python, then click. Keeping the
            # exclusion test on the Python side is what makes TicketPlus agree
            # with the other platforms; the click pass below only receives the
            # indexes it is allowed to consider.
            collected = await tab.evaluate('''
                (function() {
                    ''' + _TICKETPLUS_SESSION_CONTAINERS_JS + '''
                    return sessionContainers.map(function(c, i) {
                        return { index: i, text: c.textContent || '' };
                    });
                })();
            ''')

            if not isinstance(collected, list):
                collected = []

            allowed_indexes = [
                item['index'] for item in _ticketplus_filter_excluded(
                    config_dict, collected, lambda item: item.get('text', '')
                )
            ]

            excluded_count = len(collected) - len(allowed_indexes)
            if excluded_count:
                debug.log(f"[TicketPlus DATE] Excluded {excluded_count} session(s) by keyword_exclude")

            # An empty allowed list leaves the click pass with no containers, so
            # it reports failure and the reload path below retries -- same as
            # every other platform when everything is excluded.
            click_result = await tab.evaluate(f'''
                (function() {{
                    const originalKeyword = '{original_keyword}';
                    const autoSelectMode = {json.dumps(auto_select_mode)};
                    const dateAutoFallback = {'true' if date_auto_fallback else 'false'};
                    const allowedIndexes = {json.dumps(allowed_indexes)};

                    console.log('[TicketPlus] Starting date selection - keyword:', originalKeyword, 'mode:', autoSelectMode, 'fallback:', dateAutoFallback);

                    {_TICKETPLUS_SESSION_CONTAINERS_JS}

                    // Drop what keyword_exclude removed on the Python side, so
                    // keyword matching and the fallback below share one clean list.
                    sessionContainers = sessionContainers.filter(function(c, i) {{
                        return allowedIndexes.indexOf(i) !== -1;
                    }});

                    console.log('[TicketPlus] Found session containers:', sessionContainers.length);

                    let matchedContainers = [];

                    if (originalKeyword && originalKeyword.trim() !== '') {{
                        let keywords = [];
                        if (originalKeyword.includes(',')) {{
                            keywords = originalKeyword.split(',')
                                .map(k => k.trim().replace(/^["']|["']$/g, ''))
                                .filter(k => k.length > 0);
                        }} else {{
                            keywords = [originalKeyword.replace(/^["']|["']$/g, '').trim()];
                        }}

                        console.log('[TicketPlus] Parsed keywords:', keywords);

                        for (let i = 0; i < sessionContainers.length; i++) {{
                            const container = sessionContainers[i];
                            const text = container.textContent || '';
                            const normalizedText = text.replace(/[\\s\\u3000]/g, '').toLowerCase();

                            for (let keyword of keywords) {{
                                const normalizedKeyword = keyword.replace(/[\\s\\u3000]/g, '').toLowerCase();
                                if (normalizedText.includes(normalizedKeyword)) {{
                                    matchedContainers.push(container);
                                    console.log('[TicketPlus] Keyword "' + keyword + '" matched container ' + i);
                                    console.log('  -> Text preview:', text.substring(0, 100).replace(/\\n/g, ' '));
                                    break;
                                }}
                            }}
                        }}
                    }} else {{
                        matchedContainers = sessionContainers;
                        console.log('[TicketPlus] No keyword specified, using all', sessionContainers.length, 'containers');
                    }}

                    if (matchedContainers.length === 0 && originalKeyword && originalKeyword.trim() !== '') {{
                        if (dateAutoFallback) {{
                            console.log('[TicketPlus DATE FALLBACK] date_auto_fallback=true, triggering auto fallback');
                            matchedContainers = sessionContainers;
                        }} else {{
                            console.log('[TicketPlus DATE FALLBACK] date_auto_fallback=false, fallback is disabled');
                            console.log('[TicketPlus DATE SELECT] No date selected, will reload page and retry');
                            return {{
                                success: false,
                                error: 'No keyword matches and fallback is disabled',
                                strict_mode: true
                            }};
                        }}
                    }}

                    if (matchedContainers.length === 0) {{
                        console.log('[TicketPlus ERROR] No session containers found');
                        return {{
                            success: false,
                            error: 'No session containers found',
                            debug: {{
                                keyword: originalKeyword,
                                mode: autoSelectMode,
                                totalContainers: sessionContainers.length
                            }}
                        }};
                    }}

                    let targetIndex = 0;
                    if (autoSelectMode === 'from bottom to top') {{
                        targetIndex = matchedContainers.length - 1;
                    }} else if (autoSelectMode === 'center') {{
                        targetIndex = Math.floor(matchedContainers.length / 2);
                    }} else if (autoSelectMode === 'random') {{
                        targetIndex = Math.floor(Math.random() * matchedContainers.length);
                    }}

                    let targetContainer = matchedContainers[targetIndex];
                    const containerText = (targetContainer.textContent || '').substring(0, 150).replace(/\\n/g, ' ');
                    console.log('[TicketPlus TARGET] Selected container [' + targetIndex + '/' + matchedContainers.length + ']');
                    console.log('  -> Preview:', containerText);

                    let buyButton = targetContainer.querySelector('button.nextBtn');
                    if (!buyButton) {{
                        buyButton = targetContainer.querySelector('button');
                    }}

                    if (!buyButton) {{
                        console.log('[TicketPlus ERROR] No buy button found in container');
                        return {{
                            success: false,
                            error: 'No buy button found in container',
                            targetText: containerText
                        }};
                    }}

                    const buttonText = buyButton.textContent || '';
                    console.log('[TicketPlus BUTTON] Found button:', buttonText);

                    try {{
                        const event = new MouseEvent('click', {{
                            bubbles: true,
                            cancelable: true,
                            view: window
                        }});
                        buyButton.dispatchEvent(event);
                        console.log('[TicketPlus SUCCESS] Button clicked successfully');
                        return {{
                            success: true,
                            action: 'button_clicked',
                            matchedCount: matchedContainers.length,
                            targetText: containerText,
                            buttonText: buttonText
                        }};
                    }} catch (e) {{
                        console.log('[TicketPlus ERROR] Click failed:', e.message);
                        return {{
                            success: false,
                            error: 'Click failed: ' + e.message,
                            targetText: containerText
                        }};
                    }}
                }})();
            ''')

            parsed_result = util.parse_nodriver_result(click_result)

            if isinstance(parsed_result, dict) and parsed_result.get('success'):
                debug.log(f"Date selection and click successful: {parsed_result.get('action', 'unknown')}")
                debug.log(f"   Target text: {parsed_result.get('targetText', '')}")
                is_date_clicked = True
            else:
                debug.log(f"Date selection and click failed: {parsed_result.get('error', 'unknown') if isinstance(parsed_result, dict) else str(parsed_result)}")

        except Exception as exc:
            debug.log("JavaScript date selection click failed:", exc)

    if not is_date_clicked:
        if debug.enabled:
            if not is_vue_ready:
                debug.log("[TicketPlus DATE] Vue.js not ready, waiting for page to load...")
            elif not formated_area_list or len(formated_area_list) == 0:
                debug.log("[TicketPlus DATE] No available tickets (all sold out), waiting for refresh...")

        if auto_reload_coming_soon_page_enable and is_vue_ready and (not formated_area_list or len(formated_area_list) == 0):
            try:
                reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
                if reload_interval > 0:
                    debug.log(f"[TicketPlus DATE] Waiting {reload_interval}s before auto-reload...")
                    await sleep_before_auto_refresh(reload_interval)
                else:
                    await asyncio.sleep(1.0)

                clicked = await _ticketplus_click_refresh_button(tab, debug)
                if not clicked:
                    await reload_page_if_enabled(tab)
                    debug.log("[TicketPlus DATE] Page reloaded, waiting for content...")
                    await asyncio.sleep(0.5)
            except Exception as exc:
                debug.log(f"[TicketPlus DATE] Auto reload failed: {exc}")

    return is_date_clicked


async def _ticketplus_actions_paused(config_dict):
    return await check_and_handle_pause(config_dict) or await check_and_handle_quit(config_dict)


async def _ticketplus_sleep(tab, seconds, config_dict):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await _ticketplus_actions_paused(config_dict):
            return True
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return await _ticketplus_actions_paused(config_dict)


CONST_TICKETPLUS_STABLE_TITLE_JS = r'''element => {
    const copy = element?.cloneNode(true);
    copy?.querySelectorAll('small, .v-chip, .v-icon, [role="tooltip"]').forEach(e => e.remove());
    return (copy?.textContent || '').trim().replace(/\s+/g, ' ');
}'''


CONST_TICKETPLUS_QUANTITY_JS = r'''((name, expected, action) => {
    const visible = e => e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const normalize = STABLE_TITLE;
    const panels = Array.from(document.querySelectorAll('.seats-area .v-expansion-panel:not(:has(.seats-area))'));
    const rows = panels.length ? panels : Array.from(document.querySelectorAll('.row.py-1.py-md-4'));
    const title = row => normalize(row.querySelector('.v-expansion-panel-header, .font-weight-medium'));
    const quantity = row => {
        const controls = Array.from(row.querySelectorAll('.count-button input, .count-button div, .count-button span'));
        const values = controls.filter(e => !e.children.length || e.tagName === 'INPUT')
            .map(e => (e.value ?? e.textContent).trim()).filter(t => /^\d+$/.test(t));
        return values.length === 1 ? Number(values[0]) : null;
    };
    const documentId = location.href + '|' + performance.timeOrigin;
    const matches = rows.filter(row => title(row) === name);
    if (matches.length !== 1) return {documentId, blocked:true};
    const row = matches[0], value = quantity(row);
    const otherSelected = rows.some(r => r !== row && quantity(r) > 0);
    if (value === null || otherSelected) return {documentId, blocked:true};
    if (!action || value !== expected) return {documentId, value};
    const icon = row.querySelector(action === 'add' ? '.mdi-plus' : '.mdi-minus');
    const button = icon?.closest('button') || icon;
    if (!visible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true' ||
        button.closest('.v-btn--disabled, .disabledBtn')) return {documentId, value, blocked:true};
    button.click();
    return {documentId, value, clicked:true};
})(NAME, EXPECTED, ACTION)'''.replace('STABLE_TITLE', CONST_TICKETPLUS_STABLE_TITLE_JS)


async def _ticketplus_adjust_quantity(tab, config_dict, name):
    """Change one quantity at a time; never count attempted clicks as tickets."""
    target = config_dict.get('ticket_number')
    if not isinstance(target, int) or isinstance(target, bool) or target < 1:
        return False
    deadline = time.monotonic() + CONST_TICKETPLUS_NEXT_BUTTON_WAIT
    while time.monotonic() < deadline:
        if await _ticketplus_actions_paused(config_dict):
            return False
        script = CONST_TICKETPLUS_QUANTITY_JS.replace('EXPECTED', 'null').replace('ACTION', 'null').replace('NAME', json.dumps(name))
        current = await tab.evaluate(script)
        if not isinstance(current, dict) or current.get('blocked'):
            await _ticketplus_sleep(tab, 0.1, config_dict)
            continue
        value = current.get('value')
        if value == target:
            _state.pop('quantity_pending', None)
            if _state.pop('quantity_wait_notice', None):
                util.write_instance_status('running')
            _state['selected_ticket_row'] = {'documentId': current.get('documentId'), 'name': name}
            return True
        signature = (current.get('documentId'), name, value)
        if _state.get('quantity_pending') == signature:
            await _ticketplus_sleep(tab, 0.1, config_dict)
            continue
        if not isinstance(value, int):
            return False
        action = 'add' if value < target else 'remove'
        script = CONST_TICKETPLUS_QUANTITY_JS.replace('EXPECTED', str(value)).replace('ACTION', json.dumps(action)).replace('NAME', json.dumps(name))
        # Reserve first: an uncertain CDP result must not repeat a real click.
        _state['quantity_pending'] = signature
        result = await tab.evaluate(script)
        if isinstance(result, dict) and not result.get('clicked'):
            _state.pop('quantity_pending', None)
            if result.get('blocked'):
                await _ticketplus_sleep(tab, 0.1, config_dict)
                continue
        await _ticketplus_sleep(tab, 0.05, config_dict)
    return False


def _ticketplus_hold_unresolved_quantity(document_id):
    pending = _state.get('quantity_pending')
    if not pending or pending[0] != document_id:
        return False
    if _state.get('quantity_wait_notice') != pending:
        _state['quantity_wait_notice'] = pending
        message = '票數變更尚未確認，保留目前選票並等待更新；若持續無反應，請查看瀏覽器或手動刷新。'
        util.create_debug_logger(enabled=True).log('[TICKETPLUS] ' + message)
        util.write_instance_status('running', message)
    return True


def _ticketplus_timing(stage, config_dict, document_id=None):
    """Record one timing mark per stage; never log ticket/account identifiers."""
    now = time.monotonic()
    if document_id and _state.get('timing_document') != document_id:
        _state['timing_document'] = document_id
        start = _state.get('cycle_started_at', now)
        _state['timing_start'] = start
        _state['timing_marks'] = {}
        _state['timing_last'] = start
        ready = _state.get('cycle_ready_at', now)
        _state['timing_marks']['頁面就緒'] = max(0, ready - start)
        _state['timing_last'] = min(now, max(start, ready))
        util.create_debug_logger(config_dict).log(f'[TICKETPLUS 耗時] 頁面就緒：累計 {max(0, ready-start):.3f} 秒')
    if 'timing_start' not in _state or stage in _state.get('timing_marks', {}):
        return
    elapsed = now - _state['timing_start']
    delta = now - _state['timing_last']
    _state['timing_marks'][stage] = elapsed
    _state['timing_last'] = now
    util.create_debug_logger(config_dict).log(f'[TICKETPLUS 耗時] {stage}：累計 {elapsed:.3f} 秒，本階段 {delta:.3f} 秒')


def _ticketplus_queue_mark(stage, config_dict):
    trace = _state.get('queue_trace')
    if not trace or stage in trace['marks']:
        return
    now = time.perf_counter()
    trace['marks'][stage] = now
    previous = trace['last']
    trace['last'] = now
    util.create_debug_logger(config_dict).log(
        f'[TICKETPLUS 排隊計時] {stage}：送出後 {now-trace["start"]:.3f} 秒，距前一紀錄 {now-previous:.3f} 秒')


def _ticketplus_begin_queue_trace(config_dict):
    if not config_dict.get('advanced', {}).get('ticketplus_queue_timing', True):
        return
    trace = _state.get('queue_trace', {})
    if trace.get('document') == _state.get('submit_pending') and not trace.get('closed'):
        return
    now = time.perf_counter()
    _state['queue_trace'] = {'start': now, 'last': now, 'marks': {},
                             'document': _state.get('submit_pending'), 'requests': {}, 'sequence': 0}
    _ticketplus_queue_mark('開始點擊選票下一步', config_dict)


def _ticketplus_queue_summary(config_dict):
    trace = _state.get('queue_trace')
    if not trace or trace.get('summarized'):
        return
    marks = trace['marks']
    names = ('收到瀏覽器跳轉通知：confirmseat', '排隊監控偵測到確認頁',
             '開始處理確認頁：confirmseat', '座位確認下一步點擊已返回')
    if not all(name in marks for name in names):
        return
    navigation, detected, processing, clicked = (marks[name] for name in names)
    trace['summarized'] = True
    util.create_debug_logger(config_dict).log(
        f'[TICKETPLUS 排隊摘要] 點擊送出→瀏覽器跳轉通知 {navigation-trace["start"]:.3f} 秒'
        f'（含網站等待、網路、前端處理）；通知→監控偵測 {detected-navigation:+.3f} 秒；'
        f'偵測→處理座位頁 {processing-detected:.3f} 秒；'
        f'處理座位頁→下一步點擊返回 {clicked-processing:.3f} 秒。'
        '瀏覽器事件以程式收到通知的時間為準，HTTP 200 不代表保留成功。')


def _ticketplus_trace_endpoint(url):
    parsed = urlsplit(url)
    name = parsed.path.rstrip('/').rsplit('/', 1)[-1].lower()
    hosts = {
        'enqueue': ('queue.ticketplus.com.tw', 'apis.ticketplus.com.tw'),
        'reserve': ('api.ticketplus.com.tw', 'apis.ticketplus.com.tw'),
        'getusercurrentreservedorder': ('api.ticketplus.com.tw',),
    }
    if parsed.hostname in hosts.get(name, ()):
        return 'getUserCurrentReservedOrder' if name == 'getusercurrentreservedorder' else name
    return None


async def _ticketplus_subscribe_queue_timing(tab, config_dict):
    """Observe CDP metadata only; never read bodies, headers or credentials."""
    if not config_dict.get('advanced', {}).get('ticketplus_queue_timing', True) or _state.get('trace_tab') is tab:
        return
    try:
        tree = await tab.send(cdp.page.get_frame_tree())
        main_frame = tree.frame.id_
        subscription = object()
        async def observed(event, connection=None):
            trace = _state.get('queue_trace')
            if _state.get('trace_subscription') is not subscription or not trace:
                return
            if isinstance(event, cdp.network.RequestWillBeSent):
                name = _ticketplus_trace_endpoint(event.request.url)
                if name and event.request.method.upper() != 'OPTIONS':
                    if len(trace['requests']) >= 128:
                        trace['requests'].pop(next(iter(trace['requests'])))
                    trace['sequence'] += 1
                    trace['requests'][event.request_id] = (name, float(event.timestamp), trace['sequence'])
                    _ticketplus_queue_mark(f'網站發出 {name} #{trace["sequence"]}', config_dict)
            elif isinstance(event, cdp.network.ResponseReceived):
                request = trace['requests'].get(event.request_id)
                if request:
                    name, started, sequence = request
                    duration = max(0, float(event.timestamp) - started)
                    _ticketplus_queue_mark(f'收到 {name} #{sequence} 回應標頭（HTTP {int(event.response.status)}，請求至標頭 {duration:.3f} 秒）', config_dict)
            elif isinstance(event, (cdp.network.LoadingFinished, cdp.network.LoadingFailed)):
                request = trace['requests'].pop(event.request_id, None)
                if request:
                    name, started, sequence = request
                    duration = max(0, float(event.timestamp) - started)
                    outcome = '接收完成' if isinstance(event, cdp.network.LoadingFinished) else '載入失敗'
                    _ticketplus_queue_mark(f'{name} #{sequence} {outcome}（請求累計 {duration:.3f} 秒）', config_dict)
            elif isinstance(event, cdp.page.NavigatedWithinDocument) and event.frame_id == main_frame:
                route = _ticketplus_confirmation_route(event.url)
                if route:
                    _ticketplus_queue_mark(f'收到瀏覽器跳轉通知：{route}', config_dict)
                    _ticketplus_queue_summary(config_dict)
            elif isinstance(event, cdp.page.FrameNavigated) and event.frame.parent_id is None:
                route = _ticketplus_confirmation_route(event.frame.url)
                if route:
                    _ticketplus_queue_mark(f'收到瀏覽器跳轉通知：{route}', config_dict)
                    _ticketplus_queue_summary(config_dict)
        for event_type in (cdp.network.RequestWillBeSent, cdp.network.ResponseReceived,
                           cdp.network.LoadingFinished, cdp.network.LoadingFailed,
                           cdp.page.NavigatedWithinDocument, cdp.page.FrameNavigated):
            tab.add_handler(event_type, observed)
        _state['trace_tab'] = tab
        _state['trace_subscription'] = subscription
        await tab.send(cdp.network.enable())
        await tab.send(cdp.page.enable())
    except Exception:
        util.create_debug_logger(config_dict).log('[TICKETPLUS 排隊計時] 瀏覽器事件監聽無法啟用，仍保留程式階段計時。')


async def nodriver_ticketplus_unified_select(tab, config_dict, area_keyword):
    """TicketPlus unified selector - language-independent ticket type/area selection."""
    debug = util.create_debug_logger(config_dict)
    auto_select_mode = config_dict["area_auto_select"]["mode"]
    # Fallback is applied only after all ordered keyword groups fail.
    area_auto_fallback = not bool(area_keyword.strip())
    ticket_number = config_dict["ticket_number"]
    keyword_exclude = config_dict.get("keyword_exclude", "")

    debug.log(f"Unified selector started - keyword: {area_keyword}, tickets: {ticket_number}")

    is_selected = False

    try:
        if await _ticketplus_actions_paused(config_dict):
            return False

        exclude_keywords = []
        if keyword_exclude:
            try:
                exclude_keywords = json.loads("[" + keyword_exclude + "]")
            except:
                if util.CONST_KEYWORD_DELIMITER in keyword_exclude:
                    exclude_keywords = [kw.strip() for kw in keyword_exclude.split(util.CONST_KEYWORD_DELIMITER) if kw.strip()]
                else:
                    exclude_keywords = [keyword_exclude.strip()] if keyword_exclude.strip() else []

        # Wait for Vue.js elements to render
        auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 5)
        max_vue_wait = max(6.0, min(15.0, auto_reload_interval * 2))
        vue_check_interval = 0.15
        vue_wait_start = time.time()
        vue_elements_found = False
        last_log_time = 0

        while time.time() - vue_wait_start < max_vue_wait:
            if await _ticketplus_actions_paused(config_dict):
                return False

            try:
                vue_check = await tab.evaluate('''
                    (function() {
                        const panels = document.querySelectorAll('.v-expansion-panel').length;
                        const countBtn = document.querySelectorAll('.count-button .mdi-plus').length;
                        const rowTickets = document.querySelectorAll('.row.py-1.py-md-4').length;
                        return {
                            panels: panels,
                            countBtn: countBtn,
                            rowTickets: rowTickets,
                            hasElements: panels > 0 || countBtn > 0 || rowTickets > 0
                        };
                    })();
                ''')

                if isinstance(vue_check, list):
                    vue_check = {item[0]: item[1].get('value') if isinstance(item[1], dict) else item[1] for item in vue_check}

                elapsed = time.time() - vue_wait_start
                if elapsed - last_log_time >= 1.0:
                    debug.log(f"[VUE WAIT] {elapsed:.1f}s - panels:{vue_check.get('panels', 0)}, countBtn:{vue_check.get('countBtn', 0)}, rowTickets:{vue_check.get('rowTickets', 0)}")
                    last_log_time = elapsed

                if vue_check.get('hasElements', False):
                    vue_elements_found = True
                    debug.log(f"[VUE WAIT] Vue elements found after {elapsed:.1f}s")
                    break

            except Exception as e:
                debug.log(f"[VUE WAIT] Check error: {e}")

            await asyncio.sleep(vue_check_interval)

        if not vue_elements_found:
            debug.log(f"[VUE WAIT] Timeout after {max_vue_wait:.1f}s, Vue elements not found")
            return False

        js_result = await tab.evaluate(f'''
            (function() {{
                const keyword = {json.dumps(area_keyword, ensure_ascii=True)};
                const ticketNumber = {ticket_number};
                const autoSelectMode = {json.dumps(auto_select_mode)};
                const areaAutoFallback = {'true' if area_auto_fallback else 'false'};
                const keywordArray = keyword.trim().split(/\\s+/).filter(Boolean);
                const keyword1 = keywordArray[0] || '';
                const keyword2 = keywordArray[1] || '';
                const excludeKeywords = {json.dumps(exclude_keywords, ensure_ascii=True)};
                const previous = {json.dumps(_state.get('selected_ticket_row', {}), ensure_ascii=True)};
                const preferredName = previous.documentId === location.href + '|' + performance.timeOrigin ? previous.name : '';
                const stableTitle = {CONST_TICKETPLUS_STABLE_TITLE_JS};

                console.log('Unified selector execution - keyword:', keyword, 'tickets:', ticketNumber, 'mode:', autoSelectMode, 'fallback:', areaAutoFallback);

                function isSoldOut(element) {{
                    const text = element.textContent || '';
                    const soldOutPatterns = [/\u5269\u9918\\s*0(?!\\d)/, /\u5269\u9918\\s*[:\uFF1A]\\s*0(?!\\d)/, /sold\\s*out/i, /\u552e\u5b8c/, /\u5df2\u552e\u5b8c/, /\u552e\u7f44/, /\u7121\u5eab\u5b58/];
                    const availablePatterns = [/\u71b1\u8ce3\u4e2d/, /\u71b1\u8ce3/, /\u71b1\u552e/, /\u53ef\u8cfc\u8cb7/, /available/i, /\u5269\u9918\\s*[:\uFF1A]?\\s*[1-9]\\d*/];

                    for (let pattern of soldOutPatterns) {{
                        if (pattern.test(text)) {{
                            for (let avail of availablePatterns) {{
                                if (avail.test(text)) return false;
                            }}
                            return true;
                        }}
                    }}
                    return false;
                }}

                function containsExcludeKeywords(name) {{
                    if (!excludeKeywords || excludeKeywords.length === 0) return false;
                    for (let kw of excludeKeywords) {{
                        if (kw && name.includes(kw)) return true;
                    }}
                    return false;
                }}

                function getTargetIndex(items, mode) {{
                    const count = items.length;
                    if (count === 0) return -1;
                    switch(mode) {{
                        case 'from top to bottom': return 0;
                        case 'from bottom to top': return count - 1;
                        case 'center': return Math.floor((count - 1) / 2);
                        case 'random': return Math.floor(Math.random() * count);
                        default: return 0;
                    }}
                }}

                const hasExpansionPanel = document.querySelector('.v-expansion-panel');
                const hasCountButton = document.querySelector('.count-button .mdi-plus');

                console.log('hasExpansionPanel:', !!hasExpansionPanel, 'hasCountButton:', !!hasCountButton);

                if (hasExpansionPanel) {{
                    // Select innermost zone panels only (the leaf panels containing .mdi-plus add buttons).
                    // :not(:has(.seats-area)) excludes any panel that wraps another .seats-area (price-tier
                    // group panels), leaving only the clickable zone panels in both flat and nested layouts.
                    // :has() requires Chrome 105+; zendriver enforces Chrome 145+, so this is safe.
                    const panels = document.querySelectorAll('.seats-area .v-expansion-panel:not(:has(.seats-area))');
                    const validPanels = [];
                    const soldOutNames = [];

                    for (let i = 0; i < panels.length; i++) {{
                        const panel = panels[i];
                        const nameEl = panel.querySelector('.v-expansion-panel-header');
                        if (nameEl) {{
                            const name = stableTitle(nameEl);
                            if (containsExcludeKeywords(name)) continue;
                            if (isSoldOut(panel)) {{
                                soldOutNames.push(name);
                            }} else {{
                                validPanels.push({{ panel, name, index: i }});
                            }}
                        }}
                    }}

                    console.log('Valid panels:', validPanels.length);
                    if (validPanels.length === 0) {{
                        return {{ success: false, message: 'No valid panels' }};
                    }}

                    let target = null;
                    if (keyword1) {{
                        // Every hit, then the select order picks one; .find() always took the top hit (#416)
                        const hits = validPanels.filter(p => keywordArray.every(word => p.name.includes(word)));
                        target = hits.find(p => p.name === preferredName) || hits[getTargetIndex(hits, autoSelectMode)] || null;
                    }}
                    if (!target && keyword1 && !areaAutoFallback) {{
                        const keywordInSoldOut = soldOutNames.some(n => keywordArray.every(word => n.includes(word)));
                        return {{ success: false, strict_mode: true, attempted_keyword: keyword, keyword_in_sold_out: keywordInSoldOut }};
                    }}
                    if (!target) {{
                        const idx = getTargetIndex(validPanels, autoSelectMode);
                        target = validPanels.find(p => p.name === preferredName) || validPanels[idx];
                    }}

                    if (!target) {{
                        return {{ success: false, message: 'No target panel' }};
                    }}

                    const header = target.panel.querySelector('.v-expansion-panel-header');
                    const isExpanded = target.panel.classList.contains('v-expansion-panel--active');
                    if (!isExpanded && header) {{
                        console.log('Clicking to expand:', target.name);
                        header.click();
                    }}

                    let plusBtn = target.panel.querySelector('.mdi-plus') ||
                                  target.panel.querySelector('.count-button .mdi-plus');

                    if (plusBtn) {{
                        console.log('Found quantity controls for:', target.name);
                        return {{ success: true, type: 'expansion_panel', selected: target.name, quantityPending: true }};
                    }}

                    return {{ success: true, type: 'expansion_panel', selected: target.name, quantityPending: true }};

                }} else if (hasCountButton) {{
                    const rows = document.querySelectorAll('.row.py-1.py-md-4');
                    const validRows = [];
                    const soldOutRowNames = [];

                    for (let row of rows) {{
                        const plusBtn = row.querySelector('.count-button .mdi-plus');
                        if (!plusBtn) continue;

                        const nameEl = row.querySelector('.font-weight-medium');
                        if (nameEl) {{
                            const name = stableTitle(nameEl);
                            if (containsExcludeKeywords(name)) continue;
                            if (isSoldOut(row)) {{
                                soldOutRowNames.push(name);
                            }} else {{
                                validRows.push({{ row, name, plusBtn }});
                            }}
                        }}
                    }}

                    console.log('Valid rows:', validRows.length);
                    if (validRows.length === 0) {{
                        return {{ success: false, message: 'No valid rows' }};
                    }}

                    let target = null;
                    if (keyword1) {{
                        const hits = validRows.filter(r => keywordArray.every(word => r.name.includes(word)));
                        target = hits.find(r => r.name === preferredName) || hits[getTargetIndex(hits, autoSelectMode)] || null;
                    }}
                    if (!target && keyword1 && !areaAutoFallback) {{
                        const keywordInSoldOut = soldOutRowNames.some(n => keywordArray.every(word => n.includes(word)));
                        return {{ success: false, strict_mode: true, attempted_keyword: keyword, keyword_in_sold_out: keywordInSoldOut }};
                    }}
                    if (!target) {{
                        const idx = getTargetIndex(validRows, autoSelectMode);
                        target = validRows.find(r => r.name === preferredName) || validRows[idx];
                    }}

                    if (target && target.plusBtn) {{
                        console.log('Verifying quantity for:', target.name);
                        return {{ success: true, type: 'count_button', selected: target.name, quantityPending: true }};
                    }}
                }}

                return {{ success: false, message: 'No selectable elements found' }};
            }})();
        ''')

        result = util.parse_nodriver_result(js_result)

        if isinstance(result, dict):
            is_selected = result.get('success', False) and result.get('clicked', False)

            if result.get('quantityPending'):
                is_selected = await _ticketplus_adjust_quantity(tab, config_dict, result.get('selected', ''))
                if not is_selected:
                    result['message'] = 'Ticket quantity not verified; not submitting'

            if debug.enabled:
                if is_selected:
                    selected_type = result.get('type', '')
                    selected_name = result.get('selected', '')
                    debug.log(f"Selection successful - type: {selected_type}, item: {selected_name}")
                elif result.get('strict_mode'):
                    kw = result.get('attempted_keyword') or area_keyword
                    if result.get('keyword_in_sold_out'):
                        debug.log(f"[STRICT] Target area '{kw}' is currently sold out (remaining 0); strict mode keeps refreshing for returns.")
                    else:
                        debug.log(f"[STRICT] No purchasable area matches '{kw}'; strict mode keeps refreshing.")
                    debug.log("[STRICT] To auto-fallback to other available areas, enable: Advanced Settings -> area_auto_fallback")
                else:
                    debug.log(f"Selection failed: {result.get('message', 'unknown error')}")
        else:
            debug.log(f"Unified selector returned invalid result: {result}")
            is_selected = False

    except Exception as exc:
        if debug.enabled:
            debug.log(f"Unified selector exception error: {exc}")
            debug.log(f"Exception type: {type(exc).__name__}")
            debug.log(f"Traceback: {traceback.format_exc()}")
        is_selected = False

    return is_selected


async def nodriver_ticketplus_click_next_button_unified(tab, config_dict):
    """TicketPlus unified next button clicker - layout_style independent."""
    debug = util.create_debug_logger(config_dict)

    debug.log("Unified next button clicker started")

    try:
        # The button is polled from here rather than from a timer inside the
        # page: a page-side Promise needs evaluate(await_promise=True), and an
        # un-awaited one keeps running after this function has already reported
        # failure -- it would click Next on its own once the main loop had moved
        # on and the ticket counts had changed underneath it.
        waited = 0.0
        while True:
            if await _ticketplus_actions_paused(config_dict):
                return False
            _state['submit_pending'] = await tab.evaluate("location.href + '|' + performance.timeOrigin")
            _ticketplus_begin_queue_trace(config_dict)
            result = await tab.evaluate(CONST_TICKETPLUS_NEXT_BUTTON_JS)

            if isinstance(result, dict) and result.get('clicked'):
                _ticketplus_queue_mark('選票下一步點擊已返回', config_dict)
                _ticketplus_timing('已點選票下一步', config_dict)
                debug.log(f"[SUCCESS] Next button clicked - Button text: {result.get('buttonText', '')}")
                return True

            if isinstance(result, dict):
                _state.pop('submit_pending', None)

            found_disabled = isinstance(result, dict) and result.get('foundDisabled')
            if not found_disabled:
                debug.log("[ERROR] No next button on the page; nothing to wait for")
                return False

            if waited >= CONST_TICKETPLUS_NEXT_BUTTON_WAIT:
                debug.log(f"[ERROR] Next button still disabled after {waited:.1f}s")
                return False

            if await _ticketplus_sleep(tab, CONST_TICKETPLUS_NEXT_BUTTON_POLL, config_dict):
                return False
            waited += CONST_TICKETPLUS_NEXT_BUTTON_POLL

    except Exception as exc:
        debug.log(f"Unified next button click error: {exc}")

    return False


# A checkbox is ticked only when its label reads like the terms. The exclusions
# exist because "agree" alone also matches marketing opt-ins, which must stay off.
CONST_TICKETPLUS_AGREE_KEYWORDS = ["同意", "條款", "規則", "規定", "須知", "閱讀", "詳閱", "agree", "terms"]
CONST_TICKETPLUS_AGREE_EXCLUDE_KEYWORDS = ["行銷", "電子報", "訂閱", "推播", "廣告", "捐贈", "加購",
                                           "marketing", "newsletter", "subscribe", "donat"]

# Label text for a checkbox: its Vuetify .v-input or wrapping <label>, else a
# <label for=id>, else the parent's text.
CONST_TICKETPLUS_CHECKBOX_LABEL_JS = '''
    (el) => {
        const box = el.closest('.v-input, label');
        let text = box ? box.innerText : '';
        if (!text && el.id) {
            const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (lab) text = lab.innerText;
        }
        if (!text && el.parentElement) text = el.parentElement.innerText;
        return (text || '').trim().slice(0, 200);
    }
'''


def _is_terms_checkbox_label(label_text):
    """True when a checkbox label reads as a terms agreement, not an opt-in."""
    text = (label_text or '').lower()
    if not text:
        return False
    if any(kw in text for kw in CONST_TICKETPLUS_AGREE_EXCLUDE_KEYWORDS):
        return False
    return any(kw in text for kw in CONST_TICKETPLUS_AGREE_KEYWORDS)


async def nodriver_ticketplus_ticket_agree(tab, config_dict):
    """TicketPlus agreement checkbox."""
    if await _ticketplus_actions_paused(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    is_finish_checkbox_click = False

    try:
        agree_checkbox_list = await tab.query_selector_all('input[type="checkbox"]')

        for checkbox in agree_checkbox_list:
            try:
                if not checkbox:
                    continue

                # Terms only: add-ons, donations and marketing opt-ins stay off.
                label_text = await checkbox.apply(CONST_TICKETPLUS_CHECKBOX_LABEL_JS)
                if not _is_terms_checkbox_label(label_text):
                    debug.log(f"[AGREE] Skipping checkbox that is not the terms: {(label_text or '')[:40]!r}")
                    continue

                # Element.apply, not Element.evaluate: zendriver defines evaluate
                # only on Tab, and Element.__getattr__ returns None for unknown
                # names, so checkbox.evaluate(...) is a TypeError the except hides.
                is_checked = await checkbox.apply('(el) => el.checked')

                if not is_checked:
                    await checkbox.click()

                    is_checked_after = await checkbox.apply('(el) => el.checked')
                    if is_checked_after:
                        is_finish_checkbox_click = True
                        debug.log("successfully checked agreement checkbox")
                    else:
                        if checkbox:
                            # Element.apply: tab.evaluate's second positional
                            # arg is await_promise, not a script parameter.
                            await checkbox.apply('''
                                (el) => {
                                    el.checked = true;
                                    el.dispatchEvent(new Event('change', {bubbles: true}));
                                }
                            ''')

                            final_check = await checkbox.apply('(el) => el.checked')
                            if final_check:
                                is_finish_checkbox_click = True
                                debug.log("successfully checked agreement checkbox via JS")
                else:
                    is_finish_checkbox_click = True
                    debug.log("agreement checkbox already checked")

            except Exception as exc:
                debug.log("process checkbox fail:", exc)
                continue

    except Exception as exc:
        debug.log("find agreement checkbox fail:", exc)

    return is_finish_checkbox_click


async def nodriver_ticketplus_accept_realname_card(tab):
    """Accept realname card popup."""
    is_button_clicked = False
    try:
        button = await tab.query_selector('div.v-dialog__content > div > div > div > div.row > div > button.primary')
        if button:
            await button.click()
            is_button_clicked = True
    except Exception as exc:
        pass
    return is_button_clicked


async def nodriver_ticketplus_accept_other_activity(tab):
    """Accept other activity popup."""
    is_button_clicked = False
    try:
        button = await tab.query_selector('div[role="dialog"] > div.v-dialog > button.primary-1 > span > i.v-icon')
        if button:
            await button.click()
            is_button_clicked = True
    except Exception as exc:
        pass
    return is_button_clicked


async def nodriver_ticketplus_accept_order_fail(tab, debug=None):
    """Detect and dismiss order failure popup (e.g. "Purchase failed / Sold out").

    Uses text-driven detection: scans visible dialogs for failure keywords, then
    clicks any button whose label looks like a dismissal action. CSS selectors
    for Vuetify dialogs vary across versions, so text matching is more robust.

    Returns:
        True  -- failure popup detected (and dismissed if possible)
        False -- no failure popup found
        None  -- evaluate failed; popup state unknown (caller should skip order submission)
    """
    try:
        js_result = await tab.evaluate('''
            (function() {
                const failureTexts = ''' + json.dumps(CONST_TICKETPLUS_FAILURE_KEYWORDS) + ''';
                const buttonTexts = ''' + json.dumps(CONST_TICKETPLUS_DISMISS_BUTTON_TEXTS) + ''';
                // .v-dialog__content is position:fixed, so its offsetParent is
                // always null whether or not the dialog shows. rect + computed
                // style is the only visibility test valid for every wrapper.
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    if (!rect.width || !rect.height) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' &&
                           style.visibility !== 'hidden' &&
                           style.opacity !== '0';
                };

                const dialogs = document.querySelectorAll('[role="dialog"], .v-dialog, .v-dialog__content');
                for (const dialog of dialogs) {
                    if (!isVisible(dialog)) continue;
                    // innerText, not textContent: a closed Vuetify dialog leaves
                    // its full-screen .v-dialog__content wrapper mounted, which
                    // passes isVisible, and textContent would still read the
                    // hidden "failed" text inside it -- reloading on every pass.
                    const text = (dialog.innerText || '').trim();
                    if (!failureTexts.some(t => text.includes(t))) continue;
                    const buttons = dialog.querySelectorAll('button');
                    for (const btn of buttons) {
                        const btnText = (btn.textContent || '').trim();
                        if (buttonTexts.some(t => btnText.includes(t))) {
                            btn.click();
                            return { foundFailure: true, buttonClicked: true, dialogText: text.slice(0, 80) };
                        }
                    }
                    return { foundFailure: true, buttonClicked: false, dialogText: text.slice(0, 80) };
                }
                return { foundFailure: false, buttonClicked: false, dialogText: '' };
            })();
        ''')

        if isinstance(js_result, dict) and js_result.get('foundFailure'):
            if debug is not None:
                debug.log(f"[ORDER FAIL] Detected popup: {js_result.get('dialogText', '')}")
                if js_result.get('buttonClicked'):
                    debug.log("[ORDER FAIL] Dismissed via dialog button")
                else:
                    debug.log("[ORDER FAIL] Dismiss button not found in dialog")
            return True
        return False
    except Exception as exc:
        if debug is not None:
            debug.log(f"[ORDER FAIL][DEGRADED] evaluate failed, popup state unknown: {exc}")
        return None


async def nodriver_ticketplus_check_queue_status(tab, config_dict, force_show_debug=False):
    """Check queue status - optimized to avoid duplicate output."""
    debug = util.create_debug_logger(enabled=(config_dict.get("advanced", {}).get("verbose", False) or force_show_debug))

    try:
        result = await tab.evaluate('''
            (function() {
                const queueKeywords = ''' + json.dumps(CONST_TICKETPLUS_QUEUE_KEYWORDS) + ''';

                const failureKeywords = ''' + json.dumps(CONST_TICKETPLUS_FAILURE_KEYWORDS) + ''';

                // innerText, not textContent: it returns rendered text only, so
                // the hidden dialogs Vuetify keeps mounted stay out of it. With
                // textContent a single dormant queue dialog matched forever and
                // the monitor ran to its fuse (#389).
                const bodyText = document.body.innerText || '';

                const hasQueueKeyword = queueKeywords.some(keyword => bodyText.includes(keyword));

                // Vuetify keeps every dialog mounted, so querySelector('.v-dialog')
                // usually returns an inactive empty node and the failure veto below
                // never fires -- which is how the failure popup got read as a queue
                // in the first place (#389). Scan every *visible* dialog instead.
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    if (!rect.width || !rect.height) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' &&
                           style.visibility !== 'hidden' &&
                           style.opacity !== '0';
                };

                // innerText for the same reason as bodyText above: a closed
                // dialog's wrapper still passes isVisible.
                const dialogTexts = Array.from(document.querySelectorAll('.v-dialog, [role="dialog"]'))
                    .filter(isVisible)
                    .map(el => (el.innerText || '').trim())
                    .filter(text => text.length > 0);

                const dialogText = dialogTexts.join(' | ');
                const hasFailureDialog = dialogTexts.some(text =>
                    failureKeywords.some(keyword => text.includes(keyword)));
                const hasQueueDialog = !hasFailureDialog &&
                                       dialogTexts.some(text =>
                                           text.includes('\u6392\u968a') ||
                                           text.includes('\u8acb\u7a0d\u5019'));

                // A bare scrim is NOT a queue: every Vuetify dialog and loading
                // mask draws one, so the order-failure popup used to be read as
                // a queue and the monitor loop spun forever (#389). Queue
                // wording decides now; the scrim is only reported back for the
                // forced-debug dump below, never for the verdict.
                const overlayScrim = document.querySelector('.v-overlay__scrim');
                const hasOverlay = !!(overlayScrim && overlayScrim.style.opacity === '1');

                const foundKeywords = queueKeywords.filter(keyword => bodyText.includes(keyword));

                return {
                    inQueue: !hasFailureDialog && (hasQueueKeyword || hasQueueDialog),
                    queueTitle: '',
                    foundKeywords: foundKeywords,
                    hasOverlay: hasOverlay,
                    hasQueueDialog: hasQueueDialog,
                    hasFailureDialog: hasFailureDialog,
                    dialogText: hasQueueDialog ? dialogText.trim() : ''
                };
            })();
        ''')

        result = util.parse_nodriver_result(result)

        if isinstance(result, dict):
            is_in_queue = result.get('inQueue', False)

            # Logged unconditionally: a failure dialog forces inQueue false, so
            # this never reaches the queue dump below. It is the one line that
            # explains why a queue verdict was withheld, and the popup is rare
            # enough that reporting every hit cannot flood the log.
            if result.get('hasFailureDialog'):
                debug.log("[QUEUE] Failure dialog present, not treating as queue")

            if is_in_queue and force_show_debug:
                debug.log("[QUEUE] Queue status detected")
                if result.get('hasOverlay'):
                    debug.log("   Overlay scrim found (v-overlay__scrim)")
                if result.get('hasQueueDialog'):
                    debug.log(f"   Dialog content: {result.get('dialogText', '')}")
                if result.get('foundKeywords'):
                    keywords = result.get('foundKeywords', [])
                    if keywords and isinstance(keywords[0], dict):
                        keywords = [str(k.get('value', k)) for k in keywords]
                    elif keywords:
                        keywords = [str(k) for k in keywords]
                    if keywords:
                        debug.log(f"   Keywords found: {', '.join(keywords)}")
            return is_in_queue

        return False

    except Exception as exc:
        debug.log(f"Queue status check error: {exc}")
        return False


def _ticketplus_confirmation_route(url):
    path = urlsplit(url or "").path.rstrip('/').split('/')
    if len(path) != 4 or not path[2] or not path[3]:
        return None
    kind = path[1].lower()
    return kind if kind in ('confirmseat', 'confirm') else None


async def nodriver_ticketplus_confirm(tab, config_dict, current=None):
    """Only advance the seat-confirmation Next button; never submit buyer data."""
    if await _ticketplus_actions_paused(config_dict):
        return False
    if current is None:
        try:
            current = await tab.evaluate("({url:location.href,documentId:location.href+'|'+performance.timeOrigin})")
        except Exception:
            return False  # The page may be navigating; inspect it on the next pass.
    if not isinstance(current, dict) or _ticketplus_confirmation_route(current.get('url')) != 'confirmseat':
        return False
    _ticketplus_queue_mark('座位頁資訊已讀取', config_dict)
    document_id = current.get('documentId')
    if not document_id or _state.get('seat_next_pending') == document_id:
        return False
    script = r"""((expected) => {
        if (location.href !== expected || !/^\/confirmseat\/[^/]+\/[^/]+\/?$/i.test(location.pathname))
            return {clicked:false};
        const buttons = Array.from(document.querySelectorAll('button.nextBtn'));
        const next = buttons.filter(button => {
            const style = getComputedStyle(button);
            return button.getClientRects().length && style.visibility !== 'hidden' &&
                style.opacity !== '0' && !button.disabled &&
                button.getAttribute('aria-disabled') !== 'true' &&
                !button.matches('.v-btn--disabled, .disabledBtn') &&
                /^(下一步|Next)$/i.test(button.textContent.trim());
        });
        if (next.length !== 1) return {clicked:false};
        const button = next[0];
        const seen = new Set();
        let hasVue = false, seat = null;
        for (let node = button; node; node = node.parentElement) {
            for (let vm = node.__vue__; vm && !seen.has(vm); vm = vm.$parent) {
                seen.add(vm); hasVue = true;
                if (typeof vm.nextStep === 'function' && Array.isArray(vm.reserveTickets) &&
                    Object.prototype.hasOwnProperty.call(vm, 'selectedDiscount')) seat = vm;
            }
        }
        if (hasVue && (!seat || seat._isMounted === false || seat._isDestroyed ||
            seat.$route?.name !== 'ConfirmSt' || !seat.reserveTickets.length || !seat.canNextStep))
            return {clicked:false, reason:'initializing'};
        let rect = button.getBoundingClientRect();
        if (rect.top < 0 || rect.bottom > innerHeight || rect.left < 0 || rect.right > innerWidth) {
            button.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'});
            rect = button.getBoundingClientRect();
        }
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        if (!hit || (hit !== button && !button.contains(hit)))
            return {clicked:false, reason:'covered'};
        button.click();
        return {clicked:true};
    })(EXPECTED)""".replace('EXPECTED', json.dumps(current['url']))
    if await _ticketplus_actions_paused(config_dict):
        return False
    _state['seat_next_pending'] = document_id
    try:
        result = await tab.evaluate(script)
        if isinstance(result, dict) and result.get('clicked') is False:
            _ticketplus_queue_mark('等待座位頁下一步可用', config_dict)
            reason = {'initializing': '座位頁元件與票券資料尚未就緒',
                      'covered': '按鈕仍被載入遮罩或其他元件覆蓋'}.get(result.get('reason'))
            if reason and _state.get('seat_wait_notice') != (document_id, reason):
                _state['seat_wait_notice'] = (document_id, reason)
                util.create_debug_logger(enabled=True).log(f'[TICKETPLUS] {reason}，等待就緒後再自動點擊。')
            _state.pop('seat_next_pending', None)
            return False
        if isinstance(result, dict) and result.get('clicked'):
            _ticketplus_queue_mark('座位確認下一步點擊已返回', config_dict)
            _ticketplus_queue_summary(config_dict)
            _ticketplus_timing('已點座位確認下一步', config_dict)
            util.create_debug_logger(enabled=True).log('[TICKETPLUS] 已點座位確認的下一步，等待購票資料確認頁。')
            return True
    except Exception:
        # An uncertain result must not click the button a second time.
        util.create_debug_logger(enabled=True).log('[TICKETPLUS] 座位確認下一步回應未明，請查看瀏覽器；不重複點擊。')
    return False


async def _ticketplus_process_confirmation(tab, url, config_dict):
    route = _ticketplus_confirmation_route(url)
    if not route or await _ticketplus_actions_paused(config_dict):
        return
    _ticketplus_queue_mark(f'開始處理確認頁：{route}', config_dict)
    try:
        current = await tab.evaluate("({url:location.href,documentId:location.href+'|'+performance.timeOrigin})")
    except Exception:
        return  # Navigation is still replacing the document; retry via main loop.
    if not isinstance(current, dict) or current.get('url') != url:
        return
    _ticketplus_queue_mark(f'確認頁網址已核對：{route}', config_dict)
    _state.pop('submit_pending', None)
    _state['is_ticket_assigned'] = True
    if route == 'confirmseat':
        _ticketplus_timing('座位確認頁', config_dict)
        if _state.get('stage') != 'seat_confirmation':
            util.write_instance_status('running', '已進入座位確認頁，準備下一步；尚未完成訂單。')
        _state['stage'] = 'seat_confirmation'
        await nodriver_ticketplus_confirm(tab, config_dict, current=current)
    else:
        _ticketplus_queue_mark('交由使用者手動確認', config_dict)
        _ticketplus_handoff(config_dict)


def _ticketplus_handoff(config_dict):
    """Publish a manual handoff without claiming an order/payment succeeded."""
    if _state.get('handoff_ready'):
        return
    _ticketplus_timing('等待手動處理', config_dict)
    _state.update(handoff_ready=True, stage='manual_confirmation', is_ticket_assigned=True)
    message = '[TicketPlus] 已進入購票資料確認頁，請檢查資料並手動完成後續流程；訂單與付款尚未確認完成。'
    util.write_instance_status('waiting_manual', message)
    util.create_debug_logger(enabled=True).log(message)
    notification_config = dict(config_dict)
    advanced = dict(config_dict.get('advanced', {}))
    for key in ('discord_message', 'telegram_message'):
        custom = advanced.get(key, '').strip()
        advanced[key] = message + ('\n' + custom if custom else '')
    notification_config['advanced'] = advanced
    if advanced.get('play_sound', {}).get('order'):
        play_sound_while_ordering(config_dict)
    send_discord_notification(notification_config, 'order', 'TicketPlus')
    send_telegram_notification(notification_config, 'order', 'TicketPlus')


async def _ticketplus_wait_after_submit(tab, config_dict, debug):
    """Poll for the outcome of a submitted order instead of waiting blind.

    The previous fixed 5-10s sleep left an order-failure popup sitting unhandled
    for the whole window, and the queue check that followed mistook the popup's
    overlay for a queue (#389).

    Returns:
        "confirmed"  -- confirmation page reached
        "order_fail" -- failure popup detected (dismissed if possible)
        "queue"      -- platform queue detected
        "timeout"    -- none of the above within the wait budget
    """
    deadline = time.time() + CONST_TICKETPLUS_SUBMIT_WAIT_MAX

    while time.time() < deadline:
        if await _ticketplus_actions_paused(config_dict):
            return "timeout"

        current_url = (tab.url or "").lower()
        if '/confirm/' in current_url or '/confirmseat/' in current_url:
            _ticketplus_queue_mark('送出監控偵測到確認頁', config_dict)
            debug.log("[SUBMIT WAIT] Confirmation page reached")
            return "confirmed"

        if await nodriver_ticketplus_accept_order_fail(tab, debug) is True:
            return "order_fail"

        if await nodriver_ticketplus_check_queue_status(tab, config_dict):
            _ticketplus_queue_mark('偵測到網站排隊畫面', config_dict)
            _ticketplus_timing('網站排隊中', config_dict)
            debug.log("[SUBMIT WAIT] Platform queue detected")
            return "queue"

        if await _ticketplus_sleep(tab, CONST_TICKETPLUS_SUBMIT_POLL_INTERVAL, config_dict):
            return "timeout"

    debug.log("[SUBMIT WAIT] No outcome within wait budget")
    return "timeout"


def _ticketplus_queue_poll_interval(config_dict):
    value = config_dict.get('advanced', {}).get('ticketplus_queue_url_poll_interval',
                                             CONST_TICKETPLUS_QUEUE_URL_POLL_INTERVAL)
    try:
        value = float(value) if not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        value = 0
    return value if 0.02 <= value <= 1.0 else CONST_TICKETPLUS_QUEUE_URL_POLL_INTERVAL


async def _ticketplus_monitor_queue(tab, config_dict, debug):
    """Watch the in-page queue until it clears, moves on, or the order fails.

    Cached URL checks run independently of full queue DOM scans (5-10s) and
    failure-dialog scans (0.5s). These checks do not refresh or send site requests.

    Gives up after CONST_TICKETPLUS_QUEUE_MONITOR_MAX: removing the scrim signal
    fixed the popup that caused #389, but an unbounded loop would strand the bot
    all the same the next time a dialog happens to carry queue wording.

    Returns:
        "confirmed"  -- confirmation page reached
        "order_fail" -- failure popup detected (dismissed if possible)
        "queue_end"  -- queue wording gone, resume normal page processing
        "timeout"    -- monitor fuse blew, or paused / errored out
    """
    last_url = ""
    monitor_deadline = time.monotonic() + CONST_TICKETPLUS_QUEUE_MONITOR_MAX
    poll_interval = _ticketplus_queue_poll_interval(config_dict)
    _ticketplus_queue_mark('進入排隊監控迴圈', config_dict)

    while time.monotonic() < monitor_deadline:
        if await _ticketplus_actions_paused(config_dict):
            return "timeout"

        try:
            current_url = tab.url or ''

            if _ticketplus_confirmation_route(current_url):
                _ticketplus_queue_mark('排隊監控偵測到確認頁', config_dict)
                debug.log("Detected entry to confirmation page, exiting queue monitoring")
                return "confirmed"

            if current_url != last_url:
                debug.log(f"Page status update - URL: {current_url}")
                last_url = current_url

            if await nodriver_ticketplus_accept_order_fail(tab, debug) is True:
                debug.log("[QUEUE END] Order failure popup dismissed, exiting queue monitoring")
                return "order_fail"

            if not await nodriver_ticketplus_check_queue_status(tab, config_dict):
                debug.log("[QUEUE END] Queue ended, continuing page processing")
                return "queue_end"

            recheck_deadline = min(monitor_deadline, time.monotonic() + random.uniform(5.0, 10.0))
            next_failure_check = time.monotonic() + CONST_TICKETPLUS_QUEUE_FAILURE_POLL_INTERVAL
            while time.monotonic() < recheck_deadline:
                if await _ticketplus_sleep(tab, min(poll_interval, max(0, recheck_deadline - time.monotonic())), config_dict):
                    return "timeout"
                if tab.url != current_url:
                    break
                if time.monotonic() >= next_failure_check:
                    if await nodriver_ticketplus_accept_order_fail(tab, debug) is True:
                        debug.log("[QUEUE END] Order failure popup dismissed mid-wait, exiting queue monitoring")
                        return "order_fail"
                    next_failure_check = time.monotonic() + CONST_TICKETPLUS_QUEUE_FAILURE_POLL_INTERVAL

        except Exception as exc:
            debug.log(f"Queue monitoring error: {exc}")
            return "timeout"

    debug.log("[QUEUE END] Queue monitoring hit its time limit, returning to main loop")
    return "timeout"


async def nodriver_ticketplus_order(tab, config_dict, ocr, Captcha_Browser):
    """TicketPlus order processing - supports three layout detection modes.

    Modifies _state in place (no return value).
    """
    debug = util.create_debug_logger(config_dict)

    if await _ticketplus_actions_paused(config_dict):
        return
    await _ticketplus_subscribe_queue_timing(tab, config_dict)
    document_id = await tab.evaluate("location.href + '|' + performance.timeOrigin")
    _ticketplus_timing('開始選票', config_dict, document_id)
    if _state.get('submit_pending') == document_id and document_id:
        outcome = await _ticketplus_wait_after_submit(tab, config_dict, debug)
        if outcome == 'queue':
            outcome = await _ticketplus_monitor_queue(tab, config_dict, debug)
        if outcome == 'confirmed':
            _ticketplus_queue_mark('送出流程收到確認頁結果', config_dict)
            await _ticketplus_process_confirmation(tab, tab.url or '', config_dict)
        if outcome == 'order_fail':
            _state.get('queue_trace', {})['closed'] = True
            _state.pop('submit_pending', None)
            _state['order_page_visited'] = False
            await reload_page_if_enabled(tab)
        return
    _state.pop('submit_pending', None)

    if _state.get("is_ticket_assigned", False):
        debug.log("Ticket selection completed, skipping duplicate execution")
        return

    # The selector already checks real ticket controls; layout/button reads
    # here were diagnostic only and duplicated browser round trips.
    area_keyword_raw = config_dict.get("area_auto_select", {}).get("area_keyword", "").strip()

    keyword_array = util.parse_keyword_string_to_array(area_keyword_raw)

    debug.log(f"[TicketPlus] Parsed keywords: {keyword_array}")
    debug.log(f"[TicketPlus] Total keyword groups: {len(keyword_array)}")

    need_select_ticket = True

    debug.log(f"Ticket selection is always required (TicketPlus quirk)")

    is_price_assign_by_bot = False
    keyword_matched = False

    if len(keyword_array) > 0:
        for keyword_index, area_keyword_item in enumerate(keyword_array):
            debug.log(f"[TicketPlus AREA KEYWORD] Trying keyword #{keyword_index + 1}/{len(keyword_array)}: '{area_keyword_item}'")

            is_price_assign_by_bot = await nodriver_ticketplus_unified_select(tab, config_dict, area_keyword_item)

            if is_price_assign_by_bot:
                keyword_matched = True
                debug.log(f"[TicketPlus AREA KEYWORD] Keyword #{keyword_index + 1} matched: '{area_keyword_item}' [OK]")
                break

            if _ticketplus_hold_unresolved_quantity(document_id):
                return
            debug.log(f"[TicketPlus AREA KEYWORD] Keyword #{keyword_index + 1} failed, trying next...")

        if not keyword_matched:
            debug.log(f"[TicketPlus AREA KEYWORD] All {len(keyword_array)} keywords failed to match")
            if config_dict.get('area_auto_fallback', False):
                is_price_assign_by_bot = await nodriver_ticketplus_unified_select(tab, config_dict, "")
    else:
        debug.log(f"[TicketPlus AREA KEYWORD] No keyword specified, using auto select mode")
        is_price_assign_by_bot = await nodriver_ticketplus_unified_select(tab, config_dict, "")

    is_need_refresh = not is_price_assign_by_bot

    if is_price_assign_by_bot:
        _ticketplus_timing('票數已確認', config_dict)
        if await _ticketplus_actions_paused(config_dict):
            return

        debug.log("Ticket selection successful, processing discount code and submit")

        is_answer_sent, _state["fail_list"], is_question_popup = await nodriver_ticketplus_order_exclusive_code(tab, config_dict, _state["fail_list"])
        _ticketplus_timing('資格欄位已處理', config_dict)

        if await _ticketplus_actions_paused(config_dict):
            return
        await nodriver_ticketplus_ticket_agree(tab, config_dict)
        _ticketplus_timing('條款已處理', config_dict)

        is_form_submitted = await nodriver_ticketplus_click_next_button_unified(tab, config_dict)

        if is_form_submitted:
            outcome = await _ticketplus_wait_after_submit(tab, config_dict, debug)

            if outcome == "order_fail":
                _state.get('queue_trace', {})['closed'] = True
                _state.pop('submit_pending', None)
                debug.log("[ORDER FAIL] Popup dismissed after submit, refreshing ticket availability")
                _state["order_page_visited"] = False
                try:
                    await reload_page_if_enabled(tab)
                except Exception as reload_exc:
                    debug.log(f"[ORDER FAIL] Reload failed: {reload_exc}")
                return

            if outcome == "queue":
                debug.log(f"進入排隊監控：頁面跳轉每 {_ticketplus_queue_poll_interval(config_dict):g} 秒檢查，失敗視窗每 0.5 秒檢查，完整排隊文字每 5～10 秒檢查。")
                queue_outcome = await _ticketplus_monitor_queue(tab, config_dict, debug)
                _ticketplus_queue_mark('排隊監控已返回送出流程', config_dict)
                if queue_outcome == 'confirmed':
                    await _ticketplus_process_confirmation(tab, tab.url or '', config_dict)

                # Mirror the direct order_fail path above: the popup is gone but the
                # ticket counts on screen are stale, so refresh before the next
                # attempt instead of submitting against them.
                if queue_outcome == "order_fail":
                    _state.get('queue_trace', {})['closed'] = True
                    _state.pop('submit_pending', None)
                    debug.log("[ORDER FAIL] Popup dismissed during queue monitoring, refreshing ticket availability")
                    _state["order_page_visited"] = False
                    try:
                        await reload_page_if_enabled(tab)
                    except Exception as reload_exc:
                        debug.log(f"[ORDER FAIL] Reload failed: {reload_exc}")
                    return

            elif outcome == 'confirmed':
                await _ticketplus_process_confirmation(tab, tab.url or '', config_dict)

        debug.log('選票下一步點擊已執行，訂單結果仍以網站確認為準。' if is_form_submitted else '本輪未點擊選票下一步。')
    else:
        debug.log("Ticket selection failed, cannot continue")
        if _ticketplus_hold_unresolved_quantity(document_id):
            return

        auto_reload_interval = config_dict["advanced"].get("auto_reload_page_interval", 0)
        if auto_reload_interval >= 0:
            if auto_reload_interval > 0:
                debug.log(f"[AUTO RELOAD] Waiting {auto_reload_interval} seconds before reload...")
                await sleep_before_auto_refresh(auto_reload_interval)
            debug.log("[AUTO RELOAD] Refreshing ticket count...")
            try:
                clicked = await _ticketplus_click_refresh_button(tab, debug)
                if not clicked:
                    await reload_page_if_enabled(tab)
                    debug.log("[AUTO RELOAD] Full page reload (button not found)")
            except Exception as reload_exc:
                debug.log(f"[AUTO RELOAD] Reload failed: {reload_exc}")

    debug.log('TicketPlus 本輪選票送出處理結束；尚未確認訂單或付款完成。')


async def nodriver_ticketplus_wait_for_vue_ready(tab, max_wait_ms=800):
    """Wait for Vue.js ticket area elements to render (dynamic detection).

    Args:
        tab: NoDriver tab
        max_wait_ms: Maximum wait time in milliseconds, default 800ms

    Returns:
        bool: True if Vue.js is ready, False if timed out
    """
    try:
        result = await tab.evaluate(f'''
            (function() {{
                return new Promise((resolve) => {{
                    const startTime = Date.now();
                    const maxWait = {max_wait_ms};

                    const check = () => {{
                        const selectors = [
                            '.v-expansion-panel-header',
                            '.order-content .v-btn',
                            'button.nextBtn',
                            '.ticket-list button'
                        ];

                        let hasContent = !!document.querySelector('.count-button .mdi-plus');
                        for (const selector of selectors) {{
                            if (hasContent) break;
                            const elements = document.querySelectorAll(selector);
                            if (elements.length > 0) {{
                                hasContent = Array.from(elements).some(el => {{
                                    const text = el.textContent || '';
                                    return text.includes('NT') ||
                                           text.includes('\u5269\u9918') ||
                                           text.includes('\u71b1\u8ce3') ||
                                           text.includes('\u4e0b\u4e00\u6b65') ||
                                           text.includes('\u552e\u5b8c');
                                }});
                                if (hasContent) break;
                            }}
                        }}

                        if (hasContent) {{
                            resolve({{ ready: true, elapsed: Date.now() - startTime,
                                documentId: location.href + '|' + performance.timeOrigin }});
                        }} else if (Date.now() - startTime < maxWait) {{
                            setTimeout(check, 30);
                        }} else {{
                            resolve({{ ready: false, elapsed: maxWait,
                                documentId: location.href + '|' + performance.timeOrigin }});
                        }}
                    }};

                    check();
                }});
            }})();
        ''', await_promise=True)

        if isinstance(result, dict):
            _state['readiness_document'] = result.get('documentId')
            return result.get('ready', False)
        return False

    except Exception as exc:
        return False


async def nodriver_ticketplus_check_next_button(tab):
    """Check if next button is enabled."""
    try:
        result = await tab.evaluate('''
            (function() {
                const selectors = [
                    "div.order-footer button.nextBtn",
                    "button.nextBtn",
                    "button[class*='next']",
                    ".order-footer .nextBtn"
                ];

                for (let selector of selectors) {
                    const btn = document.querySelector(selector);
                    if (btn) {
                        return {
                            found: true,
                            enabled: !btn.disabled && !btn.classList.contains('disabledBtn')
                        };
                    }
                }

                return { found: false, enabled: false };
            })();
        ''')

        result = util.parse_nodriver_result(result)
        return result.get('enabled', False) if isinstance(result, dict) else False

    except Exception as exc:
        return False


# The .exclusive-code container is mounted by Vue after a ticket count is
# chosen, so a scan straight after selection can arrive before it exists.
CONST_TICKETPLUS_EXCLUSIVE_CODE_WAIT = 0.6
CONST_TICKETPLUS_EXCLUSIVE_CODE_POLL = 0.05

# Wording that marks a card-verification box. Kept to Chinese: TicketPlus is a
# zh-TW site, and bare English "card" or "credit" match far too much.
CONST_TICKETPLUS_CARD_KEYWORDS = [
    "信用卡",
    "卡號",
    "卡友",
    "中國信託",
    "中信",
    "簽帳金融卡",
    "金融卡",
    "前六碼",
    "前6碼",
    "前八碼",
    "前8碼",
]

CONST_TICKETPLUS_CARD_PREFIX_LENGTHS = (
    ("前六碼", 6),
    ("前6碼", 6),
    ("前八碼", 8),
    ("前8碼", 8),
)

# Wording that marks a serial or discount box. "專屬代碼" is TicketPlus's own
# default wording, shown whenever the event sets no description of its own;
# "會員" covers member presale codes (issue #434).
CONST_TICKETPLUS_DISCOUNT_KEYWORDS = [
    "序號",
    "加購",
    "優惠",
    "折扣",
    "專屬代碼",
    "會員",
]

# Placeholder TicketPlus puts on the box when the event asks a question
# instead of a code. The answer is not a code, so nothing configured fits it.
CONST_TICKETPLUS_ANSWER_PLACEHOLDER = "答案選項"

# A card box is never called a serial number, but a card-branded discount
# campaign routinely is ("信用卡優惠序號"), so serial wording decides first.
CONST_TICKETPLUS_DISCOUNT_STRONG_KEYWORDS = [
    "序號",
    "加購",
]

CONST_TICKETPLUS_EXCLUSIVE_CODE_SCAN_JS = '''
    (function() {
        const fields = [];
        const containers = document.querySelectorAll('.exclusive-code');
        for (let i = 0; i < containers.length; i++) {
            const container = containers[i];
            const label = container.querySelector('.label') || container.querySelector('label');
            const input = container.querySelector('.v-text-field__slot input[type="text"]') ||
                          container.querySelector('input[type="text"]');
            if (!input) continue;
            fields.push({
                index: i,
                label: label ? (label.textContent || '').trim() : '',
                placeholder: input.getAttribute('placeholder') || '',
                value: input.value || '',
                maxLength: input.maxLength > 0 ? input.maxLength : 0
            });
        }
        return fields;
    })();
'''


def _normalize_ticketplus_field_text(label, placeholder):
    """Combine field text and remove whitespace before matching."""
    return "".join(f"{label} {placeholder}".split())


def _classify_exclusive_code_field(label, placeholder):
    """Decide which configured value belongs in an .exclusive-code input.

    Returns "credit_card", "discount", "answer" for a question box, or "" when
    the wording names neither. The container only ever holds the event's one
    serial box, so "" means wording we do not know, not a foreign field.
    """
    text = _normalize_ticketplus_field_text(label, placeholder)

    normalized_placeholder = _normalize_ticketplus_field_text("", placeholder)
    if CONST_TICKETPLUS_ANSWER_PLACEHOLDER in normalized_placeholder:
        return "answer"
    if any(keyword in text for keyword in CONST_TICKETPLUS_DISCOUNT_STRONG_KEYWORDS):
        return "discount"
    if any(keyword in text for keyword in CONST_TICKETPLUS_CARD_KEYWORDS):
        return "credit_card"
    if any(keyword in text for keyword in CONST_TICKETPLUS_DISCOUNT_KEYWORDS):
        return "discount"
    return ""


def _get_ticketplus_card_prefix_length(label, placeholder):
    """Return the required card-prefix length stated by the field wording."""
    text = _normalize_ticketplus_field_text(label, placeholder)
    for keyword, length in CONST_TICKETPLUS_CARD_PREFIX_LENGTHS:
        if keyword in text:
            return length
    return 0


def _ticketplus_exclusive_code_fill_js(plan):
    """Build the fill script for an already-decided list of {index, value}."""
    return f'''
        (function() {{
            {CONST_NATIVE_INPUT_SETTER_JS}
            const plan = {json.dumps(plan)};
            const containers = document.querySelectorAll('.exclusive-code');
            let filled = 0;
            for (const item of plan) {{
                const container = containers[item.index];
                if (!container) continue;
                const input = container.querySelector('.v-text-field__slot input[type="text"]') ||
                              container.querySelector('input[type="text"]');
                if (!input) continue;
                if (setNativeInputValue(input, item.value)) filled++;
            }}
            return filled;
        }})();
    '''


async def nodriver_ticketplus_order_exclusive_code(tab, config_dict, fail_list):
    """Fill the .exclusive-code inputs: a discount serial, or a card prefix.

    Cardholder-exclusive events (a bank's priority window, for instance) put a
    "card number prefix" box in the same .exclusive-code container
    that ordinary events use for an add-on serial number, so the two have to be
    told apart by their wording before anything is typed.
    """
    debug = util.create_debug_logger(config_dict)

    if await _ticketplus_actions_paused(config_dict):
        return False, fail_list, False

    credit_card_prefix = config_dict.get("contact", {}).get("credit_card_prefix", "").strip()
    discount_code = config_dict.get("advanced", {}).get("discount_code", "").strip()

    if not discount_code and not credit_card_prefix:
        debug.log("[EXCLUSIVE CODE] Neither a discount code nor a card prefix is configured, skipping")
        return False, fail_list, False

    values = {"discount": discount_code, "credit_card": credit_card_prefix}

    try:
        # The container is mounted by Vue once a ticket count is chosen, so it
        # may not be there yet. Poll from Python rather than from a timer in
        # the page: an un-awaited page-side promise reports nothing back and
        # keeps running after this call has returned.
        waited = 0.0
        fields = []
        while True:
            fields = await tab.evaluate(CONST_TICKETPLUS_EXCLUSIVE_CODE_SCAN_JS)
            if fields:
                break
            if waited >= CONST_TICKETPLUS_EXCLUSIVE_CODE_WAIT:
                break
            if await _ticketplus_sleep(tab, CONST_TICKETPLUS_EXCLUSIVE_CODE_POLL, config_dict):
                return False, fail_list, False
            waited += CONST_TICKETPLUS_EXCLUSIVE_CODE_POLL

        if not fields:
            debug.log("[EXCLUSIVE CODE] No code fields on the page")
            return False, fail_list, False

        plan = []
        for field in fields:
            label = field.get('label', '')
            placeholder = field.get('placeholder', '')
            kind = _classify_exclusive_code_field(label, placeholder)

            if kind == "answer":
                debug.log(f"[EXCLUSIVE CODE] Question box, not a code; leaving it alone (label='{label}')")
                continue

            if not kind:
                # The box is required, so leaving it empty keeps the next button
                # disabled. With only a discount code set there is one candidate;
                # a card prefix, shared with ibon, is not typed into unknown wording.
                if discount_code and not credit_card_prefix:
                    kind = "discount"
                    debug.log(f"[EXCLUSIVE CODE] Unrecognized wording, filling the discount code "
                              f"(label='{label}', placeholder='{placeholder}')")
                else:
                    debug.log(f"[EXCLUSIVE CODE] Unrecognized field, leaving it alone "
                              f"(label='{label}', placeholder='{placeholder}')")
                    continue

            value = values.get(kind, '')
            if not value:
                debug.log(f"[EXCLUSIVE CODE] {kind} field found but nothing configured for it")
                continue

            configured_value = value
            if kind == "credit_card":
                required_length = _get_ticketplus_card_prefix_length(label, placeholder)
                if required_length and len(value) > required_length:
                    value = value[:required_length]
                    debug.log(f"[EXCLUSIVE CODE] Card field wording requires {required_length} "
                              f"digits; using the configured prefix truncated from "
                              f"{len(configured_value)} digits")

            existing = field.get('value', '')
            if existing:
                if existing == value:
                    debug.log(f"[EXCLUSIVE CODE] {kind} field already holds the configured value")
                else:
                    debug.log(f"[EXCLUSIVE CODE] {kind} field already filled, not overwriting")
                continue

            max_length = field.get('maxLength', 0)
            if max_length and len(value) > max_length:
                debug.log(f"[EXCLUSIVE CODE] {kind} field takes {max_length} characters "
                          f"but the configured value is {len(value)}; leaving it alone")
                continue

            plan.append({"index": field.get('index'), "value": value, "kind": kind})

        if not plan:
            debug.log("[EXCLUSIVE CODE] Nothing to fill")
            return False, fail_list, False

        filled = await tab.evaluate(_ticketplus_exclusive_code_fill_js(plan))
        if filled:
            kinds = ", ".join(sorted({item["kind"] for item in plan}))
            debug.log(f"[EXCLUSIVE CODE] Filled {filled} field(s): {kinds}")
            return True, fail_list, False

        debug.log("[EXCLUSIVE CODE] Fill did not take effect")
        return False, fail_list, False

    except Exception as e:
        debug.log(f"[EXCLUSIVE CODE] Error filling code fields: {str(e)}")
        return False, fail_list, False


async def nodriver_ticketplus_main(tab, url, config_dict, ocr, Captcha_Browser):
    """TicketPlus main entry point.

    Returns:
        dict: selection stage and manual-handoff readiness; never claims payment success.
    """
    if await _ticketplus_actions_paused(config_dict):
        return _get_status()

    debug = util.create_debug_logger(config_dict)

    if not _state:
        _ticketplus_reset_runtime_state()

    if _state.get('handoff_ready'):
        return _get_status()
    route = _ticketplus_confirmation_route(url)
    if route:
        await _ticketplus_process_confirmation(tab, url, config_dict)
        return _get_status()
    _state.pop('seat_next_pending', None)
    _state['stage'] = 'selecting'

    home_url = 'https://ticketplus.com.tw/'
    is_user_signin = False
    if home_url == url.lower():
        if config_dict["ocr_captcha"]["enable"]:
            domain_name = url.split('/')[2]
            if not Captcha_Browser is None:
                Captcha_Browser.set_domain(domain_name)

        is_user_signin = await nodriver_ticketplus_account_auto_fill(tab, config_dict)

    if is_user_signin:
        config_homepage = config_dict["homepage"].lower().rstrip('/')
        is_homepage_target = config_homepage in ['https://ticketplus.com.tw', 'ticketplus.com.tw']
        if not is_homepage_target and url.lower() != config_dict["homepage"].lower():
            try:
                await navigate_page_if_enabled(tab, config_dict["homepage"])
            except Exception as e:
                pass

    # https://ticketplus.com.tw/activity/XXX
    if '/activity/' in url.lower():
        is_event_page = False
        if _ticketplus_path_segment_count(url)==5:
            is_event_page = True

        if is_event_page:
            _state["is_popup_confirm"] = False
            _state["order_page_visited"] = False

            is_button_pressed = await nodriver_ticketplus_accept_realname_card(tab)
            debug.log(f"[TICKETPLUS] Realname Card: {is_button_pressed}")

            is_button_pressed = await nodriver_ticketplus_accept_other_activity(tab)
            debug.log(f"[TICKETPLUS] Other Activity: {is_button_pressed}")

            if config_dict["date_auto_select"]["enable"]:
                await nodriver_ticketplus_date_auto_select(tab, config_dict)

    # https://ticketplus.com.tw/order/XXX/OOO
    if '/order/' in url.lower():
        is_event_page = False
        if _ticketplus_path_segment_count(url)==6:
            is_event_page = True

        if is_event_page:
            cycle_started = time.monotonic()
            _state["start_time"] = time.time()

            is_first_visit = not _state.get("order_page_visited", False)
            if is_first_visit:
                max_wait = 2000
                _state["order_page_visited"] = True
            else:
                max_wait = 1000

            if debug.enabled:
                visit_type = "First visit" if is_first_visit else "Reload"
                debug.log(f"[VUE INIT] {visit_type}, dynamic detection (max {max_wait}ms)...")

            is_ready = await nodriver_ticketplus_wait_for_vue_ready(tab, max_wait_ms=max_wait)
            document_id = _state.pop('readiness_document', None)
            if document_id and _state.get('cycle_document') != document_id:
                _state.update(cycle_document=document_id, cycle_started_at=cycle_started)

            debug.log(f"[VUE INIT] Vue.js ready: {is_ready}")

            if not is_ready:
                return _get_status()
            _state['cycle_ready_at'] = time.monotonic()

            is_button_pressed = await nodriver_ticketplus_accept_realname_card(tab)
            is_order_fail_handled = await nodriver_ticketplus_accept_order_fail(tab, debug)

            if is_order_fail_handled is True:
                _state.pop('submit_pending', None)
                debug.log("[ORDER FAIL] Reloading page to refresh ticket availability")
                try:
                    await reload_page_if_enabled(tab)
                    await asyncio.sleep(0.5)
                except Exception as reload_exc:
                    debug.log(f"[ORDER FAIL] Reload failed: {reload_exc}")
                _state["order_page_visited"] = False
                return _get_status()
            elif is_order_fail_handled is None:
                debug.log("[ORDER FAIL][DEGRADED] Detection unavailable, skipping order submission this cycle")
                return _get_status()

            await nodriver_ticketplus_order(tab, config_dict, ocr, Captcha_Browser)

    else:
        _state["fail_list"] = []
        _state["is_ticket_assigned"] = False
        _state["start_time"] = None

    return _get_status()
