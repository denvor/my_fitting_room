import json
import threading
from io import BytesIO

import pytest
from PIL import Image

from tryon.qwen_api import ApiError, Generation, ResultFetchFailed, ResultMissing
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


def fields(person=PERSON, garment=GARMENT):
    data = {}
    if person is not None:
        data["person"] = (BytesIO(person), "p.jpg")
    if garment is not None:
        data["garment"] = (BytesIO(garment), "g.jpg")
    return data


@pytest.fixture
def make(tmp_path):
    def _make(generate_fn=ok_generation):
        root = tmp_path / "records"
        app = create_app(SETTINGS, generate_fn=generate_fn, root=root)
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
    assert body["record"]["files"]["result"].startswith("/files/")
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
    assert listed["records"][0]["files"]["thumb"] == f"/files/{rid}/thumb.jpg"

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
    assert "<script" in page and "/static/app.js" in page


def test_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
