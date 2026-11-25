"""TixCraft HTTP response parsers and browser transport; no platform/run state."""
import base64
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from zendriver import cdp

CONST_HTML_LIMIT = 8 * 1024 * 1024
CONST_SOLD_WORDS = ('已售完', '選購一空', 'sold out', 'no tickets available', '完売', '空席なし')


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'wbr'):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_document(html):
    parser = DocumentParser()
    parser.feed(html)
    return parser.root


def descendants(node, tag=None):
    for child in node.children:
        if isinstance(child, Node):
            if tag is None or child.tag == tag:
                yield child
            yield from descendants(child, tag)


def text(node):
    def pieces(item):
        for child in item.children:
            if isinstance(child, str):
                yield child
            elif child.tag not in ('script', 'style'):
                yield from pieces(child)
    return ' '.join(' '.join(pieces(node)).split())


def ticket_url(value, base, kind, event=None, session=None):
    if not isinstance(value, str):
        return None
    target, origin = urlsplit(urljoin(base, value)), urlsplit(base)
    parts = target.path.strip('/').split('/')
    if ((target.scheme, target.netloc) != (origin.scheme, origin.netloc) or target.username or target.password or
            len(parts) < 4 or parts[:2] != ['ticket', kind] or target.fragment or
            event is not None and parts[2] != event or session is not None and parts[3] != session):
        return None
    return target.geturl()


def parse_sessions(html, base, event):
    doc = parse_document(html)
    containers = [node for node in descendants(doc) if node.attrs.get('id') == 'gameList']
    if len(containers) != 1:
        return None
    result = []
    for row in descendants(containers[0], 'tr'):
        label = text(row)
        for button in descendants(row):
            href = button.attrs.get('data-href') or (button.attrs.get('href') if button.tag == 'a' else None)
            target = ticket_url(href, base, 'area', event)
            if target:
                result.append({'name': label, 'url': target, 'sold': any(word in label.casefold() for word in CONST_SOLD_WORDS)})
                break
    return result


def parse_areas(html, base, event, session):
    doc = parse_document(html)
    maps = []
    for script in descendants(doc, 'script'):
        source = ''.join(child for child in script.children if isinstance(child, str))
        match = re.search(r'\bareaUrlList\s*=\s*', source)
        if match:
            try:
                mapping, _ = json.JSONDecoder().raw_decode(source[match.end():])
                if mapping == []:
                    mapping = {}
                if isinstance(mapping, dict):
                    maps.append(mapping)
            except ValueError:
                pass
    if len(maps) != 1:
        return None
    result = []
    for anchor in descendants(doc, 'a'):
        target = ticket_url(maps[0].get(anchor.attrs.get('id')), base, 'ticket', event, session)
        if not target:
            continue
        label = text(anchor)
        count = re.search(r'(?:剩餘\s*(\d+)|(\d+)\s*(?:seat|ticket)\(s\)?\s*remaining)', label, re.I)
        result.append({'name': label, 'url': target,
                       'count': int(next(group for group in count.groups() if group)) if count else None,
                       'sold': any(word in label.casefold() for word in CONST_SOLD_WORDS)})
    return result


def date_matches(keyword, label):
    dates = [(int(y) if y else None, int(m), int(d)) for y, m, d in
             re.findall(r'(?<!\d)(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})(?!\d)', label)]
    for word in keyword.casefold().split():
        match = re.fullmatch(r'(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})', word)
        if match:
            year, month, day = match.groups()
            if not any((not year or y == int(year)) and m == int(month) and d == int(day) for y, m, d in dates):
                return False
        elif word not in label.casefold():
            return False
    return True


