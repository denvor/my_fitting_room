"""分类怎么走到生成那一步：槽位齐不齐、发的是哪条指令、第二张参考图存不存得下来。

这些用例都不许在图没齐时调用生成函数 —— 少一张图照样发请求，等于花一次额度拿一张模型自己编的。
"""

import json
from io import BytesIO

import pytest
from PIL import Image

from tryon.prompt import (
    HAIR_PROMPT,
    OUTFIT_PROMPT,
    SLOT_LABELS,
    TRYON_PROMPT,
)
from tryon.qwen_api import Generation
from tryon.quota import Quota
from tryon.server import create_app
from tryon.settings import Settings

SETTINGS = Settings(endpoint="https://example.test/generation", api_key="test-key", model="qwen-image-3.0-pro")
COMFY_URL = "http://127.0.0.1:8188"


def img(width: int, height: int, color) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


PERSON = img(896, 1152, (10, 20, 30))
TOP = img(896, 1152, (200, 210, 220))
PANTS = img(800, 1200, (30, 40, 50))  # 宽度特意和上衣不同，好让"槽序"这事能被断言出来
RESULT = img(896, 1152, (250, 120, 40))

USAGE = {"input_image_count": 3, "output_width": 896, "output_height": 1152}


class Spy:
    """记下两条来源各自收到的参数；返回一张固定的图。"""

    def __init__(self, source="api"):
        self.calls = []
        self.source = source

    def api(self, settings, person, refs, prompt, timeout=300):
        self.calls.append({"refs": refs, "prompt": prompt})
        return Generation(image=RESULT, width=896, height=1152, usage=USAGE, request_id="r-1", rewrite_status="")

    def comfy(self, comfy_url, person, refs, prompt, timeout=300, size="1k"):
        self.calls.append({"refs": refs, "prompt": prompt, "size": size})
        return Generation(
            image=RESULT, width=896, height=1152, usage=USAGE, request_id="p-1", rewrite_status="",
            source="comfyui",
        )


@pytest.fixture
def make(tmp_path):
    def _make(source="api", spy=None, comfy_fn=None):
        root = tmp_path / "records"
        (root.parent).mkdir(exist_ok=True)
        (root.parent / "settings.json").write_text(
            json.dumps({"source": source, "comfy_url": COMFY_URL, "size": "1k"}), encoding="utf-8"
        )
        spy = spy or Spy()
        app = create_app(
            SETTINGS,
            generate_fn=spy.api,
            comfy_generate_fn=comfy_fn or spy.comfy,
            root=root,
            quota_fn=lambda: Quota(total=25500, used=10756, times_1k=254, remaining_days=13),
            prefs_file=root.parent / "settings.json",
        )
        app.testing = True
        return app.test_client(), root, spy

    return _make


def post(client, mode=None, person=PERSON, top=TOP, pants=None):
    data = {"person": (BytesIO(person), "p.jpg"), "garment": (BytesIO(top), "g.jpg")}
    if pants is not None:
        data["garment2"] = (BytesIO(pants), "g2.jpg")
    if mode is not None:
        data["mode"] = mode
    return client.post("/api/tryon", data=data)


# --- 缺图 / 多图 ---


def test_outfit_without_pants_names_the_empty_slot_and_never_generates(make):
    client, _root, spy = make()
    r = post(client, mode="outfit")
    assert r.status_code == 400 and spy.calls == []
    assert "裤子/裙子照片" in r.get_json()["message"]  # 说的是哪个槽空着，不是"参数错误"


def test_hair_mode_sends_the_hair_prompt(make):
    """发型参考那张走的是同一个槽位，光靠图本身分不出它是衣服还是头发 —— 全靠这条指令。"""
    client, _root, spy = make()
    r = post(client, mode="hair")
    assert r.status_code == 200 and spy.calls[0]["prompt"] == HAIR_PROMPT
    assert len(spy.calls[0]["refs"]) == 1


def test_single_is_the_old_two_image_request(make):
    client, _root, spy = make()
    assert post(client).status_code == 200
    assert spy.calls[0]["prompt"] is TRYON_PROMPT and len(spy.calls[0]["refs"]) == 1


def test_unknown_mode_is_said_not_guessed(make):
    client, _root, spy = make()
    r = post(client, mode="hats")
    assert r.status_code == 400 and spy.calls == [] and "分类" in r.get_json()["message"]


def test_extra_reference_for_a_one_slot_mode_is_rejected(make):
    """单件分类却塞了裤子进来：静默丢掉会让人以为裤子也换上了，那比报错坏得多。"""
    client, _root, spy = make()
    r = post(client, mode="single", pants=PANTS)
    assert r.status_code == 400 and spy.calls == []
    assert SLOT_LABELS["single"][0] in r.get_json()["message"]


