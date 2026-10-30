"""Flask 服务：一次换装、图库列表与删除、图片文件读取、一个静态页。"""

import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory

from tryon import comfy_api
from tryon.auth import DEFAULT_SECRET_FILE, init_auth
from tryon.image_prep import ImageRejected, api_size, prepare_upload
from tryon.joblock import LOCK_NAME, try_acquire, who_holds, release
from tryon.prefs import DEFAULT_COMFY_URL, VALID_SIZES, VALID_SOURCES, Prefs, load_prefs, save_prefs
from tryon.prompt import TRYON_PROMPT
from tryon.quota import CREDITS_PER_1K, QuotaUnavailable, credits_for, read_quota
from tryon.qwen_api import ApiError, ResultFetchFailed, ResultMissing, generate
from tryon.records import (
    FILE_BASENAMES,
    RECORD_ID_RE,
    delete_record,
    list_records,
    new_record_id,
    save_record,
)
from tryon.settings import Settings

MAX_BODY_BYTES = 25 * 1024 * 1024  # 两张 10MB 原图加 multipart 余量
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _fail(stage: str, message: str, status: int, **extra):
    body = {"ok": False, "stage": stage, "message": message}
    body.update({k: v for k, v in extra.items() if v})
    return jsonify(body), status


def _busy(lock_path: Path):
    """现在有没有任务在跑、跑了多久、走的哪条来源；没人跑就是 None。"""
    held = who_holds(lock_path)
    return None if held is None else {"age_s": held.age_s(), "source": held.source, "pid": held.pid}


def _upload_bytes(field: str) -> bytes:
    label = "人像" if field == "person" else "衣服"
    f = request.files.get(field)
    if f is None or not f.filename:
        raise ImageRejected(f"没有收到{label}图片")
    return f.read()


