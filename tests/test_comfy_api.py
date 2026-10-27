from io import BytesIO

import pytest
from PIL import Image

from tryon import comfy_api
from tryon.comfy_api import (
    ComfyRejected,
    ComfyResultMissing,
    ComfyTimeout,
    ComfyUnavailable,
    build_graph,
    comfy_resolution,
    generate,
)
from tryon.image_prep import OUT_MAX_AREA, PreparedImage
from tryon.prompt import TRYON_PROMPT

UNET = "qwen_image_2.1_turbo_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
URL = "http://127.0.0.1:8188"

PERSON = PreparedImage(data=b"person-jpeg", width=896, height=1152, data_uri="data:image/jpeg;base64,cA==")
GARMENT = PreparedImage(data=b"garment-jpeg", width=896, height=1152, data_uri="data:image/jpeg;base64,Zw==")


def png_bytes(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (9, 8, 7)).save(buf, format="PNG")
    return buf.getvalue()


def node(g, class_type) -> dict:
    return next(n for n in g.values() if n["class_type"] == class_type)


class Fake:
    """替身传输层：记下每次调用，按预设顺序吐响应。"""

    def __init__(self, upload_names=None, prompt=None, histories=None, image=None):
        self.calls = []
        self.upload_names = list(upload_names or ["person.jpg", "garment.jpg"])
        self.prompt_result = prompt if prompt is not None else {"prompt_id": "pid-1"}
        self.histories = list(histories or [])
        self.image = image if image is not None else png_bytes(896, 1152)
        self.sleeps = 0
        self.prompt_payloads = []

    def post_multipart(self, url, field, filename, blob, timeout):
        self.calls.append(("upload", url, filename))
        return self.upload_names.pop(0)

    def post_json(self, url, payload, timeout):
        self.calls.append(("prompt", url))
        self.prompt_payloads.append(payload)
        if isinstance(self.prompt_result, Exception):
            raise self.prompt_result
        return self.prompt_result

    def get_json(self, url, timeout):
        self.calls.append(("get", url))
        return self.histories.pop(0) if self.histories else {}

    def get_bytes(self, url, timeout):
        self.calls.append(("view", url))
        return self.image

    def sleep(self, seconds):
        self.sleeps += 1


@pytest.fixture
def wire(monkeypatch):
    def _wire(fake):
        monkeypatch.setattr(comfy_api, "_post_multipart", fake.post_multipart)
        monkeypatch.setattr(comfy_api, "_post_json", fake.post_json)
        monkeypatch.setattr(comfy_api, "_get_json", fake.get_json)
        monkeypatch.setattr(comfy_api, "_get_bytes", fake.get_bytes)
        monkeypatch.setattr(comfy_api, "_sleep", fake.sleep)
        return fake

    return _wire


def completed(prompt_id="pid-1", filename="tryon_00001_.png", subfolder=""):
    return {
        prompt_id: {
            "status": {"status_str": "success", "completed": True},
            "outputs": {"10": {"images": [{"filename": filename, "subfolder": subfolder, "type": "output"}]}},
        }
    }


# --- comfy_resolution（§6.4）---


def test_portrait_needs_no_downscale():
    """1.03 百万像素在 1K 档内，0 表示按参考图自身尺寸出。"""
    assert comfy_resolution(896, 1152) == 0


def test_phone_common_size_is_also_zero():
    assert comfy_resolution(1152, 1536) == 0


def test_near_square_large_input_is_pulled_back_one_band():
    """2.36M 会越到 2K 档，向下对齐 32 压回来。"""
    res = comfy_resolution(1536, 1536)
    assert res == 1472 and res * res <= OUT_MAX_AREA


def test_resolution_never_exceeds_the_band():
    assert comfy_resolution(4096, 4096) ** 2 <= OUT_MAX_AREA


# --- build_graph（§6.2、§6.3）---


def test_reference_images_use_dotted_flat_keys():
    """唯一生效的形状；嵌套字典会被 ComfyUI 静默丢图（§6.3）。"""
    g = build_graph("p.jpg", "g.jpg", TRYON_PROMPT, 0, 7, "tryon")
    enc = node(g, "TextEncodeQwenImage21")["inputs"]
    assert enc["images.image_1"] == ["1", 0] and enc["images.image_2"] == ["2", 0]
    assert "images" not in enc


