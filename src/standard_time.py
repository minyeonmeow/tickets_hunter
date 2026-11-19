# encoding=utf-8
"""共用官方時間來源：背景校時、單調時鐘推進，不變更作業系統時間。"""
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


DEFAULT_SERVERS = ('time.stdtime.gov.tw', 'clock.stdtime.gov.tw')
NTP_PORT = 123
NTP_EPOCH = 2208988800
NTP_ERA = 4294967296
OFFICIAL_TIME_URL = 'https://www.stdtime.gov.tw/Home/GetServerTime'
TAIWAN_TIMEZONE = timezone(timedelta(hours=8))
MIN_FORCE_INTERVAL = 5.0
WALL_JUMP_TOLERANCE = 0.1
_CLOCKS = {}
_CLOCKS_LOCK = threading.Lock()
_NETWORK_LOCK = threading.Lock()
_ERROR_MESSAGES = {
    'dns': '無法解析校時伺服器。', 'timeout': 'NTP 校時逾時。',
    'network': 'NTP 校時連線失敗。', 'packet': 'NTP 回覆格式不正確。',
    'unsynced': 'NTP 伺服器尚未同步或拒絕校時。', 'origin': 'NTP 回覆與本次請求不符。',
    'timestamp': 'NTP 回覆時間無效。', 'timing': 'NTP 回覆往返時間不合理。',
    'jump': '校時期間本機時間跳動，本次樣本已捨棄。',
    'https': '官方 HTTPS 時間備援失敗。', 'https_data': '官方 HTTPS 時間格式不正確。',
    'thread': '無法啟動背景校時。', 'unknown': '官方校時未成功，稍後會再試。',
}


class _SyncError(Exception):
    pass


def _counter():
    # Windows 的 monotonic 可能只有 15.625ms 解析度；perf_counter 同樣單調且更精細。
    return time.perf_counter()


def _number(value, default, minimum, maximum):
    try:
        result = float(value) if not isinstance(value, bool) else default
    except (TypeError, ValueError):
        return default
    return result if minimum <= result <= maximum else default


def _options(config_dict):
    advanced = config_dict.get('advanced', {}) if isinstance(config_dict, dict) else {}
    if not isinstance(advanced, dict):
        advanced = {}
    candidates = advanced.get('standard_time_servers', DEFAULT_SERVERS)
    servers = []
    if isinstance(candidates, (list, tuple)):
        for value in candidates:
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}', value.strip()):
                value = value.strip().lower()
                if value not in servers:
                    servers.append(value)
    return {
        'enabled': advanced.get('standard_time_enabled', True) is not False,
        'servers': tuple(servers[:4]) or DEFAULT_SERVERS,
        'timeout': _number(advanced.get('standard_time_timeout', 1), 1.0, 0.01, 10),
        'sync_interval': _number(advanced.get('standard_time_sync_interval', 300), 300.0, 30, 86400),
        'max_age': _number(advanced.get('standard_time_max_age', 900), 900.0, 0.01, 86400),
        'retry_interval': _number(advanced.get('standard_time_retry_interval', 30), 30.0, 5, 3600),
        'https_fallback': advanced.get('standard_time_https_fallback', True) is not False,
    }


def _timestamp(raw, reference):
    seconds, fraction = struct.unpack('!II', raw)
    if seconds == 0 and fraction == 0:
        raise _SyncError('timestamp')
    epoch = seconds - NTP_EPOCH + fraction / NTP_ERA
    # NTP 2036 年會換 era；以最近的 era 解讀，容許本機有一般日期／時區誤差。
    return epoch + round((reference - epoch) / NTP_ERA) * NTP_ERA


def _validate_elapsed(start_wall, end_wall, elapsed, timeout):
    if not 0 < elapsed < float('inf') or elapsed > timeout + WALL_JUMP_TOLERANCE:
        raise _SyncError('timing')
    if abs((end_wall - start_wall) - elapsed) > WALL_JUMP_TOLERANCE:
        raise _SyncError('jump')


