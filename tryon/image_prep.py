"""上传图的校验、EXIF 矫正、缩放，以及给接口的输出尺寸推导。"""

import base64
import math
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 10_485_760   # 接口上限：单图 10MB
MAX_SIDE = 1536                 # 官方建议 384–2048，取中低值省时省额度
MIN_SIDE_AFTER_RESIZE = 384
MAX_INPUT_RATIO = 10.0          # 接口输入长宽比上限
OUT_MIN_AREA = 512 * 512        # 接口输出总像素下限
OUT_MAX_AREA = 2_250_000        # 超过它就是 2K 档，耗时翻倍
OUT_MAX_RATIO = 8.0             # 接口输出长宽比上限
DIM_STEP = 8
JPEG_QUALITY = 92


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    width: int
    height: int
    data_uri: str


class ImageRejected(Exception):
    """图片不可用，消息是给人看的中文原因。"""


def _floor_step(value: float) -> int:
    return max(DIM_STEP, int(value) // DIM_STEP * DIM_STEP)


def prepare_upload(raw: bytes) -> PreparedImage:
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

    # 长边和总像素两条约束谁更紧就按谁缩，保证留在 1K 档
    scale = min(1.0, MAX_SIDE / max(w, h), math.sqrt(OUT_MAX_AREA / (w * h)))
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


def api_size(width: int, height: int) -> str:
    """按人像图的比例定输出尺寸，锁在 1K 档，宽高取 8 的倍数。"""
    if width <= 0 or height <= 0:
        raise ImageRejected("图片尺寸无效")

    w, h = float(width), float(height)
    if w / h > OUT_MAX_RATIO:
        h = w / OUT_MAX_RATIO
    elif h / w > OUT_MAX_RATIO:
        w = h / OUT_MAX_RATIO

    area = w * h
    target = min(OUT_MAX_AREA, max(OUT_MIN_AREA, area))
    scale = math.sqrt(target / area)
    nw, nh = math.ceil(w * scale / DIM_STEP) * DIM_STEP, math.ceil(h * scale / DIM_STEP) * DIM_STEP

    # 向上取整可能越过 1K 档上限，向下取整可能掉到下限以下，逐步修回来
    while nw * nh > OUT_MAX_AREA:
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
