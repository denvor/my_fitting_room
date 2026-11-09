"""从 .env 读取接口地址、密钥和模型名。"""

from dataclasses import dataclass, replace
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
    # 第 3 档（llama.cpp 出编码）的两条部署路径。默认就指着**本 repo**：tools/llama_gate 在这儿，
    # 取数服务的客户端（gate_client）也在这儿。
    #
    # **这两条是 ComfyUI 那台机器上的路径，不是应用这台的。** 节点跑在 ComfyUI 进程里，它 import
    # gate_client、往 work_dir 落中转图、连那台的取数服务，用的都是 ComfyUI 所在机器的文件。
    # 应用与 ComfyUI 同机时默认值就对；异机时要用 .env 覆盖成 ComfyUI 那台的路径
    # （TRYON_LLAMA_REPO_ROOT / TRYON_LLAMA_WORK_DIR），否则节点会去应用自己机器上找。
    # 服务不能跨机器搬：RUNPATH 指向那台的 llama.cpp 构建，换机器要在那台重跑 tools/llama_gate/build.sh。
    llama_repo_root: str = str(PROJECT_ROOT)
    llama_work_dir: str = str(PROJECT_ROOT / "data" / "llama-gate")
    # 取数服务的 socket（.env 的 TRYON_LLAMA_GATE_SOCK）
    llama_gate_sock: str = "/run/llama-gate/gate.sock"
    # ComfyUI 在远端时设 True（.env 的 TRYON_LLAMA_REMOTE）：服务在那一台，应用连的是自己的机器
    llama_remote: bool = False

    @property
    def llama_ready(self) -> bool:
        """第 3 档在这台机上成不成立：**取数服务连不连得上**。

        探的是 socket 而不是文件：服务化之后"就绪"是"服务在不在"，不是"二进制文件在不在"。
        探不到就让设置页把第 3 档按住、并说清去装服务（server.py 里那句），
        而不是让用户切过去才拿到一句看不出来根的"ComfyUI 报错"。

        **异机时（llama_remote）不探**：服务在 ComfyUI 那台，应用连的是自己的机器、连不到。
        这时信配置让第 3 档可选；真缺的话由节点在 ComfyUI 那台报"取数服务不在：…，先看 systemctl status llama-gate"。
        """
        if self.llama_remote:
            return True
        import socket

        sock = self.llama_gate_sock or "/run/llama-gate/gate.sock"
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(0.5)
        try:
            conn.connect(sock)
            return True
        except OSError:
            return False
        finally:
            conn.close()


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
    base = Settings(
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
    # 第 3 档那两条路径不写就用本 repo 自己（`tools/llama_gate` 与 `data/llama-gate` 本来就在那儿）：
    # 默认值只写在 dataclass 一处，免得 .env 缺键时两处说法不一致
    for field, key in (("llama_repo_root", "TRYON_LLAMA_REPO_ROOT"),
                       ("llama_work_dir", "TRYON_LLAMA_WORK_DIR"),
                       ("llama_gate_sock", "TRYON_LLAMA_GATE_SOCK")):
        if values.get(key):
            base = replace(base, **{field: values[key]})
    # ComfyUI 在远端时应用连不到服务（在那一台），llama_ready 改成信配置
    if values.get("TRYON_LLAMA_REMOTE", "").lower() in ("1", "true", "yes"):
        base = replace(base, llama_remote=True)
    return base
