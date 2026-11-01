"""sample 图库：`data/samples/<大类>/<细分类>/<图>.jpg`，目录结构本身就是图库。

没有清单文件要维护 —— 加一张图就是往里放一个文件，页面下次刷新就有它。细分类可以不要，
发型那两类就是直接躺在大类下面的。缩略图缓存在同目录的 `.thumbs/` 里，那是生成物，
所以扫目录时凡是 `.` 开头的都一律不看一眼。
"""

import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from tryon.image_prep import MAX_UPLOAD_BYTES

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
        # 没图的细分类也报出来（items 是空表）：刚建好的架子在管理页要看得见，不然人会再建一次
        sections.append({"name": sub.name, "items": [_item(d, group, p) for p in _images_in(sub)]})
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
        out.append({"group": d.name, "subs": _sections(d, d.name)})
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
        # exist_ok 不是防御性写法：网格一次要几十张缩略图，两个人同时建同一层是真会发生的
        # （2026-11-01 浏览器实测就 404 了一格，报的是 FileExistsError → 路由当成"库里没这张"）
        parent.mkdir(mode=DIR_MODE, exist_ok=True)
        os.chmod(parent, DIR_MODE)  # 已存在的那层不会被 mkdir 的 mode 管到，得自己收


def _write_private(path: Path, blob: bytes, stop: Path) -> None:
    _mkdir_private(path.parent, stop)
    # 先写临时名再 replace：直接 write_bytes 的话，另一个人可能正读到半截 JPEG
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)  # mkstemp 建的本来就是 0600，replace 保留源文件权限


# ==================== 写侧：加图、建类、改名移动、删 ====================


MAX_NAME_CHARS = 60
_FORBIDDEN_CHARS = ':*?"<>|'


def _clean_name(raw: str, what: str, basename: bool = False) -> str:
    """一层目录名或文件名（不含后缀）：不许带斜杠、不许点号开头、不许空。

    `basename=True` 只给文件名用：浏览器可能整条 Windows 路径都塞进来（`C:\\fakepath\\外套.jpg`），
    那种写法在 Windows 上是正常的，取最后一段就行。分类名是人在表单里敲的，带斜杠就是别的意思，拒。
    """
    text = str(raw or "").strip()
    if basename:
        text = text.replace("\\", "/").split("/")[-1]
    if text.startswith("."):
        raise SampleRejected(f"{what}的名字不能以点号开头：{raw!r}（.thumbs 那类是生成物）")
    if "/" in text or "\\" in text:
        raise SampleRejected(f"{what}的名字里不能带斜杠：{raw!r}")
    text = "".join(ch for ch in text if ch not in _FORBIDDEN_CHARS and ch.isprintable()).strip(" .")
    if not text:
        raise SampleRejected(f"{what}的名字是空的")
    if len(text) > MAX_NAME_CHARS:
        raise SampleRejected(f"{what}的名字太长了（{MAX_NAME_CHARS} 个字以内）：{text[:20]}…")
    return text


def _room_parts(group: str, sub: str | None) -> list[str]:
    parts = [_clean_name(group, "大类")]
    if sub and str(sub).strip():
        parts.append(_clean_name(sub, "细分类"))
    return parts


def make_room(root: Path | str, group: str, sub: str | None = None) -> Path:
    """建一个大类（或大类/细分类）：已经存在就算成功，页面上一句"建好了"该是真的。"""
    root_dir = Path(root)
    root_dir.mkdir(mode=DIR_MODE, exist_ok=True)
    os.chmod(root_dir, DIR_MODE)
    target = root_dir.joinpath(*_room_parts(group, sub))
    _mkdir_private(target, root_dir)
    return target


def _check_tree_rel(rel: str) -> list[str]:
    """库里一个条目的相对路径（文件或目录都行），规则和 _check_shape 一样，只是不看后缀。"""
    if not rel or rel.startswith("/"):
        raise SampleRejected(f"图库里的路径不合法：{rel!r}")
    parts = rel.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") or "\\" in part for part in parts):
        raise SampleRejected(f"图库里的路径不合法：{rel!r}")
    return [_clean_name(part, "条目") if i == len(parts) - 1 else _clean_name(part, "分类")
            for i, part in enumerate(parts)]


def _existing(root_dir: Path, parts: list[str]) -> Path:
    found = root_dir.joinpath(*parts).resolve()
    if not found.is_relative_to(root_dir.resolve()):
        raise SampleRejected("图库里的路径跑到了外面")
    if not found.exists():
        raise SampleRejected(f"图库里没有这个：{'/'.join(parts)}")
    return found


