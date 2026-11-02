"""只编正向的 Qwen Image 2.1 编码节点 —— 省掉 `cfg=1` 下根本用不到的那趟负向编码。

装法：把这个文件放进 ComfyUI 的 `custom_nodes/`，重启 ComfyUI。节点名
`TextEncodeQwenImage21PosOnly`，输入输出与官方 `TextEncodeQwenImage21` **同形**（含那个用不到的
`negative_prompt`，留着才敢直接换 class_type），所以图里只要改 class_type 一处。

fork 自 ComfyUI 0.43.0 的 `comfy_extras/nodes_qwen.py:111-183`，唯一的行为差别在结尾那两次 encode：
官方版把 `prompt` 与 `negative_prompt` 各编一遍，我们只编 `prompt`，negative 输出直接复用同一个
conditioning 对象。前面那段 resize / VAE 参考图 latent 的逻辑一行没动（官方注释：文本编码器与 VAE
用同一个缩放，好让每个 vision slot 盖住 2x2 latent）。

**前提：cfg 必须等于 1.0。** `comfy/samplers.py:609` 在 `math.isclose(cond_scale, 1.0)` 且没人设
`disable_cfg1_optimization` 时把 uncond 置成 None，负向根本不参与计算 —— 这一条 2026-11-02 实测过：
同 seed 下本节点与官方节点出图逐位相同。一旦哪天 cfg>1，这里返回的"负向"其实是正向的别名，
出来的图就不是官方版那回事了。本项目里 cfg 钉死在 `tryon/comfy_api.py` 的 `CFG = 1.0`，`tests/test_comfy_api.py` 的
`test_sampler_uses_the_turbo_settings` 断言着图里那颗 `cfg == 1.0`；上游那段逻辑改了要对照着同步。

实测收益与验收结果见 `docs/comfyui-measurements.md` §9（本机 0.43 + ck，同 seed 逐位对比）。
"""

import math
from typing_extensions import override  # noqa: F401  与官方节点同写法，方便对照同步

import torch

import comfy.model_management
import comfy.utils
import node_helpers
from comfy_api.latest import ComfyExtension, io


class TextEncodeQwenImage21PosOnly(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="TextEncodeQwenImage21PosOnly",
            display_name="Text Encode Qwen Image 2.1 (positive only)",
            category="model/conditioning/qwen image",
            description=(
                "TextEncodeQwenImage21 的少编一遍版：negative_prompt 一律不编码，"
                "只在 cfg=1.0 下等价（采样器此时根本不读负向）。输出三个口与官方一致。"
            ),
            inputs=[
                io.Clip.Input("clip"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.String.Input("negative_prompt", multiline=True, dynamic_prompts=True,
                                tooltip="本节点不编码它，只为与官方节点同形而保留。"),
                io.Vae.Input("vae", optional=True),
                io.Int.Input("resolution", default=1024, min=0, max=4096, step=32,
                             tooltip="Reference images are resized to about resolution x resolution pixels, at multiples of 32, preserving aspect ratio. 0 keeps each reference at its own size, rounded to a multiple of 32. "),
                io.Autogrow.Input(
                    "images",
                    template=io.Autogrow.TemplateNames(
                        io.Image.Input("image"),
                        names=[f"image_{i}" for i in range(1, 17)],
                        min=0,
                    ),
                    tooltip="Reference images, seen by the text encoder and spliced into the sequence as VAE latents.",
                ),
            ],
            outputs=[
                io.Conditioning.Output(display_name="positive"),
                io.Conditioning.Output(display_name="negative (= positive, cfg must be 1)"),
                io.Latent.Output(display_name="latent",
                                 tooltip="Empty latent on the first reference image's size, to match with sampling as any other size shifts the edit."),
            ],
        )

    @classmethod
    def execute(cls, clip, prompt, negative_prompt, vae=None, resolution=1024, images: io.Autogrow.Type = None) -> io.NodeOutput:
        ref_latents = []
        images_vl = []
        images = images or {}
        latent_w = latent_h = resolution or 1024
        for name in sorted(images, key=lambda n: int(n.rsplit("_", 1)[-1])):
            image = images[name]
            if image is None:
                continue
            # same resize for the text encoder and the VAE, so every vision slot covers 2x2 latents; one image per input
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
            if not images_vl:
                latent_w, latent_h = width, height
            rgb = s[:, :, :, :3]
            if s.shape[-1] > 3:
                rgb = rgb * s[:, :, :, 3:] + (1.0 - s[:, :, :, 3:])  # the vision tower sees alpha over white, the vae keeps all four
            images_vl.append(rgb)
            if vae is not None:
                ref_latents.append(vae.encode(s))

        keep_vision = len(ref_latents) == 0
        positive = clip.encode_from_tokens_scheduled(clip.tokenize(prompt, images=images_vl, keep_vision=keep_vision, prevent_empty_text=True))
        # 官方在这里还有一次 negative = ...encode(negative_prompt...)：1K 档实测 44.4 秒，
        # 而 cfg=1 时它进不了采样器（comfy/samplers.py:609），所以本节点不编。
        if len(ref_latents) > 0:
            positive = node_helpers.conditioning_set_values(positive, {"reference_latents": ref_latents}, append=True)
        latent = torch.zeros([1, 64, latent_h // 16, latent_w // 16], device=comfy.model_management.intermediate_device())
        return io.NodeOutput(positive, positive, {"samples": latent})


class Qwen21PosOnlyExtension(ComfyExtension):
    @override
    async def get_node_list(self):
        # 返回类型不写注解：`io.BaseComfyNode` 这个符号在 0.43 的 comfy_api.latest 里不存在，
        # 拿它当注解会让整个模块在 custom_nodes 导入阶段就炸（第一次实测就这么静默没装上）
        return [TextEncodeQwenImage21PosOnly]


async def comfy_entrypoint():
    return Qwen21PosOnlyExtension()
