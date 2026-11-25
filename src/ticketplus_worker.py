"""One warm TicketPlus/TixCraft worker per instance; private control pipes only."""
import argparse
import asyncio
import builtins
import json
import os
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from urllib.parse import urlsplit


class LockedOCR:
    def __init__(self, engine):
        self.engine, self.lock = engine, threading.Lock()

    def classification(self, image):
        with self.lock:
            return self.engine.classification(image)

    def __getattr__(self, name):
        return getattr(self.engine, name)


class OCRCache:
    def __init__(self, runtime):
        self.runtime = runtime
        self.ready_key = self.model = self.loading = None
        self.loads = self.hits = 0

    def key(self, config):
        ocr = config.get('ocr_captcha', {})
        fields = ('provider', 'use_universal', 'path', 'beta', 'baidu_api_key', 'baidu_secret_key',
                  'baidu_timeout', 'baidu_retry_interval')
        signature = json.dumps({name: ocr.get(name) for name in fields}, sort_keys=True)
        files = []
        paths = (ocr.get('path') or '', self.runtime.runner.CONST_TIXCRAFT_TM_MODEL_PATH,
                 self.runtime.runner.CONST_DEFAULT_UNIVERSAL_PATH)
        for path in paths if ocr.get('provider', 'local') != 'baidu' else ():
            for name in ('custom.onnx', 'charsets.json'):
                filename = os.path.abspath(os.path.join(self.runtime.root, path, name))
                try:
                    stat = os.stat(filename)
                    files.append((filename, stat.st_mtime_ns, stat.st_size))
                except (OSError, ValueError):
                    files.append((filename, None, None))
        return signature, tuple(files)

    def build(self, config):
        model = self.runtime.runner.create_ocr_for_platform(config)
        if model is None:
            model = self.runtime.runner.create_builtin_ocr(beta=config.get('ocr_captcha', {}).get('beta', True), ranges=1)
        return LockedOCR(model)

    def report(self, kind):
        self.runtime.emit(type=kind, loads=self.loads, hits=self.hits)

    def completed(self, pair, task):
        if task.cancelled():
            if self.loading is pair:
                self.loading = None
            return
        error = task.exception()
        if self.loading is not pair:
            return
        self.loading = None
        if error:
            self.report('ocr_error')
        else:
            self.ready_key, self.model = pair[0], task.result()
            self.loads += 1
            self.report('ocr_ready')

    async def get(self, config):
        key = self.key(config)
        while self.loading and self.loading[0] != key:
            pair = self.loading
            try:
                await asyncio.shield(pair[1])
            except Exception:
                pass
            finally:
                if pair[1].done():
                    self.completed(pair, pair[1])
        if self.ready_key == key and self.model is not None:
            self.hits += 1
            self.report('ocr_ready')
            return self.model
        if self.loading is None:
            snapshot = dict(config, ocr_captcha=dict(config.get('ocr_captcha', {})))
            self.report('ocr_loading')
            task = asyncio.create_task(asyncio.to_thread(self.build, snapshot))
            pair = (key, task)
            self.loading = pair
            task.add_done_callback(lambda done: self.completed(pair, done))
        return await asyncio.shield(self.loading[1])


