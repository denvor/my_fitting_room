import json
import threading
from io import BytesIO

import pytest
from PIL import Image

from tryon.comfy_api import CheckResult, ComfyUnavailable
from tryon.qwen_api import ApiError, Generation, ResultFetchFailed, ResultMissing
from tryon.quota import Quota, QuotaUnavailable
from tryon.records import RECORD_ID_RE, new_record_id
from tryon.server import create_app
from tryon.settings import Settings

SETTINGS = Settings(endpoint="https://example.test/generation", api_key="test-key", model="qwen-image-3.0-pro")


def img_bytes(width: int, height: int, color, fmt: str = "JPEG") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


PERSON = img_bytes(896, 1152, (10, 20, 30))
GARMENT = img_bytes(896, 1152, (200, 210, 220))
RESULT_PNG = img_bytes(896, 1152, (250, 120, 40), fmt="PNG")

USAGE = {
    "output_width": 896,
    "output_height": 1152,
    "input_image_count": 2,
    "input_image_type": "qima_input_1k",
    "output_image_count": 1,
    "output_image_type": "qima_output_1k",
}


def ok_generation(settings, person, garment, timeout=300) -> Generation:
    return Generation(
        image=RESULT_PNG, width=896, height=1152, usage=USAGE, request_id="r-1", rewrite_status="success"
    )


def fake_quota() -> Quota:
    return Quota(total=25500, used=10756.767, times_1k=254, remaining_days=13)


def fields(person=PERSON, garment=GARMENT):
    data = {}
    if person is not None:
        data["person"] = (BytesIO(person), "p.jpg")
    if garment is not None:
        data["garment"] = (BytesIO(garment), "g.jpg")
    return data


COMFY_URL = "http://127.0.0.1:8188"


def ok_comfy_generation(comfy_url, person, garment, timeout=300) -> Generation:
    return Generation(
        image=RESULT_PNG, width=896, height=1152, usage={"input_image_count": 2, "resolution": 0},
        request_id="pid-9", rewrite_status="", source="comfyui",
    )


def boom_comfy(comfy_url, person, garment, timeout=300):
    """没注入本地 fn 时不许静默走通，逼测试自己说清用的是哪条来源。"""
    raise ComfyUnavailable(comfy_url, "测试没有注入 comfy_generate_fn")


def write_prefs(root, source="comfyui", comfy_url=COMFY_URL):
    (root.parent / "settings.json").write_text(
        json.dumps({"source": source, "comfy_url": comfy_url}), encoding="utf-8"
    )


class Counter:
    """包装任一 fn，记下被调了几次（两条来源签名不同，所以用 *args）。"""

    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.fn(*args, **kwargs)


def ok_check(comfy_url) -> CheckResult:
    return CheckResult(True, ())


@pytest.fixture
def make(tmp_path):
    def _make(generate_fn=ok_generation, quota_fn=fake_quota, comfy_fn=None, check_fn=None):
        root = tmp_path / "records"
        app = create_app(
            SETTINGS, generate_fn=generate_fn, root=root, quota_fn=quota_fn,
            comfy_generate_fn=comfy_fn or boom_comfy, comfy_check_fn=check_fn or ok_check,
            prefs_file=root.parent / "settings.json",
        )
        app.testing = True
        return app.test_client(), root

    return _make


@pytest.fixture
def client(make):
    c, _root = make()
    return c


def test_tryon_happy_path_writes_record(make):
    client, root = make()
    r = client.post("/api/tryon", data=fields())
    body = r.get_json()

    assert r.status_code == 200 and body["ok"] is True
    assert body["record"]["files"]["result"].startswith("files/")
    assert body["elapsed_s"] >= 0
    assert RECORD_ID_RE.fullmatch(body["record"]["id"])

    saved = json.loads((root / body["record"]["id"] / "meta.json").read_text(encoding="utf-8"))
    assert saved["request_id"] == "r-1" and saved["size"] == "896*1152"
    assert "图像1" in saved["prompt"]
    assert (root / body["record"]["id"] / "result.png").read_bytes() == RESULT_PNG


