# 试衣间

传一张本人照片和一张衣服照片，调 `qwen-image-3.0-pro` 生成"你穿着这件衣服"的图。本机网页，自己用。

设计和实测记录见 `docs/superpowers/specs/2026-10-26-qwen-tryon-design.md`。

## 装与起

```bash
uv pip install --python .venv/bin/python -r requirements.txt   # 或任意 pip install -r requirements.txt
.venv/bin/python app.py
```

- 本机打开 `http://127.0.0.1:8000`，局域网设备用本机 IP 加 `:8000`（服务绑 `0.0.0.0`，无鉴权，只在你自己网络里用）。
- 接口地址、密钥、模型名从 `.env` 读：`OPENAI_URL`、`API_KEY`、`MODEL`。图像生成地址由 `OPENAI_URL` 去掉结尾的 `/compatible-mode/v1` 再接 `/api/v1/services/aigc/multimodal-generation/generation` 推导出来。
- `.env` 和 `data/` 都不进版本库。

## 用法

1. 左边选你的照片（正面、单人、光线均匀），右边选衣服照片（只放这一件）。
2. 点「开始试穿」。一张图约 40–80 秒（实测 1K 档 41–78 秒，接口是同步的一发请求，中途没有真进度，所以进度条按 55 秒估、旁边的秒数是真实计时）。
3. 不满意点「再来一次」，不用重新上传，换随机种子再出一张。同一时间只允许一个任务在跑。
4. 结果下面那行小字是接口回传的消耗数字（输出尺寸、档位、张数）。
5. 图库里点缩略图看大图，可以下载或删除整条记录。

输出固定 1K 档（总像素不超过 2,250,000）、一次 1 张、无水印；提示词写死在 `tryon/prompt.py`，
改动它等于改质量验收线（设计文档 6.6 节是基线）。

## 图存在哪

`data/records/<时间戳-随机码>/` 一条记录一个目录：

```
person.jpg   garment.jpg   result.png   thumb.jpg   meta.json
```

`meta.json` 里有时间、耗时、提示词、输出尺寸、接口回传的 `usage` 和 `request_id`。
接口给的结果图链接 24 小时就失效，所以每次生成完立即下载落盘，之后离线也能看。

## 出错的时候

结果区显示接口原话，不改写。带上 `code` 和 `request_id` 去查最快：

- `Throttling` / 每分钟 5 次请求超限：等一会儿再点。
- 结果图下载失败：那次额度已经花掉了，页面会把原链接亮出来，24 小时内可以自己另存。
- 提示"连不上本机服务"：`app.py` 没在跑。

## 测试

```bash
.venv/bin/python -m pytest -q
```

51 个测试，全离线（接口调用用替身，不联网）。第 8 节的端到端验收需要真实调一次接口，见设计文档。
