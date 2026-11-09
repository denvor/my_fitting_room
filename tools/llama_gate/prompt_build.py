"""把 ComfyUI 那句 ChatML 模板换成 llama.cpp 认的媒体标记形式。

单独一个文件、而且**不 import torch**：调用方有两个 —— CLI `encode.py`（研究用，起进程）和
ComfyUI 里的节点 `comfy_nodes/qwen21_precomp_cond.py`（生产用，在自己进程里算）。
两边必须拼出同一串字节，否则 §19 那两个余弦数（0.98247 / 0.98080）就不是同一条序列。
"""

MEDIA_MARKER = "<__media__>"   # mtmd_context_params.media_marker，与服务端 /etc/llama-gate.env 里设的那个一致


def media_prompt(template: str, user_text: str, n_images: int) -> str:
    """把模板里的视觉说明块，换成 llama 的媒体标记（每图一个）。

    上游的插法（`comfy_nodes/qwen21_posonly_encode.py:102` 那句 `clip.tokenize(..., images=...)` 的等价形式）
    是在 `{}` 前面拼 `<image1>VISION_BLOCK <image2>VISION_BLOCK{}` —— VISION_BLOCK 是给 LLM 看的一句说明文字。
    llama.cpp 那侧同样的位置要放**每图一个** MEDIA_MARKER（§19 的 `prompt_llama_media.txt` 就是这么来的），
    而且标记数必须等于 bitmap 数，否则 `mtmd_tokenize` 直接返回 1。
    """
    slots = " ".join(f"<image{i + 1}>{MEDIA_MARKER}" for i in range(n_images))
    return template.replace("{}", slots + "{}", 1).format(user_text)
