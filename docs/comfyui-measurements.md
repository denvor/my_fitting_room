# 本机 ComfyUI 出图实测记录 · 2026-10-31 ~ 11-02

这份是**测量记录**，不是设计文档：只记"量到了什么数、口径是什么、哪些还没解释"。
README 里那些耗时数的出处、可复现方法、以及跨机器/跨版本的对照都在这份里；
规格文档看 `docs/superpowers/specs/2026-10-27-comfyui-source-design.md`。

## 0. 口径（照这五条复现，才对得上）

- **图**：线上那张 `tryon/comfy_api.build_graph`（turbo int8 + 8 步 + cfg 1 + euler/simple），不另拼图。
- **输入固定两张**：人像 `data/records/20261101-202808-ad9c/person.jpg`（`prepare_upload` 后 **1152×1536 = 1.77M**），
  衣服 `data/samples/menswear/top/denim-shirt.jpg`。指令固定 `TRYON_PROMPT`，seed 固定 **20261101**。
- **档位只改一个数**：`comfy_resolution()` 的返回值 —— 480p→544、720p→**864**、1K→**0**、2K→2016。
  `0` 的意思是"按参考图自身尺寸"，所以 1K 档实际喂给视觉塔的是 1.77M 像素。
- **计时**：`POST /prompt` 起，到 `/history/<id>` 里 `status.completed` 为真的墙钟，轮询粒度 0.25 秒 → 每发系统性偏大 ≤0.3 秒。
  不是服务端报的耗时，含上传后的往返。
- **一次只跑一发**，本机走 `data/generating.lock`，远程开跑前先看 `/queue` 有没有别人的活。

### 把"编码"和"采样"拆开的那招

这是这轮最有用的一招，值得单独记：

1. **A 发**：全量。编码 + 采样。
2. **B 发**：整张图**只改 seed**。编码节点的输入（clip 对象、两张图、prompt、`resolution`）一字未动 → 服务端缓存命中，
   于是 B ≈ 只剩采样 + VAE 解码 + 存图。
3. **A − B ≈ 编码那一段。**

关键约束：**prompt 只要变一个字符，整个编码缓存就失效**（11-01 用 A/B/A 三跑验过：151.3 / 145.6 / 56.5 秒 ——
第二发只多了一个空格，时间就从 56.5 弹回 145 那一档）。所以 B 发只能动 seed，别的都不能动。

量冷机同理要打岔：暖机那一发**故意用另一条指令**（加"（暖机）"三个字），让它把三份权重搬进显存、
又不至于把后面 A 发的编码时间提前吃掉。

### 复现脚本

一次性 spike，写在 `/tmp` 里，机器重启就没 —— 关键那几行留在这份文档里，以后照着重写二十行就够：

```python
from tryon import comfy_api, joblock
from tryon.comfy_api import comfy_resolution
from tryon.image_prep import prepare_upload
from tryon.prompt import TRYON_PROMPT

person = prepare_upload(Path(PERSON).read_bytes(), "1k")   # 输入侧两档同上限，prep 一次就够
res = comfy_resolution(person.width, person.height, size)  # 720p→864，1k→0
names = [上传一次记一次，文件名复用 —— 换名字等于换图，比较就不成立]
g_full = comfy_api.build_graph(names[0], names[1:], TRYON_PROMPT, res, SEED, prefix)
g_seed = comfy_api.build_graph(names[0], names[1:], TRYON_PROMPT, res, SEED + 7, prefix)  # 只差 seed
# 分别 POST /prompt 计时；A − B 就是编码
```

## 1. 时间花在哪：1K 那 146 秒的构成（本机，2026-11-02）

| 段 | 本机 8060S | 说明 |
|---|---|---|
| 编码 | **≈ 89.5 秒** | 占全量的 **61%**。`resolution=0` → 1.77M 像素过 qwen3vl 8B 的视觉塔 |
| 采样 + 解码 + 存图 | **≈ 56.3 秒** | 8 步；三发独立测得 56.7 / 56.3 / 56.0（极差 0.7 秒） |
| 权重从内存搬进显存 | 几秒 | 三份共 **17.3 GB**（turbo 7.26 + qwen3vl 9.35 + vae 0.68，`ls` 实数）；空显存冷起比热起只慢 6~7 秒 ≈ 3 GB/s，页缓存命中的量级，不是瓶颈 |

