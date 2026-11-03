"""来源偏好：走 token plan API、本机 ComfyUI，还是本机 ComfyUI 但编码换成 llama.cpp。

存 data/settings.json（不用数据库），.env 提供默认值；每个生成请求现读一次。
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

# 三个来源（denvor 2026-11-03 定的形态）：官方接口 / 本机 ComfyUI / 本机 ComfyUI 但编码换 llama.cpp
VALID_SOURCES: tuple[str, str, str] = ("api", "comfyui", "comfyui_llama")
LLAMA_SOURCE = "comfyui_llama"
# 后两个都落在本机那台 ComfyUI 上：不花额度、超时按本地算、2K 同样被 harakiri 掐 —— 判定只从这儿出一次
LOCAL_SOURCES = ("comfyui", LLAMA_SOURCE)
VALID_SIZES = ("480p", "720p", "1k", "2k")  # 顺序就是设置页里的顺序：小 → 大
DEFAULT_SIZE = "1k"  # 默认档：便宜、快；低档和 2K 都要人在设置页里自己切过去
# 低于 1K 的两档只有本机 ComfyUI 有：接口按面积分档，实测 0.31M 照样扣 58，还回一张输入尺寸的图
LOCAL_ONLY_SIZES = ("480p", "720p")
# 反方向：2K 只有接口那条给选。本机 2K 实测 543 秒，而线上 uwsgi.ini 里 harakiri = 120，
# 跑到两分钟就被 worker 掐了 —— 选了拿不到图，只拿到一个失败，所以这一档在本地这条按住。
API_ONLY_SIZES = ("2k",)


def is_local(source: str) -> bool:
    """这条来源是不是"跑在本机、不花额度"那两条之一。

    单独一个函数而不是到处写 `== "comfyui"`：第三档进来后每处漏掉一个判断，坏法都不一样 ——
    漏在额度上会白查一次接口额度，漏在超时上会拿 API 的 300 秒去量本地，漏在回退上会让人
    在 llama 档里被静默切到 API（花掉 58）。
    """
    return source in LOCAL_SOURCES
# 给人看的档位名：低档大写会变成 480P（那是型号写法），1K / 2K 得大写才像档位
SIZE_LABEL = {"480p": "480p", "720p": "720p", "1k": "1K", "2k": "2K"}
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"


def clamp_size(source: str, size: str) -> str:
    """和来源对不上的档位一律收成默认档，两个方向都有。

    不这么收的话，页面写着"低档"、账上按 1K 扣 58、出的还是输入尺寸那张 —— 三个字段各说一套；
    反方向写着 2K、本机跑五分钟以上（§17 实测 302.5 秒）、线上两分钟就被掐，拿到的是一个失败。
    """
    if source == "api" and size in LOCAL_ONLY_SIZES:
        return DEFAULT_SIZE
    if is_local(source) and size in API_ONLY_SIZES:
        return DEFAULT_SIZE
    return size


def fold_note(size: str) -> str:
    """把刚才收掉的那一档说清楚：静默改掉人刚点的那一项，比报错更难查。"""
    label = SIZE_LABEL.get(size, size)
    if size in LOCAL_ONLY_SIZES:
        return f"{label} 只有本机 ComfyUI 有这一档，接口那边最低就是 1K，已按 1K 存下"
    return f"{label} 在本机这条不给选（实测五分多钟，线上跑到两分钟就被掐），已按 1K 存下"


@dataclass(frozen=True)
class Prefs:
    # 默认走本机 ComfyUI：宁可 ComfyUI 没起时明确报错，也不静默花掉 58 credit
    source: str = "comfyui"
    comfy_url: str = DEFAULT_COMFY_URL
    size: str = DEFAULT_SIZE


def load_prefs(file: Path | str, defaults: Prefs | None = None) -> Prefs:
    """读不到、读不懂、值不认识 —— 一律回落到 defaults 对应那一项，不抛异常。

    单项坏只丢那一项：来源写坏了不该把用户填好的地址一起清掉。
    """
    base = defaults or Prefs()
    base = Prefs(base.source, base.comfy_url, clamp_size(base.source, base.size))  # .env 也能给出一对矛盾的值
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError 盖住 JSONDecodeError 与 UnicodeDecodeError
        return base
    if not isinstance(data, dict):
        return base
    source = data.get("source")
    comfy_url = data.get("comfy_url")
    size = data.get("size")
    chosen_source = source if source in VALID_SOURCES else base.source
    return Prefs(
        source=chosen_source,
        comfy_url=comfy_url.strip() if isinstance(comfy_url, str) and comfy_url.strip() else base.comfy_url,
        size=clamp_size(chosen_source, size if size in VALID_SIZES else base.size),
    )


def save_prefs(file: Path | str, prefs: Prefs) -> None:
    """先写临时文件再 replace：别让半截设置文件把默认值带跑。写失败不兜，让调用方报错。

    临时名必须是**每次调用都不同**的，不能是写死的 `settings.json.tmp`：这台线上是 4 进程 × 2 线程，
    同进程里两个请求就能撞在一起（首页那次自动切换和另一台手机的保存同时发生就是这种）。
    撞上的坏法不只是 500 —— 谁先 `replace` 谁就把别人写进去的内容当成自己的存了盘。
    """
    path = Path(file)
    # 700/600 写死，理由同 records.py：uWSGI 的 umask 是 0000，这里曾经落成过目录 777 + 文件 666
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp 建的临时文件本来就是 0600（umask 只能再往下削），replace 保留源文件权限 → 存完还是 0600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"source": prefs.source, "comfy_url": prefs.comfy_url, "size": prefs.size}, fh, ensure_ascii=False)
    os.replace(tmp, path)
