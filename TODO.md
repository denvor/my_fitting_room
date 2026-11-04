# TODO

这里只放**已经决定暂缓、但下次要接着做**的事。做完的、以及测出来的数，一律进
`docs/comfyui-measurements.md`，不放这个文件。

## 1. llama.cpp 编码这条路：接下来的任务

一句话现状（§23 / §24 / §25）：**三档都在界面上了，在线那一档也砍干净了。** llama.cpp 那份 0.982 的条件注进生产图，
画出来是**同构图、细节级**的另一张：干净那对（同输入字节，§24）`>16` 占 0.330%、PSNR 38.40，
看得见的是抬起那只手的袖口门襟多了一颗纽扣。denvor 已看过图并判"可以接受"（2026-11-03）。
§25 把桥里那 2.4 秒的 python 中间层砍掉之后，同实例八腿量到**三档全都更快**：480p 11.8（对 §17 的 11.2 基本打平）、
720p 25.4（−3.7）、1K 64.5（−8.7）。默认来源**没动**，仍是 `comfyui`。

- [x] ~~**T1 生成一发，判 0.982 到底换不换图**~~ —— 做完，见 §23 与 §24。
  **该引用的是 §24 那一对**（`A-fork` vs `C-live`，同一对输入字节）；§23 那对混进了 `prepare_upload` 的
  ±1 级 JPEG 重编码，结论没变但方法上是脏的。图已入库 `docs/compare/llama-cond/`（`index-live.html`）。
  两条方法上的收获已经补进 §0 第六条与 §23：**判逐位必须同一句话跨进程**；**只报"百分之几十像素不同"会把人带跑，
  要连分级（>4/>16/>32）、平均通道差与 PSNR 一起报** —— 我在 §22 上先栽过一次，§23 就是照这个改的判法。
  - 成本：几十行 + 一次重启，一次生成 ≈ 75 秒。
- [ ] **T6 第 3 档的低两档没做过图差对照**（§25 只量了时间）：480p / 720p 的"细节级"是照 1K 那对推的，
  要给人判"这档能不能上生产"，得在同输入字节下再各打一对（每对约 40 秒 GPU）。
- [ ] **T2 ROCm ↔ Vulkan 对拍**（denvor 2026-11-03 说先用 Vulkan 跑，之后再测这两个后端）
  - 对拍两件事：同一份 gguf + 同一条序列的**墙钟**，和 `pre.bin` 的**逐位/余弦**差。
  - 已知的半边：§18 用 Vulkan b12063（`~/work/llama/llama-vulkan`），§19 用源码建的 ROCm b12073
    （`~/work/llama/llama.cpp/build/bin`），两次的分段边界完全一致，所以数值差可直接算，只缺一次并排跑。
- [ ] **T3 那份 +15 行本地补丁的去留**
  - 补丁内容已归档：`tools/llama_gate/qwen3vl-prenorm.patch`（从 `git diff` 生成，在原始版本上 apply 后
    与本机逐字相同）；编译收成 `tools/llama_gate/build.sh`（打补丁 + 编库 + 编二进制，换机器一条命令）。
  - 但 `~/work/llama/llama.cpp/src/models/qwen3vl.cpp` 仍是**未提交**状态，`build/` 是拿它编的。
    要么留（补丁自己写明了"只有 `embeddings_layer_inp[n_layer]` 被请求时才改图"），要么 `git checkout` +
    重编回到原样。这个决定在他那边，不在本项目里。
- [ ] **T4 同权重的 gguf，把"差在实现还是差在权重"真正分开**
  - 现在的残差**归因是推断不是测量**：llama 侧是 unsloth 的 `Qwen3-VL-8B-Instruct-1M-UD-Q8_K_XL`，
    ComfyUI 侧是 `qwen3vl_8b_int8_convrot.safetensors`，两边量化方案不同。
  - 做法：从 ComfyUI 那份 safetensors 直接 convert 一份 f16 gguf（+ 对应 mmproj），重跑 §19 的判读。
  - 收益：若余弦从 0.9825 跳到 ~0.999，就证明 llama.cpp 的**实现**是对的，残差全在权重；
    反之说明引擎本身还有算子级差别，那 T1 的"换图"风险要重新估。
- [x] ~~**T5 仪器归档**~~ —— 做完：`tools/llama_gate/`（`dump_hidden.cpp` / `keep_rows.py` / `gate_client.py` /
  `prompt_build.py` / `encode.py`），编译产物落 `tools/llama_gate/bin/`（gitignore）。
  §19/§23/§24 那几份 `/tmp` 里的判读脚本**没**归档，方法与数都在实测记录里；`/tmp/q3vl-gate`（340 MB）
  与 `/tmp/mio`（32 MB）那两处产物重启就没，要留的是"怎么再生成"（§25.2 那套），不是那 45 MB 张量。
  - 现在散在两个 `/tmp` 目录里，重启就没：`/tmp/q3vl-gate/`（`dump_hidden.cpp` 含三条通路坑、
    `cond_comfy.py` 那个钩子、`compare_raw.py` / `compare_kept.py`、2865 行参照张量）与
    `/tmp/llama-inj/`（`make_pt.py` 的行号算术、`run_t1.py` 两腿、四张对照图）。
  - **要留的是方法与坑，不是那 45 MB 张量**；进 repo 的话建议 `tools/llama_gate/`，
    参照张量一律写"怎么再生成"而不是直接签进来。denvor 点头我再动。

## 2. 第三个生成来源（denvor 2026-11-03 定的形态）

界面上要三档，**三者都在**：

