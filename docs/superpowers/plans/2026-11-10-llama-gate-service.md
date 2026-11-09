# 常驻取数服务（llama-gate service）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把第 3 档取数从"每次起进程 + 载 8.9 GB 模型 + 写 45 MB 文件"改成常驻服务，省 ~1.9 秒/次。

**Architecture:** C++ 服务（由 `dump_hidden.cpp` 改成）读配置 → 载模型一次 → 监听 Unix socket → 循环：收请求（图 + prompt）→ 逐 chunk eval → 切行 → 回张量（115 行 / 1.9 MB）。Python 侧（节点、`encode.py`）改成 socket 客户端。systemd 管理。

**Tech Stack:** C++17（llama.cpp）、Python 3（stdlib `socket` / `struct`）、systemd。

**Spec:** `docs/superpowers/specs/2026-11-10-llama-gate-service-design.md`

## Global Constraints

- **无第三方依赖**（不用 protobuf / FlatBuffers；Python 侧也用 stdlib）
- 服务跑在 `denvor` 下（systemd `User=denvor`，环境变量照抄 ComfyUI）
- **出图必须与现在逐位相同**（反目标；输入字节、切行结果、张量数值全都要对得上）
- 协议：`magic="LGAT"`、`version=1`、**小端**
- socket：`/run/llama-gate/gate.sock`；配置：`/etc/llama-gate.env`
- 二进制名 `llama-gate`（原 `dump_hidden`）
- 提交信息用中文，**不加任何 AI 署名 trailer**

## Review Focus

1. **换模型后 `n_batch` 不够** —— `dump_hidden.cpp:8` 记着：`n_batch` 必须 ≥ 单个 chunk 的 token 数，否则 embeddings 缓冲只留最后一次 decode 的内容，**静默出错**。配置化之后，"换模型"这个动作没有任何测试守着数值。
2. **服务中途崩了 / 连接被断** —— 客户端读到半截响应时必须报错，不能拿半截字节去喂 DiT（那是"静默画错"）。
3. **socket 文件残留** —— 服务重启时旧的 `gate.sock` 还在，`bind()` 会失败；服务起不来时节点只会报一句"取数服务不在"。
4. **图数与媒体标记数不匹配** —— `mtmd_tokenize` 返回 1，服务要把它变成人话错误，而不是崩掉。
5. **debug 帧的格式** —— 离线判读（§19/§23/§24 那套）要拿全序列 + meta，格式错了判读就废了，而这条路径生产不走。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `tools/llama_gate/protocol.py` | 帧的编码/解码（纯函数，无 IO） | 新建 |
| `tools/llama_gate/llama_gate.cpp` | 服务主程序（配置 → 载模型 → socket 循环 → 切行） | 新建（由 `dump_hidden.cpp` 改） |
| `tools/llama_gate/gate_client.py` | socket 客户端（`run_gate`） | 改 |
| `tools/llama_gate/keep_rows.py` | 切行逻辑（Python 参照实现） | 不动（C++ 侧照它移植） |
| `tools/llama_gate/encode.py` | 离线 CLI | 改（改连服务） |
| `comfy_nodes/qwen21_precomp_cond.py` | 第 3 档节点 | 改（用新客户端） |
| `tools/llama_gate/build.sh` | 编译 | 改（产物改名） |
| `tools/llama_gate/llama-gate.service` | systemd unit | 新建 |
| `tools/llama_gate/llama-gate.env` | 配置模板 | 新建 |
| `tryon/settings.py` | `llama_ready` 语义 | 改 |

---

## Task 1: 协议帧（Python 侧）

**Files:**
- Create: `tools/llama_gate/protocol.py`
- Test: `tests/test_llama_gate.py`

**Interfaces:**
- Produces:
  - `MAGIC = b"LGAT"`、`VERSION = 1`
  - `encode_request(images: list[bytes], prompt: str, flags: int = 0) -> bytes`
  - `decode_response(blob: bytes) -> tuple[int, int, int, list[int], bytes]`（status, rows, n_embd, slots, tensor_bytes）
  - `FLAG_DEBUG = 1`

- [ ] **Step 1: 写失败测试**

在 `tests/test_llama_gate.py` 末尾加：