class Runtime:
    def __init__(self, instance, root, emit):
        self.instance, self.root, self.emit = instance, root, emit
        self.job = None
        self.run_id = None
        self.guard = {}
        self.guard_port = None
        self.tixcraft_guards = {}
        self.preparing_run = False

    def load(self):
        import util
        util.get_app_root = lambda: self.root
        util.set_instance_id(self.instance)
        import nodriver_tixcraft
        from platforms import ticketplus, tixcraft
        self.runner, self.platform, self.util = nodriver_tixcraft, ticketplus, util
        self.tixcraft = tixcraft
        self.ocr_cache = OCRCache(self)

    def flag(self, filename):
        path = self.util.get_instance_state_path(filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w'):
            pass

    def is_tixcraft(self, config):
        return urlsplit(config.get('homepage', '')).hostname in ('tixcraft.com', 'www.tixcraft.com')

    def prepare_library(self, config):
        # The reader waits for dispatch to finish before reading another command.
        # Initialize Windows native extensions here, without a concurrent CRT stdin
        # read; constructing/invoking models still happens outside the event loop.
        if (self.is_tixcraft(config) and self.runner.should_initialize_ocr(config) and
                config.get('ocr_captcha', {}).get('provider', 'local') != 'baidu'):
            import ddddocr

    async def prepare_ocr(self, config):
        try:
            if self.is_tixcraft(config) and self.runner.should_initialize_ocr(config):
                first = self.ocr_cache.ready_key != self.ocr_cache.key(config) and self.ocr_cache.loading is None
                started = time.monotonic()
                if first:
                    self.util.create_debug_logger(enabled=True).log('[OCR CACHE] 背景準備 OCR，尚未連接 Chrome。')
                await self.ocr_cache.get(config)
                if first:
                    self.util.create_debug_logger(enabled=True).log('[OCR CACHE] OCR 預載完成，耗時 %.3f 秒；等待開始。' % (time.monotonic()-started))
        except Exception as exc:
            self.ocr_cache.report('ocr_error')
            self.util.create_debug_logger(enabled=True).log(
                '[OCR CACHE] 預載未完成（%s），請確認 OCR 設定；開始時會重新檢查。' % type(exc).__name__)

    async def run(self, config_path, run_id, port, tixcraft=False):
        original_print = builtins.print
        self.runner.reset_url_error_state()
        self.runner._BOOT_STARTED = time.monotonic()
        if tixcraft:
            self.tixcraft._tixcraft_reset_runtime_state(self.tixcraft_guards.get(port))
        else:
            self.platform._ticketplus_reset_runtime_state(self.guard if self.guard_port == port else None)
        names = ('headless', 'homepage', 'ticket_number', 'browser', 'proxy_server',
                 'window_size', 'date_auto_select_mode', 'date_keyword', 'area_auto_select_mode', 'area_keyword',
                 'mcp_debug', 'mcp_connect')
        args = SimpleNamespace(**{key: None for key in names}, input=config_path, instance=self.instance)
        try:
            if tixcraft:
                config = self.util.load_json_file(config_path)
                self.preparing_run = True
                if self.runner.should_initialize_ocr(config):
                    self.util.write_instance_status('starting', '正在準備或取得快取 OCR；尚未接手 Chrome。')
                    started = time.monotonic()
                    cached = self.ocr_cache.ready_key == self.ocr_cache.key(config) and self.ocr_cache.model is not None
                    await self.ocr_cache.get(config)
                    self.util.create_debug_logger(enabled=True).log(
                        '[OCR CACHE] ' + ('沿用已載入模型' if cached else '模型準備完成') +
                        '；本次取得耗時 %.3f 秒。' % (time.monotonic() - started))
                pause_path = self.util.get_instance_state_path(self.runner.CONST_MAXBOT_INT28_FILE)
                if os.path.isfile(pause_path):
                    self.util.write_instance_status('paused', '已暫停，Chrome 尚未接手。')
                while os.path.isfile(pause_path):
                    if os.path.isfile(self.util.get_instance_state_path(self.runner.CONST_MAXBOT_INT28_QUIT_FILE)):
                        return
                    await asyncio.sleep(.1)
                if os.path.isfile(self.util.get_instance_state_path(self.runner.CONST_MAXBOT_INT28_QUIT_FILE)):
                    return
                self.preparing_run = False
                await self.runner.main(args, ocr_loader=self.ocr_cache.get)
            else:
                await self.runner.main(args)
        except asyncio.CancelledError:
            pass
        except (Exception, SystemExit) as exc:
            self.util.create_debug_logger(enabled=True).log(
                '[WORKER] ' + type(exc).__name__ + '\n' + ''.join(traceback.format_tb(exc.__traceback__)))
            self.util.write_instance_status('error', '搶票任務發生錯誤，請查看命令視窗後重新開始。')
        finally:
            self.preparing_run = False
            if tixcraft:
                self.tixcraft_guards[port] = self.tixcraft._tixcraft_runtime_guards()
            else:
                self.guard = self.platform._ticketplus_runtime_guards()
                self.guard_port = port
            builtins.print = original_print
            self.emit(type='stopped', run_id=run_id)

    async def stop(self, reset=False):
        if reset:
            path = self.util.get_instance_state_path(self.util.CONST_BROWSER_SESSION_RESET_FILE)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='ascii') as marker:
                marker.write(os.urandom(16).hex())
            self.flag(self.util.CONST_BROWSER_DISCONNECT_FILE)
        if self.job and not self.job.done():
            self.flag(self.runner.CONST_MAXBOT_INT28_QUIT_FILE)
            # Cancel Python waits immediately; the runner's finally detaches sockets.
            self.job.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.job), timeout=4)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                if not self.job.done():
                    raise ValueError('任務尚未停止，請使用強制停止。')

    async def dispatch(self, message):
        command = message.get('command')
        if command not in ('start', 'shutdown', 'prepare') and message.get('run_id') != self.run_id:
            return  # A delayed control command cannot stop a newer run.
        active = self.job is not None and not self.job.done()
        if command == 'start':
            if active:
                raise ValueError('此實例已有搶票任務。')
            path = message['config_path']
            config = self.util.load_json_file(path)
            if urlsplit(config.get('homepage', '')).hostname not in ('ticketplus.com.tw', 'www.ticketplus.com.tw', 'tixcraft.com', 'www.tixcraft.com'):
                raise ValueError('此常駐程序目前支援 TicketPlus 與拓元。')
            if config.get('advanced', {}).get('browser_connection_mode', 'attach') != 'attach':
                raise ValueError('常駐模式需要連接既有 Chrome。')
            self.prepare_library(config)
            self.run_id = message['run_id']
            self.job = asyncio.create_task(self.run(path, self.run_id, config.get('advanced', {}).get('browser_port', 19327), self.is_tixcraft(config)))
        elif command == 'prepare':
            if not active:
                config = self.util.load_json_file(message['config_path'])
                self.prepare_library(config)
                task = asyncio.create_task(self.prepare_ocr(config))
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        elif command in ('pause', 'resume'):
            if active:
                if command == 'pause':
                    self.flag(self.runner.CONST_MAXBOT_INT28_FILE)
                    if self.preparing_run:
                        self.util.write_instance_status('paused', '已暫停，Chrome 尚未接手。')
                else:
                    self.util.force_remove_file(self.util.get_instance_state_path(self.runner.CONST_MAXBOT_INT28_FILE))
        elif command in ('disconnect', 'shutdown'):
            await self.stop(reset=command == 'disconnect')
        else:
            raise ValueError('unknown command')


async def serve(instance, root, emit):
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    read_next = threading.Event()
    def read():
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            read_next.clear()
            loop.call_soon_threadsafe(queue.put_nowait, message)
            read_next.wait()
        loop.call_soon_threadsafe(queue.put_nowait, {'command': 'shutdown'})
    runtime = Runtime(instance, root, emit)
    runtime.load()
    threading.Thread(target=read, daemon=True).start()
    emit(type='ready')
    while True:
        message = await queue.get()
        try:
            await runtime.dispatch(message)
            emit(type='reply', seq=message.get('seq'), ok=True, run_id=runtime.run_id)
        except Exception as exc:
            emit(type='reply', seq=message.get('seq'), ok=False, error=str(exc))
        finally:
            read_next.set()
        if message.get('command') == 'shutdown':
            break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance', required=True)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    # Duplicate the protocol fd before the existing bot redirects stdout.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr
    def emit(**event):
        try:
            protocol.write(json.dumps(event, ensure_ascii=True) + '\n')
            protocol.flush()
        except (BrokenPipeError, OSError):
            pass
    asyncio.run(serve(args.instance, args.root, emit))


if __name__ == '__main__':
    main()
