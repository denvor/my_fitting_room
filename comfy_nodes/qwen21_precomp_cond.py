"""外部算好的 conditioning 直接注入 —— 第三个生成来源（llama.cpp 出编码）用的编码器节点。

最初它是为了把 §19 量到的那个 0.982 条件扰动**画出来给人看**（"余弦 0.982 到底换不换图"判不了数字，只能生成）。
生成过了：同输入同 seed 出的是**同构图、细节级**的另一张（§24 那对干净的对照，PSNR 38.40），
denvor 看过图判"可以接受"（2026-11-03）。现在它是三档来源里第 3 档的那颗编码节点。

与 `TextEncodeQwenImage21PosOnly` 的差别只有一处：**8B 那一趟不在这儿算了**，
所以图里连 `CLIPLoader` 都不需要（ComfyUI 会剪掉没被消费的节点，那颗 8.9 GB 的权重根本不进显存）。
其余一切照旧：同一套 resize（`common_upscale` lanczos、按 32 取整、`resolution=0` 走原尺寸）、
同一套 `reference_latents`、同一份空 latent 的尺寸算法。
—— 这三样必须与 fork 节点逐位一致，否则比出来的图差就说不清是谁贡献的。

输入的 `cond_path` 指向一个 `.pt`，内容是 `{"hidden": [1,115,4096], "image_slots": [8,14]}`，
即 `comfy/text_encoders/qwen_image21.py:53-66` 那套"丢 14 行 system turn + 丢两段视觉 token"之后
采样器真正吃的那 115 行，pre-norm（DiT 要的就是没最终 RMSNorm 的那份，`layer_norm_hidden_state=False`）。
行算术在 `tools/llama_gate/keep_rows.py`（锚点从 token id 现算，防的就是 §18 那次"整块错 14 行"）。

`prompt` 这个输入**算完没用**：留着是因为它进 ComfyUI 的缓存键 —— 换一句指令就必须重跑，
不然第二发是免单重放（§0 那条坑）。别把它删了图省事。

前提同 fork 节点：**cfg 必须等于 1.0**（negative 直接复用 positive）。

还不是产品件的地方（铺界面那一档时要知道的边界）：`repo_root` / `work_dir` 是明文写在图里的路径；
取数程序（`tools/llama_gate/bin/dump_hidden`）得自己按补丁版 llama.cpp 编译，不在场时报的是那句话而不是自动重建；
`cond_source` 那条路每发都要重跑一次 llama.cpp（没有跨请求缓存，第二发同 prompt 同图靠 ComfyUI 的节点缓存）。
"""

import math
import os
import sys

import torch

import comfy.model_management
import comfy.utils
import node_helpers
from comfy_api.latest import ComfyExtension, io


def _gate_modules(repo_root):
    """把 repo 里的取数模块请进本进程。

    `sys.path.append` 而不是 `insert(0, ...)`：ComfyUI 的顶层名字（`utils`、`app`、`server`…）不能被
    我们 repo 根下的同名目录盖掉，而 repo 里 `tools/` 这个名字 ComfyUI 没有，追加在末尾就够用。
    """
    if repo_root not in sys.path:
        sys.path.append(repo_root)
    from tools.llama_gate.gate_client import cond_from_meta, run_gate
    from tools.llama_gate.prompt_build import media_prompt

    return media_prompt, run_gate, cond_from_meta


def _cond_via_llama(images_vl, prompt, repo_root, work_dir):
    """节点自己 resize 完的那几张 → llama.cpp 取 pre-norm 条件 → 本进程切行，拿回 (hidden, slots)。

    像素必须由节点出、而且走 8 位 PNG 无损往返：§19 那两个余弦数（0.98247 / 0.98080）就是在这份字节上量的，
    换成原图、换成 jpg、或者 float 直传，比出来的图差就说不清是谁贡献的了。
    """
    from PIL import Image

    if not repo_root or not work_dir:
        raise RuntimeError("cond_source 填了但 repo_root / work_dir 是空的 —— 不知道去哪儿找取数脚本")
    media_prompt, run_gate, cond_from_meta = _gate_modules(repo_root)
    from comfy.text_encoders.qwen_image21 import T2I_TEMPLATE   # 本进程第一次拿它要 1.3 秒，之后白送

    os.makedirs(work_dir, exist_ok=True)
    paths = []
    for i, x in enumerate(images_vl):
        arr = (x[0].clamp(0, 1) * 255).round().to(torch.uint8).cpu().numpy()
        p = os.path.join(work_dir, f"img{i + 1}.png")
        Image.fromarray(arr, "RGB").save(p)
        paths.append(p)
    meta = run_gate(paths, media_prompt(T2I_TEMPLATE, prompt, len(paths)), os.path.join(work_dir, "gate"))
    return cond_from_meta(meta)