拆出来的数（同机同图，只换 seed 那一发当基线）：

| 档位 | `resolution` | 全量 A | 纯采样 B | 编码 = A−B |
|---|---|---|---|---|
| 720p | 864 | 66.4 ~ 67.3 秒 | 20.1 / 20.2 / 20.6 / 20.9 / 21.0 秒 | **45.7 ~ 46.3 秒** |
| 1K | 0（=1.77M） | 146.0 ~ 146.3 秒 | 56.0 ~ 56.7 秒 | **89.4 ~ 89.6 秒** |

**两条结论，都跟直觉相反：**

1. **1K 档真正贵的是"喂给模型多大的图"，不是"输出多大的图"。** 编码段 89.5 秒 vs 采样段 56.3 秒 ——
   低档之所以省时间，一半是因为采样便宜，另一半是因为 `resolution` 有数了、把参考图压小再喂。
2. README 里那句"**输入多大不影响耗时**"（1.77M / 0.79M / 0.31M 配 544 预算都是 26 秒）**只在预算固定时成立**。
   1K 档走的是 `resolution=0`，输入多大 = 编码多贵。这两句不矛盾，但放在一起容易被读成矛盾，所以写在这。

## 2. 跨机器：本机 Radeon 8060S vs RTX 5060 Ti 16GB（2026-11-02）

| | 本机 | 192.168.199.200 |
|---|---|---|
| GPU | AMD Radeon 8060S（128 GB 统一内存，可分配显存 124 GiB） | **RTX 5060 Ti 16 GB**（独立 GDDR7） |
| 系统 / 装法 | Linux，local-git | win32，ComfyUI 便携包 |
| ComfyUI（测时） | 0.39.0 → 当晚升到 0.43.0 | 0.37.0 |
| torch | 2.14.0+rocm10.1.0 | 2.13.0+cu130 |
| 启动参数 | `--gpu-only --preview-method none --use-ck-attention` | `--windows-standalone-build --listen 0.0.0.0`（预览方法默认） |
| 内存 | 122.7 GiB，空 99.5 | 31.4 GiB，空 18.5 |

同一张图、同一份输入、同一颗 seed（本机那栏是 0.39+ck 那一次；§3 说明版本与后端都不动时间，所以两栏可比。
"冷机第一发"例外 —— 那是 0.39+pytorch-CA / 0.43+ck 的 73.4 / 73.8 两发，0.39+ck 那次量到的 66.5 **不算冷**）：

| | 本机 | 5060 Ti | 本机慢 |
|---|---|---|---|
| 720p 全量 | 66.6 秒 | **16.4 秒** | 4.1× |
| 720p 纯采样 | 20.9 | 8.1 | 2.6× |
| 720p 编码段 | 45.7 | 8.3 | 5.5× |
| 1K 全量 | 146.0 秒 | **54.8 秒** | 2.7× |
| 1K 纯采样 | 56.6 | 38.3 | 1.5× |
| 1K 编码段 | 89.4 | 16.5 | 5.4× |
| 冷机第一发（720p，含建模） | 73.4 ~ 73.8 | **21.3** | — |

- **差距主要在编码段**：两个档位独立测出同一个倍数（5.5 / 5.4），不像噪声。qwen3vl 8B 那颗 9.35 GB 是纯带宽活儿，
  8060S 的 256-bit LPDDR5x 还要和 CPU 分；5060 Ti 是独立 GDDR7。采样段只差 1.5~2.6 倍。