```python
from tools.llama_gate.protocol import decode_response, encode_request


def test_请求帧的字段顺序与长度():
    blob = encode_request([b"\x89PNG1", b"\x89PNG2"], "把衣服穿上")
    assert blob[:4] == b"LGAT" and blob[4] == 1 and blob[5] == 0 and blob[6] == 2
    assert int.from_bytes(blob[7:11], "little") == 6          # 图1 长度
    assert blob[11:17] == b"\x89PNG1"
    assert int.from_bytes(blob[17:21], "little") == 6         # 图2 长度
    assert blob[21:27] == b"\x89PNG2"
    assert blob[27:31] == (9).to_bytes(4, "little")           # prompt 字节数
    assert blob[31:] == "把衣服穿上".encode()


def test_响应帧解出张量与槽位():
    tensor = b"\x00" * 4
    blob = (b"LGAT" + bytes([0]) + (2).to_bytes(4, "little") + (3).to_bytes(4, "little")
            + bytes([2]) + (8).to_bytes(4, "little") + (14).to_bytes(4, "little") + tensor * 2)
    assert decode_response(blob) == (0, 2, 3, [8, 14], tensor * 2)


def test_响应帧短了要报错():
    with pytest.raises(ValueError):
        decode_response(b"LGAT\x00\x01")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_llama_gate.py -k "请求帧 or 响应帧" -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'tools.llama_gate.protocol'`）

- [ ] **Step 3: 实现 `tools/llama_gate/protocol.py`**

用 `struct.pack` / `int.to_bytes(..., "little")`。帧布局见 Spec 的"协议"一节，逐字段拼。
`decode_response` 在长度不够时 `raise ValueError("响应帧短了…")`（Review Focus 2）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_llama_gate.py -k "请求帧 or 响应帧" -v`
Expected: PASS，且 `tests/test_llama_gate.py` 全绿

- [ ] **Step 5: 提交**

```bash
git add tools/llama_gate/protocol.py tests/test_llama_gate.py
git commit -m "加取数服务的帧编码与解码"
```

---

## Task 2: 服务程序（C++，含切行）

**Files:**
- Create: `tools/llama_gate/llama_gate.cpp`
- Modify: `tools/llama_gate/build.sh`

**Interfaces:**
- Consumes: Task 1 的帧布局（按同一份 Spec 手工对齐，不 import Python）
- Produces: 可执行文件 `tools/llama_gate/bin/llama-gate`，行为见下

行为：
1. 从环境变量读配置：`LLAMA_GATE_GGUF`、`LLAMA_GATE_MMPROJ`、`LLAMA_GATE_SOCK`（默认 `/run/llama-gate/gate.sock`）、
   `LLAMA_GATE_N_CTX`（默认 8192）、`LLAMA_GATE_N_BATCH`（默认 4096）、`LLAMA_GATE_N_GPU_LAYERS`（默认 -1）、
   `LLAMA_GATE_MEDIA_MARKER`（默认 `<__media__>`）
2. 载模型 + mmproj（**只做一次**），打印 `[计时] 模型载入 … ms`
3. `unlink` 旧 socket → `bind` → `listen`（Review Focus 3）
4. 循环：`accept` → 读请求帧 → 逐 chunk eval → 切行 → 写响应帧 → 关闭连接
5. 每请求新建 context（`n_past` 从 0 起），请求结束 `llama_free(ctx)`

- [ ] **Step 1: 把 `dump_hidden.cpp` 复制成 `llama_gate.cpp` 并改成服务骨架**

保留：参数解析（改成读环境变量）、模型/mmproj 载入、`mtmd_tokenize`、`mtmd_helper_eval_chunk_single` 循环、
`llama_get_embeddings_layer_inp` 收割。
删掉：`--out` / `write_bin` / `meta.json` 落盘（`--no-post` 相关也删，服务不产 post）。
加：socket 监听（`<sys/socket.h>`、`<sys/un.h>`）、请求/响应帧的读写、切行。
**`mtmd_tokenize` 返回非 0 时**（图数与媒体标记数不匹配，`dump_hidden.cpp:11` 记着这个坑）
→ 回 `status=1` + 一句人话（Review Focus 4），别让服务崩掉。

- [ ] **Step 2: 先只做"全序列"响应（不切行），把链路打通**

响应帧的 `rows` = `total_rows`、`slots` = 空、张量 = 全部行。
编译后起服务，用 Task 1 的帧函数连一次。

Run:
```bash
tools/llama_gate/build.sh
LLAMA_GATE_GGUF=/home/denvor/work/models/unsloth/Qwen3-VL-8B-Instruct-1M-UD-Q8_K_XL.gguf \
LLAMA_GATE_MMPROJ=/home/denvor/work/models/unsloth/mmproj-Qwen3-VL-8B-BF16.gguf \
LLAMA_GATE_SOCK=/tmp/gate-test.sock \
  tools/llama_gate/bin/llama-gate &
