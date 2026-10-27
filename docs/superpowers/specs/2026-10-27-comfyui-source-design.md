# 试衣间 · 第二个生成来源：本机 ComfyUI

日期 2026-10-27 · 状态：已实现，验收见 §9 · 上一份设计见 `2026-10-26-qwen-tryon-design.md`（本文只写增量，不重复基线）

## 1. 意图与为什么

现在只有一个生成来源：token plan 的 `qwen-image-3.0-pro`，一次 1K 实测 58 credit，月度 25500 credit 约够 439 次，等待 46–107 秒。

本机跑着 ComfyUI（AMD Radeon 8060S，133GB 显存，`0.0.0.0:8188`，v0.39.0），它的 `image_qwen_image_2_1_turbo_image_edit` 工作流用 `qwen-image-2.1 turbo` 做同一种编辑。实测**显存热的时候 20 秒出一张，一分钱额度不花**。

所以这条路接进来是有意义的补充，不是玩具：**额度见底时还能继续用，想快速多看几套搭配时不用算钱**。

## 2. 目标

- 一个独立的 `/settings` 页：选生成来源（API 或 ComfyUI）、填 ComfyUI 地址、有一个"测一下能不能连上"
- 选了哪条路，界面上处处看得出来：主页那行额度、点「开始试穿」时的确认框、图库每条记录
- 本地路同样锁在 1K 档、一次 1 张、无水印、提示词仍是 `tryon/prompt.py` 里那一份固定的
- 复用现有的一切：单任务锁、`data/records/` 落盘结构、图库分页与删除、错误文案的可读性要求

## 3. 非目标

- **不驱动官方工作流的完整图**：提示词改写支路（`TextGenerate` + 第二个 `CLIPLoader`）、`EmptyLatentImage` 那条 t2i 分支、16 个图槽位、`SaveImageAdvanced`、`ImageCompare` 全部不接。官方工作流里那两个开关存的就是 `false`，也就是它自己默认就走我这条最短路径
- 不做真进度条（见 §7，使用者明确选了零依赖那条）
- 不做任务队列、不做并发、不做取消
- 不做模型下载与显存管理
- 不做多工作流可选（只支持这一条 try-on 图）

## 4. 反目标（做到技术上能跑但踩了这些，算失败）

- **静默换模型**：ComfyUI 连不上时自动回落到 API。两条路的输出不是一个模型出的，悄悄换是在身份/服装保真这条线上挖坑
- **界面上出现编出来的百分比**
- **本地出的记录事后看不出是本地出的**（一个月后你自己就分不清哪张是哪条路给的）

## 5. 约束

- **不新增 Python 依赖**（使用者明确的选择）。ComfyUI 全部走 stdlib `urllib`，和现在 `tryon/qwen_api.py` 一个路子
- 不引入数据库。设置存 `data/settings.json`（`data/` 已在 .gitignore）；路径要像 `mfr_wsgi.py` 那样用 `PROJECT_ROOT` 拼绝对路径，因为 uWSGI 的 `chdir` 是 `/www/webroot`
- `.env` 仍是默认值来源，页面里存的值优先于 `.env`；`.env` 里 ComfyUI 项全部可缺省（缺省就用内置默认地址）
- **两种入口都要能跑**：本机 `dev_run.py`（`0.0.0.0:8000`，threaded Flask）和 `/mfr` 的 uWSGI 实例

### 5.1 两条来自使用者的部署口径（2026-10-27 他答的）

- **ComfyUI 用 frp 端口映射跟到部署机**，所以 `127.0.0.1:8188` 只是开发期默认值，**地址必须能在设置页改**（这不再是"方便"而是必需项）。本次**假定映射可用**，具体端口/主机等他实际部署时再调，不为未知拓扑预设兼容代码
- **uWSGI 是一个 app 一个 worker 单独加载**，所以一次生成阻塞 20–95 秒不会牵动同机别的站点。**因此不引入任务队列、不引入异步框架、不为 wsgi 收紧超时** —— 阻塞式请求在这个部署形态下是可接受的

