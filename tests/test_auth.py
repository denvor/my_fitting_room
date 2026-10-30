"""口令登录：全站网关、登录接口、连错限速、签名密钥怎么存。"""

from datetime import datetime, timezone

from werkzeug.http import parse_date

from tryon import auth
from tryon.auth import demand_password
from tryon.comfy_api import ComfyUnavailable
from tryon.quota import Quota
from tryon.server import create_app
from tryon.settings import Settings, SettingsError

PW = "feiting-2026"
COOKIE = "mfr_auth"


class Stubs:
    """三条会花钱 / 会联网的出口都记账，靠它证明"没登录就一步都没走"。"""

    def __init__(self):
        self.api = 0
        self.comfy = 0
        self.quota = 0

    def api_fn(self, settings, person, garment, timeout=300):
        self.api += 1
        raise RuntimeError("不该走到 API")

    def comfy_fn(self, comfy_url, person, garment, timeout=300):
        self.comfy += 1
        raise ComfyUnavailable(comfy_url, "不该走到 ComfyUI")

    def quota_fn(self):
        self.quota += 1
        return Quota(total=25500, used=0.0, times_1k=440, remaining_days=12)


def make(tmp_path, password=PW, secret_file=None, stubs=None, **over):
    stubs = stubs or Stubs()

    def check_fn(url):
        raise AssertionError("不该走自检")

    settings = Settings(
        endpoint="https://example.test/generation",
        api_key="test-key",
        model="qwen-image-3.0-pro",
        password=password,
        **over,
    )
    app = create_app(
        settings,
        root=tmp_path / "records",
        secret_file=secret_file if secret_file is not None else tmp_path / "secret.key",
        generate_fn=stubs.api_fn,
        comfy_generate_fn=stubs.comfy_fn,
        comfy_check_fn=check_fn,
        quota_fn=stubs.quota_fn,
    )
    return app, stubs


def login(client, pw=PW, ip=None):
    extra = {"environ_base": {"REMOTE_ADDR": ip}} if ip else {}
    return client.post("/api/login", json={"password": pw}, **extra)


# ---- 网关 ----