def _thumb_of(root_dir: Path, rel: str) -> Path:
    return (root_dir / THUMB_DIR / rel).with_suffix(".jpg")


def _drop_thumb(root_dir: Path, rel: str) -> None:
    """删掉某个条目的缩略图缓存：缓存是按路径存的，路径没了留着就是孤儿。

    文件和目录两种写法不一样 —— 对目录再 `with_suffix(".jpg")` 会去找一个根本不存在的
    `外套.jpg`，于是改完类名，旧路径下那一整棵缓存原样留下（2026-11-01 浏览器实测就是这样）。
    """
    cached = root_dir / THUMB_DIR / rel
    if Path(rel).suffix.lower() in IMAGE_SUFFIXES:
        cached = cached.with_suffix(".jpg")  # 文件的缓存和源文件同名，只把后缀换成 .jpg
    if cached.is_file():
        cached.unlink(missing_ok=True)
    elif cached.is_dir():
        shutil.rmtree(cached, ignore_errors=True)


def _unique(target: Path) -> Path:
    """撞名就往后编号，绝不覆盖：覆盖的话人会把上一张不小心同名的好图丢掉。"""
    if not target.exists():
        return target
    for n in range(2, 1000):
        candidate = target.with_name(f"{target.stem}-{n}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise SampleRejected(f"{target.name} 同名太多，取个别的名字再传")


def add_samples(root: Path | str, group: str, sub: str | None, files: list[tuple[str, bytes]]) -> list[str]:
    """把几张图存进 <大类>/<细分类>/，返回它们相对库根的路径（顺序照输入）。"""
    root_dir = Path(root)
    target = make_room(root_dir, group, sub)  # 不是 mkdir(parents=True)：那样父目录会按 umask 落，线上是 777
    parts = list(target.relative_to(root_dir).parts)
    added = []
    for raw_name, blob in files:
        if len(blob) > MAX_UPLOAD_BYTES:
            raise SampleRejected(f"{raw_name} 超过 10MB，先压缩一下再存进图库")
        try:
            Image.open(BytesIO(blob)).verify()
        except Exception:
            raise SampleRejected(f"{raw_name} 不是能读出来的图片")
        name = _clean_name(raw_name, "文件", basename=True)
        suffix = Path(name).suffix.lower()
        stem = Path(name).stem
        if suffix not in IMAGE_SUFFIXES:
            suffix = ".jpg"  # 内容验过了就是张图，后缀写成啥不重要，落盘按真格式给
        final = _unique(target / f"{stem}{suffix}")
        _write_private(final, blob, root_dir)
        added.append("/".join([*parts, final.name]))
    return added


def move_path(root: Path | str, src_rel: str, dst_rel: str) -> str:
    """一个操作管三件事：改大类名、改细分类名、把一张图挪到别的架子上（src/dst 是文件还是目录决定）。"""
    root_dir = Path(root)
    src = _existing(root_dir, _check_tree_rel(src_rel))
    dst_parts = _check_tree_rel(dst_rel)
    if src.is_file() and Path(dst_parts[-1]).suffix.lower() not in IMAGE_SUFFIXES:
        raise SampleRejected("换架子时文件名要带着 .jpg / .png 这样的后缀")
    dst = root_dir.joinpath(*dst_parts).resolve()
    if dst.exists():
        raise SampleRejected(f"图库里已经有这个了：{dst_rel}")
    if not dst.is_relative_to(root_dir.resolve()):
        raise SampleRejected("目标路径跑到了图库外面")
    if src.is_dir() and str(dst).startswith(str(src) + os.sep):
        raise SampleRejected("不能把一个分类挪到它自己里面去")
    _mkdir_private(dst.parent, root_dir)
    src.rename(dst)
    _drop_thumb(root_dir, src_rel)  # 缓存是按路径存的，旧路径的那份直接扔，下次要再看
    return "/".join(dst_parts)


def remove_path(root: Path | str, rel: str) -> None:
    """删一张图，或删一个**已经空了**的架子。"""
    root_dir = Path(root)
    parts = _check_tree_rel(rel)
    found = root_dir.joinpath(*parts).resolve()
    if not found.is_relative_to(root_dir.resolve()):
        raise SampleRejected("图库里的路径跑到了外面")
    if not found.exists():
        raise SampleRejected(f"图库里没有这个：{rel}")
    if found.is_file():
        found.unlink()
        _drop_thumb(root_dir, rel)
        return
    left = len([f for f in found.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES])
    if left:
        raise SampleRejected(f"{parts[-1]} 里还有 {left} 张图，先清干净再删这一类")
    shutil.rmtree(found)
    _drop_thumb(root_dir, rel)


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
