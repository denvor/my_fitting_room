"""直调取数二进制、在自己进程里切行 —— llama.cpp 那一档的客户端。

为什么要有这一层而不是让节点去起 `encode.py`：**节点本来就跑在 ComfyUI 进程里，torch 早就 import 好了**，
而 §24 那条路是 ComfyUI → python 子进程 → C++ 子进程，光"再起一个 python"就白交 2.4 秒
（实测：起解释器 + `import torch` 0.7 + `import comfy.text_encoders.qwen_image21` 1.3，见 §25）。

链路：`run_gate()` 跑二进制拿 `pre.bin` + `meta.json` → `gate_layout()` 对账拿行号 → 按行读出条件。
pre-norm 的理由、行算术的出处都和 §19/§24 一样，这儿不重讲（见 `keep_rows.py` 与 dump_hidden.cpp 头注释）。
"""

import json
import os
import subprocess
from pathlib import Path

from tools.llama_gate.keep_rows import kept_row_index

# 本机的权重（换机器改这儿，或用命令行/节点输入覆盖）
GGUF = "/home/denvor/work/models/unsloth/Qwen3-VL-8B-Instruct-1M-UD-Q8_K_XL.gguf"
MMPROJ = "/home/denvor/work/models/unsloth/mmproj-Qwen3-VL-8B-BF16.gguf"
LLAMA_ROOT = "/home/denvor/work/llama/llama.cpp"   # 动态库在这，rpath 已编进二进制，这里只备查
GATE_TIMEOUT = 600   # 一次取数 ≈ 7 秒（§25 分段账），留一百倍余量给冷盘与别的进程抢 GPU


def repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def resolve_gate_bin() -> str:
    """取数程序在哪儿：环境变量 `LLAMA_GATE_BIN` 优先，否则 repo 里的 `tools/llama_gate/bin/`。

    默认值**不能是 /tmp**：那个二进制是拿打了本地补丁的 llama.cpp 编出来的（`qwen3vl.cpp` 里 +15 行，
    见 TODO.md T3），编一次不容易，落在 /tmp 等于每次重启都要重编一遍，而节点报的错只会是"没有那个文件"。
    """
    env = os.environ.get("LLAMA_GATE_BIN")
    if env:
        return env
    return os.path.join(repo_root(), "tools", "llama_gate", "bin", "dump_hidden")


def gate_layout(meta: dict, byte_size: int) -> tuple[list[int], list[int]]:
    """对账：这份 meta 与落地的 pre.bin 是不是同一件事，然后给要读的行号与图像槽位。

    两道对账各有失败法，都不响的话拿到的是**条件张量**而不是崩溃：
      · `total_rows` 与 chunks 段长合计不符 ⇒ meta 是上一次跑的（指错目录、或者被并发覆盖）。
      · 字节数不够 ⇒ 二进制被杀 / 盘满，后半截行是 0 —— DiT 照样跑完，出一张莫名其妙的图。
    """
    n_embd = int(meta["n_embd"])
    total_rows = int(meta["total_rows"])
    chunk_rows = sum(int(c["n_tokens"]) for c in meta["chunks"])
    if total_rows != chunk_rows:
        raise ValueError(f"meta 里 total_rows={total_rows}，chunks 段长合计 {chunk_rows} —— 这份 meta 不是这次取的数")
    need = total_rows * n_embd * 4
    if byte_size < need:
        raise ValueError(f"pre.bin 只有 {byte_size} 字节，按 {total_rows} 行 × {n_embd} 列要 {need} 字节 —— "
                         f"取数程序大概没跑完（盘满、被杀，或者显存不够中途退出）")
    idx, slots = kept_row_index(meta["chunks"])
    if len(idx) == total_rows:
        raise ValueError(f"一行都没丢掉（{total_rows} 行全留）—— 分段或标记算错了，别把这条序列喂给 DiT")
    return idx, slots


def run_gate(images: list[str], prompt: str, out_dir: str, gate_bin: str | None = None,
             gguf: str = GGUF, mmproj: str = MMPROJ, want_post: bool = False,
             timeout: int = GATE_TIMEOUT) -> dict:
    """跑一次取数程序，返回 meta（`pre.bin` 落在 out_dir 下；`_dir` 一并塞进返回值，下游不用再猜目录）。

    `want_post` 默认关：DiT 要的是**没过最终 RMSNorm** 的那份（`comfy/text_encoders/qwen_image21.py:34`
    的 `layer_norm_hidden_state=False`），post.bin 一次 45 MB，写了没人读。
    """
    gate_bin = gate_bin or resolve_gate_bin()
    if not os.path.isfile(gate_bin):
        raise RuntimeError(f"取数程序不在：{gate_bin}。照 tools/llama_gate/dump_hidden.cpp 头部那行 g++ 命令，"
                           f"在打了本地补丁的 llama.cpp（{LLAMA_ROOT}）里编译一个出来")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pfile = out / "prompt.txt"
    pfile.write_text(prompt, encoding="utf-8")
    cmd = [gate_bin, "-m", gguf, "--mmproj", mmproj, "--prompt", str(pfile), "--out", str(out)]
    if not want_post:
        cmd.append("--no-post")
    for img in images:
        cmd += ["--img", img]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"取数程序退出码 {proc.returncode}：{proc.stderr[-600:]}")
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    meta["_dir"] = str(out)
    return meta


def cond_from_meta(meta: dict):
    """pre.bin + meta → (hidden [1,N,n_embd] torch 张量, image_slots)。

    按行取而不是整块搬：全序列 2865 行 × 4096 是 45 MB，要留的只有 115 行（1.9 MB）。
    这几行读盘代码没进单测（项目 `.venv` 里没有 torch/numpy），改由真机逐位验证盯 ——
    它必须还原出与 §24 那份 `cond.pt` 一字不差的张量，见实测记录 §25。
    """
    import numpy as np
    import torch

    path = Path(meta["_dir"]) / "pre.bin"
    idx, slots = gate_layout(meta, path.stat().st_size)
    pre = np.memmap(path, dtype=np.float32, mode="r", shape=(int(meta["total_rows"]), int(meta["n_embd"])))
    return torch.from_numpy(np.ascontiguousarray(pre[idx])).unsqueeze(0), slots