def check_result(value, base):
    """Only accept the captured boolean/wait contract; never execute message scripts."""
    if not isinstance(value, dict) or type(value.get('waiting')) is not bool:
        return None
    delay = value.get('time')
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay < float('inf'):
        return None
    if value['waiting']:
        return {'state': 'waiting', 'delay': delay}
    message = value.get('message', '')
    match = re.search(r'location\.replace\(\s*["\']([^"\']+)["\']\s*\)', message) if isinstance(message, str) else None
    if match:
        target, origin = urlsplit(urljoin(base, match[1])), urlsplit(base)
        if ((target.scheme, target.netloc) == (origin.scheme, origin.netloc) and
                target.path.rstrip('/') == '/ticket/checkout' and not target.query and not target.fragment):
            return {'state': 'ready', 'url': target.geturl()}
    return None


async def page_identity(tab):
    frame = (await tab.send(cdp.page.get_frame_tree())).frame
    fragment = getattr(frame, 'url_fragment', None) or ''
    return frame.url + fragment, str(frame.loader_id), frame.id_


async def current_html(tab):
    """Read the document after a user's F5 without sending another HTTP GET."""
    root = await tab.send(cdp.dom.get_document(depth=0))
    html = await tab.send(cdp.dom.get_outer_html(node_id=root.node_id))
    return (200, html) if len(html.encode('utf8')) <= CONST_HTML_LIMIT else (200, None)


async def read_html(tab, url, frame_id):
    """Same Chrome cookie context, no JavaScript, DOM rendering or external cookie copy."""
    resource = await tab.send(cdp.network.load_network_resource(
        url, cdp.network.LoadNetworkResourceOptions(disable_cache=True, include_credentials=True), frame_id=frame_id))
    stream, chunks, length = resource.stream, [], 0
    try:
        if not resource.success or resource.http_status_code != 200 or stream is None:
            return int(resource.http_status_code or 0), None
        while True:
            encoded, data, eof = await tab.send(cdp.io.read(stream, size=256 * 1024))
            chunk = base64.b64decode(data) if encoded else data.encode('utf8')
            length += len(chunk)
            if length > CONST_HTML_LIMIT:
                return 200, None
            chunks.append(chunk)
            if eof:
                return 200, b''.join(chunks).decode('utf8', errors='replace')
    finally:
        if stream is not None:
            await tab.send(cdp.io.close(stream))


CONST_SNAPSHOT_JS = r'''(() => {
    RESET_LOGIC
    const reset=resetSubmission(RESET_DATA);
    CAPTCHA_LOGIC
    const doc=location.href+'|'+performance.timeOrigin,key='ticketsHunter.tixcraft.submit';
    if((typeof action!=='undefined'&&action==='block'&&typeof rr!=='undefined')||
        /^(?:Pardon Our Interruption|Access Denied|Just a moment)/i.test(document.title))
        return {document_id:doc,blocked:true};
    const active=window[Symbol.for(key)];
    let saved=null,corrupt=false;try{saved=JSON.parse(sessionStorage.getItem(key)||'null');}catch(_){corrupt=true;}
    const pending=active||saved;
    if(pending||corrupt)return {document_id:doc,pending:true,corrupt,released:reset.cleared,
        pending_state:pending?.state,pending_nonce:pending?.nonce,
        api:pending?.transport==='http'?{nonce:pending.nonce,state:pending.state,route:pending.route,
            instance_id:pending.instanceId,
            request_ms:pending.request_ms,status:pending.status,check:pending.check,check_ms:pending.check_ms,
            active:!!active,check_inflight:!!pending.check_inflight,check_seq:pending.check_seq||0,check_due_ms:pending.check_due_ms||0,
            browser_now_ms:performance.timeOrigin+performance.now()}:null};
    const form=document.querySelector('#form-ticket-ticket');
    if(!form)return {document_id:doc,rows:[],supported:false,released:reset.cleared};
    const preparedSlot=Symbol.for(key+'.prepared');
    let prepared=window[preparedSlot];
    if(!prepared||prepared.document!==doc||prepared.form!==form||!prepared.button?.isConnected||prepared.button.form!==form){
        prepared={document:doc,form,button:form.querySelector('button[type="submit"],button:not([type]),input[type="submit"]')};
        window[preparedSlot]=prepared;
    }
    const rows=Array.from(form.querySelectorAll('select[id^="TicketForm_ticketPrice_"]')).map(s=>{
        const row=s.closest('tr'),label=row?.querySelector('h4')||row?.querySelector('td.fcBlue');
        const validValue=s.value===''||/^\d+$/.test(s.value);
        return {id:s.id,name:(label?.textContent||'').trim(),value:validValue?Number(s.value):null,
            validValue,disabled:s.matches(':disabled'),options:Array.from(s.options).filter(o=>
                !o.disabled&&!o.closest('optgroup')?.disabled&&/^\d+$/.test(o.value)&&Number(o.value)>0).map(o=>Number(o.value))};
    });
    const verify=form.querySelector('#TicketForm_verifyCode'),image=document.querySelector('#TicketForm_verifyCode-image,img[src*="/ticket/captcha"]');
    let imageData=null;
    if(NEED_IMAGE&&verify&&image?.complete&&image.naturalWidth){
        try{const c=document.createElement('canvas');c.width=image.naturalWidth;c.height=image.naturalHeight;
            c.getContext('2d').drawImage(image,0,0);imageData=c.toDataURL('image/png').split(',')[1];}catch(_){}
    }
    return {supported:true,document_id:doc,rows,ready:document.readyState,has_captcha:!!verify,captcha:captchaId(),image:imageData,
        captcha_filled:!!verify&&verify.value.length===4,captcha_ready:!verify||!!image?.complete&&!!image.naturalWidth,released:reset.cleared};
})()'''