- **16 GB 显存跑得动**：没 OOM、没崩，冷机第一发 21.3 秒就出图（本机等量要 73 秒）。
- 两处已知口径不齐，方向上**对远程不利**：远程 0.37.0（本机 0.43.0）、远程没关预览（本机 `--preview-method none`）。
  所以"5060 Ti 快 2.7~4.1 倍"这个数里没有偷来的便宜。
- **每格只有 1 发。** 从 720p 到 1K，输出像素多 **2.42 倍**（736×992 → 1152×1536），远程的采样时间多 **4.7 倍**、
  本机只多 2.7 倍 —— 远程随尺寸涨得更快。当时我怀疑是 `QwenImage21Cache` 的 `device=auto` 把前缀缓存推到内存，
  **这个嫌疑在 §8 里被算术基本排除了**（缓存跨 PCIe 读每步只值 1~2 秒，而且方向反了）；
  现在剩下的头号嫌疑是 **16 GB 装下 clip(9.35) + unet(7.26) 后没有余量，DiT 的权重在逐步/逐层地从内存回搬**。
  远程回来先跑 `off` 一发就能把这两者分开：若 `off` ≈ `auto`，缓存那台机器上压根没建，嫌疑只剩搬权重。

## 3. 版本与 attention 后端：省不了时间，但会换图（本机，2026-11-02）

| 配置 | 720p 全量 | 720p 纯采样 | 同 seed 出图（对 0.39+ck 基线） |
|---|---|---|---|
| 0.39.0 + `--use-ck-attention` | 66.6 秒 | 20.9 | — |
| 0.39.0 + `--use-pytorch-cross-attention` | 67.3 | 21.0 | **99.00% 像素有差别，平均每像素差 7.11** |
| **0.43.0 + `--use-ck-attention`** | 66.4 | 20.2 / 20.6 / 20.1 | **0.00%，单像素最大差 0 —— 逐位相同**（PNG 字节数都一样） |
| 5060 Ti（跨机器，同 seed） | 16.4 | 8.1 | 66.56% 有差别，平均每像素差 1.06（1K：67.34%，差 1.66） |

- **升到 0.43 对这条链路是纯版本号**：时间不动，出图逐位相同。
- **换 attention 后端：时间不动（0.6 秒，三发样本都低于两个单发基线，我当作没有），但图换得很厉害。**
  它对结果的影响比换一整台机器还大（平均 7.11 vs 1.06/1.66）。
- 所以那条推论要写死：**seed 不是"重出一模一样那张"的把手。** 换后端、换机器都会改写细节（袖子卷不卷这种），
  构图 / 脸 / 姿势 / 门牌倒是稳的。想留住某一张，只能留住那张文件。
- 顺带更正一处算法：我一开始报"跨机器约 80% 像素不同"，那个百分比把三通道拼在一起的直方图里第 2、3 通道的
  零值桶（索引 256、512）当成差值算进去了，任何两张图都会白送 66.7%。上表是按"每像素三通道取最大差"重算的。

## 4. 官方工作流 vs 我们那张图（2026-11-01）

对象：`workflow_templates` 里的 `image_qwen_image_2_1_image_edit.json`（8 个外层节点 + 1 个子图 13 个内部节点）。

- `refine_prompt=off`、`custom_size=off` 时，官方那张图**归约后与我们同构**：`UNETLoader → QwenImage21Cache → KSampler`、
  `CLIPLoader → TextEncodeQwenImage21`、`VAELoader` 同时喂编码与 `VAEDecode`，latent 走编码节点输出。
  实测证据：**leg3（官方全形 + 我们的 turbo/8 步，同输入同 seed）跑完只用 1.0 秒**，`nodes_that_ran` 只有
  `461`（SaveImageAdvanced）和 `480`（PreviewAny）—— 前面全被服务端缓存服务掉了，说明两个计算图逐项对得上。
- **PE（Prompt Enhancement）那条分支确实没执行**：`ComfySwitchNode` 的 `on_true`/`on_false` 在 schema 里写着 `"lazy": true`，
  `comfy_execution/graph.py` 里 `add_node(include_lazy=False)` → 未选中的分支不进依赖图。
  但**验证阶段仍要求那条分支所有节点控件齐全**（`execution.py` 遍历 `valid_inputs`）—— 摆进图里就得给全，哪怕永不执行。