sleep 30   # 等载模型
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "/home/denvor/work/qwen-image")
from tools.llama_gate.protocol import encode_request, decode_response
import socket
s = socket.socket(socket.AF_UNIX); s.connect("/tmp/gate-test.sock")
imgs = [open("/home/denvor/work/qwen-image/data/llama-gate/img1.png","rb").read(),
        open("/home/denvor/work/qwen-image/data/llama-gate/img2.png","rb").read()]
s.sendall(encode_request(imgs, "把图像2的衣服穿到图像1的人身上"))
buf = b""
while True:
    b = s.recv(1 << 20)
    if not b: break
    buf += b
print(decode_response(buf)[:4])
PY
```
Expected: 服务起来不报错；客户端打印出 `(0, 4066, 4096, [])` 之类的元组（行数 = 全序列）

- [ ] **Step 3: 加切行（照 `keep_rows.py` 逐条移植）**

移植 `kept_row_index`（丢 system turn + 丢视觉 token + 记 slots，锚点从 token id 现算，`IM_START=151644`）。
C++ 侧在 eval 循环里已经拿到每个 chunk 的 `ids` / `n_tokens`，直接算。
**自检**（Review Focus 1）：切完若 `rows == 0` 或 `rows == total_rows`，返回 `status=2` 并附一句人话。

- [ ] **Step 4: 对拍切行结果**

用 `data/llama-gate/img1.png` + `img2.png` 跑一次，把服务返回的行数/slots/张量前几行，
与 Python 侧 `keep_rows.kept_row_index(chunks)` + `cond_from_meta` 的结果比对（逐位）。

Expected: 行数 115、slots `[8, 14]`、张量逐位相同

- [ ] **Step 5: 加 debug 帧（flags bit 0）**

置位时：`rows` = `total_rows`、张量 = 全序列、并在 `slots` 位置改放 `meta.json` 的内容（UTF-8 文本，
长度用 `rows` 之外的字段携带 —— 具体：`status` 后加一个 `meta_len(4)`，实现时按 Spec 的 flags 说明补齐）。

- [ ] **Step 6: 提交**

```bash
git add tools/llama_gate/llama_gate.cpp tools/llama_gate/build.sh
git commit -m "取数程序改成常驻服务：socket + 服务端切行"
```

---

## Task 3: Python 客户端（`gate_client` 改造）

**Files:**
- Modify: `tools/llama_gate/gate_client.py`
- Test: `tests/test_llama_gate.py`

**Interfaces:**
- Consumes: Task 1 的 `encode_request` / `decode_response`；Task 2 的服务
- Produces: `run_gate(images: list[str], prompt: str, sock: str | None = None, flags: int = 0, timeout: int = 60) -> tuple[torch.Tensor, list[int]]`

- [ ] **Step 1: 写失败测试**

```python
def test_run_gate连服务拿回张量与槽位(monkeypatch, tmp_path):
    """连不上服务时要报人话，不是抛 ConnectionRefusedError 那种英文。"""
    from tools.llama_gate import gate_client
    with pytest.raises(RuntimeError) as e:
        gate_client.run_gate(["a.png"], "x", sock=str(tmp_path / "nope.sock"))
    assert "取数服务不在" in str(e.value)
    assert "systemctl status llama-gate" in str(e.value)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_llama_gate.py -k run_gate连服务 -v`
Expected: FAIL

- [ ] **Step 3: 实现 `run_gate`**

连 `sock`（默认环境变量 `LLAMA_GATE_SOCK`，再默认 `/run/llama-gate/gate.sock`）→ 发请求帧 → 读响应帧 →
`status != 0` 时 `raise RuntimeError(f"取数服务报错（status={status}）：…")`。
`torch` / `numpy` 的 import 留在函数内（沿用现在的做法，节点侧才 import）。
**删掉 `cond_from_meta`**（它的对账职责已搬进服务端；`gate_layout` 保留给离线判读用，见 Task 4）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_llama_gate.py -v`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add tools/llama_gate/gate_client.py tests/test_llama_gate.py
git commit -m "取数客户端改成连 socket"
```

---

## Task 4: 节点与 `encode.py` 改造

**Files:**
- Modify: `comfy_nodes/qwen21_precomp_cond.py`
- Modify: `tools/llama_gate/encode.py`

**Interfaces:**
- Consumes: Task 3 的 `run_gate`（返回 `(hidden, slots)`）

- [ ] **Step 1: 改 `_cond_via_llama`**

```python
hidden, slots = run_gate(paths, media_prompt(T2I_TEMPLATE, prompt, len(paths)),
                         sock=os.path.join(work_dir, "gate.sock"))