## 6. 探针已验证的事实（2026-10-27，全部实测，不是文档抄的）

### 6.1 调用序列

```
POST /upload/image     multipart，字段名 image        → {"name": "person.jpg"}   # 同名且内容相同则复用
POST /prompt           {"prompt": <节点图>, "client_id": <可选>}  → {"prompt_id": ...}
GET  /history/<pid>    轮询到 status.completed 为真
GET  /view?filename=&subfolder=&type=output            → 图片字节
```

请求体必须把节点图包在 `prompt` 键里；顶层 `client_id` 已够用，不需要再放查询串。

### 6.2 节点图（10 个节点，实测出图成立）

| 节点 | class_type | 关键输入 |
|---|---|---|
| 人像 | `LoadImage` | `image` |
| 衣服 | `LoadImage` | `image` |
| | `UNETLoader` | `unet_name="qwen_image_2.1_turbo_int8_convrot.safetensors"`, `weight_dtype="default"` |
| | `QwenImage21Cache` | `model←UNETLoader`, `device="auto"`, `dtype="default"` |
| | `CLIPLoader` | `clip_name="qwen3vl_8b_int8_convrot.safetensors"`, `type="qwen_image"` |
| | `VAELoader` | `vae_name="qwen_image_2.1_vae_bf16.safetensors"` |
| | `TextEncodeQwenImage21` | `clip`, `vae`, `prompt`, `negative_prompt=""`, `resolution`, **`images.image_1` / `images.image_2`** |
| | `KSampler` | `model←Cache`, `positive/negative←Encode[0/1]`, `latent_image←Encode[2]`, `steps=8`, `cfg=1.0`, `sampler_name="euler"`, `scheduler="simple"`, `denoise=1.0` |
| | `VAEDecode` | `samples←KSampler`, `vae←VAELoader` |
| | `SaveImage` | `images←VAEDecode`, `filename_prefix` |

权重文件名要在启动时对 `/object_info` 里的枚举值做校验，写错文件名 ComfyUI 会在入队阶段就拒绝。

**实测出图成立的那份图，原样如下**（`"1"`/`"2"` 是 `LoadImage` 的节点号，值填 `/upload/image` 回的名字；`seed` 每次随机）：

```python
{
    "1": {"class_type": "LoadImage", "inputs": {"image": PERSON_NAME}},
    "2": {"class_type": "LoadImage", "inputs": {"image": GARMENT_NAME}},
    "3": {"class_type": "UNETLoader", "inputs": {
        "unet_name": "qwen_image_2.1_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
    "4": {"class_type": "QwenImage21Cache", "inputs": {"model": ["3", 0], "device": "auto", "dtype": "default"}},
    "5": {"class_type": "CLIPLoader", "inputs": {
        "clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image"}},
    "6": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "7": {"class_type": "TextEncodeQwenImage21", "inputs": {
        "clip": ["5", 0], "vae": ["6", 0], "prompt": TRYON_PROMPT, "negative_prompt": "",
        "resolution": resolution,
        "images.image_1": ["1", 0], "images.image_2": ["2", 0]}},
    "8": {"class_type": "KSampler", "inputs": {
        "model": ["4", 0], "positive": ["7", 0], "negative": ["7", 1], "latent_image": ["7", 2],
        "seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "euler",
        "scheduler": "simple", "denoise": 1.0}},
    "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["6", 0]}},
    "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
}
```

`resolution=0` 那次跑出的就是 §6.6 那张合格的图。

### 6.3 那个坑：`images` 必须是点号平铺

`TextEncodeQwenImage21` 的参考图输入是 `COMFY_AUTOGROW_V3` 类型。**唯一能用的形状是**：

```json
"inputs": { "images.image_1": ["1", 0], "images.image_2": ["2", 0], ... }
```

写成 `"images": {"image_1": ["1",0]}`（平铺在字典里）、`{"image_1": {"image": [...]}}`（嵌套对象）、`{"images": [[...],[...]]}`（数组）、或带 `__order__` —— **四种全部被静默丢弃，不报错**。

