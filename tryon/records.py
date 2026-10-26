"""试穿记录的落盘、列表与删除：一条记录一个目录，不用数据库。"""

import json
import re
import secrets
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

FILE_NAMES = ("person", "garment", "result", "thumb")
RECORD_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_FILE_SUFFIX = {"person": "jpg", "garment": "jpg", "result": "png", "thumb": "jpg"}
THUMB_LONG_SIDE = 320
FILE_BASENAMES = tuple(f"{name}.{_FILE_SUFFIX[name]}" for name in FILE_NAMES)


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
) -> Path:
    if not RECORD_ID_RE.fullmatch(rid):
        raise ValueError(f"非法记录名：{rid}")
    d = root / rid
    d.mkdir(parents=True)
    (d / "person.jpg").write_bytes(person)
    (d / "garment.jpg").write_bytes(garment)
    (d / "result.png").write_bytes(result)
    (d / "thumb.jpg").write_bytes(make_thumb(result))
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


def _files_of(rid: str) -> dict[str, str]:
    return {name: f"/files/{rid}/{name}.{_FILE_SUFFIX[name]}" for name in FILE_NAMES}


def record_files(rid: str) -> dict[str, str]:
    """记录里四张图对外的 URL。"""
    if not RECORD_ID_RE.fullmatch(rid):
        raise ValueError(f"非法记录名：{rid}")
    return _files_of(rid)


def _complete(d: Path) -> bool:
    return (d / "meta.json").is_file() and all(
        (d / f"{name}.{_FILE_SUFFIX[name]}").is_file() for name in FILE_NAMES
    )


def list_records(root: Path) -> list[dict]:
    """最新的在前；名字不合法、meta 读不懂、图不全的目录静默跳过。"""
    if not root.is_dir():
        return []

    out: list[dict] = []
    for d in root.iterdir():
        if not d.is_dir() or not RECORD_ID_RE.fullmatch(d.name) or not _complete(d):
            continue
        try:
            meta = json.loads(d.joinpath("meta.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append({"id": d.name, "meta": meta, "files": _files_of(d.name)})
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
