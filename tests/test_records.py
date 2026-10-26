import pathlib
import re
from io import BytesIO

import pytest
from PIL import Image

from tryon.records import delete_record, list_records, make_thumb, new_record_id, save_record


def img_bytes(width: int, height: int, color: tuple[int, int, int], fmt: str = "JPEG") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


PERSON = img_bytes(896, 1152, (10, 20, 30))
GARMENT = img_bytes(896, 1152, (200, 210, 220))
RESULT = img_bytes(896, 1152, (250, 120, 40), fmt="PNG")
META = {
    "created_at": "2026-10-26T11:30:05",
    "elapsed_s": 56.2,
    "size": "896*1152",
    "usage": {"output_image_count": 1, "output_image_type": "qima_output_1k"},
    "request_id": "r-1",
    "rewrite_status": "success",
}


@pytest.fixture
def root(tmp_path):
    return tmp_path / "records"


def test_save_then_list_roundtrip_keeps_meta(root):
    save_record(root, "20261026-113005-1f3a", PERSON, GARMENT, RESULT, META)
    got = list_records(root)
    assert got[0]["id"] == "20261026-113005-1f3a"
    assert got[0]["meta"]["usage"]["output_image_count"] == 1
    assert got[0]["meta"]["elapsed_s"] == 56.2


def test_saved_files_are_readable(root):
    rid = "20261026-113005-1f3a"
    save_record(root, rid, PERSON, GARMENT, RESULT, META)
    for name in ("person.jpg", "garment.jpg", "result.png", "thumb.jpg"):
        assert (root / rid / name).stat().st_size > 0
    assert Image.open(root / rid / "result.png").size == (896, 1152)


def test_list_newest_first(root):
    save_record(root, "20261026-090000-aaaa", PERSON, GARMENT, RESULT, META)
    save_record(root, "20261026-120000-bbbb", PERSON, GARMENT, RESULT, META)
    save_record(root, "20261026-103000-cccc", PERSON, GARMENT, RESULT, META)
    assert [r["id"] for r in list_records(root)] == [
        "20261026-120000-bbbb",
        "20261026-103000-cccc",
        "20261026-090000-aaaa",
    ]


def test_broken_record_is_skipped_not_fatal(root):
    (root / "20261026-110000-bad0").mkdir(parents=True)  # 空目录，没 meta.json
    save_record(root, "20261026-120000-900d", PERSON, GARMENT, RESULT, META)
    assert [r["id"] for r in list_records(root)] == ["20261026-120000-900d"]


def test_record_missing_one_image_is_skipped(root):
    rid = "20261026-120000-5a1f"
    save_record(root, rid, PERSON, GARMENT, RESULT, META)
    (root / rid / "thumb.jpg").unlink()
    assert list_records(root) == []


def test_list_on_missing_root_is_empty(root):
    assert list_records(root) == []


def test_delete_removes_whole_dir(root):
    rid = "20261026-113005-1f3a"
    save_record(root, rid, PERSON, GARMENT, RESULT, META)
    assert delete_record(root, rid) is True
    assert not (root / rid).exists()


def test_delete_missing_returns_false(root):
    root.mkdir(parents=True)
    assert delete_record(root, "20261026-113005-1f3a") is False


def test_delete_rejects_path_that_is_not_a_record_id(root):
    root.mkdir(parents=True)
    assert delete_record(root, "../../etc") is False
    assert delete_record(root, "20261026-113005-ZZZZ") is False


def test_record_deleted_mid_read_is_skipped_not_fatal(root, monkeypatch):
    """图库轮询撞上并发删除：meta.json 刚检查完整就被删掉，不该让整个列表炸。"""
    save_record(root, "20261026-113005-1f3a", PERSON, GARMENT, RESULT, META)
    real = pathlib.Path.read_text

    def gone(self, *args, **kwargs):
        if self.name == "meta.json":
            raise FileNotFoundError(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", gone)
    assert list_records(root) == []


def test_result_is_kept_under_its_real_format(root):
    """接口哪天改吐 JPEG 时，不该还把它叫 result.png、按 image/png 发出去。"""
    rid = "20261026-113005-1f3a"
    jpeg_result = img_bytes(896, 1152, (9, 9, 9), fmt="JPEG")
    files = save_record(root, rid, PERSON, GARMENT, jpeg_result, META)

    assert (root / rid / "result.jpg").is_file()
    assert not (root / rid / "result.png").exists()
    assert files["result"] == f"/files/{rid}/result.jpg"
    assert list_records(root)[0]["files"]["result"] == f"/files/{rid}/result.jpg"


def test_thumb_is_smaller_jpeg(root):
    thumb = make_thumb(RESULT)
    assert max(Image.open(BytesIO(thumb)).size) <= 320
    assert thumb[:2] == b"\xff\xd8"


def test_new_record_id_is_path_safe():
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", new_record_id())