def _is_http_url(value) -> bool:
    """只判形状（scheme + 主机 + 无空格），不判存不存在 —— 那是自检按钮的活。"""
    if not isinstance(value, str) or not value or value.strip() != value or " " in value:
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def create_app(
    settings: Settings,
    generate_fn=generate,
    root: Path | str = "data/records",
    quota_fn=read_quota,
    comfy_generate_fn=comfy_api.generate,
    comfy_check_fn=comfy_api.check,
    prefs_file: Path | str | None = None,
    secret_file: Path | str | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    root_path = Path(root).resolve()  # 必须先转绝对路径，否则 send_from_directory 会拼到 app.root_path 上
    # 设置和记录住同一个 data 目录：dev 与 /mfr 两种入口都自动落对地方
    prefs_path = Path(prefs_file) if prefs_file else root_path.parent / "settings.json"
    # 生成锁和设置、密钥住同一个 data 目录：跨 worker 才拦得住（threading.Lock 每进程一把）
    lock_path = root_path.parent / LOCK_NAME
    # 口令为空时网关整个不装，所以连签名密钥都不会去生成（测试不该在 tmp_path 外落文件）
    init_auth(app, settings, Path(secret_file) if secret_file else DEFAULT_SECRET_FILE, STATIC_DIR)
    # .env 的两个可选键是默认值，设置页存过的值覆盖它们（README 与 §5 都这么承诺）
    prefs_defaults = Prefs(settings.source, settings.comfy_url, settings.size)

    @app.errorhandler(413)
    def too_large(_error):
        return _fail("image", "两张图加起来超过 25MB，请压缩后再传", 413)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/settings")
    def settings_page():
        return send_from_directory(STATIC_DIR, "settings.html")

    @app.get("/api/settings")
    def api_settings_read():
        prefs = load_prefs(prefs_path, prefs_defaults)
        return jsonify(
            {
                "ok": True,
                "source": prefs.source,
                "comfy_url": prefs.comfy_url,
                "default_comfy_url": DEFAULT_COMFY_URL,
                "valid_sources": list(VALID_SOURCES),
                "size": prefs.size,
                "valid_sizes": list(VALID_SIZES),
            }
        )

    @app.post("/api/settings")
    def api_settings_save():
        body = request.get_json(silent=True) or {}
        source, comfy_url = body.get("source"), body.get("comfy_url")
        # 没带 size 的旧请求保持它现有的档位，别把它悄悄换成默认档
        size = body.get("size", load_prefs(prefs_path, prefs_defaults).size)
        if source not in VALID_SOURCES:
            return _fail("settings", f"来源只能是 api 或 comfyui，收到的是 {source!r}", 400)
        if not _is_http_url(comfy_url):
            return _fail("settings", "ComfyUI 地址要写成 http://主机:端口，比如 http://127.0.0.1:8188", 400)
        if size not in VALID_SIZES:
            return _fail("settings", f"尺寸只能是 1K 或 2K，收到的是 {size!r}", 400)
        try:
            save_prefs(prefs_path, Prefs(source, comfy_url, size))
        except OSError as e:  # 存不下来要说出来，别静默回到默认值
            return _fail("settings", f"设置存不下来：{e}", 500)
        return jsonify({"ok": True, "source": source, "comfy_url": comfy_url, "size": size})

    @app.post("/api/settings/check")
    def api_settings_check():
        """连不上也算正常完成，恒 200：问题是给用户看的清单，不是 HTTP 错误。"""
        body = request.get_json(silent=True) or {}
        url = body.get("comfy_url") or load_prefs(prefs_path, prefs_defaults).comfy_url
        result = comfy_check_fn(url)
        return jsonify({"ok": result.ok, "problems": list(result.problems)})

    @app.get("/api/quota")
    def api_quota():
        """还剩多少次可用；查不到只是少一行信息，页面照常。走本地时根本不去查额度。

        顺带回答"现在有没有任务在跑"：页面拿它按住开始按钮 —— 连接被 nginx 掐断的那次，
        前端只知道请求失败了，服务端却还在跑。
        """
        prefs = load_prefs(prefs_path, prefs_defaults)
        busy = _busy(lock_path)
        if prefs.source == "comfyui":
            # 走本地不查额度，但档位仍然要回：前端按它决定浏览器压到多大
            return jsonify({"ok": True, "source": "comfyui", "size": prefs.size, "quota": None, "busy": busy})
        credits = credits_for(prefs.size)
        try:
            q = quota_fn()
            return jsonify(
                {
                    "ok": True,
                    "source": "api",
                    "size": prefs.size,
                    # times 按所选档折算：2K 一次 108，别拿 1K 的 58 报次数
                    "quota": {**asdict(q), "times": max(0, int((q.total - q.used) // credits))},
                    "credits_per_generation": credits,
                    "credits_per_1k": CREDITS_PER_1K,
                    "busy": busy,
                }
            )
        except QuotaUnavailable as e:
            return jsonify({"ok": False, "source": "api", "message": str(e), "busy": busy})

    @app.get("/api/records")
    def api_records_list():
        return jsonify({"ok": True, "records": list_records(root_path)})

    @app.delete("/api/records/<rid>")
    def api_records_delete(rid: str):
        if delete_record(root_path, rid):
            return jsonify({"ok": True})
        return _fail("record", "没找到这条记录", 404)

    @app.get("/files/<rid>/<name>")
    def api_file(rid: str, name: str):
        if not RECORD_ID_RE.fullmatch(rid) or name not in FILE_BASENAMES:
            return _fail("file", "文件不存在", 404)
        return send_from_directory(root_path / rid, name)

    @app.post("/api/tryon")
    def api_tryon():
        # 每个请求现读一次：设置页改完立刻生效，不用重启。档位得先知道，因为它决定输入上限
        prefs = load_prefs(prefs_path, prefs_defaults)
        if not try_acquire(lock_path, prefs.source):
            return _fail("busy", "上一次还在跑，请等它结束", 409)
        try:
            try:
                person = prepare_upload(_upload_bytes("person"), prefs.size)
                garment = prepare_upload(_upload_bytes("garment"), prefs.size)
            except ImageRejected as e:
                return _fail("image", str(e), 400)

            started = time.monotonic()
            local = prefs.source == "comfyui"
            try:
                g = (
                    comfy_generate_fn(prefs.comfy_url, person, garment, size=prefs.size)
                    if local
                    # 档位并进 settings 再交给接口那条路：generate 的签名不动，请求体里的尺寸读 settings.size
                    else generate_fn(replace(settings, size=prefs.size), person, garment)
                )
            except comfy_api.ComfyUnavailable as e:
                return _fail("comfy", f"{e} 设置页里可以改地址，或者把来源切回 API。", 502)
            except comfy_api.ComfyRejected as e:
                return _fail("comfy", f"ComfyUI 报错：{e.detail}", 502)  # 原话透传，不翻译
            except comfy_api.ComfyTimeout as e:
                return _fail("comfy", f"本机 ComfyUI {int(e.seconds)} 秒没出图。本地不花额度，可以直接重来一次。", 502)
            except comfy_api.ComfyResultMissing as e:
                return _fail("comfy", f"{e}。本地不花额度，可以直接重来一次。", 502)
            except ApiError as e:
                return _fail("api", e.message, 502, code=e.code, request_id=e.request_id)
            except ResultMissing as e:
                return _fail("api", str(e), 502, request_id=e.request_id)
            except ResultFetchFailed as e:
                return _fail(
                    "result",
                    f"{e}。这次已经花掉一次生成额度，结果链接 24 小时内有效，可以自己另存",
                    502,
                    result_url=e.url,
                )
            except Exception as e:
                if local:  # 本地失败不能报"已扣额度"，那是 API 那条路才有的事
                    return _fail("comfy", f"本地生成失败：{e}。不花额度，可以直接重来一次。", 502)
                # 超时多半是服务端还在跑：额度照扣（实测迟到 8 分钟照样扣），图却拿不回来
                return _fail(
                    "api",
                    f"请求接口失败：{e}。这次可能已经扣掉 {credits_for(prefs.size)} credit，图库里不会有这条记录。",
                    502,
                )

            elapsed = round(time.monotonic() - started, 1)
            rid = new_record_id()
            meta = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "elapsed_s": elapsed,
                "prompt": TRYON_PROMPT,
                # 本地那条路由 ComfyUI 自己按参考图比例算尺寸，记真实输出；API 记我们请求的那档
                "size": f"{g.width}*{g.height}" if local else api_size(person.width, person.height, prefs.size),
                "band": prefs.size,
                "source": prefs.source,
                "usage": g.usage,
                "request_id": g.request_id,
                "rewrite_status": g.rewrite_status,
            }
            try:
                files = save_record(root_path, rid, person.data, garment.data, g.image, meta)
            except OSError as e:  # 图已经拿到、额度已经花掉，落盘失败不能变成 500 HTML
                return _fail("result", f"结果图已生成，但写入本地失败：{e}。这次生成已经花掉额度，图库里不会有这条记录。", 502)
            return jsonify(
                {"ok": True, "elapsed_s": elapsed, "record": {"id": rid, "meta": meta, "files": files}}
            )
        finally:
            release(lock_path)  # 失败、超时、落盘出错都得还锁，否则页面永久点不动

    return app