- **`refine_prompt` / `thinking` / `max_length` 这三个控件不在我们的节点上。**
  `TextEncodeQwenImage21` 的 `/object_info` 只有 `clip / prompt / negative_prompt / resolution(默认1024,0–4096,step32) / images(autogrow≤16)`，
  可选 vae；输出 CONDITIONING, CONDITIONING, LATENT。那三样在模板里是**外层子图实例的 widgets**：
  `refine_prompt` / `custom_size` 是两个 `ComfySwitchNode` 的开关，`thinking` / `max_length` 往下喂给 PE 分支的 `TextGenerate`。
  也就是说：**"要不要开 refine prompt / thinking"这个问题在我们这张图上没有意义** —— 我们从头到尾没接 PE 那颗模型。
  PE 权重（`qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors`，9.47 GB）在本机 `models/text_encoders/` 里躺着，从未被引用；
  它那条链的系统提示光模板里那份就有 10494 个字符。
- **官方默认（base 权重 + 25 步）不省时间**：leg2 实测 **158.4 秒**，我们 turbo + 8 步同输入 **146.2 秒**（同 seed、同 resolution=0）。
- v3 节点的 API 写法两个坑（少一个就是整份图 `prompt_outputs_failed_validation`）：
  dynamic combo 的嵌套控件是**点号平铺键**（`format.bit_depth`、`sampling_mode.temperature` 这种，不是嵌套字典）；
  autogrow 前缀命名不统一 —— `BatchImagesNode` 是 `images.image0` 起自 **0**，`TextEncodeQwenImage21` 是 `images.image_1` 起自 **1**。

## 5. 那个文本编码器是什么（顺带钉死）

`CLIPLoader(clip_name="qwen3vl_8b_int8_convrot.safetensors", type="qwen_image")` —— **Qwen3-VL 8B**，不是 CLIP 也不是 T5。

- 调用链：`comfy/sd.py` 2.1 分支 → `comfy/text_encoders/qwen_image21.py`（`model_type="qwen3vl_8b"`、
  `thinking=True` 默认、`layer_norm_hidden_state=False`）→ `qwen3vl.py`（`embedding_size=4096`、`tokenizer_class=Qwen2Tokenizer`）。
- 取的是**最后一层 hidden state 且不加最终 RMSNorm**（注释说对齐 transformers 4.57 的 `hidden_states[-1]`）。
- **参考图是从它走的**：`TextEncodeQwenImage21` 把每张图按 `resolution` 重采样后塞进视觉塔（模板里插 `<imageN>` + vision block），
  conditioning 里文字 token 和图像 token 混在一条序列上 —— 所以 §1 那 89.5 秒、§2 那 5.4 倍差距都是它。
- **它看到的提示长这样**（`qwen_image21.py:8-24`，我们无法从图上改）：system turn 固定一句
  `Comprehend and analyze the provided prompt.`，user turn 里先拼 `refs` 再接我们的指令，末尾开 `assistant`。
  `refs = "<image1> …<image2> …"` —— 序号就是**槽序**（第 i+1 张），所以指令里那句中文"图像2"**必须**跟它对齐：
  人像恒 1 号、参考图按槽序往后排，插队会让文字指的和第 2 个 vision block 装的东西不是同一张。
- 本机 `models/text_encoders/` 里另有 PE 那两颗 9B（`qwen3.5_9b_…_pe_t2i` / `pe_i2i`，各 9.47 GB）和 `qwen3vl_32b_…`（27.14 GB），
  我们一个都不引用 —— 那是 `refine_prompt=on` 那条分支的活（§4）。

## 6. 速查：本机各档全量耗时

`resolution` 一列是**实际发出去的那个数**（1K 档人相 1.77M 在预算内 → 走 0）。

