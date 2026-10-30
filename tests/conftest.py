"""整套测试的护栏：默认来源是不花钱的本机 ComfyUI，所以真传输必须被挡死。

没有这道护栏时，一个忘了注入 comfy_generate_fn 的 server 测试会安静地连上开发者自己
开着的 ComfyUI 真生成一张图（曾经发生过），测试还是绿的 —— 只有跑分从 1 秒变成 30 秒
这件事会告诉你。这里把 comfy_api 的四个真传输换成"一调就报错"，哪个测试真碰网络就当场失败。
需要假传输的测试（tests/test_comfy_api.py）自己 monkeypatch 覆盖，顺序上晚于本 fixture。
"""

import os

import pytest

from tryon import comfy_api


class RealComfyTraffic(BaseException):
    """故意不是 Exception：路由里的兜底 except Exception 会把它吞成一个像样的 502，
    那等于"测试悄悄上网但仍然是绿的"。让它穿透兜底，粗心的用例当场失败。"""


@pytest.fixture
def daemon_umask():
    """把进程 umask 设成 uWSGI 主进程那一套（实测 /proc/<master>/status 里是 Umask: 0000）。

    开发者自己的 shell 是 022，所以"忘了显式收紧权限"这种问题在本地测试里永远看不出来；
    线上真实落成了 data/ 777、records/<id>/ 777、settings.json 666 —— 里面是人脸原图，
    而这台机同时还跑着另外四个站。需要验证权限的用例挂这个 fixture。
    """
    old = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture(autouse=True)
def no_real_comfy_traffic(monkeypatch):
    def refuse(*args, **kwargs):
        raise RealComfyTraffic(f"测试试图走真 ComfyUI 网络调用：{args[:1]}")

    for seam in ("_post_multipart", "_post_json", "_get_json", "_get_bytes", "_sleep"):
        monkeypatch.setattr(comfy_api, seam, refuse)
