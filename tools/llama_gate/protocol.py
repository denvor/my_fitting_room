"""取数服务的帧格式 —— 纯函数，没有 IO。

为什么自建而不用 protobuf：两端都是我们自己的代码（C++ 服务 + Python 客户端），要传的三样东西
（图、prompt、条件张量）本质都是"一段字节"，通用序列化框架的收益用不上，反而多两个依赖。
协议细节与设计见 `docs/superpowers/specs/2026-11-10-llama-gate-service-design.md`。

一帧的字段（全部小端）：

    请求:  magic(4)="LGAT" | version(1) | flags(1) | 图数(1)
         | [图长度(4) | 图字节]… | prompt长度(4) | prompt(UTF-8)
    响应:  magic(4)="LGAT" | status(1) | 行数(4) | n_embd(4) | slots数(1)
         | [slot(4)]… | 张量字节(float32)

`flags` 的 bit 0 = debug：置位时服务回**全序列**而不是切好的 115 行（供离线判读用）。
"""

import struct

MAGIC = b"LGAT"
VERSION = 1
FLAG_DEBUG = 1

_HEADER_RESP = struct.Struct("<4sBIIB")    # magic | status | 行数 | n_embd | slots 数


def encode_request(images: list[bytes], prompt: str, flags: int = 0) -> bytes:
    """图（原始字节）+ 指令 → 一帧请求。图的顺序就是槽序（第 1 张 = 图像1）。"""
    parts = [MAGIC, bytes([VERSION, flags & 0xFF, len(images)])]
    for img in images:
        parts += [struct.pack("<I", len(img)), img]
    text = prompt.encode("utf-8")
    parts += [struct.pack("<I", len(text)), text]
    return b"".join(parts)


def decode_response(blob: bytes) -> tuple[int, int, int, list[int], bytes]:
    """一帧响应 → (status, 行数, n_embd, slots, 张量字节)。

    长度不够就 `ValueError`：拿到半截字节去喂 DiT 是**静默画错**（实测记录 §26.3 那条口径坑的同类），
    不如当场炸。
    """
    if len(blob) < _HEADER_RESP.size:
        raise ValueError(f"响应帧短了：只有 {len(blob)} 字节，光 header 就要 {_HEADER_RESP.size}")
    magic, status, rows, n_embd, n_slots = _HEADER_RESP.unpack_from(blob, 0)
    if magic != MAGIC:
        raise ValueError(f"响应帧 magic 不对：{magic!r}")
    off = _HEADER_RESP.size
    need = off + n_slots * 4 + rows * n_embd * 4
    if len(blob) < need:
        raise ValueError(f"响应帧短了：声明 {rows} 行 × {n_embd} 列 + {n_slots} 个 slot 要 {need} 字节，只有 {len(blob)}")
    slots = list(struct.unpack_from(f"<{n_slots}I", blob, off)) if n_slots else []
    off += n_slots * 4
    return status, rows, n_embd, slots, blob[off:off + rows * n_embd * 4]