def _decode_ntp(data, transmitted, start_wall, end_wall, start_counter, end_counter, timeout, server):
    if len(data) != 48:
        raise _SyncError('packet')
    leap, version, mode = data[0] >> 6, (data[0] >> 3) & 7, data[0] & 7
    if version not in (3, 4) or mode != 4:
        raise _SyncError('packet')
    if leap == 3 or not 1 <= data[1] <= 15:
        raise _SyncError('unsynced')
    if data[24:32] != transmitted:
        raise _SyncError('origin')
    elapsed = end_counter - start_counter
    _validate_elapsed(start_wall, end_wall, elapsed, timeout)
    received = _timestamp(data[32:40], start_wall)
    sent = _timestamp(data[40:48], start_wall)
    processing = sent - received
    if processing < 0 or processing > elapsed + 0.000005:
        raise _SyncError('timing')
    rtt = max(0.0, elapsed - processing)
    dispersion = struct.unpack('!I', data[8:12])[0] / 65536
    if dispersion > 16:
        raise _SyncError('timing')
    # 直接以伺服器時間加半個網路往返推進，不把低解析度本機 wall time 加回去。
    return {'epoch': sent + rtt / 2, 'counter': end_counter, 'rtt': rtt,
            'uncertainty': rtt / 2 + dispersion, 'server': server,
            'protocol': 'ntp', 'estimated': False}


def _probe_ntp(server, timeout):
    try:
        addresses = socket.getaddrinfo(server, NTP_PORT, type=socket.SOCK_DGRAM, proto=socket.IPPROTO_UDP)
    except OSError:
        raise _SyncError('dns') from None
    error = 'network'
    # 同一伺服器最多嘗試兩個位址；已連接 UDP socket 只接收該位址的封包。
    for family, kind, protocol, _, address in addresses[:2]:
        try:
            with socket.socket(family, kind, protocol) as connection:
                connection.settimeout(timeout)
                connection.connect(address)
                start_counter, start_wall = _counter(), time.time()
                seconds = int(start_wall + NTP_EPOCH) & 0xffffffff
                transmitted = struct.pack('!I', seconds) + os.urandom(4)
                packet = bytearray(48)
                packet[0], packet[40:48] = 0x23, transmitted
                connection.send(packet)
                data = connection.recv(512)
                end_counter, end_wall = _counter(), time.time()
                return _decode_ntp(data, transmitted, start_wall, end_wall,
                                   start_counter, end_counter, timeout, server)
        except _SyncError:
            raise
        except socket.timeout:
            error = 'timeout'
        except OSError:
            error = 'network'
    raise _SyncError(error)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _parse_official_https(raw):
    try:
        value = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value, str) or not re.fullmatch(
                r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?\+08:00', value):
            raise ValueError
        value = re.sub(r'(\.\d{6})\d+(?=\+08:00)', r'\1', value)
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, UnicodeError, OverflowError):
        raise _SyncError('https_data') from None


def _probe_https(timeout):
    request = urllib.request.Request(OFFICIAL_TIME_URL, headers={
        'Accept': 'application/json', 'Cache-Control': 'no-cache'})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        start_counter, start_wall = _counter(), time.time()
        with opener.open(request, timeout=timeout) as response:
            url = urlsplit(response.geturl())
            if (response.status != 200 or url.scheme != 'https' or url.hostname != 'www.stdtime.gov.tw'
                    or url.path != '/Home/GetServerTime' or float(response.headers.get('Age', '0')) != 0):
                raise _SyncError('https_data')
            raw = response.read(4097)
        end_counter, end_wall = _counter(), time.time()
        if len(raw) > 4096:
            raise _SyncError('https_data')
        elapsed = end_counter - start_counter
        _validate_elapsed(start_wall, end_wall, elapsed, timeout)
        epoch = _parse_official_https(raw)
        # HTTPS 沒有四時間戳，半 RTT 只是一個估計；不可聲稱具有 NTP 的精度。
        return {'epoch': epoch + elapsed / 2, 'counter': end_counter, 'rtt': elapsed,
                'uncertainty': elapsed / 2, 'server': 'www.stdtime.gov.tw',
                'protocol': 'https', 'estimated': True}
    except _SyncError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise _SyncError('https') from None