```
（`sock` 的具体取值按 Spec：默认 `/run/llama-gate/gate.sock`，`work_dir` 不再需要 gate 子目录）
节点头注释里"取数程序得自己编译"那段改成"取数服务得在跑（`systemctl status llama-gate`）"。

- [ ] **Step 2: 改 `encode.py`**

`run_gate` 的返回值从 meta 变成 `(hidden, slots)`，`torch.save` 的内容照旧（`hidden` / `image_slots` / `provenance`）。
`provenance` 里"行算术"改成指向 `tools/llama_gate/llama_gate.cpp`。
`--gate-bin` 参数删掉，换成 `--sock`。

- [ ] **Step 3: 真机验证**

Run: `cd /home/denvor/work/ComfyUI && .venv/bin/python /home/denvor/work/qwen-image/tools/llama_gate/encode.py --img … --prompt … --out /tmp/cond.pt`
Expected: 输出形状 `[1, 115, 4096]`、slots `[8, 14]`

- [ ] **Step 4: 提交**

```bash
git add comfy_nodes/qwen21_precomp_cond.py tools/llama_gate/encode.py
git commit -m "第 3 档节点与离线 CLI 改用 socket 客户端"
```

---

## Task 5: systemd unit、配置模板、编译脚本

**Files:**
- Create: `tools/llama_gate/llama-gate.service`
- Create: `tools/llama_gate/llama-gate.env`
- Modify: `tools/llama_gate/build.sh`

- [ ] **Step 1: 写 unit 与配置模板**

`llama-gate.service`：
```ini
[Unit]
Description=llama.cpp 取数服务（ComfyUI 第 3 档）
After=network.target