def test_tryon_bad_image_reports_stage_image_not_500(client):
    r = client.post("/api/tryon", data=fields(person=b"junk"))
    assert r.status_code == 400 and r.get_json()["stage"] == "image"
    assert "JPG/PNG" in r.get_json()["message"]


def test_tryon_missing_file_reports_image_stage(client):
    r = client.post("/api/tryon", data=fields(garment=None))
    assert r.status_code == 400 and r.get_json()["stage"] == "image"


def test_tryon_oversized_body_returns_json_not_html(make):
    client, _root = make()
    r = client.post("/api/tryon", data=fields(person=b"0" * (26 * 1024 * 1024)))
    assert r.status_code in (400, 413) and r.get_json()["stage"] == "image"


def test_busy_second_request_is_409(make):
    entered, release = threading.Event(), threading.Event()

    def slow_generate(settings, person, garment, timeout=300):
        entered.set()
        release.wait(timeout=5)
        return ok_generation(settings, person, garment)

    client, _root = make(generate_fn=slow_generate)
    results = []
    first = threading.Thread(target=lambda: results.append(client.post("/api/tryon", data=fields())))
    first.start()
    assert entered.wait(timeout=5)

    second = client.post("/api/tryon", data=fields())
    assert second.status_code == 409 and second.get_json()["stage"] == "busy"

    release.set()
    first.join(timeout=5)
    assert results[0].status_code == 200


def test_lock_released_after_failure(make):
    def boom(settings, person, garment, timeout=300):
        raise ApiError(code="Throttling", message="限流", request_id="r-8")

    client, _root = make(generate_fn=boom)
    assert client.post("/api/tryon", data=fields()).status_code == 502
    # 上一次失败后锁必须还回来，否则第三次请求会被当成"还在跑"
    assert client.post("/api/tryon", data=fields()).status_code == 502


def test_api_error_passes_through_code_and_request_id(make):
    def fail(settings, person, garment, timeout=300):
        raise ApiError(code="InvalidParameter", message="参数不对", request_id="r-9")

    client, root = make(generate_fn=fail)
    r = client.post("/api/tryon", data=fields())
    body = r.get_json()

    assert r.status_code == 502
    assert body["stage"] == "api" and body["code"] == "InvalidParameter" and body["request_id"] == "r-9"
    assert body["message"] == "参数不对"
    assert (list(root.iterdir()) if root.exists() else []) == []  # 失败的生成不进图库


def test_missing_result_image_reports_api_stage(make):
    def no_image(settings, person, garment, timeout=300):
        raise ResultMissing(request_id="r-10")

    client, _root = make(generate_fn=no_image)
    r = client.post("/api/tryon", data=fields())
    assert r.status_code == 502 and r.get_json()["stage"] == "api"


def test_result_fetch_failure_reports_url(make):
    def fetch_failed(settings, person, garment, timeout=300):
        raise ResultFetchFailed(url="https://oss.test/a.png", reason="连接被重置")

    client, _root = make(generate_fn=fetch_failed)
    r = client.post("/api/tryon", data=fields())
    body = r.get_json()

    assert r.status_code == 502 and body["stage"] == "result"
    assert body["result_url"] == "https://oss.test/a.png"
    assert "已经花掉一次生成额度" in body["message"]


def test_unexpected_error_is_reported_not_raised(make):
    def crash(settings, person, garment, timeout=300):
        raise OSError("网卡掉了")

    client, _root = make(generate_fn=crash)
    r = client.post("/api/tryon", data=fields())
    assert r.status_code == 502 and "网卡掉了" in r.get_json()["message"]


def test_records_list_and_delete(make):
    client, root = make()
    rid = client.post("/api/tryon", data=fields()).get_json()["record"]["id"]

    listed = client.get("/api/records").get_json()
    assert [r["id"] for r in listed["records"]] == [rid]
    assert listed["records"][0]["files"]["thumb"] == f"files/{rid}/thumb.jpg"

    assert client.delete(f"/api/records/{rid}").status_code == 200
    assert client.get("/api/records").get_json()["records"] == []
    assert not (root / rid).exists()