| 档位 | resolution | 输出 | 全量（本机，0.39–0.43 + ck） | 5060 Ti | 备注 |
|---|---|---|---|---|---|
| 480p | 544 | 480×640 | 26.0 / 26.1 / 26.2 秒（10-31） | 未测 | 编码/采样未拆 |
| 720p | 864 | 736×992 | **66.4 ~ 67.3 秒** | 16.4 秒 | 拆见 §1 |
| 720p | 864 | 768×1024 | 70.5 / 70.6 秒（10-31；同法 0.91M 输出是 84.1 秒） | 未测 | **输入与今天这发不是同一份**，别当同档的重复样本 |
| 1K | 0 | 1152×1536 | **146.0 ~ 146.3 秒** | 54.8 秒 | 拆见 §1；对 §7 那句"180/205/224" |
| 1K | 0 | 896×1152 | 95.1 / 95.4 / 96.2 / 98.4 秒（10-31） | 未测 | 输入 1.03M；编码/采样未拆，别按比例倒推 |
| 2K | 2016 | 1792×2320 | **539.62 秒**（10-30） | 未测 | 线上 `harakiri=120` 等不到，所以 2K 只挂在 API 那条 |

上表都是**官方编码节点**的数。把节点换成 §9 那个少编一遍正向的，同图同 seed 出图逐位相同，
1K 101.2 秒、720p 44.4 秒（各省三成）。

## 7. 没解释的、和下次该补的

- **同机同尺寸 1K 的 146 秒 vs 10-31 的 180 / 205 / 224 秒**：差 34~78 秒，口径没查出来。
  11-02 三种配置（0.39+ck、0.39+pytorch-CA、0.43+ck）都落在 146.0~146.3，很稳 —— 所以那三次是离群值，
  但**别拿 146 当新的稳定承诺**，先记下这个洞。嫌疑方向：当时显存里还留着别的权重 / 机器上另有负载。
- 480p / 2K 两档**没做过编码与采样的拆分**，§1 那条"编码占 61%"目前只对 720p 与 1K 成立（§8 的更细拆分同样只这两档）。
- 远程那台没测 2K，也没测 480p。`QwenImage21Cache` 的 `device` / `dtype` 开关**本机已验完（§8）**；
  远程还欠一发 `off`，用来分"缓存没建"与"DiT 权重逐步回搬"（§2 末尾）。
- 所有跨机器/跨配置的比较**每格只有 1 发**（只有 720p/1K 的纯采样段有 3 发）。
- 这轮全部是 `/tmp` 里的一次性脚本，没进仓库（唯一进仓库的是 §9 那个节点，`comfy_nodes/`）；输出图也没留（只留了 §3 像素比较用的那 8 张）。

## 8. 编码段拆开 + `QwenImage21Cache` 的 6 种组合（本机 0.43，2026-11-02 下午）

**做法**：给 ComfyUI 的四个函数临时插计时（`TextEncodeQwenImage21.execute` 的 resize / `vae.encode` / 正向 / 负向，
`qwen3vl.preprocess_embed` 的视觉塔，`qwen3vl.forward` 的 8B 主干，`qwen_image21.encode_token_weights` 的切片），
然后**另起一个 8189 实例**跑它，`127.0.0.1:8188` 那个全程不重启、不受影响（Python 已经把原文件 import 进去了）。
探针自带 `cuda.synchronize()`，不然量到的只是提交时间。测完 `git checkout --` 撤干净。
脚本：`/tmp/timing-patch/{apply.py,revert.sh}`（`apply.py` 按"整行 strip 后相等"定位再按原缩进展开，不用正则，
第一版用正则时 `\"` 被当字面量写进了源文件，`py_compile` 挡下来了）。

### 编码段是谁花的（1K = `resolution=0`，两张图按原尺寸喂）

