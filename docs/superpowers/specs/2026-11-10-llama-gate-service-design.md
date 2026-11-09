# 常驻取数服务（llama-gate service）设计

## 意图

第 3 档（llama.cpp 出编码）每次取数都要：起一个进程 → 映射 8.9 GB 模型 → 建 context → 算 → 写盘 → 退出。
其中**载模型 1.23 秒 + 建 context 0.21 秒**是每发都付的固定开销（§25.1 分段计时）。

§25.5 判过"不划算"（1.44 秒占 1K 的 2%），但那是**单次视角**。实际用量是**几百次/天**
（用户在网页上点 + 后台批量跑，ComfyUI 自己排队）：

- 300 次/天 × 1.9 秒 ≈ 每天 10 分钟
- 1000 次/天 ≈ 每天 32 分钟

所以这一刀在高频下从"不划算"翻转成"划算"。**§25.5 的判断要加个前提：只在低频率下成立。**

另外，现在走文件（`pre.bin` 45 MB，实际只要 115 行 1.9 MB），改 socket 内存直传还能省 §25.1 那 0.45 秒落盘。

## 目标

1. 取数程序改成**常驻服务**，由 systemd 管理（开机自启、崩溃重拉、日志进 journal）
2. **一条通路**：所有客户端（ComfyUI 节点、`encode.py`、离线判读）都走 Unix domain socket，不再有文件接口
3. **自建二进制协议**，零第三方依赖（项目现在刻意无依赖：`comfy_api.py` 只走 stdlib urllib）
4. **服务端切行**：只回 115 行 / 1.9 MB，不回 45 MB
5. **换模型** = 改配置 + `systemctl reload`（不做运行中动态切换）

## 非目标

- 不做运行中动态切换模型（要同时占两份显存，复杂）
- 不做并发（ComfyUI 自己排队，服务串行即可）
- 不改第 2 档（posonly）的路径
- 不改权重本身

## 反目标（什么算失败）

- 出图与现在**不逐位相同**（质量回退）
- 服务崩了不能自愈，要人手动管
- 部署变复杂

## 约束

- 语言：C++（沿用 `tools/llama_gate/dump_hidden.cpp`）
- 平台：Linux + systemd
- **无第三方依赖**（不用 protobuf / FlatBuffers）
- 必须兼容现有调用方：ComfyUI 节点 `qwen21_precomp_cond.py`、`encode.py`；
  `cond_from_meta` 依赖盘上的 `pre.bin` + `meta.json`，服务化后由新的 socket 客户端取代

## 已定的设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 进程管理 | systemd 服务 | 开机自启、崩溃重拉、日志、reload |
| 通信 | Unix domain socket | 本机、低延迟、无端口 |
| 协议 | 自建二进制（header + 裸字节） | 零依赖，两端各 ~30 行 |
| 切行 | **服务端**（C++ 重写 `keep_rows`） | 只传 1.9 MB 而不是 45 MB |
| **对账** | **搬到服务端** | 见下节"对账的归属" |
| 换模型 | 改配置 + `systemctl reload` | 简单，不做动态切换 |
| 模型参数 | **进配置**（`n_ctx` / `n_batch` / `n_gpu_layers` / `media_marker`） | 换模型时上下文长度、视觉 token 上限都不同，写死会静默出错 |
| socket 路径 | `/run/llama-gate/gate.sock` | |
| 配置 | `/etc/llama-gate.env`（GGUF / MMPROJ 路径 + 模型参数） | |
| 服务连不上 | 节点报人话："取数服务不在（…），先看 `systemctl status llama-gate`" | |

## 对账的归属

现在 `gate_layout()` 在客户端做两道对账 + 一条"一行都没丢掉就报错"，防的是"拿到的是条件张量而不是崩溃"
（坏了不响、只是静默画错，§18 那个作废的 0.687 就是这么来的）。服务化之后：

- **"二进制中途死了"** → 服务自己会报错（status != 0），不用客户端猜
- **"指错目录 / 被并发覆盖"** → 不存在了，服务自己管 work_dir
- **"行数 / 字节数不对"** → **服务端接管**（它本来就有全部信息）

