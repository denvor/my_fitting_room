"""Flask 服务：一次换装、图库列表与删除、图片文件读取、一个静态页。"""

import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from tryon import comfy_api
from tryon.image_prep import ImageRejected, api_size, prepare_upload
from tryon.prefs import load_prefs
from tryon.prompt import TRYON_PROMPT
from tryon.quota import CREDITS_PER_1K, QuotaUnavailable, read_quota
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


def _upload_bytes(field: str) -> bytes:
    label = "人像" if field == "person" else "衣服"
    f = request.files.get(field)
    if f is None or not f.filename:
        raise ImageRejected(f"没有收到{label}图片")
    return f.read()


def create_app(
    settings: Settings,
    generate_fn=generate,
    root: Path | str = "data/records",
    quota_fn=read_quota,
    comfy_generate_fn=comfy_api.generate,
    prefs_file: Path | str | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    root_path = Path(root).resolve()  # 必须先转绝对路径，否则 send_from_directory 会拼到 app.root_path 上
    # 设置和记录住同一个 data 目录：dev 与 /mfr 两种入口都自动落对地方
    prefs_path = Path(prefs_file) if prefs_file else root_path.parent / "settings.json"
    lock = threading.Lock()  # 同一时间只允许一个任务在跑，两条来源共用这一把

    @app.errorhandler(413)
    def too_large(_error):
        return _fail("image", "两张图加起来超过 25MB，请压缩后再传", 413)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/quota")
    def api_quota():
        """还剩多少次可用；查不到只是少一行信息，页面照常。走本地时根本不去查额度。"""
        if load_prefs(prefs_path).source == "comfyui":
            return jsonify({"ok": True, "source": "comfyui", "quota": None})
        try:
            return jsonify(
                {
                    "ok": True,
                    "source": "api",
                    "quota": asdict(quota_fn()),
                    "credits_per_1k": CREDITS_PER_1K,
                }
            )
        except QuotaUnavailable as e:
            return jsonify({"ok": False, "source": "api", "message": str(e)})

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
        if not lock.acquire(blocking=False):
            return _fail("busy", "上一次还在跑，请等它结束", 409)
        try:
            try:
                person = prepare_upload(_upload_bytes("person"))
                garment = prepare_upload(_upload_bytes("garment"))
            except ImageRejected as e:
                return _fail("image", str(e), 400)

            started = time.monotonic()
            prefs = load_prefs(prefs_path)  # 每个请求现读一次：设置页改完立刻生效，不用重启
            local = prefs.source == "comfyui"
            try:
                g = (
                    comfy_generate_fn(prefs.comfy_url, person, garment)
                    if local
                    else generate_fn(settings, person, garment)
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
                return _fail("api", f"请求接口失败：{e}。这次可能已经扣掉 {CREDITS_PER_1K} credit，图库里不会有这条记录。", 502)

            elapsed = round(time.monotonic() - started, 1)
            rid = new_record_id()
            meta = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "elapsed_s": elapsed,
                "prompt": TRYON_PROMPT,
                # 本地那条路由 ComfyUI 自己按参考图比例算尺寸，记真实输出；API 记我们请求的那档
                "size": f"{g.width}*{g.height}" if local else api_size(person.width, person.height),
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
            lock.release()

    return app