| 段 | 正向 | 负向 | 合计 | 占编码 |
|---|---|---|---|---|
| **视觉塔**（Qwen3-VL 的 ViT） | 20.62 + 11.27 = 31.9 s | 20.62 + 11.28 = **31.9 s** | **63.8 s** | **71%** |
| **8B 主干**整趟 | 12.79 s（seq 2865） | 12.52 s（seq 2771） | 25.3 s | 28% |
| VAE 编两张参考图 | — | — | 1.0 s | 1.1% |
| resize（lanczos） | 0（1K 走不缩放分支）；720p 是 58 ms | | | |
| **一趟合计** | **44.68 s** | **44.43 s**（`chars=0`） | **90.1 s** | 100% |

对账：墙钟 A−B = 145.3 − 55.0 = **90.3 秒** ✓。720p 同法：视觉塔 31.6 / 8B 14.4 / VAE 0.9 / 编码共 47.0 秒（墙钟 A−B 45.7~46.3）。
探针自己几乎不额外耗时：8189 上 1K 的 A/B 是 145.3 / 55.0，8188 干净实例是 146.0~146.3 / 56.0~56.7。

**两条读码结论被实测钉死，而且都很大：**

1. **负向那一整趟 44.4 秒 = 一次 1K 请求（144.9 秒）的 31%，而 `cfg=1.0` 下采样器永远不用它。**
   我们传的 `negative_prompt` 是空串，节点照样把两张参考图**又过了一遍视觉塔**（31.9 秒）再走一遍 8B（12.5 秒）。
   省不掉的根源是"正负两次编码在同一个节点里"，图上怎么接线都改不了。
2. **视觉塔的产出算完就被扔掉。** 1K 正向：`cond_before=2865 → dropped=2750`，
   **真正进 DiT 的 conditioning 只有 115 个 token**（负向 21）。图像条件是走 VAE latent 进去的。
   人像 6912 个 patch（grid 96×72）→ 合并后 1728 token，衣服 4032 → 1008 token，这 2736 个就是被丢的那批。

顺带把进 DiT 的 token 账也量了（§2 与下面那个"缓存值多少"都靠它）：**目标 6912 token，参考图 latent 10944 token** →
前缀 ≈ 11,059 = 目标的 **1.60 倍**（720p 是 5849 / 2852 = 2.05 倍）。

### `QwenImage21Cache`：device × dtype 六发（1K，编码已缓存，只动这两个控件 + 同一颗 seed）

| 组合 | 采样段墙钟 | 出图 vs `gpu-default` |
|---|---|---|
| `leg0`（`auto`/`default`，全量含编码） | 144.9 s | 不同 seed，99.48% 有差别、均值 8.37（**说明这套指标对换 seed 很敏感**，下面的 0.00% 不是失灵） |
| `gpu` / `default` | **55.1 s** | — |
| `gpu` / `int8` | 56.5 s（+1.4） | 47.69% 有差别，均值 0.599，单像素最大 112 |
| `gpu` / `int4` | 58.2 s（+3.1） | 66.66% 有差别，均值 1.502，单像素最大 196 |
| `off` / `default` | **103.6 s** | **0.00%，逐位相同** |
| `off` / `int8` | 104.5 s | **0.00%，逐位相同**（off 三行彼此也是 0.00%） |
| `off` / `int4` | 105.3 s | 同上 |

四条：

1. **这个 prefix 缓存值 1.88 倍采样时间**（103.6~105.3 → 55.1）：1K 档每请求省 **48.5 秒**，占整次请求的 33%。
   token 账能对上：设每步前缀成本 P、目标 T，缓存省的是 7P ⇒ P = 48.5 / 7 ≈ **6.9 秒** —— 也就是
   **"不重置 prefix cache、跨请求留着"最多再值 6.9 秒（4.8%）**，只有"负向白跑"那 44.4 秒的六分之一。
2. **缓存是精确的，不是近似**：`gpu-default` 与 `off-default` 出图**逐位相同**（代码里那句"prefix 行按 t=0 调制"成立）。
   所以开缓存不换图，这是它敢默认开的实证。