class TextEncodeQwenImage21Precomputed(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="TextEncodeQwenImage21Precomputed",
            display_name="Text Encode Qwen Image 2.1 (precomputed, llama.cpp)",
            category="model/conditioning/qwen image",
            is_experimental=True,
            inputs=[
                io.String.Input("cond_path", advanced=True, default="",
                                tooltip="外部算好的 [1,115,4096] .pt（pre-norm）。填了就直接用它（§23 那条路）。"),
                io.String.Input("cond_source", advanced=True, default="",
                                tooltip="留空 = 只读 cond_path。填任意值 = 现场调 llama.cpp 算（本进程直调 tools/llama_gate/bin/dump_hidden）。"),
                io.String.Input("repo_root", advanced=True, default="/home/denvor/work/qwen-image",
                                tooltip="取数脚本与权重路径的出处，换机器改这一处。"),
                io.String.Input("work_dir", advanced=True, default="/tmp/llama-cond",
                                tooltip="中转的 PNG 与取数程序的 pre.bin / meta.json 落这儿（一次约 46 MB；post.bin 默认不写）。"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True,
                                tooltip="本节点不编码它，只让它进缓存键：换一句就得重跑，别拿命中当免单。"),
                io.String.Input("negative_prompt", multiline=True, dynamic_prompts=True,
                                tooltip="本节点不编码它，只为与 fork 节点同形而保留。"),
                io.Vae.Input("vae", optional=True),
                io.Int.Input("resolution", default=0, min=0, max=4096, step=32,
                             tooltip="与 TextEncodeQwenImage21PosOnly 同一个意思：参考图按 32 取整缩到这个像素预算，0 = 各自原尺寸。"),
                io.Combo.Input("cond_dtype", options=["float32", "bfloat16"], default="float32", advanced=True),
                io.Autogrow.Input(
                    "images",
                    template=io.Autogrow.TemplateNames(
                        io.Image.Input("image"),
                        names=[f"image_{i}" for i in range(1, 17)],
                        min=0,
                    ),
                    tooltip="与 fork 节点同一份：文本编码器（在这儿是外面算的）与 VAE 用同一个缩放，好让每个 vision slot 盖住 2x2 latent。",
                ),
            ],
            outputs=[
                io.Conditioning.Output(display_name="positive (= 外部那份)"),
                io.Conditioning.Output(display_name="negative (= positive, cfg must be 1)"),
                io.Latent.Output(display_name="latent"),
            ],
        )

    @classmethod
    def execute(cls, cond_path, prompt, negative_prompt, vae=None, resolution=0,
                cond_dtype="float32", cond_source="", repo_root="", work_dir="",
                images: io.Autogrow.Type = None) -> io.NodeOutput:
        del negative_prompt  # cfg=1 时负向本来就不进采样器
        # prompt 在 cond_source 为空时也只进缓存键；非空时它是送去编码的那句原文
        ref_latents = []
        images_vl = []
        latent_w = latent_h = resolution or 1024
        images = images or {}
        for name in sorted(images, key=lambda n: int(n.rsplit("_", 1)[-1])):
            image = images[name]
            if image is None:
                continue
            # 下面这一段与 qwen21_posonly_encode.py:75-99 逐字相同 —— 参考图尺寸一变，ref latent 就变，比较不成立
            samples = image[:1].movedim(-1, 1)
            if resolution > 0:
                ratio = samples.shape[3] / samples.shape[2]
                width = round(math.sqrt(resolution * resolution * ratio) / 32) * 32
                height = round(math.sqrt(resolution * resolution / ratio) / 32) * 32
            else:
                width, height = round(samples.shape[3] / 32) * 32, round(samples.shape[2] / 32) * 32
            width, height = max(32, width), max(32, height)
            if (width, height) == (samples.shape[3], samples.shape[2]):
                s = image[:1]
            else:
                s = comfy.utils.common_upscale(samples, width, height, "lanczos", "disabled").movedim(1, -1)
            if not ref_latents:
                latent_w, latent_h = width, height
            rgb = s[:, :, :, :3]
            if s.shape[-1] > 3:
                rgb = rgb * s[:, :, :, 3:] + (1.0 - s[:, :, :, 3:])  # 视觉塔看的是"alpha 叠在白底上"，VAE 保留四通道
            images_vl.append(rgb)
            if vae is not None:
                ref_latents.append(vae.encode(s))

        if cond_source.strip():
            hidden, slots = _cond_via_llama(images_vl, prompt, repo_root, work_dir)
        else:
            blob = torch.load(cond_path, map_location="cpu", weights_only=False)
            hidden, slots = blob["hidden"], blob["image_slots"]
        hidden = hidden.to(getattr(torch, cond_dtype))
        extra = {"pooled_output": None, "image_slots": list(slots)}
        positive = [[hidden, extra]]
        if len(ref_latents) > 0:
            positive = node_helpers.conditioning_set_values(positive, {"reference_latents": ref_latents}, append=True)
        latent = torch.zeros([1, 64, latent_h // 16, latent_w // 16], device=comfy.model_management.intermediate_device())
        return io.NodeOutput(positive, positive, {"samples": latent})


class Qwen21PrecomputedExtension(ComfyExtension):
    # 返回类型不写注解：与 qwen21_posonly_encode.py 同一个理由（0.43 的 comfy_api.latest 里没有 BaseComfyNode）
    async def get_node_list(self):
        return [TextEncodeQwenImage21Precomputed]


async def comfy_entrypoint():
    return Qwen21PrecomputedExtension()
