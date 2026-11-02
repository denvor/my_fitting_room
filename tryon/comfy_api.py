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
from tryon.prompt import REF_FILENAMES
from tryon.qwen_api import Generation

# 探针实测出图成立的那三个权重文件名（§6.2），写错会在入队阶段就被拒，所以自检要对着枚举校验
UNET_NAME = "qwen_image_2.1_turbo_int8_convrot.safetensors"
CLIP_NAME = "qwen3vl_8b_int8_convrot.safetensors"
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
STEPS = 8
CFG = 1.0
# 编码节点用我们自己那个 fork（源码 `comfy_nodes/qwen21_posonly_encode.py`，装法见 README「装与起」）。
# 官方 `TextEncodeQwenImage21` 会把负向也编一遍，而 cfg=1.0 时那趟根本不进采样器（comfy/samplers.py:609）：
# 1K 白烧 44 秒、720p 白烧 22 秒，同 seed 出图逐位相同（`docs/comfyui-measurements.md` §9）。
# 这个 fork **只在 cfg=1.0 下与官方等价**（它返回的"负向"是正向的别名），
# 所以 test_encoder_skips_the_negative_pass_and_keeps_the_wiring 里特意把那颗 cfg 钉在这条依赖旁边。
ENCODER_CLASS = "TextEncodeQwenImage21PosOnly"

HTTP_TIMEOUT = 30
VIEW_TIMEOUT = 60
POLL_INTERVAL = 1.0
RESOLUTION_STEP = 32
GENERATION_TIMEOUT = 300  # 1K 档实测：896×1152 是 95 秒，1152×1536 见过 180 / 205 / 224 秒 —— 300 秒只留了三成余量
                          # （原来这句写的"最长 145 秒"是 10-27 那一次的值，10-30 的记录已经超了它）
