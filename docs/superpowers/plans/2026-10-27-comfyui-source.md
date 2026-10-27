# 本机 ComfyUI 作为第二个生成来源 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让试衣间在 token plan API 之外多一条本机 ComfyUI 生成路径，来源在设置页里选，全局看得出来。

**Architecture:** 新增两个模块 —— `tryon/prefs.py` 管"当前用哪条路 + ComfyUI 地址"（存 `data/settings.json`，`.env` 给默认值），`tryon/comfy_api.py` 是一个只管 stdlib `urllib` 的 ComfyUI HTTP 客户端（上传→入队→轮询→取图）。`server.py` 每次请求读一次 prefs 决定调谁，两条路共用同一把单任务锁、同一个 `Generation` 结果类型、同一套落盘与图库。

**Tech Stack:** Flask、Pillow、pytest、stdlib `urllib`。**不新增任何依赖。** 前端仍是无构建的原生 JS/CSS。

**Spec:** `docs/superpowers/specs/2026-10-27-comfyui-source-design.md`（本文所有编号 § 都指它）

## Global Constraints

- 不新增 Python 依赖（§5）——`requirements.txt` 必须还是 `flask` / `pillow` / `pytest`
- 全站站内 URL **不带前导斜杠**（`api/tryon`、`files/<rid>/<name>`、`static/app.js`、`settings`）——§6.7，写错是本机好、`/mfr` 404
- 对话与注释用中文；提交消息不带任何 AI trailer
- 输出锁 1K 档：面积 ≤ `image_prep.OUT_MAX_AREA` = `2_250_000`（§6.4）
- 本地提示词仍是 `tryon/prompt.py:TRYON_PROMPT`，不翻译、不改写（§6.6）
- ComfyUI 挂了**绝不自动回落 API**（§4 反目标）
- 设置文件读失败按默认值走，不能让主页 500（§7）
- 所有新增测试离线：ComfyUI 的 HTTP 一律走注入的替身传输，不依赖本机真开着 ComfyUI（§8.8）
- 测试基线：**Task 1 之后必须是 67 passed**，此后不许为新功能改动任何既有断言

## Review Focus

规格里 implied、但任务测试不一定盖得住的东西，最可能咬人的五条。每条后面都跟着"归谁的活"：

1. **`images` 写成非点号形状** → ComfyUI 静默丢参考图、输出 1024 正方形，看着像模型烂（§6.3）。→ Task 3 有一条断言请求体键名的测试；Task 9 的真跑要断言输出**不是**正方形
2. **新前端写成 `/settings`、`/api/settings`** → `/mfr` 子路径下 404 且无提示（§6.7）。→ Task 7 有一条扫 HTML/JS 源码里前导斜杠的测试
3. **选了 ComfyUI 而 ComfyUI 不可用** → 期望指名地址报错，且 API 那条 fn **一次都没被调用**。→ Task 5 有专门的"不回落"测试
4. **`data/settings.json` 坏了或写了一半** → 期望按默认来源继续跑，不是 500。→ Task 2（load_prefs）与 Task 5（生成路径）各有一条
5. **本地请求迟迟不回（冷启动 95 秒、极端更久）** → 到 300 秒要报可读错误、不重试、锁要放掉、也不该出现"顺手再调一次 API"。→ Task 3（轮询超时）与 Task 5（锁与计数）

---

### Task 1: 收尾 `f74db68` 遗留的 4 条红测试

相对 URL 那次部署改动没同步 `tests/`，HEAD 上是 4 红 63 绿（§8.7）。这是本次工作的**前置**，单独提交，不混进新功能 diff。

**Files:**
- Modify: `tests/test_records.py:119-120`
- Modify: `tests/test_server.py:79,189,225`

**Interfaces:**
- Produces: 67 绿基线，后面每个 Task 的 "Run" 步骤都以它为参照

- [ ] **Step 1: 跑测试确认现状**

Run: `cd /home/denvor/work/qwen-image && .venv/bin/python -m pytest -q`
Expected: `4 failed, 63 passed`，失败名分别是 `test_result_is_kept_under_its_real_format`、`test_tryon_happy_path_writes_record`、`test_records_list_and_delete`、`test_index_page_is_served`

