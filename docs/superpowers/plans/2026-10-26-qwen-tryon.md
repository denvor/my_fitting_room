# 虚拟试穿工具 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做一个本机网页工具：上传本人照片 + 一件衣服照片，调 `qwen-image-3.0-pro` 生成"本人穿着这件衣服"的图，结果落盘成可删除的图库。

**Architecture:** Flask 单体后端（一个 `/api/tryon` 长请求 + 图库读写 + 静态页），Pillow 做上传图预处理，结果图从接口 URL 下载后写进 `data/records/<id>/`；前端一个原生 JS 页面，不引入框架。进程内一把布尔锁保证"同一时间只有一个任务"。

**Tech Stack:** Python 3.13（`.venv`）、Flask、Pillow、pytest（开发依赖）、标准库 `urllib`、原生 HTML/CSS/JS。

**Spec:** `docs/superpowers/specs/2026-10-26-qwen-tryon-design.md`

## Global Constraints

- 依赖只有 `flask`、`pillow`（测试用 `pytest`）。**不引入** `requests`、不引入任何前端框架或构建工具。
- 服务监听 `0.0.0.0:8000`，无鉴权（使用者明确要求，用于局域网调试）。
- 生成参数固定：`n=1`、`watermark=false`、输出锁 1K 档（总像素 ≤2,250,000），`prompt_extend` 与 `enable_thinking` 保持接口默认（开）。
- 接口地址与密钥只从 `.env` 读：`API_KEY`、`MODEL`、由 `OPENAI_URL` 去掉结尾 `/compatible-mode/v1` 后接 `/api/v1/services/aigc/multimodal-generation/generation`。任何日志和页面都不得出现 key。
- 图片一律以 `data:image/jpeg;base64,...` 内嵌传给接口，**绝不**把外网 URL 交给接口（实测会被拒）。
- 出图结果 URL 24 小时过期，必须在同一次请求内下载落盘。
- 代码注释、提交信息一律中文，提交信息不带任何 AI 署名 trailer。
- `data/` 已在 `.gitignore`（本人照片不进版本库）；`samples/` 是官方示例图，入库。
- 请求超时 300 秒（实测 1K 档 55.8 秒，2K 档 121.8 秒）。

## Review Focus

规格里没写清、但真实用起来最容易先撞的几件事，每个都对应到某个 Task 的测试：

- **手机照片的 EXIF 方向**：iPhone 竖拍的照片像素是横的、靠 EXIF 标记旋转。不矫正的话人像是躺的，且输出尺寸会按躺着的比例算。→ Task 2。
- **上传的"图片"根本不是图片**（HEIC、webp 动图、改了后缀名的任意文件、0 字节）：必须返回中文原因，不能 500 崩页面。iPhone 相册默认 HEIC，Pillow 读不了，文案要直接说"请改成 JPG/PNG 再传"。→ Task 2 + Task 6。
- **接口返回 200 但结构不对**（`choices` 空、content 里没有 image、缺 `usage`）：白等一分钟后再炸一次最气人，必须转成可读错误。→ Task 4。
- **图已经生成、但下载环节失败或 URL 已过期**：额度已花，不能无声吞掉，要重试 3 次并把原 URL 亮出来。→ Task 4 + Task 6。
- **磁盘上有残缺记录**（目录在、`meta.json` 或 `result.png` 没了，比如上次断电）：图库列表要跳过它而不是整个列表 500。→ Task 5。

---

## 文件结构

```
app.py                     启动入口，只调用 create_app().run(...)
tryon/__init__.py          空包标记
tryon/settings.py          .env 读取与接口地址推导
tryon/image_prep.py        上传图校验/EXIF 矫正/缩放/base64；输出尺寸推导
tryon/prompt.py            固定提示词文本
tryon/qwen_api.py          请求体构造、HTTP 调用、响应解析、结果图下载
tryon/records.py           记录落盘、列表、缩略图、删除
tryon/server.py            Flask 应用工厂、路由、并发锁
static/index.html          页面结构
static/app.js              上传、进度条、结果区、图库、删除
static/style.css           样式
samples/person.jpg         官方示例人像（验收输入）
samples/garment.jpg        官方示例衣服（验收输入）
tests/test_settings.py     Task 1
tests/test_image_prep.py   Task 2
tests/test_qwen_api.py     Task 3
tests/test_records.py      Task 4
tests/test_server.py       Task 5
```

