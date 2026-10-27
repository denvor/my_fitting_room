"""启动试穿工具：python dev_run.py，然后本机或局域网打开 http://<本机IP>:8000"""

from tryon.server import create_app
from tryon.settings import load_settings

if __name__ == "__main__":
    settings = load_settings()
    if not settings.password:
        # 局域网里不加摩擦是故意的，但这句话得让人看见：0.0.0.0 意味着同网络内谁都能用
        print("提示：.env 里没有 TRYON_PASSWORD，当前无口令，局域网内任何设备都能用这个服务。")
        print("      要放到公网前先在 .env 加一行 TRYON_PASSWORD=<你的口令> 再重启。")
    create_app(settings).run(host="0.0.0.0", port=8000, threaded=True, debug=False)
