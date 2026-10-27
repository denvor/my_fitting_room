"""本机 ComfyUI 客户端：传两张图、入队、轮询、把结果取回来。只走 stdlib urllib，不加依赖。"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from io import BytesIO
from math import isqrt

from PIL import Image

from tryon.image_prep import PreparedImage, out_area
from tryon.prompt import TRYON_PROMPT
from tryon.qwen_api import Generation

# 探针实测出图成立的那三个权重文件名（§6.2），写错会在入队阶段就被拒，所以自检要对着枚举校验
UNET_NAME = "qwen_image_2.1_turbo_int8_convrot.safetensors"
CLIP_NAME = "qwen3vl_8b_int8_convrot.safetensors"
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
STEPS = 8
CFG = 1.0

HTTP_TIMEOUT = 30
VIEW_TIMEOUT = 60
POLL_INTERVAL = 1.0
RESOLUTION_STEP = 32
GENERATION_TIMEOUT = 300  # 1K 档实测最长 145 秒，留一倍余量；本地超时无成本，到点报错就行
GENERATION_TIMEOUT_2K = 900  # 实测 539.62 秒（1K 同机 144.44 秒），留 1.7 倍余量 —— 这是上限不是预期
TIMEOUT_BY_SIZE = {"1k": GENERATION_TIMEOUT, "2k": GENERATION_TIMEOUT_2K}
CHECK_TIMEOUT = 10

# 自检要对着 /object_info 的枚举校验这三项：文件名写错时 ComfyUI 在入队阶段就拒绝，
# 而"端口通但一跑就错"比"连不上"难查得多
WEIGHTS: dict[str, tuple[str, str]] = {
    "UNETLoader": ("unet_name", UNET_NAME),
    "CLIPLoader": ("clip_name", CLIP_NAME),
    "VAELoader": ("vae_name", VAE_NAME),
}


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    problems: tuple[str, ...]


class ComfyUnavailable(Exception):
    """连不上、端口不对、传输断了 —— 消息里带着地址，方便对着设置页改。"""

    def __init__(self, url: str, reason: str = ""):
        super().__init__(f"连不上 ComfyUI（{url}）" + (f"：{reason}" if reason else ""))
        self.url = url
        self.reason = reason


class ComfyRejected(Exception):
    """服务端听懂了但拒绝，或者跑到一半报错：detail 是它的原话，不改写。"""

    def __init__(self, url: str, detail: str = ""):
        super().__init__(f"ComfyUI 返回错误：{detail}")
        self.url = url
        self.detail = detail


class ComfyTimeout(Exception):
    def __init__(self, url: str, seconds: float):
        super().__init__(f"本机 ComfyUI {int(seconds)} 秒没出图（{url}）")
        self.url = url
        self.seconds = seconds


class ComfyResultMissing(Exception):
    """跑完了，输出里没有图片。"""

    def __init__(self, url: str):
        super().__init__("ComfyUI 跑完了，但结果里没有图片")
        self.url = url


def comfy_resolution(width: int, height: int, size: str = "1k") -> int:
    """`resolution` 是像素预算不是宽高（§6.4）：0 = 按参考图自身尺寸，超一档就向下对齐 32 压回来。

    预算按档位取：1K 档 2,250,000 → 超了才压到 1472，否则原样出（基线行为）；
    2K 档 4,157,440 → 恒 2016，参考图小也铺满，所以换档不再要求你先去上传更大的图。
    """
    ceiling = out_area(size)
    if size == "2k":
        return isqrt(ceiling) // RESOLUTION_STEP * RESOLUTION_STEP
    if width * height <= ceiling:
        return 0
    return isqrt(ceiling) // RESOLUTION_STEP * RESOLUTION_STEP


def build_graph(person_name: str, garment_name: str, prompt: str, resolution: int, seed: int, prefix: str) -> dict:
    """10 个节点，逐字照抄规格 §6.2 那份实测成立的图。

    参考图必须写成 `images.image_1` 这种点号平铺的键：换成嵌套字典或数组，ComfyUI 不报错，
    只是把参考图丢掉，于是输出 1024 正方形、人和衣服全是凭空编的（§6.3）。
    """
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": person_name}},
        "2": {"class_type": "LoadImage", "inputs": {"image": garment_name}},
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"}},
        "4": {"class_type": "QwenImage21Cache", "inputs": {"model": ["3", 0], "device": "auto", "dtype": "default"}},
        "5": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "qwen_image"}},
        "6": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "7": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["5", 0],
                "vae": ["6", 0],
                "prompt": prompt,
                "negative_prompt": "",
                "resolution": resolution,
                "images.image_1": ["1", 0],
                "images.image_2": ["2", 0],
            },
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["7", 0],
                "negative": ["7", 1],
                "latent_image": ["7", 2],
                "seed": seed,
                "steps": STEPS,
                "cfg": CFG,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["6", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }


def _error_detail(entry: dict) -> str:
    """把服务端塞在 messages 里的原因捞出来，别只报"没有图片"。"""
    parts = []
    for msg in entry.get("messages") or []:
        if isinstance(msg, list) and len(msg) > 1 and isinstance(msg[1], dict):
            data = msg[1]
            detail = str(data.get("exception_message") or data.get("node_type") or msg[0])
            if data.get("node_id") is not None:
                detail = f"节点 {data['node_id']}：{detail}"
            parts.append(detail)
    return "；".join(parts) or str(entry.get("status") or {})


def _first_image(entry: dict) -> dict | None:
    for out in (entry.get("outputs") or {}).values():
        for item in out.get("images") or []:
            if isinstance(item, dict) and item.get("filename"):
                return item
    return None


def generate(
    comfy_url: str,
    person: PreparedImage,
    garment: PreparedImage,
    timeout: int | None = None,
    size: str = "1k",
) -> Generation:
    url = comfy_url.rstrip("/")
    limit = timeout if timeout is not None else TIMEOUT_BY_SIZE[size]
    resolution = comfy_resolution(person.width, person.height, size)
    person_name = _post_multipart(f"{url}/upload/image", "image", "person.jpg", person.data, HTTP_TIMEOUT)
    garment_name = _post_multipart(f"{url}/upload/image", "image", "garment.jpg", garment.data, HTTP_TIMEOUT)
    graph = build_graph(
        person_name, garment_name, TRYON_PROMPT, resolution, random.randrange(1, 2**62), "tryon"
    )
    prompt_id = str(_post_json(f"{url}/prompt", {"prompt": graph}, HTTP_TIMEOUT).get("prompt_id") or "")
    if not prompt_id:
        raise ComfyRejected(url, "入队没有返回 prompt_id")

    waited = 0.0
    entry: dict = {}
    while True:
        entry = _get_json(f"{url}/history/{prompt_id}", HTTP_TIMEOUT).get(prompt_id, {})
        if (entry.get("status") or {}).get("completed"):
            break
        if waited >= limit:
            raise ComfyTimeout(url, waited)
        _sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    if (entry.get("status") or {}).get("status_str") == "error":
        raise ComfyRejected(url, _error_detail(entry))
    item = _first_image(entry)
    if item is None:
        raise ComfyResultMissing(url)

    query = urllib.parse.urlencode(
        {
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        }
    )
    raw = _get_bytes(f"{url}/view?{query}", VIEW_TIMEOUT)
    width, height = Image.open(BytesIO(raw)).size
    return Generation(
        image=raw,
        width=width,
        height=height,
        usage={
            "input_image_count": 2,
            "output_image_count": 1,
            "output_width": width,
            "output_height": height,
            "resolution": resolution,
            "steps": STEPS,
        },
        request_id=prompt_id,
        rewrite_status="",
        source="comfyui",
    )


def _enum_options(info: dict, cls: str, widget: str) -> list | None:
    """从 /object_info/<cls> 里取某个 COMBO 输入的可选值列表。"""
    spec = ((info.get(cls) or {}).get("input", {}).get("required", {}) or {}).get(widget)
    return spec[0] if isinstance(spec, list) and spec and isinstance(spec[0], list) else None


def check(comfy_url: str) -> CheckResult:
    """设置页那颗按钮：能不能连上、有没有这个节点、权重名对不对，三种坏法分开说。"""
    url = comfy_url.rstrip("/")
    try:
        _get_json(f"{url}/system_stats", CHECK_TIMEOUT)
        if not _get_json(f"{url}/object_info/TextEncodeQwenImage21", CHECK_TIMEOUT):
            return CheckResult(False, (f"{url} 这台 ComfyUI 没有 TextEncodeQwenImage21 节点，版本或装法不对",))
        problems = []
        for cls, (widget, want) in WEIGHTS.items():
            options = _enum_options(_get_json(f"{url}/object_info/{cls}", CHECK_TIMEOUT), cls, widget)
            if options is None:
                problems.append(f"读不到 {cls} 的 {widget} 可选值，这台 ComfyUI 的接口形状和预期不一样")
            elif want not in options:
                problems.append(f"{cls} 的 {widget} 里没有 {want}，权重没下载或名字不同")
        return CheckResult(not problems, tuple(problems))
    except (ComfyUnavailable, ComfyRejected) as e:
        return CheckResult(False, (str(e),))  # 连不上就别一项项试了，每项都会再等 10 秒


# —— 以下四个是真传输，测试里整段换掉 ——


def _raise_for_http_error(url: str, e: urllib.error.HTTPError) -> None:
    body = e.read().decode("utf-8", "replace")
    raise ComfyRejected(url, f"HTTP {e.code} {body[:400]}")


def _post_multipart(url: str, field: str, filename: str, blob: bytes, timeout: int) -> str:
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    body = head + blob + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(json.loads(resp.read())["name"])
    except urllib.error.HTTPError as e:
        _raise_for_http_error(url, e)
    except (OSError, ValueError) as e:
        raise ComfyUnavailable(url, str(e) or e.__class__.__name__)


def _post_json(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        _raise_for_http_error(url, e)
    except (OSError, ValueError) as e:
        raise ComfyUnavailable(url, str(e) or e.__class__.__name__)


def _get_json(url: str, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        _raise_for_http_error(url, e)
    except (OSError, ValueError) as e:
        raise ComfyUnavailable(url, str(e) or e.__class__.__name__)


def _get_bytes(url: str, timeout: int) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        _raise_for_http_error(url, e)
    except OSError as e:
        raise ComfyUnavailable(url, str(e) or e.__class__.__name__)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