- [ ] **Step 2: 把 5 处断言里的站内路径改成不带前导斜杠**

`test_records.py:119-120` 的 `f"/files/{rid}/result.jpg"` → `f"files/{rid}/result.jpg"`；`test_server.py:79` 的 `startswith("/files/")` → `startswith("files/")`；`test_server.py:189` 的 `f"/files/{rid}/thumb.jpg"` → `f"files/{rid}/thumb.jpg"`；`test_server.py:225` 的 `"/static/app.js" in page` → `"static/app.js" in page`。**只改断言字符串，不改 `client.get("/api/...")` 这类调用**（那是测试客户端直连路由，本来就该是绝对路径）

- [ ] **Step 3: 跑全量确认绿**

Run: `.venv/bin/python -m pytest -q`
Expected: `67 passed`

- [ ] **Step 4: 提交**

```bash
git add tests/test_records.py tests/test_server.py
git commit -m "补上相对 URL 那次改动没同步的测试断言"
```

---

### Task 2: `tryon/prefs.py` —— 来源与地址的持久化

**Files:**
- Create: `tryon/prefs.py`
- Modify: `tryon/settings.py`（`Settings` 加两个带默认值的字段 + 解析 `TRYON_SOURCE`/`COMFY_URL`）
- Test: `tests/test_prefs.py`、`tests/test_settings.py`

**Interfaces:**
- Produces:
  - `VALID_SOURCES: tuple[str, str] = ("api", "comfyui")`
  - `DEFAULT_COMFY_URL = "http://127.0.0.1:8188"`
  - `@dataclass(frozen=True) Prefs(source: str = "api", comfy_url: str = DEFAULT_COMFY_URL)`
  - `load_prefs(file: Path | str, defaults: Prefs | None = None) -> Prefs`
  - `save_prefs(file: Path | str, prefs: Prefs) -> None`
  - `Settings.source: str`、`Settings.comfy_url: str`（`.env` 缺省时为 `"api"` / `DEFAULT_COMFY_URL`）
- Consumes: 无（本任务是最底层）

- [ ] **Step 1: 写失败测试**

`tests/test_prefs.py` 覆盖五件事，每条都要有独立断言与中文测试名（照仓库风格）：

```python
def test_没有设置文件时用默认值并且不报错(tmp_path):
    assert load_prefs(tmp_path / "settings.json") == Prefs(source="api", comfy_url=DEFAULT_COMFY_URL)

def test_坏文件按默认走不断首页(tmp_path):        # §7：JSON 截断 / 值是 "gps" 这种非法来源
    (tmp_path / "settings.json").write_text('{"source": "comf', encoding="utf-8")
    assert load_prefs(tmp_path / "settings.json").source == "api"

def test_存进去读出来一致(tmp_path):
    save_prefs(tmp_path / "settings.json", Prefs("comfyui", "http://10.0.0.5:8188"))
    assert load_prefs(tmp_path / "settings.json") == Prefs("comfyui", "http://10.0.0.5:8188")

def test_保存不留临时文件(tmp_path):              # 原子写：先 .tmp 再 replace
    save_prefs(tmp_path / "settings.json", Prefs("comfyui", "http://x:1"))
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]

def test_env值作为默认被文件覆盖(tmp_path):       # defaults 传 Prefs("comfyui", ...) 而文件里没有该项
    ...
```

