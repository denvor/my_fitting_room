"""首页加载时的那一次健康检查：ComfyUI 没在线就当场切到 token plan 1K 并告诉人，不是等到点"开始试穿"时偷偷换。

规矩（README「两条生成来源」那节）：
- 只在存的来源是 comfyui 时才去探，来源本来就是 api 的人不该被烦；
- 探到不在线 → 当场写盘（api / 1K），ComfyUI 的地址原样留着，人在设置页点回来就回来了；
- 本机还有任务在跑（拿着生成锁）→ 一律不判死，正忙着出图的 ComfyUI 就是活的。
"""

import pytest

from tryon import comfy_api
from tryon.comfy_api import ComfyUnavailable
from tryon.joblock import release, try_acquire
from tryon.prefs import Prefs, load_prefs, save_prefs
from tryon.server import create_app
from tryon.settings import Settings

URL = "http://127.0.0.1:8188"
DEAD_URL = "http://127.0.0.1:8199"  # 没人监听的那个端口
FAKE_KEY = "not-a-real-key"


def saved(tmp_path) -> Prefs:
    return load_prefs(tmp_path / "settings.json", Prefs())


def client_for(tmp_path, *, prefs=None, healthy=True):
    """healthy 可以是布尔，也可以是 callable（用来断言"到底有没有去探、探的哪个地址"）。"""
    calls: list[str] = []

    def health_fn(url):
        calls.append(url)
        return healthy(url) if callable(healthy) else healthy

    settings = Settings(
        endpoint="https://example.test/generation", api_key=FAKE_KEY, model="qwen-image-3.0-pro", password=""
    )
    prefs_file = tmp_path / "settings.json"
    if prefs is not None:
        save_prefs(prefs_file, prefs)
    app = create_app(
        settings,
        root=tmp_path / "records",
        prefs_file=prefs_file,
        secret_file=tmp_path / "secret.key",
        comfy_health_fn=health_fn,
        quota_fn=lambda: pytest.fail("走本地时不该去查额度"),
    )
    return app.test_client(), calls


# ---- comfy_api.healthy：只打一个接口，坏法一律算"不在线" ----


def test_healthy_hits_only_system_stats(monkeypatch):
    seen = []

    def fake_get_json(url, timeout):
        seen.append((url, timeout))
        return {"system": {"os": "Linux"}}

    monkeypatch.setattr(comfy_api, "_get_json", fake_get_json)
    assert comfy_api.healthy(URL) is True
    assert seen == [(f"{URL}/system_stats", comfy_api.HEALTH_TIMEOUT)], "多打一个接口首页就慢一截"


def test_healthy_is_false_when_comfyui_is_not_listening(monkeypatch):
    def boom(url, timeout):
        raise ComfyUnavailable(url, "[Errno 111] Connection refused")

    monkeypatch.setattr(comfy_api, "_get_json", boom)
    assert comfy_api.healthy(URL) is False


def test_health_timeout_is_a_couple_of_seconds():
    # 首屏等它，自检那套 10 秒 × 4 个请求在这儿不能用；但 1 秒又容易被忙碌的机器误判成没起
    assert 1 <= comfy_api.HEALTH_TIMEOUT <= 5


# ---- 那个接口：什么时候切、什么时候不切 ----


def test_api_source_is_left_alone(tmp_path):
    # 人已经选定走 API 了，再探一次 ComfyUI 只是多一次请求和多一句弹窗
    client, calls = client_for(tmp_path, prefs=Prefs("api", URL, "2k"), healthy=False)
    body = client.post("/api/comfy/fallback").get_json()
    assert body["switched"] is False and body["source"] == "api"
    assert calls == [], "来源本来就是 api，一个探活请求都不该发"
    assert saved(tmp_path) == Prefs("api", URL, "2k")


