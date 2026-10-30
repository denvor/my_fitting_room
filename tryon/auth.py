"""口令登录：一个共享口令 + Flask 自带的签名 cookie，不引新依赖。

网关装在 create_app 里、用 before_request 实现，所以以后新增的路由**默认就是受保护的** ——
忘了放行只会让人打不开，不会把接口和图库裸露出去。放行的只有登录页、登录接口、静态资源三项。
"""

import hmac
import os
import secrets
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import jsonify, redirect, request, send_from_directory, session, url_for

from tryon.settings import Settings, SettingsError

MAX_FAILURES = 5  # 同一来源连错这么多次就冷一段
COOLDOWN_SECONDS = 15 * 60
SESSION_DAYS = 30  # 手机上的登录态留住，别每次开都要输口令
COOKIE_NAME = "mfr_auth"  # 共享主机上别和别的应用的 session cookie 撞名
OPEN_ENDPOINTS = frozenset({"static", "login_page", "api_login"})
DEFAULT_SECRET_FILE = Path(__file__).resolve().parents[1] / "data" / "secret.key"

_now = time.monotonic  # 测试里把这个名字换掉，就能让时间前进


class _Failures:
    """按来源 IP 记失败次数。公网上一个脚本一秒能试几十个口令，不拦等于把口令交给它挑。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def wait_seconds(self, ip: str) -> int:
        """还要等多久才放行；0 表示现在可以试。"""
        with self._lock:
            hits = [t for t in self._hits.get(ip, []) if _now() - t < COOLDOWN_SECONDS]
            self._hits[ip] = hits
            if len(hits) < MAX_FAILURES:
                return 0
            return max(1, int(COOLDOWN_SECONDS - (_now() - hits[0])) + 1)

    def add(self, ip: str) -> None:
        with self._lock:
            self._hits.setdefault(ip, []).append(_now())

    def clear(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)


def _read_secret(secret_file: Path) -> str:
    raw = secret_file.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"{secret_file} 是空的：删掉这个文件再重启即可（所有人需要重新登录一次）。")
    return raw


def load_secret(secret_file: Path) -> str:
    """签名密钥存在文件里：服务重启不该把正在生成的页面踢回登录页，密钥也不进版本库（data/ 已 gitignore）。

    多 worker 会同时走到这里（uWSGI 起四个 worker 就是四次），所以要"谁先落地谁算、晚到的采用那一份"：
    各自生成一把的话，A 发的 cookie 在 B 身上验不过，登录的人会时好时坏地被踢回登录页。
    落盘用 os.link 而不是 os.replace —— 目标已存在时 link 直接失败，绝不覆盖；
    临时文件带进程号，别人搬不走，也就不会出现 rename 时 ENOENT 把整个 worker 顶成降级页。
    """
    if secret_file.exists():
        return _read_secret(secret_file)
    secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # 700 写死：见 records.py 里那段理由
    tmp = secret_file.with_name(f"{secret_file.name}.{os.getpid()}.tmp")
    tmp.write_text(secrets.token_hex(32), encoding="utf-8")
    os.chmod(tmp, 0o600)
    try:
        os.link(tmp, secret_file)  # 原子：已存在就 FileExistsError，不会把别人的钥匙换掉
    except FileExistsError:
        pass  # 有 worker 抢先落成了，下面读它那一份
    finally:
        tmp.unlink(missing_ok=True)
    return _read_secret(secret_file)


def demand_password(settings: Settings) -> None:
    """公网入口用：没口令就别启动。静默无保护地暴露出去，是最坏的那种结果。"""
    if not settings.password:
        raise SettingsError("公网入口必须设口令：在 .env 里加一行 TRYON_PASSWORD=<你的口令> 再重启。")


def _reject(message: str, status: int):
    return jsonify({"ok": False, "stage": "auth", "message": message}), status


def init_auth(app, settings: Settings, secret_file: Path, static_dir: Path) -> None:
    """password 为空时网关整个不装（局域网开发靠它免登录）。"""
    password = settings.password
    if password:
        app.secret_key = load_secret(Path(secret_file))
        app.config.update(
            SESSION_COOKIE_NAME=COOKIE_NAME,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            SESSION_COOKIE_SECURE=settings.cookie_secure,
            PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
        )
    failures = _Failures()

    @app.before_request
    def gate():
        if not password or request.endpoint in OPEN_ENDPOINTS or session.get("auth_ok"):
            return None
        # 数据接口回 JSON（前端在原地判断），页面回 302 送到登录页
        if str(request.endpoint).startswith("api_"):
            return _reject("还没登录，或者登录已经过期。", 401)
        return redirect(url_for("login_page"))

    @app.get("/login")
    def login_page():
        return send_from_directory(static_dir, "login.html")

    @app.post("/api/login")
    def api_login():
        if not password:
            return _reject("没设口令，不需要登录。", 400)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            # 只收 JSON：跨站表单发不出 application/json，这一条顺带挡住 CSRF
            return _reject("登录请求要用 JSON 提交。", 400)
        got = body.get("password")
        if not isinstance(got, str) or not got.strip():
            return _reject("口令是空的。", 401)  # 手滑不该记进限速账
        ip = request.remote_addr or "?"
        wait = failures.wait_seconds(ip)
        if wait:
            return _reject(f"错太多次了，等 {wait} 秒再试。", 429)
        if not hmac.compare_digest(got.encode("utf-8"), password.encode("utf-8")):
            failures.add(ip)
            return _reject("口令不对。", 401)
        failures.clear(ip)
        session["auth_ok"] = 1
        session.permanent = True
        return jsonify({"ok": True})