`tests/test_settings.py` 补一条：`.env` 里没有 `TRYON_SOURCE`/`COMFY_URL` 时 `Settings.source == "api"` 且 `comfy_url == "http://127.0.0.1:8188"`；有 `COMFY_URL=http://a:1` 时被读进来。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_prefs.py -q`
Expected: `ModuleNotFoundError: No module named 'tryon.prefs'`

- [ ] **Step 3: 实现 `tryon/prefs.py`**

`load_prefs` 的容错口径：文件不存在、JSON 解析失败、顶层不是 dict、`source` 不在 `VALID_SOURCES`、`comfy_url` 不是 str —— 全部**回落到 defaults 对应项**（单项坏只丢那一项，不整体丢）。`save_prefs` 写 `{"source":…, "comfy_url":…}`，`json.dumps(..., ensure_ascii=False)`，先写 `file.with_suffix(".json.tmp")` 再 `os.replace`；`mkdir(parents=True, exist_ok=True)` 建父目录。**不捕获 `OSError`** —— 写失败要让调用方（Task 6 的路由）看见并报错。

- [ ] **Step 4: `tryon/settings.py` 加两个字段**

`Settings` 已经是 `@dataclass(frozen=True)`，新增 `source: str = "api"` 与 `comfy_url: str = DEFAULT_COMFY_URL`（有默认值 → 现有测试里 `Settings(endpoint=..., api_key=..., model=...)` 的构造不破）。`load_settings` 里 `values.get("TRYON_SOURCE") or "api"`、`values.get("COMFY_URL") or DEFAULT_COMFY_URL`；`source` 不是 `VALID_SOURCES` 里的值时按 `"api"` 处理（不抛错，因为这是可选增强项，不是必填配置）

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_prefs.py tests/test_settings.py -q`
Expected: 全绿

- [ ] **Step 6: 跑全量并提交**

```bash
.venv/bin/python -m pytest -q      # 预期 67 passed + 本任务新增
git add tryon/prefs.py tryon/settings.py tests/test_prefs.py tests/test_settings.py
git commit -m "新增来源偏好：当前走 API 还是本机 ComfyUI"
```

---

### Task 3: `tryon/comfy_api.py` —— ComfyUI 客户端

§6.1–§6.4 的全部事实落进这一个文件。**这是整个功能里最容易做错的一个**，因为 §6.3 那个坑不报错。

**Files:**
- Create: `tryon/comfy_api.py`
- Test: `tests/test_comfy_api.py`
- Modify: `tryon/qwen_api.py`（`Generation` 加 `source: str = "api"` 字段）

**Interfaces:**
- Produces:
  - `class ComfyUnavailable(Exception)` — `.url`；连不上/超时之外的传输失败
  - `class ComfyRejected(Exception)` — `.url`、`.detail`；`/prompt` 入队阶段就被拒
  - `class ComfyTimeout(Exception)` — `.url`、`.seconds`
  - `class ComfyResultMissing(Exception)` — 跑完了但没有图片输出
  - `comfy_resolution(width: int, height: int) -> int`
  - `build_graph(person_name: str, garment_name: str, prompt: str, resolution: int, seed: int, prefix: str) -> dict`
  - `generate(comfy_url: str, person: PreparedImage, garment: PreparedImage, timeout: int = 300, transport=None) -> Generation`
  - seams：`_post_multipart(url, field, filename, blob, timeout) -> str`、`_post_json(url, payload, timeout) -> dict`、`_get_json(url, timeout) -> dict`、`_get_bytes(url, timeout) -> bytes`、`_sleep(seconds)`
- Consumes: `image_prep.PreparedImage`、`image_prep.OUT_MAX_AREA`、`prompt.TRYON_PROMPT`、`qwen_api.Generation`

- [ ] **Step 1: 写 `comfy_resolution` 的失败测试**

按 §6.4 的三档给死数值：

```python
def test_样例人像不需要缩小():
    assert comfy_resolution(896, 1152) == 0            # 1.03M ≤ 2.25M → 0 = 用参考图自身尺寸

def test_手机常见尺寸也是零():
    assert comfy_resolution(1152, 1536) == 0           # 1.77M

def test_接近正方形的大图要被压回一档():
    res = comfy_resolution(1536, 1536)                 # 2.36M > 2.25M
    assert res == 1472 and res * res <= OUT_MAX_AREA   # 向下对齐 32
```

- [ ] **Step 2: 写 `build_graph` 的失败测试 —— 这条挡住的正是 §6.3**

