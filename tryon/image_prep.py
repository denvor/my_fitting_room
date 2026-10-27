"""上传图的校验、EXIF 矫正、缩放，以及给接口的输出尺寸推导。"""

import base64
import math
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 10_485_760   # 接口上限：单图 10MB
MAX_SIDE = 1536                 # 官方建议 384–2048，取中低值省时省额度
MAX_SIDE_2K = 2048              # 2K 档允许到人相长边的建议上沿，否则输入只有 1K 像素，出不了 2K
MIN_SIDE_AFTER_RESIZE = 384
MAX_INPUT_RATIO = 10.0          # 接口输入长宽比上限
OUT_MIN_AREA = 512 * 512        # 接口输出总像素下限
OUT_MAX_AREA = 2_250_000        # 1K 档上限：超过就进 2K 计费
OUT_MAX_AREA_2K = 4_157_440     # 2K 档上限 = 实测成立的 1792×2320（扣 108 credit，不是 58 的两倍）
OUT_MAX_RATIO = 8.0             # 接口输出长宽比上限
DIM_STEP = 8
AREA_BY_SIZE = {"1k": OUT_MAX_AREA, "2k": OUT_MAX_AREA_2K}
SIDE_BY_SIZE = {"1k": MAX_SIDE, "2k": MAX_SIDE_2K}
JPEG_QUALITY = 92


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    width: int
    height: int
    data_uri: str


class ImageRejected(Exception):
    """图片不可用，消息是给人看的中文原因。"""


def out_area(size: str) -> int:
    """这一档的输出总像素上限。档位由 prefs 决定，函数各自的默认值一律取 1K（历史上只有这一档）。"""
    return AREA_BY_SIZE[size]


def max_side(size: str) -> int:
    """这一档允许的输入长边。2K 档要给到 2048，不然输入像素不够，输出也就到不了 2K。"""
    return SIDE_BY_SIZE[size]


def _floor_step(value: float) -> int:
    return max(DIM_STEP, int(value) // DIM_STEP * DIM_STEP)


def prepare_upload(raw: bytes, size: str = "1k") -> PreparedImage:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageRejected("图片超过 10MB，请先压缩后再传")

    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception:
        raise ImageRejected("不是可识别的图片，请改成 JPG/PNG 再传")

    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if w <= 0 or h <= 0:
        raise ImageRejected("图片尺寸无效")
    if max(w, h) / min(w, h) > MAX_INPUT_RATIO:
        raise ImageRejected("图片长宽比超过 10:1，请换一张")

    # 长边和总像素两条约束谁更紧就按谁缩，保证留在所选档位内
    side, area = max_side(size), out_area(size)
    scale = min(1.0, side / max(w, h), math.sqrt(area / (w * h)))
    nw, nh = _floor_step(w * scale), _floor_step(h * scale)
    if min(nw, nh) < MIN_SIDE_AFTER_RESIZE:
        raise ImageRejected("图片太小，重新拍一张（短边至少 384 像素）")

    buf = BytesIO()
    img.resize((nw, nh)).save(buf, format="JPEG", quality=JPEG_QUALITY)
    data = buf.getvalue()
    return PreparedImage(
        data=data,
        width=nw,
        height=nh,
        data_uri="data:image/jpeg;base64," + base64.b64encode(data).decode(),
    )


def api_size(width: int, height: int, size: str = "1k") -> str:
    """按人像图的比例定输出尺寸，锁在所选档位内，宽高取 8 的倍数。"""
    if width <= 0 or height <= 0:
        raise ImageRejected("图片尺寸无效")

    w, h = float(width), float(height)
    if w / h > OUT_MAX_RATIO:
        h = w / OUT_MAX_RATIO
    elif h / w > OUT_MAX_RATIO:
        w = h / OUT_MAX_RATIO

    area = w * h
    ceiling = out_area(size)
    # 1K 档按人相自身的像素走（§6.6 的质量基线就是这个形状，不顺手改）；
    # 2K 档把档位当目标：人相小也铺满 2K 面积，否则"选了 2K 还是出 1K"这件事没人看得出来是为什么
    target = ceiling if size == "2k" else min(ceiling, max(OUT_MIN_AREA, area))
    scale = math.sqrt(target / area)
    nw, nh = math.ceil(w * scale / DIM_STEP) * DIM_STEP, math.ceil(h * scale / DIM_STEP) * DIM_STEP

    # 向上取整可能越过档位上限，向下取整可能掉到下限以下，逐步修回来
    while nw * nh > ceiling:
        if nw >= nh:
            nw -= DIM_STEP
        else:
            nh -= DIM_STEP
    while nw * nh < OUT_MIN_AREA:
        if nw >= nh:
            nw += DIM_STEP
        else:
            nh += DIM_STEP
    while max(nw / nh, nh / nw) > OUT_MAX_RATIO:
        if nw > nh:
            nw -= DIM_STEP
        else:
            nh -= DIM_STEP

    return f"{nw}*{nh}"