def test_delete_unknown_record_is_404(make):
    client, _root = make()
    assert client.delete(f"/api/records/{new_record_id()}").status_code == 404


def test_files_route_serves_record_image(make):
    client, _root = make()
    rid = client.post("/api/tryon", data=fields()).get_json()["record"]["id"]
    r = client.get(f"/files/{rid}/result.png")
    assert r.status_code == 200 and r.data == RESULT_PNG


def test_files_route_rejects_unknown_name(make):
    client, _root = make()
    rid = client.post("/api/tryon", data=fields()).get_json()["record"]["id"]
    assert client.get(f"/files/{rid}/meta.json").status_code in (400, 404)


def test_files_route_rejects_traversal(make):
    client, _root = make()
    assert client.get("/files/..%2f..%2f.env/person.jpg").status_code in (400, 404)
    assert client.get("/files/not-an-id/person.jpg").status_code in (400, 404)


def test_index_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    page = r.get_json() if r.is_json else r.get_data(as_text=True)
    assert "你的照片" in page and "衣服照片" in page
    assert "<script" in page and "static/app.js" in page


def test_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_files_route_works_with_relative_root(tmp_path, monkeypatch):
    """线上 root 是相对路径 data/records，不先转绝对路径会拼到 Flask 的 root_path（tryon/ 包目录）上。"""
    monkeypatch.chdir(tmp_path)
    app = create_app(SETTINGS, generate_fn=ok_generation, root="records")
    app.testing = True
    client = app.test_client()
    rid = client.post("/api/tryon", data=fields()).get_json()["record"]["id"]
    assert (tmp_path / "records" / rid / "result.png").is_file()
    assert client.get(f"/files/{rid}/result.png").status_code == 200


def test_record_save_failure_degrades_to_readable_error(make, tmp_path, monkeypatch):
    """图已经生成（额度已花），落盘失败时不能吐 500 HTML：前端会把"连不上本机服务"糊上去。"""
    from tryon import server

    def boom(root, rid, person, garment, result, meta):
        raise OSError("设备上没有空间")

    monkeypatch.setattr(server, "save_record", boom)
    client, root = make()
    r = client.post("/api/tryon", data=fields())
    body = r.get_json()

    assert body is not None, "响应必须是 JSON，不能是 500 HTML 页"
    assert r.status_code == 502 and body["ok"] is False and body["stage"] == "result"
    assert "设备上没有空间" in body["message"]
    assert (list(root.iterdir()) if root.exists() else []) == []


def test_jpeg_result_is_served_with_real_name(make):
    """结果图换成 JPEG 字节时，落盘名与 /files 白名单要一起跟上。"""
    from tryon.qwen_api import Generation as G

    def gen_jpeg(settings, person, garment, timeout=300):
        return G(image=img_bytes(896, 1152, (5, 6, 7), fmt="JPEG"), width=896, height=1152,
                 usage=USAGE, request_id="r-j", rewrite_status="success")

    client, root = make(generate_fn=gen_jpeg)
    rec = client.post("/api/tryon", data=fields()).get_json()["record"]
    url = rec["files"]["result"]

    assert url.endswith("/result.jpg")
    r = client.get(url)
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("image/jpeg")


def test_quota_endpoint_reports_how_many_1k_left(make):
    client, _ = make(quota_fn=fake_quota)
    body = client.get("/api/quota").get_json()

    assert body["ok"] is True and body["quota"]["times_1k"] == 254
    assert body["quota"]["remaining_days"] == 13


def test_quota_endpoint_degrades_when_cli_is_unusable(make):
    """查不到额度只能少一行信息，不能把页面带崩。"""
    def boom():
        raise QuotaUnavailable("本机没有 qianwen 命令")

    client, _ = make(quota_fn=boom)
    r = client.get("/api/quota")
    body = r.get_json()

    assert r.status_code == 200 and body["ok"] is False
    assert "qianwen" in body["message"]