```python
def test_参考图必须是点号平铺的键():
    g = build_graph("p.jpg", "g.jpg", "把图像2的衣服穿到图像1的人身上", 0, 7, "tryon")
    enc = [n for n in g.values() if n["class_type"] == "TextEncodeQwenImage21"][0]["inputs"]
    assert "images.image_1" in enc and "images.image_2" in enc
    assert "images" not in enc                      # 嵌套字典写法会被静默丢图，绝不能用

def test_权重文件名与探针一致():
    types = {n["class_type"] for n in build_graph("p", "g", "x", 0, 1, "t").values()}
    assert {"UNETLoader", "QwenImage21Cache", "CLIPLoader", "VAELoader",
            "TextEncodeQwenImage21", "KSampler", "VAEDecode", "SaveImage", "LoadImage"} <= types
    unet = [n for n in g.values() if n["class_type"] == "UNETLoader"][0]
    assert unet["inputs"]["unet_name"] == "qwen_image_2.1_turbo_int8_convrot.safetensors"
```

（`test_权重文件名与探针一致` 里 `g` 就是上一行 `build_graph(...)` 的结果，实现者按此展开。）另外断言 `KSampler` 的 `steps == 8`、`cfg == 1.0`、`sampler_name == "euler"`、`scheduler == "simple"`，以及 `VAEDecode` 的 `vae` 与 `TextEncodeQwenImage21` 的 `vae` 指向同一个 `VAELoader` 节点。

- [ ] **Step 3: 写 `generate` 的失败测试（替身传输）**

```python
def test_一轮调用走完上传入队轮询取图(...):     # 断言顺序、参数、返回的 Generation
def test_入队被拒时把服务端的拒绝原因带出来(...)  # ComfyRejected.detail 含 "value_not_in_list" 之类原文
def test_轮询超过上限报超且不重试(...)           # _sleep 计数：只轮询，绝不重新 /prompt
def test_跑完没有图片输出时报结果缺失(...)
def test_连不上时报错并指名地址(...)              # ComfyUnavailable.url == comfy_url
```

`Generation` 的返回值断言写死：`source == "comfyui"`、`request_id == prompt_id`、`rewrite_status == ""`、`usage["input_image_count"] == 2`、`usage["resolution"] == 传入值`、`width/height` 取自 `Image.open(...).size`（用测试里造的 PNG 字节推出来的真实尺寸，不是猜的）。

- [ ] **Step 4: 实现 `build_graph`**

**逐字照抄规格 §6.2 代码块里那份图**（那是实测出图成立的唯一版本），只把 `PERSON_NAME`/`GARMENT_NAME`/`resolution`/`seed`/`prefix`/`prompt` 换成参数。`images.image_1`、`images.image_2` 与 `clip`、`vae` 等一律平铺在同一个 `inputs` 字典里。

- [ ] **Step 5: 实现 `generate`**