---

### Task 1: 环境与配置（settings.py）

**Files:**
- Create: `requirements.txt`、`tryon/__init__.py`、`tryon/settings.py`、`tests/__init__.py`、`tests/test_settings.py`
- Move: `spike_in/person.png` → `samples/person.jpg`，`spike_in/garment.png` → `samples/garment.jpg`（用 Pillow 转 JPEG，quality=92）

**Interfaces:**
- Produces:
  - `Settings(endpoint: str, api_key: str, model: str)`（frozen dataclass）
  - `load_settings(env_file: str | Path = ".env") -> Settings`
  - `derive_endpoint(openai_url: str) -> str`
  - `class SettingsError(Exception)`，`str(e)` 是给人看的中文

- [ ] **Step 1: 装依赖并建骨架** — `printf 'flask\npillow\npytest\n' > requirements.txt`，`.venv/bin/pip install -r requirements.txt`，建 `tryon/__init__.py`（空）与 `tests/__init__.py`（空）。验证：`.venv/bin/python -c "import flask, PIL, pytest"` 无输出、退出码 0。

- [ ] **Step 2: 写失败测试** `tests/test_settings.py`

```python
def test_derive_endpoint_strips_compatible_mode():
    assert derive_endpoint("https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1") == (
        "https://token-plan.maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation")

def test_derive_endpoint_without_suffix_uses_host_only():
    assert derive_endpoint("https://token-plan.maas.qianwenaiapi.com/") == (
        "https://token-plan.maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation")

def test_load_settings_reads_real_env_file():
    s = load_settings(Path(__file__).resolve().parents[1] / ".env")
    assert s.model == "qwen-image-3.0-pro" and s.api_key and s.endpoint.startswith("https://")

def test_load_settings_missing_key_raises(tmp_path):
    p = tmp_path / ".env"; p.write_text("OPENAI_URL=https://x/compatible-mode/v1\nMODEL=m\n")
    with pytest.raises(SettingsError, match="API_KEY"):
        load_settings(p)
```

- [ ] **Step 3: 跑测试确认失败** — `cd /home/denvor/work/qwen-image && .venv/bin/python -m pytest tests/test_settings.py -q`，预期 `ModuleNotFoundError`/`ImportError`。
- [ ] **Step 4: 实现** `tryon/settings.py`：手工按行解析 `KEY=VALUE`（不引入 python-dotenv），`derive_endpoint` 先去掉结尾 `/`，去掉结尾 `/compatible-mode/v1`（若有）再接固定路径；`load_settings` 对 `OPENAI_URL`/`API_KEY`/`MODEL` 逐项缺失即抛 `SettingsError("缺少 API_KEY")`。
- [ ] **Step 5: 跑测试确认通过** — 同上命令，预期 4 passed。
- [ ] **Step 6: 转移验收示例图** — 用 Pillow 把 `spike_in/person.png`、`spike_in/garment.png` 存成 `samples/person.jpg`、`samples/garment.jpg`（长边 1536 以内、quality=92）。验证：`.venv/bin/python -c "from PIL import Image; [print(i, Image.open('samples/'+i).size) for i in ('person.jpg','garment.jpg')]"` 两张都在 896×1152 附近。
- [ ] **Step 7: 提交** — `git add -A requirements.txt tryon tests samples && git commit -m "加上配置读取与验收用示例图"`

---

### Task 2: 图像预处理（image_prep.py）

**Files:**
- Create: `tryon/image_prep.py`、`tests/test_image_prep.py`

