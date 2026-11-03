"""外部算好的 conditioning 直接注入 —— llama.cpp 那一档的最小可用件（2026-11-03 spike）。

**它存在的唯一目的**：把 §19 量到的那个 0.982 条件扰动**画出来给人看**。
"0.982 的余弦到底是换图还是不换图"这件事判不了数字，只能生成（TODO.md T1）。

与 `TextEncodeQwenImage21PosOnly` 的差别只有一处：**8B 那一趟不在这儿算了**，
所以图里连 `CLIPLoader` 都不需要（ComfyUI 会剪掉没被消费的节点，那颗 8.9 GB 的权重根本不进显存）。
其余一切照旧：同一套 resize（`common_upscale` lanczos、按 32 取整、`resolution=0` 走原尺寸）、
同一套 `reference_latents`、同一份空 latent 的尺寸算法。
—— 这三样必须与 fork 节点逐位一致，否则比出来的图差就说不清是谁贡献的。

输入的 `cond_path` 指向一个 `.pt`，内容是 `{"hidden": [1,115,4096], "image_slots": [8,14]}`，
即 `comfy/text_encoders/qwen_image21.py:53-66` 那套"丢 14 行 system turn + 丢两段视觉 token"之后
采样器真正吃的那 115 行，pre-norm（DiT 要的就是没最终 RMSNorm 的那份，`layer_norm_hidden_state=False`）。
制作过程见 `/tmp/llama-inj/make_pt.py`（它的三道断言就是为了不再犯 §18 那次"整块错 14 行"）。

`prompt` 这个输入**算完没用**：留着是因为它进 ComfyUI 的缓存键 —— 换一句指令就必须重跑，
不然第二发是免单重放（§0 那条坑）。别把它删了图省事。

前提同 fork 节点：**cfg 必须等于 1.0**（negative 直接复用 positive）。

还不是产品件的地方：条件是盘上一个文件、路径写在图里、没有校验行数对不上就报错、
没有把 llama.cpp 接成在线服务。这些等图看过再决定要不要做（TODO.md §2 第 1 步）。
"""

import math

import torch

import comfy.model_management
import comfy.utils
import node_helpers
from comfy_api.latest import ComfyExtension, io


class TextEncodeQwenImage21Precomputed(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="TextEncodeQwenImage21Precomputed",
            display_name="Text Encode Qwen Image 2.1 (precomputed, llama.cpp)",
            category="model/conditioning/qwen image",
            is_experimental=True,
            inputs=[
                io.String.Input("cond_path", advanced=True, tooltip="外部算好的 [1,115,4096] .pt（pre-norm）"),
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
                cond_dtype="float32", images: io.Autogrow.Type = None) -> io.NodeOutput:
        del prompt, negative_prompt  # 只进缓存键，不参与计算（cfg=1 时负向本来就不进采样器）
        ref_latents = []
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
            if vae is not None:
                ref_latents.append(vae.encode(s))

        blob = torch.load(cond_path, map_location="cpu", weights_only=False)
        hidden = blob["hidden"].to(getattr(torch, cond_dtype))
        extra = {"pooled_output": None, "image_slots": list(blob["image_slots"])}
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