[Service]
Type=simple
User=denvor
EnvironmentFile=/etc/llama-gate.env
ExecStart=/home/denvor/work/qwen-image/tools/llama_gate/bin/llama-gate
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```
`llama-gate.env`（模板，值与 `gate_client.py` 现在硬编码的一致）：
```bash
LLAMA_GATE_GGUF=/home/denvor/work/models/unsloth/Qwen3-VL-8B-Instruct-1M-UD-Q8_K_XL.gguf
LLAMA_GATE_MMPROJ=/home/denvor/work/models/unsloth/mmproj-Qwen3-VL-8B-BF16.gguf
LLAMA_GATE_SOCK=/run/llama-gate/gate.sock
LLAMA_GATE_N_CTX=8192
LLAMA_GATE_N_BATCH=4096
LLAMA_GATE_N_GPU_LAYERS=-1
LLAMA_GATE_MEDIA_MARKER=<__media__>
```

- [ ] **Step 2: 改 `build.sh`**

产物从 `bin/dump_hidden` 改成 `bin/llama-gate`，源文件从 `dump_hidden.cpp` 改成 `llama_gate.cpp`。
补丁与编库那两步不动。

- [ ] **Step 3: 真机装一次**

```bash
sudo cp tools/llama_gate/llama-gate.service /etc/systemd/system/
sudo cp tools/llama_gate/llama-gate.env /etc/llama-gate.env
sudo mkdir -p /run/llama-gate && sudo chown denvor /run/llama-gate
sudo systemctl daemon-reload && sudo systemctl enable --now llama-gate
systemctl status llama-gate
```
Expected: `active (running)`，日志里有 `[计时] 模型载入 … ms`

- [ ] **Step 4: 提交**

```bash
git add tools/llama_gate/llama-gate.service tools/llama_gate/llama-gate.env tools/llama_gate/build.sh
git commit -m "加 systemd unit 与配置模板，编译产物改名 llama-gate"
```

---

## Task 6: `llama_ready` 改成"服务在不在"，文档跟着改

**Files:**
- Modify: `tryon/settings.py`
- Modify: `tests/test_server.py`
- Modify: `README.md`
- Modify: `.env.remote.sample`

- [ ] **Step 1: 写失败测试**

在 `tests/test_server.py` 的 `llama_settings` helper 旁边加：

```python
def test_服务不在时llama_ready是False(make, tmp_path):
    client, _ = make(saved_prefs=None, settings=replace(Settings(endpoint="https://example.test/generation", api_key="k", model="m"),
                          llama_repo_root=str(tmp_path), llama_work_dir=str(tmp_path / "gate-work")))
    assert client.get("/api/settings").get_json()["llama_ready"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_server.py -k llama_ready -v`
Expected: FAIL

- [ ] **Step 3: 改 `llama_ready`**

`tryon/settings.py` 的 `llama_ready` 改成：连一下 socket（`socket.socket(AF_UNIX)` + `connect`，超时 0.5 秒），
连得上即 True。`llama_remote=True` 时仍信配置（异机时应用连不到 ComfyUI 那台的 socket）。
删掉 `os.path.isfile(...dump_hidden)` 那套。

- [ ] **Step 4: 改 `GATE_REL` 与相关断言**

`tests/test_server.py:65` 的 `GATE_REL` 删掉（不再看文件），相关 helper（`:76-80`）改成不需要造文件。
`tests/test_llama_gate.py` 里 `test_二进制默认落在repo的bin里` / `test_环境变量能指定另一个二进制` /
`test_二进制不在时报错要说清怎么编出来` 三条删掉（它们测的是被移除的行为）。

- [ ] **Step 5: 跑全量测试**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全绿

- [ ] **Step 6: 改文档**

`README.md`：第 3 档那段从"跑 `tools/llama_gate/build.sh` 编二进制"改成"装并起 `llama-gate` 服务"
（含 `sudo systemctl enable --now llama-gate`）。
`.env.remote.sample`：`TRYON_LLAMA_REMOTE` 那段的说明跟着改。

- [ ] **Step 7: 提交**

```bash
git add tryon/settings.py tests/ README.md .env.remote.sample
git commit -m "llama_ready 改成探测服务，文档跟着改"
```

---

## Task 7: 逐位回归与性能验收

**Files:** 无（只验证）

- [ ] **Step 1: 先存基准**（改代码之前若还没做，此时补）

用**现在这套 CLI**（`dump_hidden` 还在 `bin/` 里）跑一对固定输入，存下 `pre.bin` + `meta.json` + 切好的 `.pt`。
输入固定用 `data/llama-gate/img1.png` + `img2.png`，记录它们的 sha256。

- [ ] **Step 2: 逐位回归**

服务跑起来后，用 `encode.py` 跑同一对输入，比对 `.pt` 里的 `hidden` 与第 1 步的基准。
Expected: **逐位相同**（最大绝对差 0）

- [ ] **Step 3: 端到端出图回归**

ComfyUI 里跑第 3 档、同 seed、同一对图，与改造前的出图比对（§0 的口径）。
Expected: 逐位相同

- [ ] **Step 4: 性能**

连跑 5 发 720p 第 3 档（每发换不同的衣服图，避开编码缓存），取均值。
Expected: 从 **25.15 秒**降到 **~23.2 秒**（省 ~1.9 秒）

- [ ] **Step 5: 换模型验证**

改 `/etc/llama-gate.env` 的 `LLAMA_GATE_GGUF` → `sudo systemctl restart llama-gate` → 再跑一次第 2 步。
Expected: 能切，数值与用同一颗模型直接跑 CLI 的结果一致

- [ ] **Step 6: 记进实测文档**

在 `docs/comfyui-measurements.md` 加一节（§27），记：改造前后的数、逐位回归的结论、
`llama_ready` 语义变化、以及"§25.5 的不划算只在低频率下成立"这条更正。

- [ ] **Step 7: 提交**

```bash
git add docs/comfyui-measurements.md
git commit -m "§27：取数服务化后的实测与逐位回归"
```