def test_read_timeout_tells_the_user_credits_may_be_gone(make):
    """接口不回包的超时是最贵的失败：钱可能花了，图没拿到。"""
    def hang(settings, person, garment, timeout=600):
        raise TimeoutError("The read operation timed out")

    client, _ = make(generate_fn=hang)
    body = client.get_json() if False else client.post("/api/tryon", data=fields()).get_json()

    assert body["ok"] is False and body["stage"] == "api"
    assert "read operation timed out" in body["message"]
    assert "58 credit" in body["message"]


# --- 来源分派（Task 5）---


def test_comfyui_source_calls_local_and_never_api(make):
    api_calls, comfy_calls = Counter(ok_generation), Counter(ok_comfy_generation)
    client, root = make(generate_fn=api_calls, comfy_fn=comfy_calls)
    write_prefs(root)

    body = client.post("/api/tryon", data=fields()).get_json()

    assert body["ok"] is True and api_calls.calls == 0 and comfy_calls.calls == 1
    assert body["record"]["meta"]["source"] == "comfyui"


def test_local_failure_never_falls_back_to_api(make):
    """Review Focus 第 3 条：静默换模型是在质量线上挖坑。"""
    api_calls = Counter(ok_generation)

    def boom(comfy_url, person, garment, timeout=300):
        raise ComfyUnavailable(comfy_url)

    client, root = make(generate_fn=api_calls, comfy_fn=boom)
    write_prefs(root)
    r = client.post("/api/tryon", data=fields())

    assert r.status_code == 502 and r.get_json()["stage"] == "comfy"
    assert api_calls.calls == 0


def test_local_failure_message_names_the_address(make):
    def boom(comfy_url, person, garment, timeout=300):
        raise ComfyUnavailable(comfy_url)

    client, root = make(comfy_fn=boom)
    write_prefs(root, comfy_url="http://10.0.0.9:8188")

    assert "http://10.0.0.9:8188" in client.post("/api/tryon", data=fields()).get_json()["message"]


def test_corrupt_prefs_file_keeps_api_path(make):
    """Review Focus 第 4 条：设置文件坏了一半也不能把生成请求带崩。"""
    client, root = make()
    (root.parent / "settings.json").write_text('{"source": "com', encoding="utf-8")

    body = client.post("/api/tryon", data=fields()).get_json()

    assert body["ok"] is True and body["record"]["meta"]["source"] == "api"


def test_source_is_visible_in_the_record_list(make):
    """一个月后要能分清哪张是本地出的（§4 反目标第 3 条）。"""
    client, root = make(comfy_fn=ok_comfy_generation)
    write_prefs(root)
    client.post("/api/tryon", data=fields())

    assert client.get("/api/records").get_json()["records"][0]["meta"]["source"] == "comfyui"


def test_local_and_api_share_one_task_lock(make):
    entered, release = threading.Event(), threading.Event()

    def slow_comfy(comfy_url, person, garment, timeout=300):
        entered.set()
        release.wait(timeout=5)
        return ok_comfy_generation(comfy_url, person, garment)

    client, root = make(comfy_fn=slow_comfy)
    write_prefs(root)
    results = []
    first = threading.Thread(target=lambda: results.append(client.post("/api/tryon", data=fields())))
    first.start()
    assert entered.wait(timeout=5)

    second = client.post("/api/tryon", data=fields())
    release.set()
    first.join(timeout=5)

    assert second.status_code == 409 and second.get_json()["stage"] == "busy"


def test_quota_endpoint_skips_cli_for_local_source(make):
    def never():
        raise AssertionError("本地来源不该去查 qianwen")

    client, root = make(quota_fn=never)
    write_prefs(root)

    body = client.get("/api/quota").get_json()

    assert body["ok"] is True and body["source"] == "comfyui" and body["quota"] is None