所以客户端只剩一条轻校验：响应里的 `行数` 与 `slots` 合理（> 0、`n_embd` 与预期一致）。
协议里已带这些字段，不用额外加东西。

## 协议

```
请求:  magic(4)="LGAT" | version(1)=1 | flags(1) | 图数(1) | [图长度(4) | 图字节]… | prompt长度(4) | prompt(UTF-8)
响应:  magic(4)="LGAT" | status(1) | 行数(4) | n_embd(4) | slots数(1) | [slot(4)]… | 张量字节(float32, 小端)
```

- **图**：JPEG/PNG 原始字节（服务端自己解码，与现在 `dump_hidden --img` 一致）
- **响应张量**：切行后的 `[1, 行数, n_embd]`，行数通常 115
- **status**：0 = 成功；非 0 = 失败（失败时的消息格式由 builder 定，能让人看懂即可）
- **flags**：bit 0 = debug（回**全序列** + `meta.json` 的内容，供 §19/§23/§24 那套离线判读用；默认关闭）

## 组件

| 件 | 改动 |
|---|---|
| `tools/llama_gate/dump_hidden.cpp` | 从 CLI 改成服务主程序（读配置 → 载模型 → 监听 socket → 循环） |
| `tools/llama_gate/gate_client.py` | 加 socket 客户端，`run_gate` 改成连服务 |
| `tools/llama_gate/encode.py` | 改成连服务（不再是"起子进程"） |
| `comfy_nodes/qwen21_precomp_cond.py` | 用新客户端，不再读 `pre.bin` |
| `tools/llama_gate/build.sh` | 编服务程序（原来编 CLI） |
| `tools/llama_gate/llama-gate.service` | systemd unit（新文件，进 repo，安装时拷到 `/etc/systemd/system/`） |
| `tools/llama_gate/llama-gate.env` | 配置模板 |

## 留给 builder 的

- `keep_rows` 的 C++ 实现细节（锚点从 token id 现算，`IM_START=151644`；现有 `keep_rows.py` 是参照）
- 错误帧的具体格式
- socket 的清理（重启时删旧 socket 文件）
- systemd unit 里 `reload` 用 SIGHUP 还是直接 `restart`
- 测试的写法

## 验证

0. **先存基准**（在改任何代码之前）：用现有 CLI 版 `dump_hidden` 跑一对固定输入，把 `pre.bin` + `meta.json`
   和切好的张量**存下来**，作为回归的对照物。用哪一对输入要固定（建议 `data/llama-gate/` 那对，
   并记录它的字节；§24.2/§24.3 明确过"输入字节不同 → 成图有 0.098% 的 >16 像素差"，
   基准和回归必须用同一对）。
1. **逐位回归**：新服务跑**同一对输入**，切出来的张量与第 0 步存的基准**逐位相同**（§24.1 那套对账）
2. **端到端**：ComfyUI 出图与现在**逐位相同**（同 seed，§0 的口径）
3. **性能**：720p 第 3 档从 25.15 秒降到 ~23.2 秒（省 1.9 秒）
4. **换模型**：改配置 + reload 能切

## 部署决策（已定）

1. **服务跑在 `denvor` 下**：systemd 系统单元 + `User=denvor`，环境变量照抄 ComfyUI 的
   （含 `COMFYUI_ENABLE_MIOPEN=1`）。理由：现在 `dump_hidden` 是 ComfyUI（pm2、`denvor`）
   `subprocess.run` 起的，继承的就是这套环境与身份；换 root + 干净环境会让 ROCm/HIP 选型与缓存目录变化，
   **数值是否仍逐位相同没量过**。装 unit 要 sudo。
2. **服务不可用时就报错，给一句人话**（不自动回退）：节点报"取数服务不在（…），先看
   `systemctl status llama-gate`"。`llama_ready` 的语义从"二进制文件在不在"改成"**服务在不在**"
   （连一下 socket），设置页 / README / `.env.remote.sample` / 测试跟着改。
3. **二进制改名 `llama-gate`**：`dump_hidden` 是 CLI 的名字，服务化后语义不符。
   改名要同步 `build.sh`、`gate_client.py`、`llama_ready`、测试。
