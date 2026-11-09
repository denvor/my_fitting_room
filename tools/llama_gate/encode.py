"""llama.cpp 那一档的命令行入口：两张（已 resize 好的）PNG + 一句指令 → 采样器要的那 `[1,N,4096]` .pt。

**生产不走这个文件**：走的是 `comfy_nodes/qwen21_precomp_cond.py`，它在 ComfyUI 进程里直接调
`gate_client`（起一个 python 子进程去 import torch 白花 2.4 秒，实测见 §25）。
这个 CLI 留着做研究和离线复现：§19/§23/§24 那些判读都是拿它跑出来的，`--dry-run` 也是核对提示字节的地方。

链路与两个不显然的点（pre-norm、像素必须是节点自己 resize 完的那两份）都在 `gate_client.py`
与 `prompt_build.py` 的头注释里，这儿不重复。

用 ComfyUI 那套 venv 的 python 跑（项目 `.venv` 里没有 torch）：
    /home/denvor/work/ComfyUI/.venv/bin/python tools/llama_gate/encode.py --img a.png --img b.png \
        --prompt "把衣服穿上" --out /tmp/cond.pt
（取数服务要先在跑：systemctl status llama-gate）
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # 仓库根，好 import tools.llama_gate.*

from tools.llama_gate.gate_client import run_gate  # noqa: E402,F401
from tools.llama_gate.prompt_build import media_prompt  # noqa: E402,F401 —— re-export：老调用方还从这儿拿


def main() -> int:
    ap = argparse.ArgumentParser(description="llama.cpp 编码 → 注入用的 .pt")
    ap.add_argument("--img", action="append", required=True, help="已 resize 的 PNG，顺序=图像1、图像2…")
    ap.add_argument("--prompt", required=True, help="用户指令原文（不含模板）")
    ap.add_argument("--out", required=True, help="输出 .pt 路径")
    ap.add_argument("--comfy-root", default="/home/denvor/work/ComfyUI")
    ap.add_argument("--sock", default=None, help="取数服务的 socket，默认 /run/llama-gate/gate.sock")
    ap.add_argument("--debug", action="store_true", help="要全序列 + meta（离线判读用），不是切好的 115 行")
    ap.add_argument("--dry-run", action="store_true", help="只打印拼出来的提示，不跑模型")
    a = ap.parse_args()

    sys.path.insert(0, a.comfy_root)
    import comfy.text_encoders.qwen_image21 as qi   # 模板常量以本地 ComfyUI 为准，别在这儿抄第二份

    prompt = media_prompt(qi.T2I_TEMPLATE, a.prompt, len(a.img))
    if a.dry_run:
        print(prompt)
        return 0
    from tools.llama_gate.protocol import FLAG_DEBUG

    hidden, slots, meta = run_gate(a.img, prompt, sock=a.sock, flags=FLAG_DEBUG if a.debug else 0)
    import torch

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    blob = {"hidden": hidden, "image_slots": slots,
            "provenance": {"行算术": "tools/llama_gate/llama_gate.cpp"}}
    if a.debug:
        # meta.json 的原文：离线判读（§19/§23/§24 那套）要靠它重建切行坐标（chunks 的 ids）
        blob["meta.json"] = meta
    torch.save(blob, a.out)
    print(json.dumps({"out": a.out, "形状": list(hidden.shape), "image_slots": slots}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