丢弃后的特征足以自检：`TextEncodeQwenImage21.execute` 里 `latent_w = latent_h = resolution or 1024`，只有真的收到参考图时才改成参考图的比例。所以**输出成正方形就意味着参考图根本没进去**，实现或改代码后要用这个判据验一次。探针时四种错形状跑出来的图分别是"凭空生成的女人"和"中世纪男人"，看着像模型不行，其实全是被丢图了。

### 6.4 `resolution` 是像素预算，不是宽高

源码：`width = round(sqrt(res²·ratio)/32)*32`，`height = round(sqrt(res²/ratio)/32)*32`，`ratio` 取第一张参考图的宽高比。`0` 表示按参考图自身尺寸（对齐到 32）。

- 样例人像 896×1152：`resolution=0` → 输出 896×1152；`resolution=1024` → 也算出 896×1152
- 要锁 1K 档（面积 ≤ 2,250,000，沿用 `image_prep.OUT_MAX_AREA`）：`resolution = 对齐到 32 的 min(1500, round(sqrt(人像面积)))`，这样 `resolution²` 就是面积上限
- 人像最长边已被浏览器和服务端压到 ≤1536，所以只有接近正方形的照片（1536×1536 ≈ 2.36M）才会撞到 2K 边界，上面的算法就是为它准备的

### 6.5 耗时与成本（同一对样例，同 1K 档）

| | 本地 ComfyUI | token plan API |
|---|---|---|
| 显存热 | **20 秒** | 46–107 秒 |
| 冷启动（模型换入显存） | 95 秒 | — |
| 输出 | 896×1152，跟人相比例 | 同 |
| 成本 | 0 credit | 58 credit |

冷启动和热的差了近 5 倍，这条路的耗时**比 API 更不可预测**，因为取决于显存里还留着谁。

### 6.6 质量线在本地路上的验证状态

用与 §6.6（基线文档）同一对官方样例、同 1K 档，本地出图**保住了身份和款式**：同一个人、同样侧脸低头姿势、灰绿棚拍背景；单胸袋在穿者左胸、白纽扣、水洗牛仔色。

看得出的差别（各只有一张，种子不同，**不足以定论**）：本地蓝得更饱和、面料斜纹与磨白更少、构图稍松多露一截身长。

中文固定提示词在 2.1 turbo 上直接可用，不需要翻译，也不需要官方那段英文改写支路。

**质量线的判定权在使用者**：他看过 §6.6 这张并排对比图，但**还没有对"本地这个质量够不够当试衣间的一个来源"表态**，只定了接入方式和界面。所以这一条挂在 §8 第 3 项上：第一次真跑之后由他看着自己那次的图判定。基线文档里"用自己照片实测"同样归他，不在验收项内。

### 6.7 新增页面必须沿用相对 URL 约定（另一个"本机好、部署坏"的坑）

`f74db68`（2026-10-26，使用者自己提的）把全站 URL 改成了**不带前导斜杠**：页面里是 `static/app.js`，请求是 `api/records`、`api/quota`，记录里的图是 `files/<rid>/<name>`。为的是挂在 `/mfr/` 这种子路径下时由浏览器按当前目录解析，而不是顶到站点根。

新加设置页时三个地方都踩得到：主页指向设置页的链接、设置页自己的 `fetch`、以及后端返回给前端的任何路径。**写成 `/settings`、`/api/settings` 这种绝对路径，本机 `dev_run.py` 完全正常，`/mfr` 实例上直接 404，且不会有任何报错提示你去查这一层**。改完后要在浏览器地址栏带子路径的情况下点一遍，不能只在 `localhost:8000` 上验。

（那次提交没同步改 `tests/`，留下了 4 条红的 —— 见 §8 第 7 项的前置处理。）

## 7. 我的判断，不是使用者提的