def test_weight_file_names_match_the_verified_probe():
    g = build_graph("p.jpg", "g.jpg", "x", 0, 1, "t")
    assert node(g, "UNETLoader")["inputs"]["unet_name"] == UNET
    assert node(g, "CLIPLoader")["inputs"]["clip_name"] == CLIP
    assert node(g, "VAELoader")["inputs"]["vae_name"] == VAE
    assert {n["class_type"] for n in g.values()} >= {
        "LoadImage", "UNETLoader", "QwenImage21Cache", "CLIPLoader", "VAELoader",
        "TextEncodeQwenImage21", "KSampler", "VAEDecode", "SaveImage",
    }


def test_sampler_uses_the_turbo_settings():
    s = node(build_graph("p.jpg", "g.jpg", "x", 0, 42, "t"), "KSampler")["inputs"]
    assert (s["steps"], s["cfg"], s["sampler_name"], s["scheduler"], s["denoise"]) == (8, 1.0, "euler", "simple", 1.0)
    assert s["seed"] == 42


def test_encode_and_decode_share_one_vae_node():
    g = build_graph("p.jpg", "g.jpg", "x", 0, 1, "t")
    enc, dec = node(g, "TextEncodeQwenImage21")["inputs"], node(g, "VAEDecode")["inputs"]
    vae_key = next(k for k, v in g.items() if v["class_type"] == "VAELoader")
    assert enc["vae"] == dec["vae"] == [vae_key, 0]


def test_prompt_and_resolution_are_passed_through():
    g = build_graph("p.jpg", "g.jpg", "把衣服穿上", 1472, 1, "pre")
    enc, save = node(g, "TextEncodeQwenImage21")["inputs"], node(g, "SaveImage")["inputs"]
    assert enc["prompt"] == "把衣服穿上" and enc["resolution"] == 1472 and save["filename_prefix"] == "pre"


# --- generate ---


def test_one_round_trip_uploads_queues_polls_and_fetches(wire):
    fake = wire(Fake(histories=[{}, completed()]))
    g = generate(URL, PERSON, GARMENT, timeout=60)

    kinds = [c[0] for c in fake.calls]
    assert kinds == ["upload", "upload", "prompt", "get", "get", "view"]
    assert fake.calls[0][1].endswith("/upload/image") and fake.calls[2][1].endswith("/prompt")
    assert g.source == "comfyui" and g.request_id == "pid-1" and g.rewrite_status == ""
    assert (g.width, g.height) == (896, 1152)  # 取自真实 PNG，不是传进去的数
    assert g.usage["input_image_count"] == 2 and g.usage["resolution"] == 0
    assert g.image.startswith(b"\x89PNG")


def test_uploaded_names_come_from_the_response_not_from_us(wire):
    """ComfyUI 可能回一个改过的名字（同名复用），图里必须用它给的那个。"""
    fake = wire(Fake(upload_names=["person (2).jpg", "garment (1).jpg"], histories=[completed()]))
    generate(URL, PERSON, GARMENT, timeout=60)
    assert [c[2] for c in fake.calls if c[0] == "upload"] == ["person.jpg", "garment.jpg"]
    graph = fake.prompt_payloads[0]["prompt"]
    assert graph["1"]["inputs"]["image"] == "person (2).jpg"
    assert graph["2"]["inputs"]["image"] == "garment (1).jpg"


def test_enqueue_rejection_carries_the_server_detail(wire):
    wire(Fake(prompt=ComfyRejected(URL, "value_not_in_list unet_name")))
    with pytest.raises(ComfyRejected) as e:
        generate(URL, PERSON, GARMENT, timeout=60)
    assert "value_not_in_list" in e.value.detail and e.value.url == URL


def test_poll_timeout_reports_and_never_requeues(wire):
    fake = wire(Fake(histories=[{} for _ in range(50)]))
    with pytest.raises(ComfyTimeout) as e:
        generate(URL, PERSON, GARMENT, timeout=3)
    assert [c for c in fake.calls if c[0] == "prompt"] == [("prompt", f"{URL}/prompt")]
    assert e.value.seconds >= 3 and fake.sleeps >= 3


def test_missing_image_output_is_reported(wire):
    done = {
        "pid-1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {},
        }
    }
    wire(Fake(histories=[done]))
    with pytest.raises(ComfyResultMissing):
        generate(URL, PERSON, GARMENT, timeout=60)