**Interfaces:**
- Produces:
  - `PreparedImage(data: bytes, width: int, height: int, data_uri: str)`（frozen dataclass，`data` 是 JPEG 字节，`data_uri` 形如 `data:image/jpeg;base64,...`）
  - `class ImageRejected(Exception)`，`str(e)` 是给用户看的中文原因
  - `prepare_upload(raw: bytes) -> PreparedImage`
  - `api_size(width: int, height: int) -> str`（返回 `"896*1152"`）
  - 常量：`MAX_UPLOAD_BYTES = 10_485_760`、`MAX_SIDE = 1536`、`MIN_SIDE_AFTER_RESIZE = 384`、`MAX_INPUT_RATIO = 10.0`、`OUT_MIN_AREA = 262_144`、`OUT_MAX_AREA = 2_250_000`、`OUT_MAX_RATIO = 8.0`、`DIM_STEP = 8`

- [ ] **Step 1: 写失败测试** `tests/test_image_prep.py`（辅助函数 `png(w,h)` 用 Pillow 现造纯色图，返回 bytes）

```python
def test_small_input_rejected_with_readable_reason():
    with pytest.raises(ImageRejected, match="太小"):
        prepare_upload(png(200, 300))

def test_oversized_bytes_rejected():
    with pytest.raises(ImageRejected, match="10MB"):
        prepare_upload(b"\x00" * (MAX_UPLOAD_BYTES + 1))

def test_non_image_rejected_says_convert_to_jpg():
    with pytest.raises(ImageRejected, match="JPG/PNG"):
        prepare_upload(b"not an image at all")

def test_extreme_ratio_rejected():
    with pytest.raises(ImageRejected, match="长宽比"):
        prepare_upload(png(3000, 100))

def test_long_side_capped_at_1536():
    p = prepare_upload(png(4000, 3000))
    assert max(p.width, p.height) == MAX_SIDE and p.data[:2] == b"\xff\xd8"

def test_exif_orientation_is_applied_before_measuring():
    # 辅助函数 rotated_jpeg_with_exif(w,h,orientation)：像素照 w×h 画，再
    # ex = Image.Exif(); ex[0x0112] = orientation; img.save(buf, "JPEG", exif=ex.tobytes())
    # 即 EXIF=6（右转 90 显示）时像素本身仍是横躺的 1500×1000
    p = prepare_upload(rotated_jpeg_with_exif(1500, 1000, orientation=6))
    assert p.height > p.width

def test_data_uri_prefix_and_decodable():
    p = prepare_upload(png(800, 1200))
    assert p.data_uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(p.data_uri.split(",", 1)[1]) == p.data

def test_api_size_keeps_ratio_in_1k_band_and_step8():
    w, h = (int(v) for v in api_size(1536, 1152).split("*"))
    assert OUT_MIN_AREA <= w * h <= OUT_MAX_AREA and w % 8 == 0 and h % 8 == 0
    assert abs((w / h) - (1536 / 1152)) < 0.02

def test_api_size_clamps_ratio_to_8():
    w, h = (int(v) for v in api_size(1536, 120).split("*"))
    assert max(w / h, h / w) <= 8.0

def test_api_size_upscales_tiny_person_to_minimum_area():
    w, h = (int(v) for v in api_size(400, 300).split("*"))
    assert w * h >= OUT_MIN_AREA
```

- [ ] **Step 2: 跑测试确认失败** — `.venv/bin/python -m pytest tests/test_image_prep.py -q`，预期 ImportError。
- [ ] **Step 3: 实现** `tryon/image_prep.py`：顺序是 字节数校验 → `Image.open` 失败即抛 `ImageRejected("不是可识别的图片，请改成 JPG/PNG 再传")` → `ImageOps.exif_transpose` → 转 RGB → 原图长宽比 >10 抛"长宽比"、缩放后短边或长边小于 384 抛"太小" → 缩放系数 `min(1.0, MAX_SIDE/长边, sqrt(OUT_MAX_AREA/面积))` → 宽高各向下取 8 的倍数 → `save(format="JPEG", quality=92)`。`api_size` 是纯函数：钳比例到 8:1、按 `sqrt(目标面积/当前面积)` 缩放到 `[OUT_MIN_AREA, OUT_MAX_AREA]`、宽高取 8 的倍数。
- [ ] **Step 4: 跑测试确认通过** — 预期 9 passed。
- [ ] **Step 5: 提交** — `git commit -am "上传图片的校验、EXIF 矫正与输出尺寸推导"`

---

