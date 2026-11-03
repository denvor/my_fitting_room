"""llama.cpp 那一档的编码入口：两张（已 resize 好的）PNG + 一句指令 → 采样器要的那 `[1,N,4096]` .pt。

链路（一次调用全在这里，节点只负责把像素与指令递进来、把 .pt 读回去）：

    media_prompt() 拼出带 <__media__> 的那句 → dump_hidden（补丁版 llama.cpp，见 dump_hidden.cpp 头注释）
    → pre.bin（整条 pre-norm）+ meta.json（分段与 token id）→ kept_row_index() 切出要留的行 → .pt

两个不显然的点：
  · **pre-norm 而不是 post-norm**：DiT 吃的是"最后一层 hidden state、没过最终 RMSNorm"那一份
    （`comfy/text_encoders/qwen_image21.py:34` 的 `layer_norm_hidden_state=False`）。llama.cpp 默认出口是
    post-norm，pre-norm 得靠 `src/models/qwen3vl.cpp` 的本地补丁 + `llama-ext.h` 那个 staging 口，
    而且 **HTTP 永远拿不到** —— 所以这里是自己编的二进制，不是 llama-server。
  · **像素必须是节点自己 resize 完的那两份**（8 位 PNG 无损往返）。换一份像素就等于换了条件，
    §19 那些余弦数也就不成立了。

用 python 直接跑，或 `--dry-run` 只看它会拼出什么提示。
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # 仓库根，好 import tools.llama_gate.*

from tools.llama_gate.keep_rows import kept_row_index  # noqa: E402

# 本机的权重与二进制（换机器要改，或者用命令行覆盖）
GGUF = "/home/denvor/work/models/unsloth/Qwen3-VL-8B-Instruct-1M-UD-Q8_K_XL.gguf"
MMPROJ = "/home/denvor/work/models/unsloth/mmproj-Qwen3-VL-8B-BF16.gguf"
GATE_BIN = "/tmp/llama-gate/dump_hidden"
LLAMA_ROOT = "/home/denvor/work/llama/llama.cpp"   # 动态库在这，rpath 已编进去，这里只备查
MEDIA_MARKER = "<__media__>"                        # mtmd_context_params.media_marker，与 --prompt 里的必须一致


def media_prompt(template: str, user_text: str, n_images: int) -> str:
    """把 ComfyUI 那句 ChatML 模板里的视觉说明块，换成 llama 的媒体标记。

    上游的插法（`comfy_nodes/qwen21_posonly_encode.py:104` 那一句的等价形式）是在 `{}` 前面拼
    `<image1>VISION_BLOCK <image2>VISION_BLOCK{}` —— VISION_BLOCK 是给 LLM 看的一句说明文字。
    llama.cpp 那侧同样的位置要放**每图一个** `MEDIA_MARKER`（§19 的 `prompt_llama_media.txt` 就是这么来的），
    而且标记数必须等于 bitmap 数，否则 `mtmd_tokenize` 直接返回 1。
    """
    slots = " ".join(f"<image{i + 1}>{MEDIA_MARKER}" for i in range(n_images))
    return template.replace("{}", slots + "{}", 1).format(user_text)


def run_gate(images: list[str], prompt: str, out_dir: str, gate_bin=GATE_BIN, gguf=GGUF, mmproj=MMPROJ) -> dict:
    """跑一次取数程序，返回 meta（pre.bin / post.bin 落在 out_dir 下）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pfile = out / "prompt.txt"
    pfile.write_text(prompt, encoding="utf-8")
    cmd = [gate_bin, "-m", gguf, "--mmproj", mmproj, "--prompt", str(pfile), "--out", str(out)]
    for img in images:
        cmd += ["--img", img]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"取数程序退出码 {proc.returncode}：{proc.stderr[-600:]}")
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    meta["_dir"] = str(out)
    return meta


def cond_from_meta(meta: dict):
    """pre.bin + meta → (hidden [1,N,n_embd], slots)。"""
    import numpy as np
    import torch

    n = int(meta["n_embd"])
    rows = int(meta["total_rows"])
    pre = torch.from_numpy(np.fromfile(Path(meta["_dir"]) / "pre.bin", dtype=np.float32)).view(rows, n)
    idx, slots = kept_row_index(meta["chunks"])     # 行算术的唯一出处在 keep_rows.py，这儿不重复一遍
    if len(idx) == rows:
        raise ValueError(f"一行都没丢掉（{rows} 行全留）—— 分段或标记算错了，别把这条序列喂给 DiT")
    return pre[idx].unsqueeze(0), slots


def main() -> int:
    ap = argparse.ArgumentParser(description="llama.cpp 编码 → 注入用的 .pt")
    ap.add_argument("--img", action="append", required=True, help="已 resize 的 PNG，顺序=图像1、图像2…")
    ap.add_argument("--prompt", required=True, help="用户指令原文（不含模板）")
    ap.add_argument("--out", required=True, help="输出 .pt 路径")
    ap.add_argument("--comfy-root", default="/home/denvor/work/ComfyUI")
    ap.add_argument("--gate-bin", default=GATE_BIN)
    ap.add_argument("--gguf", default=GGUF)
    ap.add_argument("--mmproj", default=MMPROJ)
    ap.add_argument("--work", default="", help="取数程序的输出目录，默认临时目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印拼出来的提示，不跑模型")
    a = ap.parse_args()

    sys.path.insert(0, a.comfy_root)
    import comfy.text_encoders.qwen_image21 as qi   # 模板常量以本地 ComfyUI 为准，别在这儿抄第二份

    prompt = media_prompt(qi.T2I_TEMPLATE, a.prompt, len(a.img))
    if a.dry_run:
        print(prompt)
        return 0
    work = a.work or tempfile.mkdtemp(prefix="llama-gate-")
    meta = run_gate(a.img, prompt, work, a.gate_bin, a.gguf, a.mmproj)
    hidden, slots = cond_from_meta(meta)
    import torch

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    torch.save({"hidden": hidden, "image_slots": slots,
                "provenance": {"行算术": "tools/llama_gate/keep_rows.py", "段数": len(meta["chunks"]),
                               "总行数": meta["total_rows"], "补丁取的层": meta.get("补丁取的层")}}, a.out)
    print(json.dumps({"out": a.out, "形状": list(hidden.shape), "image_slots": slots,
                      "work": work}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
