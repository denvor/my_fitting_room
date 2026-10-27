"""uWSGI entry for My Fitting Room, mounted at / on the shared socket.

records 目录用绝对路径(uwsgi 的 chdir 是 /www/webroot),避免写到站点根目录去。
"""

from tryon.auth import demand_password
from tryon.server import create_app
from tryon.settings import PROJECT_ROOT, load_settings

RECORDS = str(PROJECT_ROOT / "data" / "records")


def _explain(problem: str, hints: list[str]):
    """配置没齐也要给出一页能读的话。一片空白最容易让人以为是代码坏了，其实差的是一行 .env。"""
    page = (
        "<meta charset='utf-8'><h1>试衣间没起来</h1><p>"
        + problem
        + "</p><ul>"
        + "".join(f"<li>{h}</li>" for h in hints)
        + "</ul>"
    ).encode("utf-8")

    def application(environ, start_response):
        start_response("500 Internal Server Error", [("Content-Type", "text/html; charset=utf-8")])
        return [page]

    return application


try:
    _settings = load_settings()
    # 这是暴露到公网的那个入口：没口令就别起来。静默无保护地跑着，比启动失败难查得多。
    demand_password(_settings)
    app = create_app(_settings, root=RECORDS)
except Exception as e:  # 启动失败要说人话，别只留一行 uWSGI 日志和一片白屏
    app = _explain(
        f"配置没到位：{e}",
        [
            "在 .env 里加一行 <code>TRYON_PASSWORD=&lt;你的口令&gt;</code>，然后 reload uWSGI。",
            "<code>data/</code> 必须可写 —— 登录用的签名密钥存在 <code>data/secret.key</code>。",
        ],
    )
