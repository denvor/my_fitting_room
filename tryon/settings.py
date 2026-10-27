"""从 .env 读取接口地址、密钥和模型名。"""

from dataclasses import dataclass
from pathlib import Path

from tryon.prefs import DEFAULT_COMFY_URL, DEFAULT_SIZE, VALID_SIZES, VALID_SOURCES

GENERATION_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
COMPATIBLE_SUFFIX = "/compatible-mode/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    endpoint: str
    api_key: str
    model: str
    # 两个可选增强项：.env 里没写就用默认值，不报缺配置
    source: str = "comfyui"
    comfy_url: str = DEFAULT_COMFY_URL
    # 口令为空 = 登录网关整个不装（局域网开发靠它免登录）；公网入口 mfr_wsgi 会拦住启动
    password: str = ""
    # 站在 HTTPS 反代后面时设 TRYON_COOKIE_SECURE=1，登录 cookie 才只走加密通道
    cookie_secure: bool = False
    size: str = DEFAULT_SIZE


class SettingsError(Exception):
    """配置有问题，消息是给人看的中文。"""


def derive_endpoint(openai_url: str) -> str:
    """把 .env 里的 OpenAI 兼容地址换成同主机上的图像生成地址。"""
    base = openai_url.strip().rstrip("/")
    if base.endswith(COMPATIBLE_SUFFIX):
        base = base[: -len(COMPATIBLE_SUFFIX)]
    return base + GENERATION_PATH


def _unquote(val: str) -> str:
    """API_KEY="sk-..." 这种写法里的成对引号要去掉，否则会跟着进 Bearer 头。"""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    return val


def load_settings(env_file: str | Path | None = None) -> Settings:
    """env_file 不给时按项目根目录找 .env，这样从任何目录启动都对。"""
    path = PROJECT_ROOT / ".env" if env_file is None else Path(env_file)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise SettingsError(
            f"找不到 {path}。把 .env 放在项目根目录（在 {PROJECT_ROOT} 里运行 python dev_run.py 也可以）。"
        )

    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = _unquote(val.strip())

    for required in ("OPENAI_URL", "API_KEY", "MODEL"):
        if not values.get(required):
            raise SettingsError(f"{path} 里缺少 {required}")

    env_source = values.get("TRYON_SOURCE")
    return Settings(
        endpoint=derive_endpoint(values["OPENAI_URL"]),
        api_key=values["API_KEY"],
        model=values["MODEL"],
        # 来源写错不拦启动：这是可选增强项，按默认来源走比报错有用
        source=env_source if env_source in VALID_SOURCES else "comfyui",
        comfy_url=values.get("COMFY_URL") or DEFAULT_COMFY_URL,
        password=values.get("TRYON_PASSWORD", ""),
        cookie_secure=values.get("TRYON_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
        # 档位写错照样不拦启动，按默认档走（和 TRYON_SOURCE 一个道理）
        size=values.get("TRYON_SIZE") if values.get("TRYON_SIZE") in VALID_SIZES else DEFAULT_SIZE,
    )