### Task 3: 提示词与接口客户端（prompt.py / qwen_api.py）

**Files:**
- Create: `tryon/prompt.py`、`tryon/qwen_api.py`、`tests/test_qwen_api.py`

**Interfaces:**
- Consumes: `Settings`、`PreparedImage`、`api_size`
- Produces:
  - `TRYON_PROMPT: str`
  - `Generation(image: bytes, width: int, height: int, usage: dict, request_id: str, rewrite_status: str)`
  - `class ApiError(Exception)`：属性 `code: str`、`message: str`、`request_id: str`
  - `class ResultMissing(Exception)`；`class ResultFetchFailed(Exception)`：属性 `url: str`、`reason: str`
  - `build_request(person: PreparedImage, garment: PreparedImage, size: str, model: str) -> dict`
  - `download_image(url: str, attempts: int = 3, timeout: int = 120) -> bytes`
  - `generate(settings: Settings, person: PreparedImage, garment: PreparedImage, timeout: int = 300) -> Generation`

- [ ] **Step 1: 写 `tryon/prompt.py`** — 逐字使用下面这段（第 6.6 节基线就是这句话的变体跑出来的，改动等于改验收线）：

```python
TRYON_PROMPT = (
    "把图像2中的衣服穿到图像1中的人身上，替换图像1人物原来的上衣。"
    "必须严格保持图像1中人物的脸部五官、发型、身材比例、姿势和背景不变。"
    "必须完整还原图像2中衣服的款式、颜色、纽扣、口袋、缝线、面料纹理和做旧细节。"
    "忽略图像2里的衣架、挂钩、背景和其他衣物。"
    "输出看起来像真实拍摄的照片，光影与图像1一致。"
)
```

- [ ] **Step 2: 写失败测试** `tests/test_qwen_api.py`（用 monkeypatch 替换 `qwen_api._post` / `qwen_api._get`，**测试不得联网**）

```python
def test_build_request_image_order_is_person_then_garment_then_text():
    body = build_request(person, garment, "896*1152", "qwen-image-3.0-pro")
    kinds = [k for item in body["input"]["messages"][0]["content"] for k in item]
    assert kinds == ["image", "image", "text"]
    assert body["input"]["messages"][0]["content"][2]["text"] == TRYON_PROMPT
    assert body["parameters"] == {"n": 1, "size": "896*1152", "watermark": False}
    assert body["model"] == "qwen-image-3.0-pro"

def test_generate_returns_image_and_usage(monkeypatch):
    g = generate(settings, person, garment)
    assert g.image == b"fakepng" and g.usage["output_image_count"] == 1 and g.request_id == "r-1"
    assert monkeypatched_urls[0].startswith("data:image/jpeg;base64,")   # 内嵌，不是外网 URL

def test_generate_raises_apiperror_with_server_message(monkeypatch):
    # 模拟 400 {"code":"InvalidParameter","message":"...","request_id":"r-2"}
    with pytest.raises(ApiError) as e: generate(settings, person, garment)
    assert e.value.code == "InvalidParameter" and e.value.request_id == "r-2"

def test_generate_handles_200_without_image(monkeypatch):
    with pytest.raises(ResultMissing): generate(settings, person, garment)

def test_download_image_retries_three_times_then_reports_url(monkeypatch):
    with pytest.raises(ResultFetchFailed) as e: download_image("https://x/y.png", attempts=3)
    assert calls == 3 and e.value.url == "https://x/y.png"
```

- [ ] **Step 3: 跑测试确认失败** — `.venv/bin/python -m pytest tests/test_qwen_api.py -q`，预期 ImportError。
- [ ] **Step 4: 实现** `tryon/qwen_api.py`：`_post(url, body, key, timeout) -> (status, bytes)` 与 `_get(url, timeout) -> bytes` 两个薄封装（标准库 `urllib.request`，带 `Authorization: Bearer`），便于测试替换。`generate` 内部：`size = api_size(person.width, person.height)`；解析响应时取 `output.choices[0].message.content[*].image` 的**第一个**，取不到即抛 `ResultMissing`（异常里带上 `request_id`）；HTTP 非 200 或响应含 `code` 字段即抛 `ApiError`，`message`/`code`/`request_id` 原样透传，不做任何改写；`download_image` 每次失败间隔 2 秒重试，全部失败抛 `ResultFetchFailed(url, reason)`。
- [ ] **Step 5: 跑测试确认通过** — 预期 5 passed。
- [ ] **Step 6: 提交** — `git commit -am "接上千问换装接口：请求体、报错透传、结果图下载重试"`