1. 官方 API（`qwen-image-3.0-pro`，`.env` 里那套）
2. 现在的 fork（本机 ComfyUI + `TextEncodeQwenImage21PosOnly`，就是生产在用的这条）
3. fork + llama.cpp 的改动（llama.cpp 出 pre-norm 条件，DiT/VAE 仍走 fork）

现状：**三档都在**（§25.6）—— `prefs.VALID_SOURCES` 三个值、`comfy_api.build_graph(..., encoder=)` 两张图、`Settings.llama_ready` 决定第三档给不给点。

**注入 + 在线取数 + 派发都已完成**（§23 / §24 / §25）：`comfy_nodes/qwen21_precomp_cond.py` 两种模式都在 ——
`cond_path` 读盘上那份（§23），`cond_source` 在 ComfyUI 进程里直调 `tools/llama_gate/bin/dump_hidden`（§25，
已验与离线那份 `cond.pt` 最大绝对差 0.0、与 §24 的 C-live 像素逐位相同）。
**8188 已经重启并验过**（§25.7，2026-11-03 21:40）：两颗节点都在 `/object_info` 里，
用应用的派发链在生产那台上打了一发 —— 与临时实例那发**像素逐位相同**（66.3 秒，重启后第一发的价）。

**要做成真的第 3 档，还差这些**（顺序即建议顺序）：

1. ~~把"每次起进程"换成常驻服务~~ —— **不用做了，收益已经用别的办法拿到**（§25.1/§25.5）：
   那 4.9 秒里最大的一笔是"节点在 ComfyUI 进程里却又起了一个 python 子进程"（2.4 秒，与 GPU 无关），
   砍掉它就行；`post.bin` 也关了（二进制加 `--no-post`）。剩下能收的只有载模型 1.23 + 建 context 0.21
   = **1.44 秒**（64.5 秒里占 2%），要它得多一个要人管的常驻进程 —— 现在这条不划算，别做。
   - 附带结论：**别拿"12 GB 常驻 + ComfyUI 并存"那笔账去换 1.44 秒**，那条 TODO 作废。
2. ~~界面与派发铺开~~ —— 做完（§25.6）：`VALID_SOURCES` 三个值 + `is_local()` 一处判定、
   `build_graph(..., encoder="llama")`（只删 `CLIPLoader`、只换编码那颗节点）、
   `Settings.llama_ready` 查二进制在场（不在则设置页按住那一档并说清去编译）、额度/超时/回退按本机那条走、
   `app.js` 徽标与估算表三格。**还没定的两件事**：① 要不要把第 3 档设成某些人的默认（现在没人默认它是）；
   ② 第 3 档出图的 `docs/compare/` 那对之外，低两档没做过对照 —— 480p/720p 的图差量级是照 1K 推的，没量。
3. 一个还没算的账：第 3 档里 ComfyUI **不再加载那颗 8B**（省 19.5 秒与约 8.9 GB），
   但 llama.cpp 那侧常驻要占约 12 GB（11 GB gguf + 1.1 GB mmproj）—— 净账看着是正的，
   但要实测一次同机并存的墙钟，别拿两笔分别量的数相加。

## 3. 顺手记着的旧尾巴

- [ ] §7 那格"没解释的"里仍未结的项（`docs/comfyui-measurements.md` §7）。
- [ ] 部署到 `/www/webroot/mfr`：由 denvor 自己执行。前置三件事 —— 目标机装 fork 节点并重启、
      AMD 机要 `COMFYUI_ENABLE_MIOPEN=1`、上线前四档各打一发预热。
      **§25 之后这里多一条分岔**：目标机**不想开第 3 档**就什么都不用多做 —— 缺 `tools/llama_gate/bin/dump_hidden`
      时 `Settings.llama_ready` 是 False，设置页自己把那一档按住并把缺什么写在副标里（不会让人点得到一个看不出来根的错）。
      想开的话要凑齐：两颗 custom_nodes + 重启、打了补丁的 `qwen3vl.cpp` 重编出的二进制、unsloth 那对 gguf（11 GB + 1.1 GB）、
      以及 ComfyUI 与这台应用**同机**（第三档的路径是明文落在图里的）。
      **§20/§21/§22 给这几条加了新内容**：
      ① 预热不是可省的讲究，它是在**替用户付 MIOpen 的 find-tuning** —— 端到端实测冷 db 那一发 192.4 秒 vs 平时 73.2 秒，
      贵 124 秒；而且**预热不改变任何一张图的像素**（空 db 从头调优与攒了三天的真 db 逐位相同，§22），所以随便挑时候做。
      ② `export MIOPEN_ENABLE_CACHE=1` 那行**删掉** —— MIOpen 没这个名字（只有 `MIOPEN_DISABLE_CACHE`），写了不报错也没效果。
      ③ `MIOPEN_FIND_MODE=FAST` **别默认开**：它能把那 124 秒免掉，代价是丢掉逐位复现能力
      （端到端 0.70% 像素明显变、PSNR 36.36，人眼看是"同一次试穿的另一个细节版本"，差别在袖口卷起程度 —— 看图判，§22.1）。
      要不要这个代价由 denvor 判，判之前先看 `/tmp/mio/compare/` 那七份。
      ④ 顺手确认部署用户能写 `~/.config/miopen/`：写不了的话不是"少攒缓存"，是**每一发都重新调优**，比冷 db 更糟。
      ⑤ 想省冷账又不想动数值口径，走 find-db 随发布带走（90 KB 文本 + `MIOPEN_USER_DB_PATH`），见 §20.4。
