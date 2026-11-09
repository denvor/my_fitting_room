"""连取数服务拿条件张量 —— llama.cpp 那一档的客户端。

链路：`run_gate()` 把图 + 指令发过 Unix socket → 服务端解算并切行 → 拿回 `[1,115,4096]` 的 pre-norm 条件。
服务是常驻的（`tools/llama_gate/llama_gate.cpp`，systemd 管），所以每发省掉"起进程 + 载 8.9 GB 模型"
的 1.44 秒（§25.1 分段计时）。

为什么节点不自己去起 `encode.py`：**节点本来就跑在 ComfyUI 进程里，torch 早就 import 好了**，
而起一个 python 子进程光 import 就白交 2.4 秒（实测见 §25）。

pre-norm 的理由、行算术的出处都和 §19/§24 一样，这儿不重讲（见 `keep_rows.py` 与 llama_gate.cpp 头注释）。
"""

import os
from pathlib import Path

from tools.llama_gate.keep_rows import kept_row_index

DEFAULT_SOCK = "/run/llama-gate/gate.sock"
GATE_TIMEOUT = 600   # 一次取数 ≈ 7 秒（§25 分段账），留一百倍余量给冷盘与别的进程抢 GPU


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


def run_gate(images: list[str], prompt: str, sock: str | None = None,
             flags: int = 0, timeout: int = GATE_TIMEOUT) -> tuple["object", list[int], bytes]:
    """连取数服务，发图 + 指令，拿回 (hidden [1,N,n_embd], image_slots, meta)。

    `meta` 只在 `flags` 带 FLAG_DEBUG 时有内容（服务端回的 meta.json，离线判读要用它重建切行坐标）；
    普通请求是空 bytes。
    服务不在时报一句人话（现场要的是"去看 systemctl status"，不是 ConnectionRefusedError）。
    切行在服务端做，客户端拿到的就是采样器真正吃的那 115 行。
    """
    import socket

    from tools.llama_gate.protocol import decode_response, encode_request

    sock = sock or os.environ.get("LLAMA_GATE_SOCK") or DEFAULT_SOCK

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(sock)
    except OSError as e:
        raise RuntimeError(
            f"取数服务不在（{sock}）：{e.strerror or e}。先看 systemctl status llama-gate"
        ) from e
    payload = encode_request([Path(p).read_bytes() for p in images], prompt, flags)
    try:
        conn.sendall(payload)
        chunks = []
        while True:
            b = conn.recv(1 << 20)
            if not b:
                break
            chunks.append(b)
    finally:
        conn.close()

    # numpy/torch 只在真要组装张量时才 import：项目 `.venv` 里没有它们（节点跑在 ComfyUI 进程里才有），
    # 而"连不上服务"这条路径不该被它们挡住
    import numpy as np
    import torch

    want_debug = bool(flags & 1)
    status, rows, n_embd, slots, tensor, meta = decode_response(b"".join(chunks), want_debug=want_debug)
    hidden = torch.from_numpy(np.frombuffer(tensor, dtype=np.float32).copy()).reshape(1, rows, n_embd)
    return hidden, slots, (meta if want_debug else b"")