---

### Task 4: 记录存储（records.py）

**Files:**
- Create: `tryon/records.py`、`tests/test_records.py`

**Interfaces:**
- Consumes: `Generation`、`PreparedImage`
- Produces:
  - `new_record_id(now: datetime | None = None) -> str`（形如 `20261026-113005-1f3a`，4 位十六进制随机后缀）
  - `save_record(root: Path, rid: str, person: bytes, garment: bytes, result: bytes, meta: dict) -> Path`（写 `person.jpg`/`garment.jpg`/`result.png`/`thumb.jpg`/`meta.json`）
  - `make_thumb(result: bytes, long_side: int = 320) -> bytes`
  - `list_records(root: Path) -> list[dict]`（每项 `{"id": str, "meta": dict}`，id 倒序；残缺目录跳过）
  - `delete_record(root: Path, rid: str) -> bool`
  - `FILE_NAMES = ("person", "garment", "result", "thumb")`

- [ ] **Step 1: 写失败测试** `tests/test_records.py`（`root` 用 `tmp_path`，图片用 Task 2 那种现造 bytes）

```python
def test_save_then_list_roundtrip_keeps_meta(root):
    save_record(root, "20261026-113005-1f3a", person, garment, result,
                {"created_at": "2026-10-26T11:30:05", "elapsed_s": 56.2,
                 "size": "896*1152", "usage": {"output_image_count": 1},
                 "request_id": "r-1", "rewrite_status": "success"})
    got = list_records(root)
    assert got[0]["id"] == "20261026-113005-1f3a"
    assert got[0]["meta"]["usage"]["output_image_count"] == 1

def test_list_newest_first(root):    # 造两条不同 id，断言倒序
def test_broken_record_is_skipped_not_fatal(root):
    (root / "20261026-110000-bad0").mkdir(parents=True)   # 空目录，没 meta.json
    save_record(root, "20261026-120000-good", person, garment, result, {"created_at": "x"})
    assert [r["id"] for r in list_records(root)] == ["20261026-120000-good"]

def test_delete_removes_whole_dir(root):
    assert delete_record(root, rid) is True and not (root / rid).exists()

def test_delete_missing_returns_false(root):
    assert delete_record(root, "no-such-id") is False

def test_thumb_is_smaller_jpeg(root):
    assert max(Image.open(BytesIO(make_thumb(result))).size) <= 320

def test_new_record_id_is_path_safe():
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", new_record_id())
```

- [ ] **Step 2: 跑测试确认失败** — `.venv/bin/python -m pytest tests/test_records.py -q`
- [ ] **Step 3: 实现** `tryon/records.py`：`list_records` 只对"目录名合法 + `meta.json` 可解析 + 四张图齐全"的记录出列，其余静默跳过（残缺记录不炸列表）。`delete_record` 用 `shutil.rmtree`，路径拼接前先按 `new_record_id` 的正则校验 `rid`，不合法直接返回 `False`（防目录穿越）。
- [ ] **Step 4: 跑测试确认通过** — 预期 7 passed。
- [ ] **Step 5: 提交** — `git commit -am "试穿记录落盘、图库列表与删除"`

---

### Task 5: HTTP 服务（server.py / app.py）

**Files:**
- Create: `tryon/server.py`、`app.py`、`tests/test_server.py`

**Interfaces:**
- Consumes: `load_settings`、`prepare_upload`、`generate`、`records.*`
- Produces:
  - `create_app(settings: Settings, generate_fn=generate, root: Path | str = "data/records") -> Flask`
  - 路由：`GET /`、`GET /api/records`、`POST /api/tryon`、`DELETE /api/records/<rid>`、`GET /files/<rid>/<name>`
  - 成功响应：`{"ok": true, "elapsed_s": 56.2, "record": {"id":..., "meta":..., "files": {"person":"/files/<id>/person.jpg", ...}}}`
  - 失败响应（HTTP 4xx/409/502）：`{"ok": false, "stage": "image|api|result|busy", "message": str, "code": str?, "request_id": str?, "result_url": str?}`

