"""sample 图库：`data/samples/<大类>/<细分类>/<图>.jpg`，目录结构本身就是图库。

没有清单文件要维护 —— 加一张图就是往里放一个文件，页面下次刷新就有它。细分类可以不要，
发型那两类就是直接躺在大类下面的。缩略图缓存在同目录的 `.thumbs/` 里，那是生成物，
所以扫目录时凡是 `.` 开头的都一律不看一眼。
"""

import os
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

# 顺序就是图库弹层里 tab 的顺序；这四类之外的目录跟在后面按名字排（大概是临时加的）
GROUP_ORDER = ("男装", "女装", "男士发型", "女士发型")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
THUMB_DIR = ".thumbs"
THUMB_LONG_SIDE = 240  # 网格里的格子就这么大，没必要把人家的原图解码一遍
THUMB_QUALITY = 80
# 和 records.py、prefs.py 同一套数字：uWSGI 主进程的 umask 实测是 0000，交给它图库目录会落成 777
DIR_MODE = 0o700
FILE_MODE = 0o600


class SampleRejected(Exception):
    """路径不在库里、不是图、或者压根不存在 —— 消息是给人看的中文。"""


def _check_shape(rel: str) -> list[str]:
    """先验形状再看磁盘：`..`、绝对路径、点号开头的那些（.thumbs 本身、.DS_Store）一律不进。"""
    if not rel or rel.startswith("/") or "\\" in rel:
        raise SampleRejected(f"图库里的路径不合法：{rel!r}")
    parts = rel.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        raise SampleRejected(f"图库里的路径不合法：{rel!r}")
    if Path(parts[-1]).suffix.lower() not in IMAGE_SUFFIXES:
        raise SampleRejected(f"那不是图库认识的图片：{rel!r}")
    return parts


def sample_path(root: Path | str, rel: str) -> Path:
    """图库里那张图的真实路径；不在库里（含软链接跑出去）就抛 SampleRejected。"""
    root_dir = Path(root)
    parts = _check_shape(rel)
    found = (root_dir / "/".join(parts)).resolve()
    # resolve 之后再问一次归属：库里塞一个指向 /etc 的软链接，光看字符串是拦不住的
    if not found.is_relative_to(root_dir.resolve()):
        raise SampleRejected(f"图库里的路径跑到了外面：{rel!r}")
    if not found.is_file():
        raise SampleRejected(f"图库里没有这张：{rel!r}")
    return found


def _images_in(d: Path) -> list[Path]:
    return sorted(
        (p for p in d.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda p: p.stem.casefold(),
    )


def _sections(d: Path, group: str) -> list[dict]:
    """一个大类下面的若干节：直接躺在大类下的图自成一节（用大类名），然后是各细分类目录。"""
    sections = []
    loose = _images_in(d)
    if loose:
        sections.append({"name": group, "items": [_item(d, group, p) for p in loose]})
    for sub in sorted((p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name):
        items = _images_in(sub)
        if items:
            sections.append({"name": sub.name, "items": [_item(d, group, p) for p in items]})
    return sections


def _item(group_dir: Path, group: str, file: Path) -> dict:
    """相对路径是从大类那层算起的，所以有没有细分类都是同一种写法：男装/上衣/衬衫A.jpg。"""
    full = f"{group}/{file.relative_to(group_dir).as_posix()}"
    return {"name": file.stem, "path": full, "url": f"samples/{full}", "thumb": f"samples/thumb/{full}"}


def list_samples(root: Path | str) -> list[dict]:
    """[{group, subs: [{name, items: [{name, path, url, thumb}]}]}]；库不存在就是空列表。"""
    root_dir = Path(root)
    if not root_dir.is_dir():
        return []
    groups = [d for d in root_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    # 认识的四类按写死的顺序在前，其余按名字跟在后面 —— 页面不该因为我临时加了个目录就重排
    groups.sort(key=lambda d: (GROUP_ORDER.index(d.name) if d.name in GROUP_ORDER else len(GROUP_ORDER), d.name))
    out = []
    for d in groups:
        subs = _sections(d, d.name)
        if subs:
            out.append({"group": d.name, "subs": subs})
    return out


def _mkdir_private(path: Path, stop: Path) -> None:
    """从 stop 往下把缺的几层目录逐层建出来并 chmod 700。

    不能用 `mkdir(parents=True, mode=...)`：它不把 mode 传给顺带创建的父目录（records.py 里同样的坑）。
    stop 必须已经存在 —— 传进来的就是图库根目录。
    """
    missing = []
    d = path
    while d != stop and not d.is_dir():
        missing.append(d)
        d = d.parent
    for parent in reversed(missing):
        parent.mkdir(mode=DIR_MODE)
        os.chmod(parent, DIR_MODE)


def _write_private(path: Path, blob: bytes, stop: Path) -> None:
    _mkdir_private(path.parent, stop)
    path.write_bytes(blob)
    os.chmod(path, FILE_MODE)


def sample_thumb(root: Path | str, rel: str) -> Path:
    """缩略图路径：没有就现算一张存进 .thumbs/，源图比缓存新就重算。"""
    root_dir = Path(root)
    source = sample_path(root_dir, rel)
    target = (root_dir / THUMB_DIR / rel).with_suffix(".jpg")
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    img = Image.open(source)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((THUMB_LONG_SIDE, THUMB_LONG_SIDE))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=THUMB_QUALITY)
    _write_private(target, buf.getvalue(), root_dir)
    return target