CONST_FORM_PREPARE_JS = r'''(() => {
    const p=PARAMETERS,key='ticketsHunter.tixcraft.submit',slot=Symbol.for(key);
    if(Date.now()>p.expires)return {state:'expired'};
    CAPTCHA_LOGIC
    const doc=location.href+'|'+performance.timeOrigin;
    if(doc!==p.document||captchaId()!==p.captcha)return {state:'changed'};
    if(window[slot]||sessionStorage.getItem(key))return {state:'pending'};
    const form=document.querySelector('#form-ticket-ticket');
    if(!form)return {state:'not_ready'};
    const action=new URL(form.getAttribute('action')||location.href,location.href);
    if(action.origin!==location.origin||action.pathname!==location.pathname||!/^\/ticket\/ticket\//.test(action.pathname))return {state:'unsupported'};
    const rows=Array.from(form.querySelectorAll('select[id^="TicketForm_ticketPrice_"]'));
    const chosen=rows.filter(s=>s.id===p.id);
    if(chosen.length!==1||new Set(rows.map(s=>s.id)).size!==rows.length||chosen[0].matches(':disabled')||
        !Number.isInteger(p.count)||p.count<=0||p.count>p.target||!p.allow_less&&p.count!==p.target)return {state:'not_ready'};
    const select=chosen[0];
    if(!Array.from(select.options).some(o=>o.value===String(p.count)&&!o.disabled&&!o.closest('optgroup')?.disabled))return {state:'not_ready'};
    const agree=form.querySelector('#TicketForm_agree'),verify=form.querySelector('#TicketForm_verifyCode');
    if(!agree||agree.disabled||p.require_captcha&&!verify)return {state:'not_ready'};
    const names=rows.map(s=>s.name);
    if(names.some(n=>!/^TicketForm\[ticketPrice\]\[[^\]]+\]$/.test(n))||new Set(names).size!==names.length)return {state:'not_ready'};
    // Prepare through the website's regular field events, then read back before clicking.
    for(const row of rows){
        if(row.matches(':disabled'))continue;
        const value=row===select?String(p.count):'0';
        if(row!==select&&!Array.from(row.options).some(o=>o.value==='0')){
            if(row.value!=='')return {state:'not_ready'};
            continue;
        }
        if(row.value!==value){row.value=value;row.dispatchEvent(new Event('input',{bubbles:true}));row.dispatchEvent(new Event('change',{bubbles:true}));}
    }
    if(!agree.checked)agree.click();
    if(!agree.checked)return {state:'not_ready'};
    if(verify&&p.answer!==null){verify.value=p.answer;verify.dispatchEvent(new Event('input',{bubbles:true}));verify.dispatchEvent(new Event('change',{bubbles:true}));}
    if(!p.submit)return {state:'prepared',count:p.count};
    if(p.require_captcha&&(!verify||verify.disabled||verify.value.length!==4)||!form.checkValidity())return {state:'not_ready'};
    if(form.querySelector('button[type="submit"]:disabled,input[type="submit"]:disabled'))return {state:'not_ready'};
    const body=new URLSearchParams(new FormData(form));
    if(!body.get('_csrf'))return {state:'unsupported'};
    body.set('TicketForm[agree]','1');
    const quantities=Array.from(body.entries()).filter(([n])=>/^TicketForm\[ticketPrice\]\[/.test(n));
    if(quantities.length!==rows.filter(s=>!s.matches(':disabled')&&s.selectedOptions.length>0).length||quantities.some(([n,v])=>!(v===''||/^\d+$/.test(v))||Number(v)!==(n===select.name?p.count:0)))return {state:'not_ready'};
    if(Date.now()>p.expires)return {state:'expired'};
'''