- [ ] **Step 1: 写失败测试** `tests/test_server.py`（`create_app` 注入假的 `generate_fn` 和 `tmp_path` 作 root，用 `app.test_client()`；`generate_fn` 返回值用真 PNG 字节造 `Generation`）

```python
def test_tryon_happy_path_writes_record(client):
    r = client.post("/api/tryon", data={"person": (BytesIO(person), "p.jpg"),
                                        "garment": (BytesIO(garment), "g.jpg")})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert r.get_json()["record"]["files"]["result"].startswith("/files/")

def test_tryon_bad_image_reports_stage_image_not_500(client):
    r = client.post("/api/tryon", data={"person": (BytesIO(b"junk"), "p.png"),
                                        "garment": (BytesIO(garment), "g.jpg")})
    assert r.status_code == 400 and r.get_json()["stage"] == "image"

def test_busy_second_request_is_409(client):
    # 用一把已被占用的锁（或让假 generate 阻塞在 Event 上）验证第二个请求立刻 409
    assert second.status_code == 409 and second.get_json()["stage"] == "busy"

def test_api_error_passes_through_code_and_request_id(client):
    assert body["stage"] == "api" and body["code"] == "InvalidParameter" and body["request_id"] == "r-9"

def test_result_fetch_failure_reports_url(client):
    # 假 generate 抛 ResultFetchFailed(url="https://x/y.png")
    assert body["stage"] == "result" and body["result_url"] == "https://x/y.png"

def test_records_list_and_delete(client, root):
    # 落一条 → GET /api/records 有它 → DELETE /api/records/<id> → 再 GET 为空，目录消失

def test_files_route_rejects_traversal(client):
    assert client.get("/files/..%2f..%2f.env/person.jpg").status_code in (400, 404)

def test_failed_generation_leaves_no_record(client, root):
    # 假 generate_fn 抛 ApiError 时，root 下不该多出任何目录（规格 7：失败的生成不进图库）
    assert client.post("/api/tryon", data=...).status_code == 502
    assert (list(root.iterdir()) if root.exists() else []) == []
```

- [ ] **Step 2: 跑测试确认失败** — `.venv/bin/python -m pytest tests/test_server.py -q`
- [ ] **Step 3: 实现** `tryon/server.py`：模块级 `threading.Lock`，`POST /api/tryon` 用 `lock.acquire(blocking=False)`，抢不到立即 409 `stage="busy"`，`finally` 里释放；`Flask` 设 `MAX_CONTENT_LENGTH = 25 * 1024 * 1024`（两张 10MB 原图加 multipart 余量）；静态页与 `static/` 由同一 app 提供；`/files/<rid>/<name>` 先校验 `rid` 正则、`name` 必须在 `FILE_NAMES` 内，再 `send_from_directory`。`app.py`：`create_app(load_settings()).run(host="0.0.0.0", port=8000, threaded=True, debug=False)`。
- [ ] **Step 4: 跑测试确认通过** — 预期 7 passed。全量回归 `.venv/bin/python -m pytest -q`。
- [ ] **Step 5: 提交** — `git commit -am "后端接口：换装、图库、删除、并发锁"`

---

### Task 6: 页面（static/）

**Files:**
- Create: `static/index.html`、`static/app.js`、`static/style.css`

**Interfaces:**
- Consumes: Task 5 的 JSON 形状
- Produces: 无（终端界面）