def test_a_dead_comfyui_switches_to_the_api_one_k_band(tmp_path):
    client, calls = client_for(tmp_path, prefs=Prefs("comfyui", DEAD_URL, "1k"), healthy=False)
    body = client.post("/api/comfy/fallback").get_json()
    assert body["switched"] is True and body["source"] == "api" and body["size"] == "1k"
    assert calls == [DEAD_URL], "探的是设置里存着的那个地址，不是写死的 8188"
    # 地址不能跟着一起丢：那是人填的 frp 端口，丢了下次切回来还得重填
    assert saved(tmp_path) == Prefs("api", DEAD_URL, "1k")


@pytest.mark.parametrize("band", ["2k", "480p", "720p"])
def test_the_auto_switch_lands_on_the_cheapest_band_not_whatever_was_saved(tmp_path, band):
    # 自动切换只退到 1K：本来存的 2K 一次 108，低档在 API 那条又根本走不到，
    # 想在 API 上跑大档得人去设置页点，不该由一次探活替人花那个钱
    client, _ = client_for(tmp_path, prefs=Prefs("comfyui", URL, band), healthy=False)
    body = client.post("/api/comfy/fallback").get_json()
    assert body["size"] == "1k" and body["credits"] == 58
    assert saved(tmp_path).size == "1k"


def test_a_live_comfyui_changes_nothing(tmp_path):
    client, calls = client_for(tmp_path, prefs=Prefs("comfyui", URL, "480p"), healthy=True)
    body = client.post("/api/comfy/fallback").get_json()
    assert body["switched"] is False and body["source"] == "comfyui" and body["size"] == "480p"
    assert saved(tmp_path) == Prefs("comfyui", URL, "480p")


def test_a_task_in_flight_is_never_judged_dead(tmp_path):
    # ComfyUI 正忙着出图的时候 HTTP 会慢甚至不搭 —— 那正是它活着的样子，判死了就是替人花 58
    lock = tmp_path / "generating.lock"
    assert try_acquire(lock, "comfyui")
    client, calls = client_for(tmp_path, prefs=Prefs("comfyui", URL, "1k"), healthy=False)
    body = client.post("/api/comfy/fallback").get_json()
    assert body["switched"] is False and body["source"] == "comfyui"
    assert calls == [], "有人拿着锁就不该去探，更不该切"
    release(lock)


def test_the_switch_is_visible_to_the_header_line(tmp_path):
    # 切完页眉要立刻是真话：来源 api、按 1K 折算次数，别再挂着"不花额度"那句
    def api_quota():
        from tryon.quota import Quota

        return Quota(total=25500, used=0.0, times_1k=439, remaining_days=8)

    settings = Settings(endpoint="https://example.test/generation", api_key=FAKE_KEY, model="m", password="")
    prefs_file = tmp_path / "settings.json"
    save_prefs(prefs_file, Prefs("comfyui", DEAD_URL, "1k"))
    app = create_app(
        settings,
        root=tmp_path / "records",
        prefs_file=prefs_file,
        secret_file=tmp_path / "secret.key",
        comfy_health_fn=lambda url: False,
        quota_fn=api_quota,
    )
    client = app.test_client()
    assert client.post("/api/comfy/fallback").get_json()["switched"] is True
    quota = client.get("/api/quota").get_json()
    assert quota["source"] == "api" and quota["size"] == "1k" and quota["credits_per_generation"] == 58


def test_the_fallback_endpoint_is_behind_the_password_gate(tmp_path):
    settings = Settings(
        endpoint="https://example.test/generation", api_key=FAKE_KEY, model="m", password="sesame-open-sesame"
    )
    app = create_app(
        settings,
        root=tmp_path / "records",
        prefs_file=tmp_path / "settings.json",
        secret_file=tmp_path / "secret.key",
        comfy_health_fn=lambda url: False,
    )
    r = app.test_client().post("/api/comfy/fallback")
    assert r.status_code == 401, "这个接口会改设置，没登录谁都能把来源翻掉"
    assert saved(tmp_path).source == "comfyui", "被网关拦下时一个字都不该写盘"
