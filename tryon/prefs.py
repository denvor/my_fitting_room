"""来源偏好：现在走 token plan API 还是本机 ComfyUI。存 data/settings.json，不用数据库。"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

VALID_SOURCES: tuple[str, str] = ("api", "comfyui")
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"


@dataclass(frozen=True)
class Prefs:
    source: str = "api"
    comfy_url: str = DEFAULT_COMFY_URL


def load_prefs(file: Path | str, defaults: Prefs | None = None) -> Prefs:
    """读不到、读不懂、值不认识 —— 一律回落到 defaults 对应那一项，不抛异常。

    单项坏只丢那一项：来源写坏了不该把用户填好的地址一起清掉。
    """
    base = defaults or Prefs()
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError 盖住 JSONDecodeError 与 UnicodeDecodeError
        return base
    if not isinstance(data, dict):
        return base
    source = data.get("source")
    comfy_url = data.get("comfy_url")
    return Prefs(
        source=source if source in VALID_SOURCES else base.source,
        comfy_url=comfy_url.strip() if isinstance(comfy_url, str) and comfy_url.strip() else base.comfy_url,
    )


def save_prefs(file: Path | str, prefs: Prefs) -> None:
    """先写临时文件再 replace：别让半截设置文件把默认值带跑。写失败不兜，让调用方报错。"""
    path = Path(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"source": prefs.source, "comfy_url": prefs.comfy_url}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)