CONST_SEND_JS = CONST_FORM_PREPARE_JS + r'''
    const prepared=window[Symbol.for(key+'.prepared')];
    const button=prepared?.document===doc&&prepared.form===form&&prepared.button?.isConnected&&prepared.button.form===form
        ?prepared.button:form.querySelector('button[type="submit"],button:not([type]),input[type="submit"]');
    if(!button||button.form!==form||button.matches(':disabled')||!button.getClientRects().length)return {state:'not_ready'};
    const style=getComputedStyle(button);
    if(button.getAttribute('aria-disabled')==='true'||button.classList.contains('disabled')||
        style.visibility==='hidden'||style.pointerEvents==='none')return {state:'not_ready'};
    window[Symbol.for(key+'.prepared')]={document:doc,form,button};
    if(doc!==location.href+'|'+performance.timeOrigin||captchaId()!==p.captcha)return {state:'changed'};
    if(Date.now()>p.expires)return {state:'expired'};
    const nonce=globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36);
    const record={documentId:doc,route:location.href,nonce,state:'invoked',transport:'native-button',
        captcha:p.captcha,resetToken:p.generation,instanceId:p.instance,invoking:false};
    try{sessionStorage.setItem(key,JSON.stringify({documentId:doc,route:record.route,nonce,state:record.state,
        transport:record.transport,resetToken:p.generation,instanceId:p.instance}));}
    catch(_){return {state:'not_ready'};}
    window[slot]=record;
    if(!form[Symbol.for(key+'.clickGuard')]){
        form.addEventListener('click',event=>{
            const current=window[slot],target=event.target.closest?.('button,input[type="submit"]');
            if(current?.transport==='native-button'&&!current.invoking&&event.isTrusted&&target?.form===form&&
                (target.type==='submit')&&current.state!=='rejected'){
                event.preventDefault();event.stopImmediatePropagation();
            }
        },true);
        form[Symbol.for(key+'.clickGuard')]=true;
    }
    // No fetch, synthetic form submit, or polling: the original click/submit
    // handlers and the resulting document navigation belong to the website.
    record.invoking=true;
    try{button.click();return {state:'invoked',nonce,count:p.count};}
    catch(_){return {state:'unknown'};}
    finally{record.invoking=false;}
})()'''


