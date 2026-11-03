"""llama.cpp 那一档的"取哪 115 行"：丢掉 system turn 与全部视觉 token，锚点从 token id 现算。

这块规则是 `comfy/text_encoders/qwen_image21.py:53-66` 在 Python 侧的对等移植 —— 它必须与上游一致，
否则喂进 DiT 的条件不是上游那一份的同一段。§18 那个后来作废的"带图 0.687"就是拿错坐标切张量造出来的：
`image_spans` 是**丢掉 system turn 之前**的完整序列坐标，去切已经丢过头的张量就整块错 14 行。
所以这里专门有两条用例盯这两件事：锚点不许写死、`start_pos`（M-RoPE 位置）不许当行号用。
"""

import pytest

from tools.llama_gate.encode import media_prompt
from tools.llama_gate.keep_rows import kept_row_index

IM_START = 151644
# 合成模板，形状与上游 `qwen_image21.py` 的 T2I_TEMPLATE 一致：{} 那个洞就是要插指令的地方。
# 特殊标记在这儿**拼出来写**：整串字面量（竖线把 im_start 包起来那种）在我这侧的编辑通道里会被吞成空串，
# 这个坑今天踩过一次（`keep_rows_ref.py` 是从盘上拷来的，所以那份里的字面量完好）。别改回字面量。
NL = chr(10)
SEP = chr(60) + chr(124)          # 左尖括号 + 竖线
END = chr(124) + chr(62)          # 竖线 + 右尖括号


def _tok(name):
    return SEP + name + END


def _turn(body):
    return _tok("im_start") + chr(10) + body + _tok("im_end") + chr(10)


TPL = (_turn("system" + NL + "读题。") + _turn("user" + NL + "{}") + _tok("im_start") + "assistant" + NL)
# §19 / §23 那一发的真实分段：文本 22（第二个 im_start 在第 14 行）+ 图 1728 + 文本 6 + 图 1008 + 文本 101
REAL_CHUNKS = [
    {"kind": "text", "n_tokens": 22, "start_pos": 0,
     "ids": [IM_START] + [7001] * 13 + [IM_START] + [7002] * 7},
    {"kind": "image", "n_tokens": 1728, "start_pos": 22},
    {"kind": "text", "n_tokens": 6, "start_pos": 70, "ids": [151653, 366, 1805, 17, 29, 151652]},
    {"kind": "image", "n_tokens": 1008, "start_pos": 76},
    {"kind": "text", "n_tokens": 101, "start_pos": 112, "ids": [151653] + [8000 + i for i in range(100)]},
]


def test_真实分段切出115行与槽位8_14():
    idx, slots = kept_row_index(REAL_CHUNKS)
    assert len(idx) == 115                        # 头部 8 + 中间 6 + 尾部 101
    assert idx[:3] == [14, 15, 16]
    assert idx[8] == 1750                         # 中间那段文字（人像那 1728 行整块丢掉）
    assert idx[-1] == 2864
    assert slots == [8, 14]                       # 每张图的 latent 接在第几行之前


def test_system_turn变短_边界跟着走():
    """把 system turn 从 14 行改成 12 行：头部保留数必须跟着变，证明锚点是算出来的不是写死的。"""
    chunks = [{"kind": "text", "n_tokens": 20,
               "ids": [IM_START] + [7001] * 11 + [IM_START] + [7002] * 7},
              {"kind": "image", "n_tokens": 6},
              {"kind": "text", "n_tokens": 2, "ids": [151653, 151652]},
              {"kind": "image", "n_tokens": 4},
              {"kind": "text", "n_tokens": 5, "ids": [151653, 8000, 8001, 8002, 8003]}]
    idx, slots = kept_row_index(chunks)
    assert idx == list(range(12, 20)) + [26, 27] + list(range(32, 37))
    assert slots == [8, 10]


def test_start_pos是M_RoPE位置_拿去当行号就错():
    """图像块只推进约 2 个位置却有 1728 个 token（§19 记过这个读法坑）：行区间一律由 n_tokens 累加。"""
    bogus = [{**c, "start_pos": 999} for c in REAL_CHUNKS]
    assert kept_row_index(bogus) == kept_row_index(REAL_CHUNKS)


def test_没有图时只丢system_turn不留槽位():
    chunks = [{"kind": "text", "n_tokens": 11, "ids": [IM_START] + [7001] * 5 + [IM_START] + [7002] * 4}]
    idx, slots = kept_row_index(chunks)
    assert idx == [6, 7, 8, 9, 10]      # 第二个 im_start 在第 6 行，其后 5 行全留
    assert slots == []


def test_文本段缺ids就报错_不许退回写死的14():
    """§19 那份 meta.json 没有 ids 字段，拿来当输入必须当场失败。"""
    with pytest.raises(KeyError):
        kept_row_index([{"kind": "text", "n_tokens": 22}, {"kind": "image", "n_tokens": 1728}])


def test_两图时每个视觉块位置换成一个媒体标记():
    """数量必须等于 bitmap 数，否则 `mtmd_tokenize` 返回 1（§18 那条通路坑）。"""
    p = media_prompt(TPL, "把衣服穿上", 2)
    assert p.count("<__media__>") == 2
    assert "<image1><__media__> <image2><__media__>把衣服穿上" in p
    assert "<|vision_start|>" not in p          # 说明文字整块换掉，不留残文


def test_无图时模板原样只有指令():
    p = media_prompt(TPL, "把衣服穿上", 0)
    assert p.count("<__media__>") == 0
    assert p.endswith("assistant\n") and "把衣服穿上<" in p