def test_quota_endpoint_reports_source_api_by_default(make):
    client, _root = make()

    body = client.get("/api/quota").get_json()

    assert body["source"] == "api" and body["quota"]["times_1k"] == 254


def test_unexpected_local_error_does_not_claim_credit_loss(make):
    """兜底文案说"可能已经扣掉 58 credit"在本地路径上是假的，会白吓好人。"""

    def crash(comfy_url, person, garment, timeout=300):
        raise RuntimeError("显存炸了")

    client, root = make(comfy_fn=crash)
    write_prefs(root)
    body = client.post("/api/tryon", data=fields()).get_json()

    assert body["stage"] == "comfy" and "58 credit" not in body["message"] and "不花额度" in body["message"]


# --- 设置接口（Task 6）---


def test_settings_endpoint_returns_defaults(make):
    client, _ = make()

    body = client.get("/api/settings").get_json()

    assert body["ok"] is True and body["source"] == "api"
    assert body["comfy_url"] == body["default_comfy_url"] == "http://127.0.0.1:8188"
    assert body["valid_sources"] == ["api", "comfyui"]


def test_posted_source_takes_effect_without_restart(make):
    """设置页改完立刻生效：来源是每个请求现读的。"""
    api_calls, comfy_calls = Counter(ok_generation), Counter(ok_comfy_generation)
    client, _ = make(generate_fn=api_calls, comfy_fn=comfy_calls)

    assert client.post("/api/settings", json={"source": "comfyui", "comfy_url": COMFY_URL}).status_code == 200
    client.post("/api/tryon", data=fields())

    assert comfy_calls.calls == 1 and api_calls.calls == 0


def test_invalid_source_is_rejected_with_a_readable_message(make):
    client, _ = make()

    r = client.post("/api/settings", json={"source": "gps", "comfy_url": COMFY_URL})
    body = r.get_json()

    assert r.status_code == 400 and body["stage"] == "settings" and "api" in body["message"]


@pytest.mark.parametrize("bad", ["localhost:8188", "http://a b", "ftp://x", ""])
def test_comfy_url_must_be_a_plain_http_address(make, bad):
    """缺协议是这里最容易犯的错，报错要直接给出该长什么样。"""
    client, _ = make()

    r = client.post("/api/settings", json={"source": "comfyui", "comfy_url": bad})

    assert r.status_code == 400 and "http://" in r.get_json()["message"]


def test_save_failure_reports_without_traceback(make, monkeypatch):
    def boom(file, prefs):
        raise OSError("设备上没有空间")

    monkeypatch.setattr("tryon.server.save_prefs", boom)
    client, _ = make()

    r = client.post("/api/settings", json={"source": "comfyui", "comfy_url": COMFY_URL})
    body = r.get_json()

    assert r.status_code == 500 and body is not None and "设备上没有空间" in body["message"]
    assert "Traceback" not in body["message"]


def test_check_endpoint_returns_problems_and_is_always_200(make):
    client, _ = make(check_fn=lambda url: CheckResult(False, ("连不上 http://x",)))

    r = client.post("/api/settings/check", json={"comfy_url": "http://x"})
    body = r.get_json()

    assert r.status_code == 200 and body["ok"] is False and "连不上" in body["problems"][0]


def test_check_endpoint_falls_back_to_stored_url(make):
    seen = []

    def spy(url):
        seen.append(url)
        return CheckResult(True, ())

    client, _ = make(check_fn=spy)
    client.post("/api/settings", json={"source": "comfyui", "comfy_url": "http://stored:8188"})

    client.post("/api/settings/check", json={})

    assert seen == ["http://stored:8188"]


def test_settings_page_is_served_with_relative_asset_refs(make):
    """§6.7：页面里出现带前导斜杠的站内引用，本机好、/mfr 上直接 404。"""
    client, _ = make()

    r = client.get("/settings")
    page = r.get_data(as_text=True)

    assert r.status_code == 200 and "设置" in page
    assert 'href="static/style.css"' in page
    assert '"/static/' not in page and '"/api/' not in page and '"/settings' not in page
