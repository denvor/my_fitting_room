"""启动试穿工具：python app.py，然后本机或局域网打开 http://<本机IP>:8000"""

from tryon.server import create_app
from tryon.settings import load_settings

if __name__ == "__main__":
    create_app(load_settings()).run(host="0.0.0.0", port=8000, threaded=True, debug=False)
