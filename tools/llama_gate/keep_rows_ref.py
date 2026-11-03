"""把 §19 那份 llama.cpp 的 2865 行 pre-norm 向量，切出**采样器真吃的 115 行**，存成注入用的 .pt。

行号算术（与 `comfy/text_encoders/qwen_image21.py:53-66` 同一套规则，不是抄来的）：
  完整序列 2865 行 = text 22 + image 1728 + text 6 + image 1008 + text 101
  丢 system turn（前 14 行，即第二个 <|im_start|> 之前）→ 头部留 14..21（8 行）
  丢两段视觉 token（span 起点取自 `spans.json`，是**丢头之前**的完整序列坐标 —— §18 就是拿它去切丢过头的张量，
  整块错 14 行，那个"带图 0.687"就是这么造出来的）→ 中间留 1750..1755（6 行）、尾部留 2764..2864（101 行）
  合计 8 + 6 + 101 = 115

三道断言，就是为了不再犯 §18 那种错：行数、与 ComfyUI 那份的余弦、`image_slots` 按规则重算等于盘上那份。
"""

import json

import numpy as np
import torch

G = "/tmp/q3vl-gate"
OUT = "/tmp/llama-inj/cond.pt"

meta = json.load(open(G + "/meta.json"))
spans = json.load(open(G + "/spans.json"))["image_spans_在完整序列里"]
rows, n_embd = int(meta["total_rows"]), int(meta["n_embd"])
pre = torch.from_numpy(np.fromfile(G + "/pre.bin", dtype=np.float32)).view(rows, n_embd)

# 按 token 数累加出行区间（start_pos 是 M-RoPE 位置，不是行号 —— §19 记过这个坑）
cursor, segs = 0, []
for c in meta["chunks"]:
    segs.append((c["kind"], cursor, int(c["n_tokens"])))
    cursor += int(c["n_tokens"])
assert cursor == rows, f"分段累加 {cursor} ≠ 总行数 {rows}"
img_segs = [(s, n) for kind, s, n in segs if kind == "image"]
assert img_segs == [(s, n) for s, n in spans], f"llama 的 image 分段 {img_segs} 与 ComfyUI 的 image_spans {spans} 不一致"

SYSTEM = 14  # qwen_image21.py 丢掉的 system turn 长度（§18 那 14 格偏移就是它）
idx = list(range(SYSTEM, img_segs[0][0]))                                  # 头部
idx += list(range(img_segs[0][0] + img_segs[0][1], img_segs[1][0]))        # 中间
idx += list(range(img_segs[1][0] + img_segs[1][1], rows))                   # 尾部

kept = torch.load(G + "/cond_kept.pt", weights_only=False)
ref = kept["hidden"][0].float()
assert len(idx) == ref.shape[0] == 115, f"行数不等：llama 切出 {len(idx)}，ComfyUI {tuple(ref.shape)}"

sel = torch.tensor(idx)
cond = pre[sel].unsqueeze(0)
cos = float(torch.nn.functional.cosine_similarity(cond.reshape(-1), ref.reshape(-1), dim=-1))
per_row = float(torch.nn.functional.cosine_similarity(cond[0], ref, dim=-1).mean())

# image_slots 按 qwen_image21.py 的规则重算：每张图起点之前还剩多少 kept 行
keep = torch.ones(rows, dtype=torch.bool)
keep[:SYSTEM] = False
for start, size in img_segs:
    keep[start:start + size] = False
slots = [int(keep[:start].sum()) for start, _ in img_segs]
assert slots == kept["mask"]["image_slots"], f"image_slots 重算 {slots} ≠ 盘上 {kept['mask']['image_slots']}"

torch.save({"hidden": cond, "image_slots": slots,
            "provenance": {"来源": "llama.cpp b12073 ROCm + qwen3vl.cpp 本地补丁的 layer_inp[36]",
                           "行数算术": [len(idx), len(img_segs)], "与 ComfyUI 那份的整体余弦": round(cos, 5),
                           "逐行余弦均值": round(per_row, 5)}}, OUT)
print(json.dumps({"存到": OUT, "形状": list(cond.shape), "image_slots": slots,
                  "整体余弦": round(cos, 5), "逐行余弦均值": round(per_row, 5),
                  "相对L2": round(float((cond[0] - ref).norm() / ref.norm()), 4)}, ensure_ascii=False), flush=True)
