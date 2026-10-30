"""试穿记录的落盘、列表与删除：一条记录一个目录，不用数据库。"""

import json
import os
import re
import secrets
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

RECORD_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
# 记录里是用户上传的人脸原图，所以权限写死而不是靠 umask（uWSGI 主进程的 umask 实测 0000）：
# 只有跑服务的那个账号读得动。prefs.py 与 auth.py 建 data/ 时用的是同一套数字。
RECORD_DIR_MODE = 0o700
RECORD_FILE_MODE = 0o600
_FILE_SUFFIX = {"person": "jpg", "garment": "jpg", "result": "png", "thumb": "jpg"}
THUMB_LONG_SIDE = 320
# 结果图按接口实际返回的格式命名，不硬写成 .png
RESULT_FORMAT_NAMES = {"PNG": "result.png", "JPEG": "result.jpg", "WEBP": "result.webp", "GIF": "result.gif"}
DEFAULT_RESULT_NAME = "result.png"
FILE_BASENAMES = tuple(
    f"{name}.{_FILE_SUFFIX[name]}" for name in ("person", "garment", "thumb")
) + tuple(RESULT_FORMAT_NAMES.values())


def result_filename(result: bytes) -> str:
    try:
        fmt = Image.open(BytesIO(result)).format
    except Exception:
        return DEFAULT_RESULT_NAME
    return RESULT_FORMAT_NAMES.get(fmt, DEFAULT_RESULT_NAME)


def new_record_id(now: datetime | None = None) -> str:
    ts = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{secrets.token_hex(2)}"


def make_thumb(result: bytes, long_side: int = THUMB_LONG_SIDE) -> bytes:
    img = Image.open(BytesIO(result))
    img.thumbnail((long_side, long_side))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def save_record(
    root: Path, rid: str, person: bytes, garment: bytes, result: bytes, meta: dict
) -> dict[str, str]:
    if not RECORD_ID_RE.fullmatch(rid):
        raise ValueError(f"非法记录名：{rid}")
    d = root / rid
    # 目录 700、文件 600：显式写死，不靠 umask。uWSGI 主进程的 umask 实测是 0000，
    # 交给它的话人脸原图会落成 777/666，而同机器上还跑着别的站（2026-10-30 线上就是这样）。
    # Path.mkdir 的 parents=True 不会把 mode 传给顺带创建的父目录，所以 root 单独来一遍。
    root.mkdir(parents=True, exist_ok=True, mode=RECORD_DIR_MODE)
    os.chmod(root, RECORD_DIR_MODE)
    d.mkdir(mode=RECORD_DIR_MODE)
    result_name = result_filename(result)
    meta = {**meta, "result_name": result_name}
    written = [
        (d / "person.jpg", person),
        (d / "garment.jpg", garment),
        (d / result_name, result),
        (d / "thumb.jpg", make_thumb(result)),
    ]
    for path, blob in written:
        path.write_bytes(blob)
        os.chmod(path, RECORD_FILE_MODE)
    meta_path = d / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.chmod(meta_path, RECORD_FILE_MODE)
    return _files_of(rid, result_name)


def _files_of(rid: str, result_name: str = DEFAULT_RESULT_NAME) -> dict[str, str]:
    """记录里四张图对外的 URL。"""
    files = {name: f"files/{rid}/{name}.{_FILE_SUFFIX[name]}" for name in ("person", "garment", "thumb")}
    files["result"] = f"files/{rid}/{result_name}"
    return files


def _has_images(d: Path, result_name: str) -> bool:
    """结果图的名字来自 meta.json，不能按固定后缀查。"""
    return all(
        (d / f"{name}.{_FILE_SUFFIX[name]}").is_file() for name in ("person", "garment", "thumb")
    ) and (d / result_name).is_file()


def list_records(root: Path) -> list[dict]:
    """最新的在前；名字不合法、meta 读不懂、图不全的目录静默跳过。"""
    if not root.is_dir():
        return []

    out: list[dict] = []
    for d in root.iterdir():
        if not d.is_dir() or not RECORD_ID_RE.fullmatch(d.name) or not (d / "meta.json").is_file():
            continue
        try:
            meta = json.loads(d.joinpath("meta.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):  # 轮询撞上并发删除时，这条看不见就好，别让整个列表 500
            continue
        result_name = meta.get("result_name", DEFAULT_RESULT_NAME)
        if not _has_images(d, result_name):
            continue
        out.append({"id": d.name, "meta": meta, "files": _files_of(d.name, result_name)})
    out.sort(key=lambda r: r["id"], reverse=True)
    return out


def delete_record(root: Path, rid: str) -> bool:
    if not RECORD_ID_RE.fullmatch(rid):
        return False
    d = root / rid
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True