流程（§6.1）：`_post_multipart(f"{url}/upload/image", "image", name, data)` 传两张 → `person_name`/`garment_name` 用**上传响应里的 `name`**（同名内容相同时 ComfyUI 会复用，自己拼名字不可靠）→ `_post_json(f"{url}/prompt", {"prompt": graph})` 拿 `prompt_id` → 循环 `_get_json(f"{url}/history/{prompt_id}")`，`_sleep(1)` 一次一秒，累计超过 `timeout` 抛 `ComfyTimeout` → 从 `outputs` 里取**第一个**带 `images` 的节点，`/view?filename=&subfolder=&type=output` 拿字节 → 非 200 或 HTTPError 一律包成 `ComfyRejected`（入队阶段）或 `ComfyUnavailable`（传输阶段）。`usage` 与 `request_id` 按 Step 3 的断言填。

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_comfy_api.py -q`
Expected: 全绿

- [ ] **Step 7: 全量并提交**

```bash
.venv/bin/python -m pytest -q          # 既有断言一条都不许改
git add tryon/comfy_api.py tryon/qwen_api.py tests/test_comfy_api.py
git commit -m "本机 ComfyUI 客户端：上传、入队、轮询、取图"
```

---

### Task 4: `check()` —— 设置页那颗"测一下能不能连上"

三件事分开报，因为"端口不通"和"这台没有这个节点"和"权重文件名不对"是三种不同的修法（§7）。

**Files:**
- Modify: `tryon/comfy_api.py`（新增 `check`）
- Test: `tests/test_comfy_api.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) CheckResult(ok: bool, problems: tuple[str, ...])`
  - `check(comfy_url: str, transport=None) -> CheckResult`
  - `WEIGHTS: dict[str, tuple[str, str]]` —— `{"UNETLoader": ("unet_name", "qwen_image_2.1_turbo_int8_convrot.safetensors"), "CLIPLoader": ("clip_name", …), "VAELoader": ("vae_name", …)}`

- [ ] **Step 1: 写失败测试**

四种情形各一条：地址不通（`ok=False`，problems 里含那个 URL 字符串）；`/object_info/TextEncodeQwenImage21` 返回空对象（problems 说"这台 ComfyUI 没有 TextEncodeQwenImage21 节点"）；某个权重文件名不在枚举里（problems 点名是哪个节点哪个值）；全对（`ok=True`、`problems=()`）。

- [ ] **Step 2: 实现 `check`**

顺序：`GET /system_stats` → `GET /object_info/TextEncodeQwenImage21` → 对 `WEIGHTS` 三项各 `GET /object_info/<class>`，从 `list(values.values())[0]["input"]["required"][<widget>][0]` 取枚举列表判断在不在。任何一步抛传输异常即 `problems=(…)` 直接返回，不继续查后面几项。**不做重试、不加超时之外的新配置**，单次 10 秒。

- [ ] **Step 3: 跑测试、全量、提交**

```bash
.venv/bin/python -m pytest tests/test_comfy_api.py -q
.venv/bin/python -m pytest -q
git add tryon/comfy_api.py tests/test_comfy_api.py
git commit -m "ComfyUI 自检：能不能连上、有没有节点、权重名对不对"
```

---

### Task 5: `server.py` 按来源分派，且不回落

**Files:**
- Modify: `tryon/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `Prefs`/`load_prefs`（Task 2）、`comfy_api.generate`（Task 3）
- Produces:
  - `create_app(settings, generate_fn=generate, root="data/records", quota_fn=read_quota, comfy_generate_fn=comfy_api.generate, prefs_file=None, comfy_check_fn=comfy_api.check)`
  - `prefs_file=None` → `Path(root).resolve().parent / "settings.json"`（和 records 同一个 `data/` 目录，dev 与 `/mfr` 两种入口都自动对，测试里落 `tmp_path`）
  - 成功响应与 `meta.json` 里多一个键 `meta["source"] ∈ {"api","comfyui"}`
  - `GET api/quota` 响应多一个键 `source`

- [ ] **Step 1: 写失败测试（Review Focus 第 3、4、5 条在这里钉）**

```python
def test_选comfyui时走本地不走api(tmp_path, make):     # 注入两个 fn，断言 api fn 调用次数为 0
def test_本地失败绝不偷偷调api(tmp_path, make):        # comfy fn 抛 ComfyUnavailable，断言 api fn 次数仍为 0 且响应 stage=="comfy"
def test_报错指名地址里的那串内容(tmp_path, make):      # message 含 "http://127.0.0.1:8188"
def test_设置文件坏了按api走且页面不500(tmp_path, make)
def test_来源写进记录和列表(tmp_path, make)             # meta["source"]=="comfyui"，GET api/records 里也带
def test_两路共用一把单任务锁(tmp_path, make)           # comfy fn 里占住时第二个请求 409 stage=="busy"
def test_本地时不查额度只回来源(tmp_path, make):        # quota_fn 断言未被调用，且响应 source=="comfyui"
```

`make` fixture 现在长这样（`tests/test_server.py:52`），新增两个注入参数并给默认值：

```python
def _make(generate_fn=ok_generation, quota_fn=fake_quota, comfy_fn=None, prefs_source="api"):
    ...
    app = create_app(SETTINGS, generate_fn=generate_fn, root=root, quota_fn=quota_fn,
                     comfy_generate_fn=comfy_fn or boom_comfy, prefs_file=root.parent / "settings.json")
```

