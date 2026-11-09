"""llama.cpp 那一档的"取哪 115 行"：丢掉 system turn 与全部视觉 token，锚点从 token id 现算。

这块规则是 `comfy/text_encoders/qwen_image21.py:53-66` 在 Python 侧的对等移植 —— 它必须与上游一致，
否则喂进 DiT 的条件不是上游那一份的同一段。§18 那个后来作废的"带图 0.687"就是拿错坐标切张量造出来的：
`image_spans` 是**丢掉 system turn 之前**的完整序列坐标，去切已经丢过头的张量就整块错 14 行。
所以这里专门有两条用例盯这两件事：锚点不许写死、`start_pos`（M-RoPE 位置）不许当行号用。
"""

import pytest

from tools.llama_gate.encode import media_prompt
from tools.llama_gate.gate_client import gate_layout, run_gate
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


# —— gate_layout：落地字节数与 meta 的账。读盘那几行需要 torch，放在真机验证（§25），这里只量算术 ——

# 37 行的小序列（段长 20/6/2/4/5），kept 是 15 行
SMALL_CHUNKS = [
    {"kind": "text", "n_tokens": 20, "ids": [IM_START] + [7001] * 11 + [IM_START] + [7002] * 7},
    {"kind": "image", "n_tokens": 6},
    {"kind": "text", "n_tokens": 2, "ids": [151653, 151652]},
    {"kind": "image", "n_tokens": 4},
    {"kind": "text", "n_tokens": 5, "ids": [151653, 8000, 8001, 8002, 8003]},
]
N_EMBD = 3
META = {"n_embd": N_EMBD, "total_rows": 37, "chunks": SMALL_CHUNKS}


def test_gate_layout给出要读的行号与槽位():
    idx, slots = gate_layout(META, 37 * N_EMBD * 4)
    assert idx == list(range(12, 20)) + [26, 27] + list(range(32, 37))
    assert slots == [8, 10]


def test_gate_layout文件短了要报错():
    """进程被杀 / 盘满时 pre.bin 只剩半截，硬拿会读出一堆越界或 0 —— 条件是向量，看不出来。"""
    with pytest.raises(ValueError, match="pre.bin"):
        gate_layout(META, 10 * N_EMBD * 4)


def test_gate_layout段长与总行数不符要报错():
    """total_rows 与 chunks 各说一套 = 那份 meta 不是这次跑的（比如指错了目录）。"""
    with pytest.raises(ValueError, match="total_rows"):
        gate_layout({**META, "total_rows": 40}, 40 * N_EMBD * 4)


def test_一行都没丢掉要报错():
    """没套 ChatML 模板的裸指令：没有 im_start、也没有视觉块 ⇒ 一行都不丢。
    这时喂给 DiT 的是整条序列，slot 位置全对不上，但**它不崩，只是图坏**。"""
    bare = {"n_embd": N_EMBD, "total_rows": 4, "chunks": [{"kind": "text", "n_tokens": 4, "ids": [100, 101, 102, 103]}]}
    with pytest.raises(ValueError, match="一行都没丢"):
        gate_layout(bare, 4 * N_EMBD * 4)


def test_连不上服务时报人话(tmp_path):
    """服务没起（或者崩了）时，现场要的是"去看 systemctl status"，不是 ConnectionRefusedError。"""
    with pytest.raises(RuntimeError) as e:
        run_gate(["a.png"], "文本", sock=str(tmp_path / "nope.sock"))
    assert "取数服务不在" in str(e.value)
    assert "systemctl status llama-gate" in str(e.value)


# --- 常驻服务的帧（Task 1）-------------------------------------------------

from tools.llama_gate.protocol import decode_response, encode_request  # noqa: E402


def test_请求帧的字段顺序与长度():
    blob = encode_request([b"\x89PNG1", b"\x89PNG2"], "把衣服穿上")
    assert blob[:4] == b"LGAT" and blob[4] == 1 and blob[5] == 0 and blob[6] == 2
    assert int.from_bytes(blob[7:11], "little") == 5          # 图1 长度
    assert blob[11:16] == b"\x89PNG1"
    assert int.from_bytes(blob[16:20], "little") == 5         # 图2 长度
    assert blob[20:25] == b"\x89PNG2"
    assert blob[25:29] == len("把衣服穿上".encode()).to_bytes(4, "little")   # prompt 字节数
    assert blob[29:] == "把衣服穿上".encode()


def test_响应帧解出张量与槽位():
    tensor = b"\x00" * (2 * 3 * 4)          # rows × n_embd × float32
    blob = (b"LGAT" + bytes([0]) + (2).to_bytes(4, "little") + (3).to_bytes(4, "little")
            + bytes([2]) + (8).to_bytes(4, "little") + (14).to_bytes(4, "little") + tensor)
    assert decode_response(blob) == (0, 2, 3, [8, 14], tensor, b"")


def test_debug帧回全序列与meta():
    """离线判读（§19/§23/§24 那套）要的是全序列 + meta.json，不是切好的 115 行。"""
    meta = '{"n_embd":4096,"chunks":[]}'.encode()
    tensor = b"\x00" * (2 * 3 * 4)
    blob = (b"LGAT" + bytes([0]) + (2).to_bytes(4, "little") + (3).to_bytes(4, "little")
            + bytes([0]) + len(meta).to_bytes(4, "little") + meta + tensor)
    assert decode_response(blob, want_debug=True) == (0, 2, 3, [], tensor, meta)


def test_响应帧短了要报错():
    with pytest.raises(ValueError):
        decode_response(b"LGAT\x00\x01")


def test_错误帧把服务端那句人话带回来():
    """status != 0 时，帧里带的是给人看的原因（比如"媒体标记数要与图数相等"），不能只报"帧短了"。"""
    msg = "mtmd_tokenize 返回 1（媒体标记数要与图数相等）".encode()
    blob = b"LGAT" + bytes([1]) + len(msg).to_bytes(4, "little") + msg
    with pytest.raises(ValueError, match="媒体标记数要与图数相等"):
        decode_response(blob)