- **来源选择只影响之后的生成，不追溯记录**。`meta.source` 在生成时写死
- **本地也用估算卷尺 + 真实秒数**。ComfyUI 确实广播 WS `"progress"`（这个版本源码里 13 处），能给出 8 步真百分比，但代价是多一个 websocket 客户端依赖 —— 使用者看到三个选项后明确选了"零新依赖"。所以本地不装真进度
- **两条路用同一个 `ESTIMATE_MS` 思想但不同基数**：API 90 秒、本地 30 秒。冷启动 95 秒时曲线会先到 90% 然后停住，但秒数是真的，和 API 那条路现在一样的处理方式
- **确认框按来源换文案**：本地说"本地生成 · 不花额度 · 约 20–95 秒"，不说"还剩多少次"
- **`/settings` 的"测一下能不能连上"检查三件事**：地址通不通、三个权重文件名在不在 `/object_info` 的枚举里、`TextEncodeQwenImage21` 这个节点在不在。第二项能挡住"工作流能开但一跑就错"
- **本地路径的超时**：入队 + 轮询总时长上限 300 秒。本地超时无成本（不像 API 超时照样扣 58），所以到点就明确报错，不重试
- **单任务锁继续覆盖两条路**，不分来源各开一锁
- 设置文件读写失败（磁盘满、权限）不能让主页 500：读失败按默认值走，写失败返回可读错误

## 8. 验收要能看到的东西

1. 打开设置页（相对链接，见 §6.7），来源在 API 和 ComfyUI 之间切换，刷新后保持
2. 选 ComfyUI 并点"测一下能不能连上"：地址错、权重文件名错、正常三种情况给出三种不同且能照着办的中文提示
3. 选 ComfyUI 后走一次真实生成：出图、落盘、图库里这条记录标着来源是本地、耗时和 `resolution` 在元数据里
4. 关掉 ComfyUI 再生成：明确报错并指名那个地址，**不会自动改走 API**，也不扣额度
5. 输出的宽高比与人像图一致（不是正方形），且总像素 ≤ 2,250,000 —— 这一条同时证明 §6.3 那个坑没踩回去
6. 主页额度那行在两种来源下显示不同内容；确认框文案跟着变
7. 选 API 时整条老路径行为与本次改动前一致。**测试基线的口径**：HEAD 上 `pytest` 是 **4 红 63 绿**（`f74db68` 改相对 URL 时没跟着改断言）。本次工作开始前先把那 4 条断言改成相对 URL、单独一个提交收尾（不是新功能 diff 的一部分），此后以 **67 绿**为基线，不许为了新功能改动任何既有断言
8. 新增测试全部离线：ComfyUI 的 HTTP 调用用替身，不依赖本机真开着 ComfyUI

## 9. 实现后的实测与验收状态

实现落在分支 `comfyui-source`（9 个提交），离线测试 **123 通过**。

**已经用真跑验过的：**

| 项 | 结果 |
|---|---|
| 走应用真跑本地一次 | `ok:true`、`meta.source="comfyui"`、`size 896*1152`、`usage.resolution=0`、`steps=8`、耗时 **95.2 秒**（显存冷了，与 §6.5 的冷启动一致） |
| §8.5 那条判据 | 输出 **不是正方形**，比例与人像一致 → §6.3 那个坑没踩回去 |
| 不回落 | 地址改成死端口后：`stage="comfy"`、报错原文带着 `http://127.0.0.1:9/upload/image`，记录数没变（不造垃圾），且**没有任何指向 token plan 域名的出站** |
| 本地不花额度 | 本地生成那 95 秒窗口额度 `+3.975`，而**纯空闲 100 秒对照 `+4.262`** —— 同速率，说明本地生成实花 0。**那条漂移的来源已查明**：本会话自己跑在同一个 token plan 上（约 2.5 credit/分钟，评审子代理在跑时几秒就能 +68），所以昨天那套「空闲对照读数恒为 0」的前提今天不成立了 —— 58 这个数仍然只以昨天的零噪声对照为准，今天补测时被 58 迟到入账（生成结束后数秒内才落地） |
| 死端口失败尝试 | 额度 `+0.000` |
| 界面分辨来源 | 图库角标序列 `本地 · API · API · API · API`；灯箱标题 `2026-10-27T15:57:25 · 本地 ComfyUI · 输出 896×1152 · 用时 95.2 秒` |
| 设置页真机自检 | 对活着的 ComfyUI → 「都通」；死端口 → 指名地址；`localhost:8188`（缺协议）→ 400 并给出正确格式示例 |
| §6.7 相对 URL | 设置页与主页源码静态扫描 + `grep -o '"/api\|"/static\|"/settings'` 双重确认无前导斜杠 |

