import base64
from io import BytesIO

import pytest
from PIL import Image

from tryon.image_prep import (
    MAX_UPLOAD_BYTES,
    MAX_SIDE,
    OUT_MAX_AREA,
    OUT_MIN_AREA,
    ImageRejected,
    api_size,
    prepare_upload,
)


def png(width: int, height: int) -> bytes:
    """现造一张纯色 PNG，用来当上传的原始字节。"""
    buf = BytesIO()
    Image.new("RGB", (width, height), (120, 130, 140)).save(buf, format="PNG")
    return buf.getvalue()


def rotated_jpeg_with_exif(width: int, height: int, orientation: int) -> bytes:
    """像素按 width×height 画，只在 EXIF 里写方向标记（手机竖拍就是这样）。"""
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def dimensions(size_str: str) -> tuple[int, int]:
    w, h = size_str.split("*")
    return int(w), int(h)


def test_small_input_rejected_with_readable_reason():
    with pytest.raises(ImageRejected, match="太小"):
        prepare_upload(png(200, 300))


def test_oversized_bytes_rejected():
    with pytest.raises(ImageRejected, match="10MB"):
        prepare_upload(b"\x00" * (MAX_UPLOAD_BYTES + 1))


def test_non_image_rejected_says_convert_to_jpg():
    with pytest.raises(ImageRejected, match="JPG/PNG"):
        prepare_upload(b"not an image at all")


def test_extreme_ratio_rejected():
    with pytest.raises(ImageRejected, match="长宽比"):
        prepare_upload(png(3000, 100))


def test_long_side_capped_at_1536():
    p = prepare_upload(png(4000, 3000))
    assert max(p.width, p.height) == MAX_SIDE and p.data[:2] == b"\xff\xd8"


def test_exif_orientation_is_applied_before_measuring():
    # EXIF=6（右转 90 显示）时像素本身是横躺的 1500×1000
    p = prepare_upload(rotated_jpeg_with_exif(1500, 1000, orientation=6))
    assert p.height > p.width


def test_data_uri_prefix_and_decodable():
    p = prepare_upload(png(800, 1200))
    assert p.data_uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(p.data_uri.split(",", 1)[1]) == p.data


def test_api_size_keeps_ratio_in_1k_band_and_step8():
    w, h = dimensions(api_size(1536, 1152))
    assert OUT_MIN_AREA <= w * h <= OUT_MAX_AREA and w % 8 == 0 and h % 8 == 0
    assert abs((w / h) - (1536 / 1152)) < 0.02


def test_api_size_clamps_ratio_to_8():
    w, h = dimensions(api_size(1536, 120))
    assert max(w / h, h / w) <= 8.0


def test_api_size_upscales_tiny_person_to_minimum_area():
    w, h = dimensions(api_size(400, 300))
    assert w * h >= OUT_MIN_AREA


def test_api_size_never_exceeds_1k_band():
    # 面积正好压在 2K 档分界线上的方图，取整后不能越过 2,250,000
    w, h = dimensions(api_size(1500, 1500))
    assert w * h <= OUT_MAX_AREA
