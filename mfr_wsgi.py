"""uWSGI entry for My Fitting Room, mounted at / on the shared socket.

records 目录用绝对路径(uwsgi 的 chdir 是 /www/webroot),避免写到站点根目录去。
"""

from tryon.server import create_app
from tryon.settings import PROJECT_ROOT, load_settings

app = create_app(load_settings(), root=str(PROJECT_ROOT / "data" / "records"))
