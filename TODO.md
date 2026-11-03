# TODO

这里只放**已经决定暂缓、但下次要接着做**的事。做完的、以及测出来的数，一律进
`docs/comfyui-measurements.md`，不放这个文件。

## 1. llama.cpp 编码这条路：接下来的任务

一句话现状（§23）：**T1 已经打完。** llama.cpp 那份 0.982 的条件注入生产图，画出来是
**同构图、细节级**的另一张：构图量出来没动（掩膜质心与包围盒两腿一致），`>16` 的像素只占 0.385%、
PSNR 38.19，看得见的是袖口多了一颗纽扣。在线收益实测闭环 ≈ **59 秒，−19%**。
剩下的不是技术未知数，是要不要养那套东西的生意判断。

- [x] ~~**T1 生成一发，判 0.982 到底换不换图**~~ —— 做完，见 §23（节点 `comfy_nodes/qwen21_precomp_cond.py` 已在仓库里，
  两腿产物与脚本在 `/tmp/llama-inj/`，重启就没，要留就得归档 → T5）。
  两条方法上的收获已经补进 §0 第六条与 §23：**判逐位必须同一句话跨进程**；**只报"百分之几十像素不同"会把人带跑，
  要连分级（>4/>16/>32）、平均通道差与 PSNR 一起报** —— 我在 §22 上先栽过一次，§23 就是照这个改的判法。
  - 成本：几十行 + 一次重启，一次生成 ≈ 75 秒。
- [ ] **T2 ROCm ↔ Vulkan 对拍**（denvor 2026-11-03 说先用 Vulkan 跑，之后再测这两个后端）
  - 对拍两件事：同一份 gguf + 同一条序列的**墙钟**，和 `pre.bin` 的**逐位/余弦**差。
  - 已知的半边：§18 用 Vulkan b12063（`~/work/llama/llama-vulkan`），§19 用源码建的 ROCm b12073
    （`~/work/llama/llama.cpp/build/bin`），两次的分段边界完全一致，所以数值差可直接算，只缺一次并排跑。
- [ ] **T3 那份 +15 行本地补丁的去留**
  - `~/work/llama/llama.cpp/src/models/qwen3vl.cpp` 现在是**未提交**状态，`build/` 是我用他的 `build.sh`
    重编出来的。要么留（在那文件顶部补一行"本地补丁"说明，写明只有 `embeddings_layer_inp[n_layer]`
    被请求时才改图），要么 `git checkout` + 重编回到原样。这个决定在他那边，不在本项目里。
- [ ] **T4 同权重的 gguf，把"差在实现还是差在权重"真正分开**
  - 现在的残差**归因是推断不是测量**：llama 侧是 unsloth 的 `Qwen3-VL-8B-Instruct-1M-UD-Q8_K_XL`，
    ComfyUI 侧是 `qwen3vl_8b_int8_convrot.safetensors`，两边量化方案不同。
  - 做法：从 ComfyUI 那份 safetensors 直接 convert 一份 f16 gguf（+ 对应 mmproj），重跑 §19 的判读。
  - 收益：若余弦从 0.9825 跳到 ~0.999，就证明 llama.cpp 的**实现**是对的，残差全在权重；
    反之说明引擎本身还有算子级差别，那 T1 的"换图"风险要重新估。
- [ ] **T5 仪器归档**（T1 有结论了，这一条可以定了）
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

现状：应用里只有 1 和 2（`tryon/comfy_api.py` 的 `ENCODER_CLASS`、`tryon/prefs.py` 的 `VALID_SOURCES`）。

**第 1 步（只做注入、不做界面）已经做完了，就是 §23 那次**：`comfy_nodes/qwen21_precomp_cond.py`
（`TextEncodeQwenImage21Precomputed`）在仓库里，也拷进了 `~/work/ComfyUI/custom_nodes/`
⇒ **使用者那台 8188 下次重启就会加载它**（不装载图里没人引用它，行为不变）。它现在是纯 spike：
条件从**盘上一个 .pt** 读、路径写死在图里、行数对不上不报错、没有把 llama.cpp 接成在线服务。

**要做成真的第 3 档，还差这些**（顺序即建议顺序）：

1. 把"离线 .pt"换成"在线取数"：节点里调 llama.cpp（补丁版 b12073 + `dump_hidden` 那套）现算 115 行，
   输入必须是**节点自己 resize 完的那两份像素**（否则 §19 那个逐位对齐就废了）；
   服务算谁的进程（pm2 加一条？）、它挂了回退第 2 档还是直接报错、约 12 GB 常驻与 ComfyUI 并存的实测墙钟。
2. 界面与派发铺开：`VALID_SOURCES` 加一个值、
   `clamp_size`/`fold_note` 第 3 档给不给 480p/720p/2K、`Prefs` 要不要多一个 llama 服务地址、
   `server.py` 的派发与健康探测（第 3 档要**两个**后端都在线）、`joblock` 的 `source` 字符串、
   额度显示（第 3 档也是 0 credit）、`static/app.js` 的徽标与估算基数表（离线那一发实测 53.8 秒，见 §23）。
3. 一个还没算的账：第 3 档里 ComfyUI **不再加载那颗 8B**（省 19.5 秒与约 8.9 GB），
   但 llama.cpp 那侧常驻要占约 12 GB（11 GB gguf + 1.1 GB mmproj）—— 净账看着是正的，
   但要实测一次同机并存的墙钟，别拿两笔分别量的数相加。

## 3. 顺手记着的旧尾巴

- [ ] §7 那格"没解释的"里仍未结的项（`docs/comfyui-measurements.md` §7）。
- [ ] 部署到 `/www/webroot/mfr`：由 denvor 自己执行。前置三件事 —— 目标机装 fork 节点并重启、
      AMD 机要 `COMFYUI_ENABLE_MIOPEN=1`、上线前四档各打一发预热。
      **§20/§21/§22 给这几条加了新内容**：
      ① 预热不是可省的讲究，它是在**替用户付 MIOpen 的 find-tuning** —— 端到端实测冷 db 那一发 192.4 秒 vs 平时 73.2 秒，
      贵 124 秒；而且**预热不改变任何一张图的像素**（空 db 从头调优与攒了三天的真 db 逐位相同，§22），所以随便挑时候做。
      ② `export MIOPEN_ENABLE_CACHE=1` 那行**删掉** —— MIOpen 没这个名字（只有 `MIOPEN_DISABLE_CACHE`），写了不报错也没效果。
      ③ `MIOPEN_FIND_MODE=FAST` **别默认开**：它能把那 124 秒免掉，代价是丢掉逐位复现能力
      （端到端 0.70% 像素明显变、PSNR 36.36，人眼看是"同一次试穿的另一个细节版本"，差别在袖口卷起程度 —— 看图判，§22.1）。
      要不要这个代价由 denvor 判，判之前先看 `/tmp/mio/compare/` 那七份。
      ④ 顺手确认部署用户能写 `~/.config/miopen/`：写不了的话不是"少攒缓存"，是**每一发都重新调优**，比冷 db 更糟。
      ⑤ 想省冷账又不想动数值口径，走 find-db 随发布带走（90 KB 文本 + `MIOPEN_USER_DB_PATH`），见 §20.4。
