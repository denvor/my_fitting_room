"""跨进程的生成锁：同一时间只让一个任务压显存，顺便让前端问得出"现在有没有人在跑"。

为什么不用 threading.Lock：uWSGI 起四个 worker，那把锁每个进程一把、互相看不见 —— 两个请求
落到不同 worker 时 GPU 同时在跑两件事，而"上一次还在跑"那句 409 本来也拦不住（2026-10-30 线上
就是：nginx 掐断连接后前端以为结束了、解禁了按钮，用户再点撞上还在跑的 worker）。

锁就是 data 目录里的一个文件，用 O_CREAT|O_EXCL 拿到它的人算开始，内容是"谁在跑、什么时候开始、
走的哪条来源" —— 所以它同时是给页面看的状态。
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

LOCK_NAME = "generating.lock"
# 锁最多可信多久，超过就当持有者已经死了并接管：比 tryon/comfy_api.py 里 2K 的 900 秒上限宽一倍，
# 又不至于让一个被 harakiri/重启杀掉的 worker 永久把页面锁成点不动。
STALE_AFTER_S = 1800
DIR_MODE = 0o700
FILE_MODE = 0o600  # 和 records/prefs 同一套：显式写死，不靠 umask（uWSGI 的 umask 实测 0000）


@dataclass(frozen=True)
class Held:
    pid: int
    started_at: float
    source: str

    def age_s(self, now: float | None = None) -> int:
        """已经跑了多少秒（给页面看的数，向下取整成非负）。"""
        return max(0, int((time.time() if now is None else now) - self.started_at))


def who_holds(lock_file: Path | str) -> Held | None:
    """现在有没有任务在跑；读不懂（半截写入、被人删过）一律当作没人拿。"""
    try:
        data = json.loads(Path(lock_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError 盖住 JSONDecodeError
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Held(int(data["pid"]), float(data["started_at"]), str(data.get("source") or ""))
    except (KeyError, TypeError, ValueError):
        return None


def try_acquire(lock_file: Path | str, source: str) -> bool:
    """拿到锁返回 True；已经有人在跑返回 False。死人锁（超过 STALE_AFTER_S）会被接管。"""
    path = Path(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    payload = json.dumps({"pid": os.getpid(), "started_at": time.time(), "source": source})
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        except FileExistsError:
            if attempt == 0:
                held = who_holds(path)
                if held is None or held.age_s() > STALE_AFTER_S:
                    path.unlink(missing_ok=True)  # 读不懂的、或者主人多半已经死的：清掉再抢一次
                    continue
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        return True
    return False


def release(lock_file: Path | str) -> None:
    Path(lock_file).unlink(missing_ok=True)