- [ ] **Step 1: 写 `static/index.html`** — 结构：两个 `<input type="file" accept="image/*">`（各自旁边一个 `<img>` 预览）+「开始试穿」按钮 +「再来一次」按钮（初始隐藏）+ 进度条容器（`<div class="bar"><i></i></div>` 加一行 `<span class="elapsed">`）+ 结果区 + `usage` 一行小字 + 图库网格 + 大图查看遮罩层（含「删除这条」）。文案中文，不放任何参数控件和文本输入框（规格 5.1/3）。
- [ ] **Step 2: 写 `static/app.js`** — 关键点：
  - 页面模块级保存两个 `File` 对象，"再来一次"复用它们，不要求重新上传。
  - 提交：`FormData` 带 `person`/`garment` 两字段 POST `/api/tryon`；请求期间 `btn.disabled = true`，完成后恢复。
  - 进度：`const ESTIMATE_MS = 55_000`；每 100ms 一个 tick，条宽 `min(90, 100*(1-Math.exp(-3*t)))`（前快后慢、封顶 90%），文字 `生成中 · ${秒数} 秒`（秒数为真实计时）；响应到达后置 100%，1 秒后隐藏。**不设前端超时**（后端 300 秒兜底），不做取消按钮（规格 7）。
  - 渲染 `usage`：把接口回传的 dict 原样拼成 `输出 896×1152 · 1K 档 · 1 张`（有 `output_image_type` 就用，缺字段就跳过该段）。
  - 错误：`ok:false` 时把 `message` 原文显示在结果区，有 `code`/`request_id` 就在下面小字列出（规格 7 失败处理），`stage==="busy"` 显示"上一次还在跑"。
  - 图库：`GET /api/records` 渲染缩略图（用 `thumb`），点开看 `result` 大图，大图层里「删除这条」→ `confirm()` 原生确认 → `DELETE /api/records/<id>` → 重新拉列表。
- [ ] **Step 3: 起服务手测（不联网）** — `.venv/bin/python app.py`，浏览器开 `http://127.0.0.1:8000`：页面能出、两个预览能显示、`.env` 里故意留个坏 key 之前先看清错误区能显示接口原话（此项到 Task 7 真跑时一并验证）。
- [ ] **Step 4: 手机/局域网可达性检查** — 同一局域网设备用本机 IP `:8000` 打开页面，确认能选本地照片并进入"生成中"状态（Task 7 真跑前不需要出图）。
- [ ] **Step 5: 提交** — `git commit -am "试穿页面：上传、估算进度条、结果与图库"`

---

### Task 7: 端到端验收（真实调用一次接口）

**Files:**
- Modify: 无（只跑验证、删临时文件）

- [ ] **Step 1: 真跑一次** — 服务起着，用 `samples/person.jpg` + `samples/garment.jpg` 走页面提交一次（或 `.venv/bin/python -c` 直接调 `generate`）。预期：约 60 秒内返回，结果落进 `data/records/<id>/`。
- [ ] **Step 2: 对照第 6.6 节质量线** — 打开 `result.png`，逐条核对：脸/发型/姿势/灰绿背景是否保留、衬衫水洗蓝与胸袋位置和白色纽扣是否还原、图左侧那半件黑衬衫是否被忽略。任一条不达标 → 只改 `tryon/prompt.py` 的文案再跑，不动其他代码。
- [ ] **Step 3: 验收清单逐条走查**（规格第 8 节）— 重启服务后图库仍在（第 4 条）；「再来一次」不重新上传能出第二张不同的图（第 5 条）；图库删除后页面与磁盘目录都消失（第 8 条）；断网提交 → 结果区显示原始错误、页面不白屏（第 6 条）。
- [ ] **Step 4: 检查外联** — `grep -rn "http" tryon/ static/` 结果里除了 `.env` 推导出的接口地址和接口回传的 OSS 结果地址，不应出现任何别的域名。
- [ ] **Step 5: 清理探针文件** — `git rm -r --cached spike_in 2>/dev/null; rm -rf spike_in spike_iso.py spike_tryon.py iso_*.png`（`samples/` 已入库，不受影响）。
- [ ] **Step 6: 补一份 README** — 怎么装依赖、怎么起、页面能干什么、接口失败时去哪儿看 `request_id`。CLAUDE.md 若有中英双语段落需同步（当前只有中文，无此负担）。
- [ ] **Step 7: 提交** — `git commit -am "端到端验收通过，清掉探针脚本"`
- [ ] **Step 8: 全量回归** — `.venv/bin/python -m pytest -q` 预期全绿；记录实际测试数，写进最终汇报。
