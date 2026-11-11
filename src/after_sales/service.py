from __future__ import annotations

import fcntl
from concurrent.futures import ThreadPoolExecutor

from .actions import Actions
from .agent import Runner
from .schemas import DomainError


class Service:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider
        self.actions = Actions(store)
        self.runner = Runner(store, provider)
        lock_path = store.root / "service.lock"
        if lock_path.is_symlink():
            raise DomainError("lock_path", "服务锁路径异常", 409)
        self._lock = lock_path.open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise DomainError("service_active", "此数据目录已有服务运行", 409) from None
        try:
            self.runner.recover()
            self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="after-sales")
        except BaseException:
            self._lock.close()
            raise
        self._closed = False

    def submit(self, principal, sid, command):
        if self._closed:
            raise DomainError("service_closed", "服务已停止", 409)
        result = self.actions.message(principal, sid, command)
        if result["run"]:
            self._pool.submit(self.runner.run, principal, result["run"]["id"])
        return result

    def close(self):
        if not self._closed:
            self._closed = True
            self._pool.shutdown(wait=True, cancel_futures=True)
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
