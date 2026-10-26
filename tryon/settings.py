"""从 .env 读取接口地址、密钥和模型名。"""

from dataclasses import dataclass
from pathlib import Path

GENERATION_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
COMPATIBLE_SUFFIX = "/compatible-mode/v1"


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


def load_settings(env_file: str | Path = ".env") -> Settings:
    values: dict[str, str] = {}
    for line in Path(env_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip()

    for required in ("OPENAI_URL", "API_KEY", "MODEL"):
        if not values.get(required):
            raise SettingsError(f"{env_file} 里缺少 {required}")

    return Settings(
        endpoint=derive_endpoint(values["OPENAI_URL"]),
        api_key=values["API_KEY"],
        model=values["MODEL"],
    )
