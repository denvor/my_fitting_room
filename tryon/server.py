"""Flask 服务：一次换装、图库列表与删除、图片文件读取、一个静态页。"""

import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from tryon.image_prep import ImageRejected, api_size, prepare_upload
from tryon.prompt import TRYON_PROMPT
from tryon.qwen_api import ApiError, ResultFetchFailed, ResultMissing, generate
from tryon.records import (
    FILE_BASENAMES,
    RECORD_ID_RE,
    delete_record,
    list_records,
    new_record_id,
    record_files,
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
    settings: Settings, generate_fn=generate, root: Path | str = "data/records"
) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    root_path = Path(root).resolve()  # 必须先转绝对路径，否则 send_from_directory 会拼到 app.root_path 上
    lock = threading.Lock()  # 同一时间只允许一个任务在跑

    @app.errorhandler(413)
    def too_large(_error):
        return _fail("image", "两张图加起来超过 25MB，请压缩后再传", 413)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

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
            try:
                g = generate_fn(settings, person, garment)
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
                return _fail("api", f"请求接口失败：{e}", 502)

            elapsed = round(time.monotonic() - started, 1)
            rid = new_record_id()
            meta = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "elapsed_s": elapsed,
                "prompt": TRYON_PROMPT,
                "size": api_size(person.width, person.height),
                "usage": g.usage,
                "request_id": g.request_id,
                "rewrite_status": g.rewrite_status,
            }
            save_record(root_path, rid, person.data, garment.data, g.image, meta)
            return jsonify(
                {"ok": True, "elapsed_s": elapsed, "record": {"id": rid, "meta": meta, "files": record_files(rid)}}
            )
        finally:
            lock.release()

    return app