3. **`device=off` 时 `dtype` 真的不被读**：off 那三行逐位相同 ✓（与读码一致）。
4. **`auto` 在本机 = `gpu`**（55.1 vs 之前的 55.0），显存充裕时它确实放进了显存。**量化在这台机器上是纯亏**：
   int8 慢 1.4 秒、int4 慢 3.1 秒，还各掉一档细节。它们的用途在"内存吃紧"的卡上，不是这里。

缓存体积按实数算：`L=32`、`inner_dim=4096`（从 turbo 文件头读出来的张量形状），前缀 11,059 token，bf16
⇒ 每个 conditioning `2 × 32 × 11059 × 4096 × 2 B = 5.81 GB`，正负两份 = **11.6 GB**。
`auto` 的建缓存门槛是"空闲显存 > 4 × 体积"= 46.5 GB —— 本机 124 GB 统一内存过，**16 GB 的 5060 Ti 不可能过**，
只能退到"挪不活跃的模型内存做 host cache"那条分支（那台机器空闲内存 18.5 GB，也紧）。
但跨 PCIe 每步读 5.8 GB 只值 1~2 秒，撑不起 §2 那个 4.7 倍，**而且方向是反的**（720p 的前缀/目标比是 2.05，
比 1K 的 1.60 更大，缓存缺失更该伤 720p）—— 所以 §2 那条嫌疑改判：**头号嫌疑是 16 GB 上 DiT 权重逐步回搬**，
远程回来跑一发 `off` 就能分开（`off ≈ auto` ⇒ 那台上缓存根本没建）。

## 9. 只编正向的编码节点：省下整趟负向编码，出图逐位相同（本机 0.43，2026-11-02 傍晚）

§8 那条"负向 44.4 秒 = 一次 1K 请求的 31%，而 `cfg=1.0` 下采样器永远不读它"是个纯亏的洞，
图上怎么接线都改不了（正负两次编码在同一个节点里）。所以把它填了：**fork 一个节点，少编那一遍**。

节点在 `comfy_nodes/qwen21_posonly_encode.py`，id `TextEncodeQwenImage21PosOnly`，
fork 自 0.43.0 的 `comfy_extras/nodes_qwen.py:111-183`。逐行差别只有三处（execute 体，归一化后 diff）：

- 去掉 `negative = ...encode(negative_prompt...)` 那一行
- 去掉给 negative 挂 `reference_latents` 那一行
- 结尾 `io.NodeOutput(positive, negative, ...)` 改成 `(positive, positive, ...)`

输入输出口与官方**同形**（连那个用不到的 `negative_prompt` 也留着），所以图里只需改 `class_type` 一处。

**装没装上，别看 import 那行。** 头两发都是 `{"node_loaded": false}` + `missing_node_type` 被拒：
`get_node_list` 照抄了官方的返回注解 `-> list[type[io.BaseComfyNode]]`，而 `io.BaseComfyNode` 这个符号
在 0.43 的 `comfy_api.latest` 里根本不存在 → 模块在 custom_nodes 导入阶段就抛，ComfyUI 只打一行
`0.0 seconds: .../qwen21_posonly_encode.py` 就继续启动，界面上一切正常。判据是 `GET /object_info/<节点id>`
拿到 `{}` 就是没装上。注解删掉即好，这个坑写进了节点的注释里。

### 验收（8190 第三实例，同 seed 20261102，同一句带"今天没见过"后缀的指令 → 两发编码都是冷的；只换 class_type）

| 档 | 官方节点 | 只编正向 | 墙钟差 | 出图 |
|---|---|---|---|---|
| 1K | 150.3 秒 | **101.2 秒** | 49.1 秒 | **逐位相同**（差异像素 0.000%，单像素最大差 0） |
| 720p | 66.0 秒 | **44.4 秒** | 21.6 秒 | **逐位相同** |