`boom_comfy` 默认抛 `ComfyUnavailable`，这样**任何没显式注入本地 fn 的既有测试即使误加分流也不会静默走通**。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_server.py -q`
Expected: 新增的 7 条失败（`create_app` 不认识 `comfy_generate_fn` → TypeError）

- [ ] **Step 3: 实现分派**

`api_tryon` 内，锁已拿到之后：

```python
prefs = load_prefs(prefs_file)          # 读失败自己回落默认，不抛
fn = comfy_generate_fn if prefs.source == "comfyui" else generate_fn
```

`comfyui` 分支调用 `fn(prefs.comfy_url, person, garment)`，`api` 分支保持 `fn(settings, person, garment)`；`meta["source"] = prefs.source`。本地那三个异常各自映射成 `_fail("comfy", …, 502)`：`ComfyUnavailable` → "连不上 ComfyUI（<url>）。设置页里可以改地址，或者把来源切回 API。"；`ComfyRejected` → 附上 `detail` 原文（不翻译不改写，和接口原话一个口径）；`ComfyTimeout` → "本机 ComfyUI <n> 秒没出图。本地生成不花额度，可以直接重来一次。"。**`except Exception` 那个兜底分支里绝对不许出现"改走 API"的调用。**

`api_quota` 先读 prefs：`source == "comfyui"` 时**不调 `quota_fn`**，返回 `{"ok": True, "source": "comfyui", "quota": None}`（前端按 source 决定显示什么）。

- [ ] **Step 4: 跑测试确认通过，再跑全量**

Run: `.venv/bin/python -m pytest -q`
Expected: 全绿，既有断言零改动

- [ ] **Step 5: 提交**

```bash
git add tryon/server.py tests/test_server.py
git commit -m "生成路径按来源分派：ComfyUI 不可用只报错，不回落 API"
```

---

### Task 6: 设置页的后端路由

**Files:**
- Modify: `tryon/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `Prefs`/`save_prefs`/`load_prefs`（Task 2）、`comfy_check_fn`（Task 4）
- Produces:
  - `GET settings` → 返回 `static/settings.html`（路由字符串是 `"/settings"`，Flask 路由本来就要带斜杠；**只有模板里和前端 JS 里的引用不带**）
  - `GET api/settings` → `{"ok": True, "source": …, "comfy_url": …, "default_comfy_url": DEFAULT_COMFY_URL, "valid_sources": […]}`
  - `POST api/settings` → 收 `{"source":…, "comfy_url":…}`，非法 source 返回 `400`（`stage: "settings"`），`comfy_url` 必须以 `http://` 或 `https://` 开头且无空白，否则 `400`；`OSError` → `500` 带中文说明
  - `POST api/settings/check` → 收 `{"comfy_url":…}`（没带就用已存的），返回 `{"ok": bool, "problems": [...]}`，永远 200（连不上也是一种正常完成）

- [ ] **Step 1: 写失败测试**

`GET api/settings` 无文件时返回默认；`POST` 存 `comfyui` 后 `GET` 读回且 `api_tryon` 立刻走本地（来源即时生效，不重启）；非法 `source` 与非法 `comfy_url`（`"localhost:8188"` 缺协议、`"http://a b"` 带空格）各回 400 且 message 说清要什么；`save_prefs` 抛 `OSError` 时 500 且不把 traceback 漏进 message；`POST api/settings/check` 把 `CheckResult` 原样转成 JSON 且 HTTP 恒 200；`GET settings` 返回 200 且页面里 `fetch("api/settings` 出现、`fetch("/api/settings` 不出现（§6.7 那条 Review Focus 的静态扫描放在这儿一起测）。

- [ ] **Step 2: 跑测试确认失败** → 404 / 路由不存在

- [ ] **Step 3: 实现四个路由**

`send_from_directory(STATIC_DIR, "settings.html")` 复用现有 `index()` 的写法。校验函数 `_valid_url(s) -> bool` 用 `urlsplit` 判 scheme ∈ {http, https} 且 `netloc` 非空且无空格 —— 不为"地址必须真的存在"写任何检查，那是 check 按钮的活。

- [ ] **Step 4: 跑测试、全量、提交**

```bash
git add tryon/server.py tests/test_server.py static/settings.html
git commit -m "设置接口：读写来源与地址，并做 ComfyUI 自检"
```

---

### Task 7: 设置页前端

**Files:**
- Create: `static/settings.html`
- Create: `static/settings.js`
- Modify: `static/index.html:12-16`（header 里加一个 `← 返回试衣间` 与 `设置` 入口）、`static/style.css`（设置页样式，沿用现有变量）

