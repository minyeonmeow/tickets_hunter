"""Local process control. This module never imports platform/browser code."""
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

CONST_RUN_CONTROL_FILES = ('MAXBOT_INT28_IDLE.txt', 'MAXBOT_INT28_QUIT.txt', 'MAXBOT_BROWSER_DISCONNECT.txt')


def supports_warm_worker(config):
    advanced = config.get('advanced', {})
    host = urlsplit(config.get('homepage', '')).hostname
    enabled = (host in ('ticketplus.com.tw', 'www.ticketplus.com.tw') and advanced.get('warm_ticketplus_worker', True) or
               host in ('tixcraft.com', 'www.tixcraft.com') and advanced.get('warm_tixcraft_worker', True))
    return bool(not getattr(sys, 'frozen', False) and enabled and advanced.get('browser_connection_mode', 'attach') == 'attach')


class Worker:
    def __init__(self, root, instance):
        self.root = root
        self.instance = instance
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.ready = threading.Event()
        self.pending = {}
        self.busy = False
        self.run_id = None
        self.cancelled = None
        self.expected_exit = False
        self.ocr_ready = False
        self.ocr_loading = False
        self.ocr_loads = 0
        self.ocr_hits = 0
        self.prepared_stamp = None
        self.preparing_stamp = None
        env = dict(os.environ, PYTHONUTF8='1')
        env.pop('PYTHONHOME', None)
        script = str(Path(__file__).with_name('ticketplus_worker.py'))
        self.process = subprocess.Popen([sys.executable, script, '--root', root, '--instance', instance],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True,
            encoding='utf-8', bufsize=1, env=env, cwd=root)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    if event.get('type') == 'ready':
                        self.ready.set()
                    if event.get('type') in ('ocr_loading', 'ocr_ready', 'ocr_error'):
                        self.ocr_ready = event['type'] == 'ocr_ready'
                        self.ocr_loading = event['type'] == 'ocr_loading'
                        self.ocr_loads = event.get('loads', self.ocr_loads)
                        self.ocr_hits = event.get('hits', self.ocr_hits)
                    if event.get('type') == 'stopped' and event.get('run_id') == self.run_id:
                        self.busy = False
                    waiter = self.pending.get(event.get('seq'))
                    if waiter:
                        waiter[1].update(event)
                        waiter[0].set()
        finally:
            self.ready.set()
            with self.lock:
                failed = self.busy and not self.expected_exit
                self.busy = False
                for done, reply in self.pending.values():
                    reply.update(ok=False, error='工作程序已退出，請重新開始。')
                    done.set()
            if failed:
                import util
                base = Path(self.root) if self.instance == 'default' else Path(self.root) / 'instances' / self.instance
                util.force_remove_file(str(base / 'heartbeat.txt'))
                util.write_instance_status('error', '工作程序意外退出；請檢查瀏覽器頁面後再開始。',
                                           path=str(base / util.CONST_INSTANCE_STATUS_FILE))

    def snapshot(self):
        alive = self.process.poll() is None
        return {'worker_alive': alive, 'worker_ready': alive and self.ready.is_set(),
                'worker_busy': alive and self.busy, 'run_id': self.run_id,
                'worker_ocr_ready': alive and self.ocr_ready, 'worker_ocr_loading': alive and self.ocr_loading,
                'ocr_loads': self.ocr_loads, 'ocr_hits': self.ocr_hits}

    def preload(self, config_path):
        try:
            info = os.stat(config_path)
            stamp = (os.path.abspath(config_path), info.st_mtime_ns, info.st_size)
        except OSError:
            return
        with self.lock:
            if self.busy or stamp in (self.prepared_stamp, self.preparing_stamp):
                return
            self.preparing_stamp = stamp
        def prepare():
            try:
                self.request('prepare', config_path=config_path)
                with self.lock:
                    self.prepared_stamp = stamp
            except (OSError, ValueError):
                pass
            finally:
                with self.lock:
                    if self.preparing_stamp == stamp:
                        self.preparing_stamp = None
        threading.Thread(target=prepare, daemon=True).start()

    def request(self, command, wait_timeout=10, **payload):
        with self.lock:
            if command == 'start':
                if self.busy:
                    raise ValueError('此實例已有任務或正在停止，請稍候。')
                # Clear old flags now, before waiting for process readiness. A
                # pause/disconnect requested during startup must remain visible.
                base = Path(self.root) if self.instance == 'default' else Path(self.root) / 'instances' / self.instance
                for filename in CONST_RUN_CONTROL_FILES:
                    (base / filename).unlink(missing_ok=True)
                self.run_id = uuid.uuid4().hex
                self.busy = True
            elif command in ('disconnect', 'shutdown'):
                self.cancelled = self.run_id
            run_id = self.run_id
        if not self.ready.wait(30) or self.process.poll() is not None:
            if command == 'start':
                self.busy = False
            raise ValueError('工作程序尚未就緒或已退出，請重新開始。')
        with self.lock:
            if command == 'start' and self.cancelled == run_id:
                self.busy = False
                raise ValueError('啟動已取消。')
            seq = uuid.uuid4().hex
            done, reply = threading.Event(), {}
            self.pending[seq] = (done, reply)
        try:
            with self.write_lock:
                if command == 'start' and self.cancelled == run_id:
                    self.busy = False
                    raise ValueError('啟動已取消。')
                self.process.stdin.write(json.dumps(dict(command=command, seq=seq, run_id=run_id, **payload)) + '\n')
                self.process.stdin.flush()
            if not done.wait(wait_timeout):
                raise ValueError('指令回應未明，請查看實例狀態，避免重複開始。')
            if not reply.get('ok'):
                if command == 'start':
                    self.busy = False
                raise ValueError(reply.get('error', '工作程序無法執行指令。'))
            return reply
        finally:
            with self.lock:
                self.pending.pop(seq, None)

    def close(self, force=False):
        self.expected_exit = True
        if self.process.poll() is None:
            try:
                if force:
                    self.process.terminate()
                elif not self.ready.is_set():
                    self.process.stdin.close()
                else:
                    self.request('shutdown', wait_timeout=5)
                self.process.wait(timeout=4)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=4)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


class WorkerManager:
    def __init__(self, root):
        self.root = root
        self.workers = {}
        self.lock = threading.RLock()
        self.closed = False
        self.close_done = threading.Event()

    def get(self, instance):
        with self.lock:
            worker = self.workers.get(instance or 'default')
            return worker if worker and worker.process.poll() is None else None

    def prepare(self, instance, config_path=None):
        instance = instance or 'default'
        with self.lock:
            if self.closed:
                raise ValueError('設定服務正在結束。')
            worker = self.get(instance)
            if worker is None:
                previous = self.workers.get(instance)
                if previous:
                    previous.reader.join(timeout=1)
                    previous.close()
                worker = Worker(self.root, instance)
                self.workers[instance] = worker
            if config_path:
                worker.preload(config_path)
            return worker

    def close(self):
        with self.lock:
            already_closing = self.closed
            self.closed = True
            workers = list(self.workers.values())
        if already_closing:
            self.close_done.wait(12)
            return
        threads = [threading.Thread(target=w.close, daemon=True) for w in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.close_done.set()
