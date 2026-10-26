"""从 .env 读取接口地址、密钥和模型名。"""

from dataclasses import dataclass
from pathlib import Path

GENERATION_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
COMPATIBLE_SUFFIX = "/compatible-mode/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    endpoint: str
    api_key: str
    model: str


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
            f"找不到 {path}。把 .env 放在项目根目录（在 {PROJECT_ROOT} 里运行 python app.py 也可以）。"
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

    return Settings(
        endpoint=derive_endpoint(values["OPENAI_URL"]),
        api_key=values["API_KEY"],
        model=values["MODEL"],
    )