def test_pages_redirect_to_login_when_logged_out(tmp_path):
    client = make(tmp_path)[0].test_client()
    for path in ("/", "/settings"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert r.headers["Location"].endswith("/login"), path


def test_api_and_files_get_json_401_not_a_login_page(tmp_path):
    app, stubs = make(tmp_path)
    client = app.test_client()
    for path in ("/api/records", "/api/quota", "/api/settings", "/files/20261027-170240-a87f/person.jpg"):
        r = client.get(path)
        assert r.status_code == 401, path
        body = r.get_json()
        assert body["ok"] is False and body["stage"] == "auth", path
        assert "登录" in body["message"], path
    assert stubs.quota == 0  # 网关在视图之前，额度查询根本没发生


def test_tryon_is_gated_before_any_cost_is_spent(tmp_path):
    client = make(tmp_path)[0].test_client()
    r = client.post("/api/tryon", data={"person": (b"x", "p.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 401
    assert r.get_json()["stage"] == "auth"


def test_login_page_and_static_are_open_without_cookie(tmp_path):
    client = make(tmp_path)[0].test_client()
    r = client.get("/login")
    assert r.status_code == 200
    assert "口令" in r.get_data(as_text=True)
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/login.js").status_code == 200


def test_login_page_uses_relative_urls(tmp_path):
    # 站内 URL 一律不带前导斜杠，挂在 /mfr/ 这种子路径下才解析得对（设计文档 §6.7）
    text = make(tmp_path)[0].test_client().get("/login").get_data(as_text=True)
    assert 'href="/' not in text and 'src="/' not in text
    assert "static/login.js" in text and 'action=""' not in text


def test_no_password_means_no_gate(tmp_path):
    app, stubs = make(tmp_path, password="", source="api")
    client = app.test_client()
    assert client.get("/").status_code == 200  # 不重定向，局域网里照旧直接进
    assert client.get("/api/records").status_code == 200
    assert client.get("/api/quota").get_json()["source"] == "api"
    assert stubs.quota == 1


# ---- 登录接口 ----


def test_right_password_logs_in_and_opens_everything(tmp_path):
    client = make(tmp_path)[0].test_client()
    r = login(client)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.get("/").status_code == 200
    assert "试衣间" in client.get("/").get_data(as_text=True)
    assert client.get("/api/records").status_code == 200
    assert client.get("/settings").status_code == 200


def test_wrong_password_rejected_without_setting_cookie(tmp_path):
    client = make(tmp_path)[0].test_client()
    r = login(client, "猜的")
    assert r.status_code == 401
    assert r.get_json()["ok"] is False and "口令" in r.get_json()["message"]
    assert client.get("/api/records").status_code == 401


def test_login_body_must_be_json(tmp_path):
    # 跨站表单发不出 application/json，这一条顺带挡住 CSRF
    client = make(tmp_path)[0].test_client()
    r = client.post("/api/login", data=f"password={PW}")
    assert r.status_code == 400


def test_blank_password_is_refused_without_counting(tmp_path):
    # 没填 / 只填空格 是手滑，不是猜测：报错要说清是"空的"，而且不许记进限速账
    client = make(tmp_path)[0].test_client()
    for body in ({"password": "   "}, {"password": ""}, {}):
        r = client.post("/api/login", json=body)
        assert r.status_code == 401
        assert "空" in r.get_json()["message"]
    assert login(client, PW).status_code == 200  # 六次空提交之后仍然能正常登录


def test_session_cookie_is_http_only_and_lasts_days(tmp_path):
    r = login(make(tmp_path)[0].test_client())
    cookies = [c for c in r.headers.getlist("Set-Cookie") if c.startswith(f"{COOKIE}=")]
    assert len(cookies) == 1
    header = cookies[0]
    assert "HttpOnly" in header and "SameSite=Lax" in header
    # 不是浏览器会话 cookie（关掉浏览器就得重输），而是留 30 天：手机上每次开都要输口令会让人直接放弃这个功能
    assert "Expires=" in header
    expiry = parse_date(header.split("Expires=")[1].split(";")[0])
    days_left = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
    assert auth.SESSION_DAYS - 1 <= days_left <= auth.SESSION_DAYS


def test_cookie_secure_when_asked(tmp_path):
    r = login(make(tmp_path, cookie_secure=True)[0].test_client())
    header = [c for c in r.headers.getlist("Set-Cookie") if c.startswith(f"{COOKIE}=")][0]
    assert "Secure" in header


def test_cookie_never_leaks_the_password(tmp_path):
    r = login(make(tmp_path)[0].test_client())
    assert PW not in r.headers.get("Set-Cookie", "")


# ---- 连错限速 ----


def test_five_wrong_attempts_cool_down_that_ip_only(tmp_path):
    app, _ = make(tmp_path)
    client = app.test_client()
    for _ in range(5):
        assert login(client, "猜的", ip="203.0.113.7").status_code == 401
    r = login(client, PW, ip="203.0.113.7")  # 冷期间连对的也不放行
    assert r.status_code == 429
    assert "等" in r.get_json()["message"]
    assert login(client, PW, ip="203.0.113.8").status_code == 200  # 换个来源不受影响


def test_success_clears_the_failure_counter(tmp_path):
    app, _ = make(tmp_path)
    client = app.test_client()
    for _ in range(4):
        login(client, "猜的", ip="203.0.113.9")
    assert login(client, PW, ip="203.0.113.9").status_code == 200
    # 记账清零了：再错 4 次然后输对，仍然放行
    for _ in range(4):
        login(client, "猜的", ip="203.0.113.9")
    assert login(client, PW, ip="203.0.113.9").status_code == 200


def test_lockout_expires(tmp_path, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(auth, "_now", lambda: now["t"])  # 不真等 15 分钟
    client = make(tmp_path)[0].test_client()
    for _ in range(5):
        login(client, "猜的", ip="203.0.113.10")
    assert login(client, PW, ip="203.0.113.10").status_code == 429
    now["t"] += 15 * 60 + 1
    assert login(client, PW, ip="203.0.113.10").status_code == 200


# ---- 签名密钥 ----


def test_session_survives_a_restart(tmp_path):
    """密钥落在文件里，重启服务不该把正在生成的页面踢回登录页。"""
    client = make(tmp_path)[0].test_client()
    login(client)
    value = client.get_cookie(COOKIE).value
    other = make(tmp_path)[0].test_client()  # 同一个 secret.key，新的 app 实例
    other.set_cookie(COOKIE, value)
    assert other.get("/api/records").status_code == 200


def test_cookie_signed_with_another_secret_is_rejected(tmp_path):
    client = make(tmp_path)[0].test_client()
    login(client)
    value = client.get_cookie(COOKIE).value
    other = make(tmp_path, secret_file=tmp_path / "other.key")[0].test_client()
    other.set_cookie(COOKIE, value)
    assert other.get("/api/records").status_code == 401


def test_secret_file_is_created_once_and_not_world_readable(tmp_path):
    secret = tmp_path / "secret.key"
    make(tmp_path, secret_file=secret)
    first = secret.read_bytes()
    assert secret.stat().st_mode & 0o777 == 0o600
    assert len(first) >= 32
    make(tmp_path, secret_file=secret)  # 第二次启动不许换钥匙
    assert secret.read_bytes() == first


def test_secret_dir_is_not_world_writable(tmp_path, daemon_umask):
    """密钥文件本身早就 chmod 600 了，但它所在的 data/ 目录在 umask 0 下会落成 777 ——
    同目录里还有用户上传的原图。建目录时也要显式收紧。"""
    secret = tmp_path / "data" / "secret.key"
    auth.load_secret(secret)
    assert secret.parent.stat().st_mode & 0o077 == 0, oct(secret.parent.stat().st_mode & 0o777)


def test_late_worker_adopts_the_secret_that_already_landed(tmp_path, monkeypatch):
    """uWSGI 起四个 worker，同一个瞬间都发现密钥文件还不存在：慢一步的那个不许把自己那份盖上去。
    否则 A 签的 cookie 在 B 身上验不过，登录的人会时好时坏地被踢回登录页 —— 线上真的撞上过。"""
    secret = tmp_path / "secret.key"
    first = auth.load_secret(secret)
    tokens = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(auth.secrets, "token_hex", lambda n: next(tokens))
    lied = []
    real_exists = auth.Path.exists

    def exists(self):
        # 只在"我能不能走早退分支"这一次撒谎：并发时检查的那一刻对方确实还没落成
        if self == secret and not lied:
            lied.append(1)
            return False
        return real_exists(self)

    monkeypatch.setattr(auth.Path, "exists", exists)
    assert auth.load_secret(secret) == first, "晚到的 worker 自己生成了一把，cookie 会在 worker 之间失效"
    assert secret.read_text(encoding="utf-8").strip() == first
    assert not list(tmp_path.glob("*.tmp"))  # 中途失败的临时文件不该留下


def test_secret_file_survives_losing_the_publish_race(tmp_path, monkeypatch):
    """抢先落成的那个 worker 赢了，我这个 worker 的发布该安静地失败、然后采用它那份 —— 不是炸掉。
    线上炸过一次：临时文件用的是共享名字，被先动手的人 rename 走，剩下的一群 worker 停在降级页（8 个请求 5 个 500）。"""
    secret = tmp_path / "secret.key"
    other = "b" * 64
    secret.write_text(other, encoding="utf-8")
    real_exists = auth.Path.exists
    lied = []

    def exists(self):  # 假装检查的那一刻它还不存在，就是并发时的样子
        if self == secret and not lied:
            lied.append(1)
            return False
        return real_exists(self)

    monkeypatch.setattr(auth.Path, "exists", exists)
    assert auth.load_secret(secret) == other
    assert secret.read_text(encoding="utf-8") == other  # 没被人盖掉
    assert not list(tmp_path.glob("*.tmp"))  # 输家的临时文件要清干净


def test_empty_secret_file_is_not_silently_replaced(tmp_path):
    """静默换钥匙 = 所有人被踢下线，还得查半天，所以要当场说清楚怎么办。"""
    secret = tmp_path / "secret.key"
    secret.write_bytes(b"")
    try:
        make(tmp_path, secret_file=secret)
    except ValueError as e:
        assert "secret.key" in str(e)
    else:
        raise AssertionError("空密钥文件该报错，不该悄悄生成一把新的")


# ---- 公网入口的保险 ----


def test_demand_password_passes_when_set(tmp_path):
    demand_password(Settings(endpoint="e", api_key="k", model="m", password=PW))


def test_demand_password_refuses_public_entry_without_one():
    settings = Settings(endpoint="e", api_key="k", model="m")
    try:
        demand_password(settings)
    except SettingsError as e:
        assert "TRYON_PASSWORD" in str(e)
    else:
        raise AssertionError("公网入口没口令必须拒绝启动")