**Interfaces:**
- Consumes: Task 6 的四个端点
- Produces: 一个能选来源、填地址、点自检的页面

- [ ] **Step 1: 写 `settings.html`**

结构：标题「设置」；一段来源单选（两个 `<label><input type="radio" name="source">`，文案「token plan API（一次 58 credit）」/「本机 ComfyUI（不花额度）」）；一个 `comfy_url` 文本框 + placeholder 用 `default_comfy_url`；一个「测一下能不能连上」按钮；一个 `role="status"` 的结果区；底部「保存」。**所有 `href`/`src` 不带前导斜杠**：`static/style.css`、`static/settings.js`，`fetch` 用 `api/settings`。返回链接是 `""`（同级 index 用 `.` 或直接 `<a href=".">`）。

- [ ] **Step 2: 写 `settings.js`**

载入时 `GET api/settings` 回填；改任一控件即 `POST api/settings`（保存失败把 message 显示在结果区，**不要把异常吞掉**）；点自检时 `POST api/settings/check`，把 `problems` 逐条列成 `<li>`（用 `textContent`，和 `app.js` 里现有的防 XSS 口径一致）；`ok` 时显示「都通」。选 ComfyUI 且自检失败过，保存照常成功 —— 允许先存后修，别把地址校验做严。

- [ ] **Step 3: 手工验收（这台机器上有真 ComfyUI，别浪费）**

```bash
fuser -k 8000/tcp; nohup .venv/bin/python dev_run.py > /tmp/tryon-app.log 2>&1 &
```
浏览器开 `http://127.0.0.1:8000/settings`：切到 ComfyUI → 点自检 → 应显示"都通"；把地址改成 `http://127.0.0.1:9999` → 点自检 → 应显示连不上并带那个地址。再用子路径自查一遍相对 URL：`curl -s http://127.0.0.1:8000/settings | grep -o '"/api\|"/static\|"/settings'` 预期**无输出**。

- [ ] **Step 4: 跑全量、提交**

```bash
.venv/bin/python -m pytest -q
git add static/settings.html static/settings.js static/index.html static/style.css tests/test_server.py
git commit -m "设置页：选生成来源、填 ComfyUI 地址、自检连通"
```

---

### Task 8: 主页处处看得出来走的是哪条路

**Files:**
- Modify: `static/app.js`（`ESTIMATE_MS` 拆分、`loadQuota`、`confirmStart`、`usageLine`、`recordCell`/灯箱标题）
- Modify: `static/index.html:15`（`#quota` 那行旁边留一个来源位）
- Modify: `static/style.css`（来源标签样式）
- Test: 手工（前端无测试框架，和既有做法一致）

**Interfaces:**
- Consumes: `GET api/quota` 里新增的 `source`（Task 5）、`meta.source`（Task 5）
- Produces: 无下游

- [ ] **Step 1: 估算基数按来源分开**

`const ESTIMATE_MS = 90000` 改成 `const ESTIMATE = { api: 90000, comfyui: 30000 }` + `let source = "api"`（由 `loadQuota` 的响应设置），`startTape` 里用 `ESTIMATE[source]`。§7 明确记着：冷启动 95 秒时曲线会先到 90% 停住，秒数是真的。

- [ ] **Step 2: 页眉那行按来源显示**

`source === "comfyui"` 时显示「本机 ComfyUI · 不花额度 · 约 20–95 秒」（**不去查额度**，Task 5 已经保证后端不查）；否则保持现在的「还能试穿 N 次 · 一次 58 credit · N 天后额度重置」。这一行**不报 ComfyUI 的连通状态** —— 地址错了是点「开始试穿」或自检时才知道，页眉不该假装知道。`api/quota` 整体失败（服务没起来）时才退回现有的「额度查不到：…」文案。

- [ ] **Step 3: 确认框按来源换文案**

`confirmStart()`：本地时第二段换成「本地生成 · 不花额度 · 约 20–95 秒」，第一行仍问「开始试穿？」，末行「取消不动图片」保留。**本地路径不许出现"还剩多少次"**。

- [ ] **Step 4: 结果行与图库标注来源（§4 反目标第 3 条）**