GENERATION_TIMEOUT_2K = 900  # 实测 539.62 秒（1K 同机 144.44 秒），留 1.7 倍余量 —— 这是上限不是预期
TIMEOUT_BY_SIZE = {
    "480p": GENERATION_TIMEOUT,
    "720p": GENERATION_TIMEOUT,  # 实测 70.6 秒，和 1K 共用 300 秒上限
    "1k": GENERATION_TIMEOUT,
    "2k": GENERATION_TIMEOUT_2K,
}
CHECK_TIMEOUT = 10
# 首页加载要等它，所以只打一个接口、几秒就下结论：`check()` 那套是 6 个请求 × 10 秒（11-02 起多一个节点），放这儿能把首屏拖死
HEALTH_TIMEOUT = 3

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

    除了 2K，每一档都是**上限**：参考图在预算内就原样出（1K 的基线行为，480p / 720p 照同一套），
    本来就比这一档小的图不去放大它 —— 放大花的时间和铺满一样，细节一点不多。
    只有 2K 把档位当**目标**（恒 2016，参考图小也铺满），否则"选了 2K 还是出 1K"没人看得出来是为什么。

    预算 = 档位面积上限开方后向下对齐 32：480p → 544、720p → 864、1K → 1472、2K → 2016。
    实测（同机、8 步、输入多大都一样）：480p 26 秒、720p 70 秒、1K 95 秒、2K 543 秒 —— 耗时正比于这一档的面积。
    """
    ceiling = out_area(size)
    if size != "2k" and width * height <= ceiling:
        return 0
    return isqrt(ceiling) // RESOLUTION_STEP * RESOLUTION_STEP


def build_graph(person_name: str, ref_names: list[str], prompt: str, resolution: int, seed: int, prefix: str) -> dict:
    """人像占 1 号、参考图从 2 号起连排，模型链再往后接 —— 单参考图时逐字等于规格 §6.2 那份图。

    参考图必须写成 `images.image_1` 这种点号平铺的键：换成嵌套字典或数组，ComfyUI 不报错，
    只是把参考图丢掉，于是输出 1024 正方形、人和衣服全是凭空编的（§6.3）。
    节点号只是号，但编码节点里那些 `["7", 0]` 引用是真的按号连的，所以整条链跟着参考图张数平移。
    """
    slots = 1 + len(ref_names)  # 人像 + 参考图，模型链从这之后开始编号
    load = {str(i + 1): {"class_type": "LoadImage", "inputs": {"image": name}}
            for i, name in enumerate([person_name, *ref_names])}
    unet, cache, clip, vae, enc, samp, dec, save = (str(slots + k) for k in range(1, 9))
    graph = {
        **load,
        unet: {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_NAME, "weight_dtype": "default"}},
        cache: {"class_type": "QwenImage21Cache", "inputs": {"model": [unet, 0], "device": "auto", "dtype": "default"}},
        clip: {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "qwen_image"}},
        vae: {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        enc: {
            "class_type": ENCODER_CLASS,
            "inputs": {
                "clip": [clip, 0],
                "vae": [vae, 0],
                "prompt": prompt,
                "negative_prompt": "",
                "resolution": resolution,
                **{f"images.image_{i + 1}": [str(i + 1), 0] for i in range(slots)},
            },
        },
        samp: {
            "class_type": "KSampler",
            "inputs": {
                "model": [cache, 0],
                "positive": [enc, 0],
                "negative": [enc, 1],
                "latent_image": [enc, 2],
                "seed": seed,
                "steps": STEPS,
                "cfg": CFG,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        dec: {"class_type": "VAEDecode", "inputs": {"samples": [samp, 0], "vae": [vae, 0]}},
        save: {"class_type": "SaveImage", "inputs": {"images": [dec, 0], "filename_prefix": prefix}},
    }
    return graph


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
    refs: list[PreparedImage],
    prompt: str,
    timeout: int | None = None,
    size: str = "1k",
) -> Generation:
    url = comfy_url.rstrip("/")
    limit = timeout if timeout is not None else TIMEOUT_BY_SIZE[size]
    resolution = comfy_resolution(person.width, person.height, size)
    # 文件名只是 ComfyUI input 目录里给人看的那个名字，槽序由我们传的顺序决定
    names = [
        _post_multipart(f"{url}/upload/image", "image", filename, blob.data, HTTP_TIMEOUT)
        for filename, blob in zip(("person.jpg", *REF_FILENAMES), [person, *refs])
    ]
    graph = build_graph(names[0], names[1:], prompt, resolution, random.randrange(1, 2**62), "tryon")
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
            "input_image_count": 1 + len(refs),
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


def healthy(comfy_url: str) -> bool:
    """ComfyUI 在不在 —— 首页加载问一次，不在就当场退到 token plan（README「两条生成来源」）。

    只打 `/system_stats` 一个接口、只等 `HEALTH_TIMEOUT` 秒，所以它只回答"通不通"，
    不回答"能不能出图"：权重没下、缺节点这些都算在线，那种错该在生成时原样报出来（那是 `check()` 的活）。
    """
    try:
        _get_json(comfy_url.rstrip("/") + "/system_stats", HEALTH_TIMEOUT)
        return True
    except (ComfyUnavailable, ComfyRejected):
        return False


def check(comfy_url: str) -> CheckResult:
    """设置页那颗按钮：能不能连上、节点在不在、权重名对不对，几种坏法分开说。"""
    url = comfy_url.rstrip("/")
    try:
        _get_json(f"{url}/system_stats", CHECK_TIMEOUT)
        # 官方那个编码节点图里已经不用了，留这一条是当"ComfyUI 版本够新"的代理：
        # 它随 2.1 的工作流一起进核心，缺它基本意味着这台跑不了这套图（`QwenImage21Cache` 也一起缺）
        if not _get_json(f"{url}/object_info/TextEncodeQwenImage21", CHECK_TIMEOUT):
            return CheckResult(False, (f"{url} 这台 ComfyUI 没有 TextEncodeQwenImage21 节点，版本或装法不对",))
        if not _get_json(f"{url}/object_info/{ENCODER_CLASS}", CHECK_TIMEOUT):
            return CheckResult(False, (
                f"{url} 这台 ComfyUI 没装 {ENCODER_CLASS}：把仓库里的 comfy_nodes/qwen21_posonly_encode.py "
                "拷进 ComfyUI 的 custom_nodes 目录，再重启 ComfyUI 才生效（图里发的就是它）",))
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
