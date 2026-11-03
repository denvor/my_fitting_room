"""从 llama.cpp 的整条 hidden state 里，切出**采样器真正吃的那一段**，外加每张图的 latent 槽位。

这是 `comfy/text_encoders/qwen_image21.py:53-66`（`QwenImage21TEModel.encode_token_weights`）
那套规则在 Python 侧的对等移植，逐条照抄：

1. **丢掉 system turn** —— 第二个 im_start（151644）之前的全部行（上游是 `keep[:im_starts[1]] = False`）。
   锚点从 token id 现算：文本段里 id 等于 151644 的就是 im_start，
   而它的**行位置**要把前面每个图像段"一个占位符代表 size 行"补进偏移 —— 上游那句
   `offset += next(spans, (0, 1))[1] - 1` 就是干这个的。§19 的判读脚本把这个位置写死成 14，
   那是研究代码；在线这一档不能写死，因为**错位不会崩，只会静默地画错**（§18 那个作废的
   "带图 0.687" 就是拿完整序列坐标去切已丢头的张量，整块错 14 行）。
2. **丢掉两段视觉 token** —— `image_spans` 是**丢掉 system turn 之前**的完整序列坐标。
   每丢一张，记下"这张图的 latent 要接在第几行之前" = `slots`，也就是上游的 `extra["image_slots"]`。

行区间一律由 `n_tokens` 累加，**绝不用 `start_pos`**：那是 M-RoPE 位置，1728 个图像 token 只推进约 2 个位置。
"""

IM_START = 151644  # qwen 词表里的 im_start；上游那条规则也是拿它当锚点（同一个值，别改这儿）


def kept_row_index(chunks: list[dict]) -> tuple[list[int], list[int]]:
    """输入 `meta.json` 的 chunks（每段要有 kind / n_tokens，文本段还要有 ids）。

    返回 `(保留的行号, image_slots)`。行数由分段累加得到，与 `total_rows` 不必一致 —— 调用方自己对一下。
    """
    tokens: list[object] = []      # 上游那份 token 列表的形状：文本是裸 id，图像是一个占位符
    spans: list[tuple[int, int]] = []
    cursor = 0
    for c in chunks:
        n = int(c["n_tokens"])
        if c["kind"] == "text":
            ids = c["ids"]         # 缺这个键就让它 KeyError：没 id 算不出锚点，退回写死更糟
            if len(ids) != n:
                raise ValueError(f"文本段 n_tokens={n} 与 ids 长度 {len(ids)} 不一致，分段不可信")
            tokens.extend(ids)
        elif c["kind"] == "image":
            tokens.append(("image", n))
            spans.append((cursor, n))
        else:
            raise ValueError(f"不认识的段类型 {c['kind']!r}")
        cursor += n

    im_starts: list[int] = []
    offset = 0
    spans_iter = iter(spans)
    for i, t in enumerate(tokens):
        if isinstance(t, tuple):
            offset += next(spans_iter)[1] - 1
        elif t == IM_START:
            im_starts.append(i + offset)

    keep = [True] * cursor
    for r in range(im_starts[1] if len(im_starts) > 1 else 0):
        keep[r] = False
    slots: list[int] = []
    for start, size in spans:
        for r in range(start, start + size):
            keep[r] = False
        slots.append(sum(keep[:start]))
    return [r for r, v in enumerate(keep) if v], slots