def test_execution_error_reports_the_server_message_not_bare_missing(wire):
    """跑挂了（显存不够等）要把服务端原因带出来，不能只说"没有图片"。"""
    failed = {
        "pid-1": {
            "status": {"status_str": "error", "completed": True},
            "messages": [["execution_error", {"node_id": "8", "exception_message": "CUDA out of memory"}]],
            "outputs": {},
        }
    }
    wire(Fake(histories=[failed]))
    with pytest.raises(ComfyRejected) as e:
        generate(URL, PERSON, GARMENT, timeout=60)
    assert "CUDA out of memory" in e.value.detail


def test_transport_failure_names_the_address(wire):
    wire(Fake(prompt=ComfyUnavailable(URL)))
    with pytest.raises(ComfyUnavailable) as e:
        generate(URL, PERSON, GARMENT, timeout=60)
    assert URL in str(e.value) and e.value.url == URL


def test_view_query_carries_filename_subfolder_and_type(wire):
    fake = wire(Fake(histories=[completed(filename="a.png", subfolder="tryon")]))
    generate(URL, PERSON, GARMENT, timeout=60)
    url = [c[1] for c in fake.calls if c[0] == "view"][0]
    assert "filename=a.png" in url and "subfolder=tryon" in url and "type=output" in url


# --- check()（§7：三种坏法要分开报）---


class Serve:
    """按 URL 尾巴查表吐 /object_info 响应。"""

    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get_json(self, url, timeout):
        self.urls.append(url)
        for suffix, payload in self.responses.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"未预期的请求 {url}")


def enum_info(cls, widget, names):
    return {cls: {"input": {"required": {widget: [names, {}]}}}}


GOOD = {
    "/system_stats": {"system": {"comfyui_version": "0.39.0"}},
    "/object_info/TextEncodeQwenImage21": {"TextEncodeQwenImage21": {"input": {"required": {}}}},
    "/object_info/UNETLoader": enum_info("UNETLoader", "unet_name", [UNET, "other.safetensors"]),
    "/object_info/CLIPLoader": enum_info("CLIPLoader", "clip_name", [CLIP]),
    "/object_info/VAELoader": enum_info("VAELoader", "vae_name", [VAE]),
}


@pytest.fixture
def serve(monkeypatch):
    def _serve(responses):
        s = Serve(responses)
        monkeypatch.setattr(comfy_api, "_get_json", s.get_json)
        return s

    return _serve


def test_check_passes_when_everything_is_there(serve):
    s = serve(GOOD)
    result = comfy_api.check(URL)
    assert result.ok and result.problems == ()
    assert any(u.endswith("/system_stats") for u in s.urls)


def test_check_reports_unreachable_and_stops(serve):
    """端口不通就别再查节点和权重了：后面每项都会再超时一次。"""
    s = serve({"/system_stats": ComfyUnavailable(URL, "Connection refused")})
    result = comfy_api.check(URL)
    assert not result.ok and URL in result.problems[0] and len(result.problems) == 1
    assert len(s.urls) == 1


def test_check_reports_missing_text_encode_node(serve):
    """端口通、但这台没有那个节点：得说清是节点缺失，不是"连不上"。"""
    serve({**GOOD, "/object_info/TextEncodeQwenImage21": {}})
    result = comfy_api.check(URL)
    assert not result.ok and "TextEncodeQwenImage21" in result.problems[0]


def test_check_names_the_weight_that_is_missing(serve):
    """权重文件名对不上要点名是哪个节点哪个值，不然没人知道该改哪儿。"""
    serve({**GOOD, "/object_info/VAELoader": enum_info("VAELoader", "vae_name", ["something_else.safetensors"])})
    result = comfy_api.check(URL)
    assert not result.ok
    assert "VAELoader" in result.problems[0] and VAE in result.problems[0]


def test_check_collects_every_missing_weight_at_once(serve):
    """两个都对不上就别让人点两次自检。"""
    serve({
        **GOOD,
        "/object_info/UNETLoader": enum_info("UNETLoader", "unet_name", ["nope"]),
        "/object_info/CLIPLoader": enum_info("CLIPLoader", "clip_name", ["nope"]),
    })
    result = comfy_api.check(URL)
    assert not result.ok and len(result.problems) == 2
    assert any("UNETLoader" in p for p in result.problems) and any("CLIPLoader" in p for p in result.problems)