# --- 槽序与记录 ---


def test_outfit_keeps_slot_order_on_both_sources(make):
    for source in ("api", "comfyui"):
        client, _root, spy = make(source=source)
        r = post(client, mode="outfit", pants=PANTS)
        assert r.status_code == 200, r.get_json()
        refs = spy.calls[0]["refs"]
        assert [(ref.width, ref.height) for ref in refs] == [(896, 1152), (800, 1200)]
        assert spy.calls[0]["prompt"] is OUTFIT_PROMPT


def test_record_stores_mode_and_the_prompt_actually_sent(make):
    client, root, _spy = make()
    body = post(client, mode="outfit", pants=PANTS).get_json()
    meta = json.loads((root / body["record"]["id"] / "meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "outfit"
    assert meta["prompt"] == OUTFIT_PROMPT  # 记的是真发出去的那条，不是常量


def test_second_reference_image_is_written_and_served(make):
    """裤子那张也得进记录 —— 事后要说清这次是拿什么配的什么，而且它得能被 /files 取回来。"""
    client, root, _spy = make()
    body = post(client, mode="outfit", pants=PANTS).get_json()
    rid = body["record"]["id"]
    assert (root / rid / "garment2.jpg").is_file()
    assert body["record"]["files"]["garment2"] == f"files/{rid}/garment2.jpg"
    assert client.get(f"/files/{rid}/garment2.jpg").status_code == 200


def test_single_mode_record_has_no_second_image(make):
    client, root, _spy = make()
    body = post(client).get_json()
    rid = body["record"]["id"]
    assert "garment2" not in body["record"]["files"]
    assert not (root / rid / "garment2.jpg").exists()


def test_three_uploads_fit_the_body_cap(make):
    """三张 10MB 原图 = 30MB，原来那个 25MB 的帽子是给两张图定的，会把套装请求直接掐死在 413。"""
    from tryon.server import MAX_BODY_BYTES

    assert MAX_BODY_BYTES >= 3 * 10 * 1024 * 1024


def test_quota_answer_does_not_change_with_mode(make):
    """换算次数只看档位，不看分类：分类不多花钱（至少现在没有实测依据说它花）。"""
    client, _root, _spy = make()
    q = client.get("/api/quota").get_json()
    assert q["ok"] and q["credits_per_generation"] == 58


# --- 服务端到真客户端那条缝 ---


def test_the_server_really_calls_the_real_comfy_client(make, monkeypatch):
    """全站都是桩函数，这条是唯一一条 server → tryon.comfy_api.generate 真调用。

    签名对不上时别的测试一律照样绿（桩各吃各的参数），线上却会当场 502 —— 2026-11-01 改分类时
    就是这么差点混过去的，所以宁可在这里把传输层替掉也要把真函数跑一遍。
    """
    from io import BytesIO

    from PIL import Image

    from tryon import comfy_api

    uploads, graphs = [], []

    def fake_multipart(url, field, filename, blob, timeout):
        uploads.append(filename)
        return filename

    def fake_post_json(url, payload, timeout):
        graphs.append(payload)
        return {"prompt_id": "pid-1"}

    done = {
        "pid-1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"11": {"images": [{"filename": "tryon_0001_.png", "subfolder": "", "type": "output"}]}},
        }
    }
    png = BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(png, format="PNG")

    monkeypatch.setattr(comfy_api, "_post_multipart", fake_multipart)
    monkeypatch.setattr(comfy_api, "_post_json", fake_post_json)
    monkeypatch.setattr(comfy_api, "_get_json", lambda url, timeout: done)
    monkeypatch.setattr(comfy_api, "_get_bytes", lambda url, timeout: png.getvalue())
    monkeypatch.setattr(comfy_api, "_sleep", lambda s: None)

    client, _root, _spy = make(source="comfyui", comfy_fn=comfy_api.generate)
    r = client.post(
        "/api/tryon",
        data={
            "person": (BytesIO(PERSON), "p.jpg"),
            "garment": (BytesIO(TOP), "g.jpg"),
            "garment2": (BytesIO(PANTS), "g2.jpg"),
            "mode": "outfit",
        },
    )
    assert r.status_code == 200, r.get_json()
    assert uploads == ["person.jpg", "garment.jpg", "garment2.jpg"]
    enc = next(n["inputs"] for n in graphs[0]["prompt"].values()
               if n["class_type"] == "TextEncodeQwenImage21PosOnly")
    assert enc["prompt"] == OUTFIT_PROMPT
    assert [k for k in enc if k.startswith("images.")] == ["images.image_1", "images.image_2", "images.image_3"]