`usageLine(meta, elapsed)` 开头分支：`meta.source === "comfyui"` 时返回 `["本地 ComfyUI", `输出 ${w}×${h}`, `用时 ${elapsed} 秒"]`；API 路径原样不动（现有 67 测试里对 API 的断言不许变）。图库每格左上角加一个小角标 `本地` / `API`（CSS 绝对定位，`aria-hidden`，灯箱标题串里也带上来源）。

- [ ] **Step 5: 手工验收 + 提交**

`http://127.0.0.1:8000/` 上切两种来源各看一遍页眉、确认框、结果行、图库角标。

```bash
git add static/app.js static/index.html static/style.css
git commit -m "界面标出生成来源：页眉、确认框、结果行与图库"
```

---

### Task 9: 端到端真跑一次 + 文档收尾

前八个 Task 全部离线。**唯一能证明 §6.3 那个坑没踩回去的是一次真出图**，本地不花额度，没有理由省。

**Files:**
- Modify: `README.md`（用法、来源切换、故障排查、测试数量）
- Modify: `docs/superpowers/specs/2026-10-27-comfyui-source-design.md`（状态从「待评审」改成「已实现」，并回填实测数字）

- [ ] **Step 1: 确认 ComfyUI 在跑，来源切到 ComfyUI，用样例对真生成一次**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8188/system_stats   # 期望 200
curl -s -X POST http://127.0.0.1:8000/api/settings -H 'Content-Type: application/json' \
     -d '{"source":"comfyui","comfy_url":"http://127.0.0.1:8188"}'
curl -s -X POST http://127.0.0.1:8000/api/tryon -F person=@samples/person.jpg -F garment=@samples/garment.jpg
```

Expected: `ok: true`、`record.meta.source == "comfyui"`。**打开 `data/records/<新id>/result.png` 用眼睛确认 §8.5**：宽高比与人像一致（约 896×1152，**不是正方形**），且总像素 ≤ 2,250,000。正方形就是 `images` 形状又丢了，回到 Task 3 修，别往下走。

- [ ] **Step 2: 把地址改成一个死端口再点一次（不要去杀使用者的 ComfyUI）**

杀进程会动到使用者自己开着的实例，换个等价又无损的证法：把来源保持 `comfyui`、地址指到一个没人监听的端口，再请求一次。

```bash
curl -s -X POST http://127.0.0.1:8000/api/settings -H 'Content-Type: application/json' \
     -d '{"source":"comfyui","comfy_url":"http://127.0.0.1:9"}'
curl -s -X POST http://127.0.0.1:8000/api/tryon -F person=@samples/person.jpg -F garment=@samples/garment.jpg
```

Expected: `ok: false`、`stage: "comfy"`、message 里带着 `http://127.0.0.1:9`（证明错误文案真把地址露出来了）；`/tmp/tryon-app.log` 与 `ss` 层面**没有**对 token plan 域名的出站请求（"不回落"的最后证据）；`qianwen subscription status` 换算出的 used **没有 +58**。测完把地址改回 `http://127.0.0.1:8188`。

- [ ] **Step 3: 切回 API 跑一次，确认老路径没坏（§8.7）**

来源改回 `api`，真生成一次，出图正常、额度 `+58`、记录里 `source: "api"`。**这一步花 58 credit**，先跟使用者打过招呼再做。

- [ ] **Step 4: 让使用者判定本地质量（§6.6 留给他的那条）**

把 Step 1 那张图给他看，问他"本地这个质量够不够当试衣间的一个来源"。他的答复原样写进设计文档 §6.6，别替他结论。

- [ ] **Step 5: 更新 README**

补：来源在设置页切换（`/settings` 或主页入口）、frp 部署时地址要改成映射后的、ComfyUI 挂了不会偷偷走 API、本地一次 20–95 秒不花钱、测试数量改成最新值、启动脚本是 `dev_run.py`（那次部署提交没更新，顺手核一遍）。

- [ ] **Step 6: 全量测试 + 提交**

```bash
.venv/bin/python -m pytest -q
git add README.md docs/superpowers/specs/2026-10-27-comfyui-source-design.md
git commit -m "端到端验收本机 ComfyUI 来源，更新文档"
```