**后来补上的（使用者点头之后）：**

| 项 | 结果 |
|---|---|
| §8.7 API 侧真跑回归 | 切回 api 真出图：`ok:true`、`896*1152`、48.2 秒、`rewrite_status:"success"`、`meta.source="api"` → 老路径没被这轮改动弄坏 |
| 本地质量够不够 | **使用者判定：可以**（原话「2 可以」，看图对象是 `20261027-155725-e080` 那张 896×1152）→ §6.6 那条留给他的判定了结 |
| `/mfr` 子路径部署 | 用 `DispatcherMiddleware` 挂 `/mfr` 实测：`/mfr/`、`/mfr/settings`、`/mfr/static/{app,settings}.js`、`/mfr/api/{records,quota}` 全 200，页面内引用仍是无前导斜杠的相对路径 |

**仍然没验的**：自己的照片走本地（和基线文档同一口径，归使用者在手机上试）。

## 10. 终审结果

整分支独立评审（9 提交 / 123 测试当时）：**无 Critical**，两条 Important 已在本分支修掉 ——
① `.env` 的 `TRYON_SOURCE`/`COMFY_URL` 是死配置（计划自带的洞：Task 2 设计了 `defaults` 参数并测了它，Task 5 的伪码没接线），
现在四处 `load_prefs` 都传 `Prefs(settings.source, settings.comfy_url)`，配 4 条测试；
② 手机从设置页物理返回主页时页面走 bfcache 不重跑初始化，确认框会陈旧的写着"不花额度"而实际花 58 ——
补 `pageshow` 监听，浏览器实测页眉由「还能试穿 225 次…」变「本机 ComfyUI…」。
五条 Minor 记在 ledger 里未修，其中一条是评审员自己看错了文件（说 `settings.js` 监听 `#person`，实际不存在）。

评审另指出规格自相矛盾：**§7「读失败按默认（api）走」与 §4「静默换模型算失败」没调和过**。
本次取的解释是「宁可多花钱，不静默换模型」，已把这句写进 README；要改行为得回头改 §7。

## 11. 之后：默认来源改成不花钱的那条（使用者 2026-10-27 拍板）

上面那条矛盾的解法不是加警告，而是使用者的一句话：「默认为 comfyui」。于是矛盾消失 —— 设置文件读不出时
回落的默认本来就不花钱，不存在静默扣费。代价随之确定并被他接受：**全新装、或 ComfyUI 没起时，
第一次点会明确报错而不是出图**，要花钱那条得自己去设置页切。改动面：`Prefs.source` 与 `Settings.source`
的缺省值、`.env` 的缺省分支、README、6 条测试的意图。

**这次改动自己捅的娄子，记下来免得重演**：默认一翻成 comfyui，`tests/test_server.py` 里唯一一处没注入
`comfy_generate_fn` 的 `create_app(SETTINGS, …)`（`test_files_route_works_with_relative_root`）就顺着真实
默认参数连上了开发者本机活着的 ComfyUI，真生成了两张图 —— **测试仍然是绿的**，唯一征兆是整套跑分从
1 秒变成 30–96 秒。修法是那个用例自己钉成 api 并注入报错替身；另加 `tests/conftest.py` 的 autouse 护栏，
把 `comfy_api` 的四个真传输换成抛一个 `BaseException` 子类（故意不是 `Exception`，否则会被路由兜底分支
吞成一个看起来正常的 502），让同类疏忽当场失败而不是悄悄上网。误生成的两张已从 ComfyUI 输出目录删除，
Task 9 那张有意的留着。