# Experimental transport shares all document, field, challenge and payload checks.
CONST_EXPERIMENTAL_SEND_JS = CONST_FORM_PREPARE_JS + r'''
    const nonce=globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36);
    const record={documentId:doc,route:location.href,nonce,state:'posting',transport:'http',captcha:p.captcha,
        resetToken:p.generation,instanceId:p.instance};
    const save=()=>{if(window[slot]===record)sessionStorage.setItem(key,JSON.stringify({documentId:doc,route:record.route,nonce,
        state:record.state,transport:'http',resetToken:p.generation,instanceId:p.instance,request_ms:record.request_ms,status:record.status}));};
    try{sessionStorage.setItem(key,JSON.stringify(record));}catch(_){return {state:'not_ready'};}
    window[slot]=record;
    if(!form[Symbol.for(key+'.httpGuard')]){
        form.addEventListener('submit',event=>{const r=window[slot];if(r?.transport==='http'&&r.state!=='rejected'){
            event.preventDefault();event.stopImmediatePropagation();}},true);
        form[Symbol.for(key+'.httpGuard')]=true;
    }
    const started=performance.now();
    record.promise=(async()=>{
        try{
            const response=await fetch(action.href,{method:'POST',credentials:'same-origin',redirect:'follow',
                headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body.toString()});
            const html=await response.text();
            if(window[slot]!==record||location.href+'|'+performance.timeOrigin!==doc)return;
            record.status=response.status;record.request_ms=performance.now()-started;
            const target=new URL(response.url);
            if(response.status===200&&target.origin===location.origin&&target.pathname==='/ticket/order')record.state='queued';
            else if(response.status===200&&target.origin===location.origin&&target.pathname==='/ticket/checkout')record.state='checkout';
            else {
                record.state='unknown';
                if(response.status===200&&target.origin===location.origin&&target.pathname===action.pathname){
                    const parsed=new DOMParser().parseFromString(html,'text/html');
                    const errors=Array.from(parsed.querySelectorAll('script')).map(s=>s.textContent.match(/^\s*(?:window\.)?alert\(\s*(["'])([\s\S]*?)\1\s*\)\s*;?\s*$/)).filter(Boolean).map(m=>
                        m[2].replace(/\\u([0-9a-f]{4})/gi,(_,n)=>String.fromCharCode(parseInt(n,16)))).join(' ');
                    if(/(?:驗證碼(?:錯誤|不正確|輸入錯誤)|(?:incorrect|invalid|wrong)\s+(?:verification code|captcha))/i.test(errors))record.state='rejected';
                }
            }
            save();
        }catch(_){if(window[slot]===record){record.state='unknown';save();}}
    })();
    return {state:'invoked',nonce,count:p.count};
})()'''


CONST_CHECK_JS = r'''(() => {
    const p=PARAMETERS,key='ticketsHunter.tixcraft.submit',slot=Symbol.for(key);
    if(Date.now()>p.expires)return false;
    let r=window[slot];
    if(!r){try{r=JSON.parse(sessionStorage.getItem(key)||'null');}catch(_){};
        if(r?.transport==='http'&&r.nonce===p.nonce&&r.route===location.href)window[slot]=r;}
    if(!r||r.nonce!==p.nonce||r.transport!=='http'||r.state!=='queued'||r.check_inflight)return false;
    r.check=null;r.check_inflight=true;r.check_seq=(r.check_seq||0)+1;
    const started=performance.now();
    r.check_promise=(async()=>{
        try{
            const response=await fetch('/ticket/check',{credentials:'same-origin',cache:'no-store',redirect:'error',
                headers:{'Accept':'application/json, text/javascript, */*; q=0.01','X-Requested-With':'XMLHttpRequest'}});
            const result=await response.json();if(window[slot]!==r)return;
            r.check=response.status===200?result:{error:true};r.check_ms=performance.now()-started;
            if(response.status===200&&result?.waiting===true&&typeof result.time==='number'&&Number.isFinite(result.time)&&result.time>=0){
                r.check_due_ms=performance.timeOrigin+performance.now()+result.time*1000;
                const saved=JSON.parse(sessionStorage.getItem(key)||'null');
                if(saved?.nonce===r.nonce){saved.check_due_ms=r.check_due_ms;sessionStorage.setItem(key,JSON.stringify(saved));}
            }
        }catch(_){if(window[slot]===r)r.check={error:true};}
        finally{if(window[slot]===r)r.check_inflight=false;}
    })();return true;
})()'''