**对账 —— 49.1 这个数虚高，别照它承诺**：official 那发是 8190 冷起后的第一发，而 §1 量过"空显存冷起比热起慢 6~7 秒"。
按 §8 探针的口径反推：`101.2 + 44.4（负向那一趟）= 145.6 秒`，落在 §1 的 1K 基线 **146.0~146.3** 里，
也和同一个 8189 实例上热机那发的 145.1 对得上 → **1K 档实际省 44~45 秒**。
720p 那发是热机，21.6 秒 vs §8 的 720p 编码合计 46.9 的一半（23.5）差 1.9 秒 —— 负向那趟本来就短一点
（seq 2771 vs 2865 token，1K 档），不是严格等分。

两档都省 **全程的三成上下**（30% / 33%），跟 §1 的构成自洽：编码占 61%（1K）与 70%（720p），
负向是编码那两趟里的一趟。这比 §8 那条"缓存不重置最多再值 6.9 秒"大 6~7 倍。

### 前提与代价

**`cfg` 必须等于 1.0**，只有这一条。`comfy/samplers.py:609` 在 `math.isclose(cond_scale, 1.0)`
且没人设 `disable_cfg1_optimization` 时把 uncond 置成 `None` —— 负向根本不参与计算，所以"输出的负向其实是正向的别名"
这件事没人能观察到。哪天 cfg>1，这里出来的图就不是官方版那回事（**是错，不是慢**）。
本项目 cfg 钉在 `tryon/comfy_api.py` 的 `CFG = 1.0`，`tests/test_comfy_api.py::test_sampler_uses_the_turbo_settings`
断言着图里那颗 `cfg == 1.0`，改了就红。

代价是**多一份要跟官方同步的源码**：ComfyUI 升级时若 `nodes_qwen.py` 那段改了（resize、`keep_vision`、
`prevent_empty_text` 这类），这个 fork 不会跟着变，也不会有人提醒。所以升级后要手工 diff 一次。

**已上车（11-02 晚）**：`build_graph` 里那颗编码节点的 class_type 走 `comfy_api.ENCODER_CLASS`，默认就是 fork；
`check()` 多查一项（缺了会把"拷进 `custom_nodes/` 再重启"说出来 —— 对着没重启的 8188 实测过话术）。
钉住这件事的两条测试：`test_encoder_skips_the_negative_pass_and_keeps_the_wiring`（图里发的是 fork、
三个输出口照旧、旁边那句 `cfg == 1.0` 是它的前提）和 `test_check_reports_the_missing_custom_encoder`。
装法与"换机器不会自动跟着走"写进 README「装与起」，规格 §6.2 的节点表加了日期注。

**上车后再验的一次（同一晚，还在 8190）**：这回不发 spike 拼的图，直接调应用自己的 `comfy_api.generate()`（上传、入队、轮询、取图全走产品代码），
720p 档同一颗 seed 三发：暖机 72.2 秒（冷实例，比热机基线多 6 秒，与 §1 那句"冷起慢 6~7 秒"吻合）、
官方节点 **67.1 秒**、产品默认（fork）**44.2 秒** → 省 **22.9 秒**，与上面 spike 的 21.6 秒对得上（两条独立路径同一个差值）。
像素逐位相同（差异像素 0.000%、单像素最大差 0）；**PNG 的字节不同** —— 图里带着 prompt_id 那类元数据，
所以"逐位相同"说的永远是像素，别拿 `sha256` 当判据（上面几张的 sha 各不相同，正是这个原因）。

还欠一件：**(b)** 远程那台 5060 Ti —— 想把来源指回它，得先把同一个文件拷过去并重启，否则 `check()` 会红、生成会被拒。
另外使用者的 8188 到记录时为止**还没重启**，也就是还没加载上这个节点：这份代码一旦部署上去而 8188 不重启，
生成会当场报 `missing_node_type`（自检按钮会提前告诉你同一件事）。**先重启、后部署**，或者两步连着做。

现场状态：`/home/denvor/work/ComfyUI/custom_nodes/qwen21_posonly_encode.py` 里那份与仓库 `comfy_nodes/` 逐字节相同，
下次 8188 重启就会多出这个节点 —— 但图里不换 `class_type` 就一个字节都不影响它，它只是多一个可选节点。
测完可以撤（删掉那个文件即可，没有别的残留）。