class _Clock:
    def __init__(self, options):
        self.options = options
        self.lock = threading.Lock()
        self.sample = None
        self.running = False
        self.last_attempt = None
        self.error = ''
        self.thread = None

    def start(self, force=False):
        with self.lock:
            now = _counter()
            interval = (MIN_FORCE_INTERVAL if force else self.options[
                'retry_interval' if self.error else 'sync_interval'])
            if self.running or (self.last_attempt is not None and now - self.last_attempt < interval):
                return False
            if not _NETWORK_LOCK.acquire(blocking=False):
                return False
            self.running, self.last_attempt = True, now
            self.thread = threading.Thread(target=self._sync, name='TicketsHunter-standard-time', daemon=True)
            try:
                self.thread.start()
            except RuntimeError:
                self.running, self.error = False, _ERROR_MESSAGES['thread']
                _NETWORK_LOCK.release()
                return False
            return True

    def _sync(self):
        samples, error = [], 'unknown'
        try:
            for server in self.options['servers']:
                try:
                    samples.append(_probe_ntp(server, self.options['timeout']))
                except _SyncError as exc:
                    error = exc.args[0] if exc.args else 'unknown'
            if not samples and self.options['https_fallback']:
                try:
                    samples.append(_probe_https(self.options['timeout']))
                except _SyncError as exc:
                    error = exc.args[0] if exc.args else 'unknown'
            with self.lock:
                if samples:
                    self.sample = min(samples, key=lambda sample: sample['rtt'])
                    self.error = ''
                else:
                    self.error = _ERROR_MESSAGES.get(error, _ERROR_MESSAGES['unknown'])
        except Exception:
            with self.lock:
                self.error = _ERROR_MESSAGES['unknown']
        finally:
            with self.lock:
                self.running = False
            _NETWORK_LOCK.release()

    def snapshot(self):
        with self.lock:
            now, local_epoch = _counter(), time.time()
            local_offset = datetime.fromtimestamp(local_epoch).astimezone().utcoffset()
            age = max(0.0, now - self.sample['counter']) if self.sample else None
            fresh = self.sample is not None and age <= self.options['max_age']
            current = self.sample['epoch'] + age if self.sample else None
            status = ('syncing' if self.running else 'synced' if fresh else
                      'stale' if self.sample else 'error')
            return {'status': status, 'synchronized': fresh, 'epoch': current if fresh else None,
                    'local_epoch': local_epoch, 'offset_ms': (current - local_epoch) * 1000 if current is not None else None,
                    'local_utc_offset_minutes': local_offset.total_seconds() / 60 if local_offset else 0,
                    'rtt_ms': self.sample['rtt'] * 1000 if self.sample else None,
                    'server': self.sample['server'] if self.sample else '', 'age_seconds': age,
                    'error': self.error, 'protocol': self.sample['protocol'] if self.sample else None,
                    'source': self.sample['protocol'] if self.sample else None,
                    'estimated': self.sample['estimated'] if self.sample else False,
                    'uncertainty_ms': self.sample['uncertainty'] * 1000 if self.sample else None,
                    'max_age_seconds': self.options['max_age'],
                    'sync_interval_seconds': self.options['sync_interval'],
                    'retry_interval_seconds': self.options['retry_interval']}


def _clock(options):
    signature = tuple((key, value) for key, value in options.items() if key != 'enabled')
    with _CLOCKS_LOCK:
        if signature not in _CLOCKS:
            _CLOCKS[signature] = _Clock(options)
        return _CLOCKS[signature]


def request_sync(config_dict=None, force=False):
    """只排定一個背景校時工作；立即回傳，不在呼叫執行緒等待網路。"""
    options = _options(config_dict)
    return _clock(options).start(force=bool(force)) if options['enabled'] else False


def get_status(config_dict=None):
    options = _options(config_dict)
    if options['enabled']:
        clock = _clock(options)
        clock.start()
        return clock.snapshot()
    local_epoch = time.time()
    local_offset = datetime.fromtimestamp(local_epoch).astimezone().utcoffset()
    return {'status': 'local', 'synchronized': False, 'epoch': local_epoch,
            'local_epoch': local_epoch, 'offset_ms': 0.0, 'rtt_ms': None, 'server': '',
            'local_utc_offset_minutes': local_offset.total_seconds() / 60 if local_offset else 0,
            'age_seconds': None, 'error': '', 'protocol': 'local', 'source': 'local',
            'estimated': False, 'uncertainty_ms': None, 'max_age_seconds': options['max_age'],
            'sync_interval_seconds': options['sync_interval'], 'retry_interval_seconds': options['retry_interval']}


def get_timestamp(config_dict=None):
    """回傳可用標準 epoch；啟用但尚未校時或已過期時回傳 None。"""
    return get_status(config_dict)['epoch']


def get_datetime(config_dict=None):
    """官方時間為 UTC+8 naive datetime；停用時保留原本機時區行為。"""
    if not _options(config_dict)['enabled']:
        return datetime.now()
    epoch = get_timestamp(config_dict)
    return datetime.fromtimestamp(epoch, TAIWAN_TIMEZONE).replace(tzinfo=None) if epoch is not None else None
