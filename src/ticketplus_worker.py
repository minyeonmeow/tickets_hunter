"""One warm TicketPlus worker per instance; stdin/stdout are private JSON pipes."""
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


class Runtime:
    def __init__(self, instance, root, emit):
        self.instance, self.root, self.emit = instance, root, emit
        self.job = None
        self.run_id = None
        self.guard = {}
        self.guard_port = None

    def load(self):
        import util
        util.get_app_root = lambda: self.root
        util.set_instance_id(self.instance)
        import nodriver_tixcraft
        from platforms import ticketplus
        self.runner, self.platform, self.util = nodriver_tixcraft, ticketplus, util

    def flag(self, filename):
        path = self.util.get_instance_state_path(filename)
        with open(path, 'w'):
            pass

    async def run(self, config_path, run_id, port):
        original_print = builtins.print
        self.runner.reset_url_error_state()
        self.runner._BOOT_STARTED = time.monotonic()
        self.platform._ticketplus_reset_runtime_state(self.guard if self.guard_port == port else None)
        names = ('headless', 'homepage', 'ticket_number', 'browser', 'proxy_server',
                 'window_size', 'date_auto_select_mode', 'date_keyword', 'area_auto_select_mode', 'area_keyword',
                 'mcp_debug', 'mcp_connect')
        args = SimpleNamespace(**{key: None for key in names}, input=config_path, instance=self.instance)
        try:
            await self.runner.main(args)
        except asyncio.CancelledError:
            pass
        except (Exception, SystemExit) as exc:
            self.util.create_debug_logger(enabled=True).log(
                '[WORKER] ' + type(exc).__name__ + '\n' + ''.join(traceback.format_tb(exc.__traceback__)))
            self.util.write_instance_status('error', '搶票任務發生錯誤，請查看命令視窗後重新開始。')
        finally:
            self.guard = self.platform._ticketplus_runtime_guards()
            self.guard_port = port
            builtins.print = original_print
            self.emit(type='stopped', run_id=run_id)

    async def stop(self):
        if self.job and not self.job.done():
            self.flag(self.util.CONST_BROWSER_DISCONNECT_FILE)
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
        if command not in ('start', 'shutdown') and message.get('run_id') != self.run_id:
            return  # A delayed control command cannot stop a newer run.
        active = self.job is not None and not self.job.done()
        if command == 'start':
            if active:
                raise ValueError('此實例已有搶票任務。')
            path = message['config_path']
            config = self.util.load_json_file(path)
            if urlsplit(config.get('homepage', '')).hostname not in ('ticketplus.com.tw', 'www.ticketplus.com.tw'):
                raise ValueError('此常駐程序目前只支援 TicketPlus。')
            if config.get('advanced', {}).get('browser_connection_mode', 'attach') != 'attach':
                raise ValueError('常駐模式需要連接既有 Chrome。')
            for filename in (self.runner.CONST_MAXBOT_INT28_FILE, self.runner.CONST_MAXBOT_INT28_QUIT_FILE,
                             self.util.CONST_BROWSER_DISCONNECT_FILE):
                self.util.force_remove_file(self.util.get_instance_state_path(filename))
            self.run_id = message['run_id']
            self.job = asyncio.create_task(self.run(path, self.run_id, config.get('advanced', {}).get('browser_port', 19327)))
        elif command in ('pause', 'resume'):
            if active:
                if command == 'pause':
                    self.flag(self.runner.CONST_MAXBOT_INT28_FILE)
                else:
                    self.util.force_remove_file(self.util.get_instance_state_path(self.runner.CONST_MAXBOT_INT28_FILE))
        elif command in ('disconnect', 'shutdown'):
            await self.stop()
        else:
            raise ValueError('unknown command')


async def serve(instance, root, emit):
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    def read():
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            loop.call_soon_threadsafe(queue.put_nowait, message)
        loop.call_soon_threadsafe(queue.put_nowait, {'command': 'shutdown'})
    threading.Thread(target=read, daemon=True).start()
    runtime = Runtime(instance, root, emit)
    runtime.load()
    emit(type='ready')
    while True:
        message = await queue.get()
        try:
            await runtime.dispatch(message)
            emit(type='reply', seq=message.get('seq'), ok=True, run_id=runtime.run_id)
        except Exception as exc:
            emit(type='reply', seq=message.get('seq'), ok=False, error=str(exc))
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
