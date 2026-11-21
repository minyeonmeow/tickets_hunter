# encoding=utf-8
"""可選的百度通用文字辨識介接；不包含瀏覽器或購票操作。"""
import base64
import hashlib
import math
import re
import threading
import time

import requests


TOKEN_URL = 'https://aip.baidubce.com/oauth/2.0/token'
OCR_URL = 'https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic'
ERROR_MESSAGES = {
    6: '百度 OCR 帳戶尚未取得此 API 的使用權限。',
    17: '百度 OCR 每日可用額度已用完，請檢查帳戶方案。',
    18: '百度 OCR 呼叫頻率超過帳戶限制，請稍候再試。',
    19: '百度 OCR 可用總額度已用完，請檢查帳戶方案。',
    110: '百度 OCR 授權無效，重新授權後仍未成功。',
    111: '百度 OCR 授權已過期，重新授權後仍未成功。',
}


class BaiduOCRError(Exception):
    """僅包含可安全記錄的錯誤，不附帶金鑰、token、圖片或回應本文。"""


def _seconds(value, default, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and minimum <= number <= maximum else default


class BaiduOCR:
    """提供與既有 classification(bytes) 相同的文字回傳介面。"""
    def __init__(self, options):
        self._api_key = str(options.get('baidu_api_key') or '').strip()
        self._secret_key = str(options.get('baidu_secret_key') or '').strip()
        if not self._api_key or not self._secret_key:
            raise BaiduOCRError('請先在 OCR 設定填入自己的百度 API Key 與 Secret Key。')
        self._timeout = _seconds(options.get('baidu_timeout', 5), 5, 0.1, 30)
        self._interval = _seconds(options.get('baidu_retry_interval', 1), 1, 0.1, 60)
        self._lock = threading.Lock()
        self._token = ''
        self._expires_at = 0
        self._retry_at = 0
        self._image_hash = None
        self._answer = ''

    def _post(self, url, data, params=None):
        response = None
        try:
            response = requests.post(url, data=data, params=params, timeout=self._timeout,
                                     allow_redirects=False)
            if response.status_code != 200:
                raise BaiduOCRError('百度 OCR 服務回應失敗，請檢查連線與帳戶狀態。')
            result = response.json()
            if not isinstance(result, dict):
                raise BaiduOCRError('百度 OCR 回應格式不正確。')
            return result
        except (requests.RequestException, ValueError):
            raise BaiduOCRError('百度 OCR 連線逾時、連線失敗或回應格式不正確。') from None
        finally:
            if response is not None:
                response.close()

    def _get_token(self):
        if self._token and time.perf_counter() < self._expires_at:
            return self._token
        result = self._post(TOKEN_URL, {'grant_type': 'client_credentials',
                                      'client_id': self._api_key, 'client_secret': self._secret_key})
        token = result.get('access_token')
        lifetime = _seconds(result.get('expires_in'), 0, 1, 366 * 86400)
        if not isinstance(token, str) or not token.strip() or lifetime <= 0:
            raise BaiduOCRError('百度 OCR 授權失敗，請檢查 API Key、Secret Key 與服務權限。')
        self._token = token
        self._expires_at = time.perf_counter() + lifetime - min(60, lifetime / 10)
        return token

    def _recognize(self, image):
        encoded = base64.b64encode(image).decode('ascii')
        for attempt in range(2):
            result = self._post(OCR_URL, {'image': encoded}, {'access_token': self._get_token()})
            code = result.get('error_code')
            if code in (110, 111) and attempt == 0:
                self._token, self._expires_at = '', 0
                continue
            if code is not None:
                message = ERROR_MESSAGES.get(code) if isinstance(code, int) else None
                raise BaiduOCRError(message or '百度 OCR 辨識失敗，請檢查授權、額度或呼叫頻率。')
            words = result.get('words_result')
            if not isinstance(words, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get('words'), str) for item in words):
                raise BaiduOCRError('百度 OCR 辨識結果格式不正確。')
            return re.sub(r'[^A-Za-z0-9]', '', ''.join(item['words'] for item in words))
        return ''

    def classification(self, image):
        if not isinstance(image, (bytes, bytearray)) or not 0 < len(image) <= 3 * 1024 * 1024:
            raise BaiduOCRError('百度 OCR 需要不超過 3 MB 的圖片位元組。')
        fingerprint = hashlib.sha256(image).digest()
        with self._lock:
            if fingerprint == self._image_hash and self._answer:
                return self._answer
            if time.perf_counter() < self._retry_at:
                raise BaiduOCRError('百度 OCR 正在等待下一次辨識，請稍候。')
            self._retry_at = time.perf_counter() + self._interval
            try:
                answer = self._recognize(image)
            except BaiduOCRError:
                self._retry_at = time.perf_counter() + self._interval
                raise
            self._image_hash, self._answer = fingerprint, answer
            return answer
