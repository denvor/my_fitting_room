"""跨进程的生成锁：同一时间只许一个任务压显存，而且前端要能问出"现在有没有人在跑"。

threading.Lock 是每个 worker 一把，uWSGI 起四个 worker 时互相看不见 —— 2026-10-30 线上就是
这样：nginx 掐断连接后前端解禁了按钮，用户再点撞上还在跑的 worker 拿到 409，而那句 409
本身也防不住落到别的 worker 上的请求。
"""

import json
import time
from io import BytesIO

import pytest
from PIL import Image

from tryon import comfy_api, joblock
from tryon.joblock import STALE_AFTER_S, try_acquire, who_holds
from tryon.quota import Quota, QuotaUnavailable
from tryon.server import create_app
from tryon.settings import Settings


def jpeg(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (120, 90, 60)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def lock(tmp_path):
    return tmp_path / "data" / "generating.lock"


# ---- 锁本身 ----


def test_lock_is_exclusive_until_released(lock):
    assert try_acquire(lock, "comfyui") is True
    assert try_acquire(lock, "comfyui") is False, "第二个人不该挤进去"
    joblock.release(lock)
    assert try_acquire(lock, "comfyui") is True


def test_who_holds_describes_the_running_job(lock):
    assert who_holds(lock) is None
    try_acquire(lock, "api")
    held = who_holds(lock)
    assert held.source == "api" and held.age_s() < 5
    joblock.release(lock)
    assert who_holds(lock) is None


def test_a_dead_holder_cannot_wedge_the_page_forever(lock):
    """拿着锁的进程被 harakiri 或重启杀掉时，锁得能被接管 —— 否则按钮永久按不下去。"""
    try_acquire(lock, "comfyui")
    stamp = json.loads(lock.read_text(encoding="utf-8"))
    lock.write_text(json.dumps({**stamp, "started_at": time.time() - STALE_AFTER_S - 10}), encoding="utf-8")
    assert try_acquire(lock, "comfyui") is True, "死锁没人收，页面就永久点不动"


def test_garbage_lock_file_counts_as_free(lock):
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("半截写入", encoding="utf-8")
    assert who_holds(lock) is None
    assert try_acquire(lock, "comfyui") is True


def test_lock_file_and_dir_are_owner_only(lock, daemon_umask):
    """锁和人脸原图住同一个目录，权限按同一套规矩来。"""
    try_acquire(lock, "comfyui")
    assert lock.stat().st_mode & 0o077 == 0, oct(lock.stat().st_mode & 0o777)
    assert lock.parent.stat().st_mode & 0o077 == 0, oct(lock.parent.stat().st_mode & 0o777)


# ---- 路由怎么用它 ----


class Refuse:
    """让生成立刻失败：这里要验的是锁的取与放，不是出图。"""

    def comfy(self, comfy_url, person, garment, timeout=300, size="1k"):
        raise comfy_api.ComfyUnavailable(comfy_url, "记账用")

    def quota(self):
        return Quota(total=25500, used=0.0, times_1k=440, remaining_days=12)

    def quota_fails(self):
        raise QuotaUnavailable("命令不在 PATH 上")


def make(tmp_path, prefs_text=None, quota_fn=None):
    prefs_file = tmp_path / "settings.json"
    if prefs_text:
        prefs_file.write_text(prefs_text, encoding="utf-8")
    rec = Refuse()
    app = create_app(
        Settings(endpoint="https://example.test/g", api_key="k", model="m", password=""),
        root=tmp_path / "records",
        prefs_file=prefs_file,
        secret_file=tmp_path / "secret.key",
        comfy_generate_fn=rec.comfy,
        comfy_check_fn=lambda url: None,
        quota_fn=quota_fn or rec.quota,
    )
    # 锁住在 records 的上一层，和 settings.json、secret.key 同一个 data 目录
    return app.test_client(), tmp_path / "generating.lock"


def post_tryon(client):
    return client.post(
        "/api/tryon",
        data={"person": (BytesIO(jpeg(896, 1152)), "p.jpg"), "garment": (BytesIO(jpeg(800, 600)), "g.jpg")},
        content_type="multipart/form-data",
    )


def test_tryon_locks_the_data_dir_and_frees_it_afterwards(tmp_path):
    client, lock = make(tmp_path)
    assert who_holds(lock) is None
    body = post_tryon(client).get_json()
    assert body["stage"] == "comfy"  # 走到生成了，说明锁拿到了
    assert who_holds(lock) is None, "失败的路径没还锁，页面会永久点不动"


def test_second_request_gets_409_while_the_lock_is_held(tmp_path):
    client, lock = make(tmp_path)
    try_acquire(lock, "comfyui")
    body = post_tryon(client).get_json()
    assert (body["ok"], body["stage"]) == (False, "busy")
    joblock.release(lock)
    assert post_tryon(client).get_json()["stage"] != "busy"


def test_quota_reports_the_running_job_so_the_button_can_stay_off(tmp_path):
    """这条是前端那个需求的根：连接被 nginx 掐断、前端以为结束了，服务端其实还在跑。"""
    client, lock = make(tmp_path)
    assert client.get("/api/quota").get_json()["busy"] is None
    try_acquire(lock, "comfyui")
    busy = client.get("/api/quota").get_json()["busy"]
    assert busy and busy["source"] == "comfyui" and busy["age_s"] < 5
    joblock.release(lock)
    assert client.get("/api/quota").get_json()["busy"] is None


def test_busy_survives_a_failed_quota_query(tmp_path):
    """busy 是给按钮用的，不能跟着额度一起"查不到"就消失。"""
    prefs = '{"source": "api", "comfy_url": "http://x", "size": "1k"}'
    client, lock = make(tmp_path, prefs_text=prefs, quota_fn=Refuse().quota_fails)
    try_acquire(lock, "api")
    body = client.get("/api/quota").get_json()
    assert body["ok"] is False and body["busy"]["source"] == "api"
