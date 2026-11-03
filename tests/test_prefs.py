import os
import threading
from pathlib import Path

from tryon.prefs import DEFAULT_COMFY_URL, VALID_SOURCES, Prefs, is_local, load_prefs, save_prefs


def test_第三档是合法来源而且算本机那条():
    """`comfyui_llama` 换的只是编码那颗节点，DiT/VAE 还在本机 ComfyUI 上跑。
    所以"不花额度、超时按本地算、2K 不给选"这些**都该跟着本机那条走** —— is_local 就是这个判定的唯一出处。"""
    assert VALID_SOURCES == ("api", "comfyui", "comfyui_llama")
    assert is_local("comfyui") and is_local("comfyui_llama")
    assert not is_local("api") and not is_local("gps")


def test_第三档存得进去也读得回来(tmp_path):
    f = tmp_path / "settings.json"
    save_prefs(f, Prefs("comfyui_llama", DEFAULT_COMFY_URL, "720p"))
    assert load_prefs(f) == Prefs("comfyui_llama", DEFAULT_COMFY_URL, "720p")


def test_第三档拼错时照旧回落(tmp_path):
    """`comfy_llama`（漏了 ui）这种错要当"值不认识"回落默认来源，不能当合法值存下去。"""
    f = tmp_path / "settings.json"
    f.write_text('{"source": "comfy_llama", "comfy_url": "http://x:1"}', encoding="utf-8")
    assert load_prefs(f).source == "comfyui"


def test_missing_file_falls_back_to_defaults(tmp_path):
    """还没有设置文件时不该报错，也不该追问；默认走不花钱的那条。"""
    assert load_prefs(tmp_path / "settings.json") == Prefs(source="comfyui", comfy_url=DEFAULT_COMFY_URL)


def test_corrupt_file_falls_back_to_default_source(tmp_path):
    """半截 JSON 只让来源回到默认（comfyui），主页不能因此 500。

    这条顺带说明为什么"读坏静默换来源"不再是问题：默认就是不花钱的那条。
    """
    f = tmp_path / "settings.json"
    f.write_text('{"source": "comf', encoding="utf-8")
    assert load_prefs(f).source == "comfyui"


def test_illegal_source_value_uses_default(tmp_path):
    """值写成 "gps" 这种不认识的来源，按默认来源走而不是存个非法值。"""
    f = tmp_path / "settings.json"
    f.write_text('{"source": "gps", "comfy_url": "http://x:1"}', encoding="utf-8")
    p = load_prefs(f)
    assert p.source == "comfyui" and p.comfy_url == "http://x:1"  # 单项坏只丢那一项


def test_non_string_url_uses_default(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"source": "comfyui", "comfy_url": 8188}', encoding="utf-8")
    p = load_prefs(f)
    assert p.source == "comfyui" and p.comfy_url == DEFAULT_COMFY_URL


def test_round_trip(tmp_path):
    f = tmp_path / "settings.json"
    save_prefs(f, Prefs("comfyui", "http://10.0.0.5:8188"))
    assert load_prefs(f) == Prefs("comfyui", "http://10.0.0.5:8188")


def test_save_leaves_no_temp_file(tmp_path):
    """原子写：先 .tmp 再 replace，目录里不能留垃圾。"""
    save_prefs(tmp_path / "settings.json", Prefs("comfyui", "http://x:1"))
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_two_concurrent_saves_do_not_share_a_temp_name(tmp_path, monkeypatch):
    """临时文件名写死的话，两个写者会踩同一个文件：谁先 replace 谁就把别人写进去的内容当成自己的存盘。

    2026-11-01 首页那次自动切换就是这么炸的 —— 一次页面加载发了两个 POST（`pageshow` 在首次加载也算），
    日志里是 `FileNotFoundError: settings.json.tmp` 配一个 500，另一个请求报什么全看抢到哪一步。
    """
    names = []
    real_replace = os.replace

    def spy(src, dst):
        names.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    save_prefs(tmp_path / "settings.json", Prefs("api", "http://x", "1k"))
    save_prefs(tmp_path / "settings.json", Prefs("comfyui", "http://x", "2k"))
    assert len(names) == 2 and names[0] != names[1], "两次保存用了同一个临时文件"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_parallel_saves_never_raise_and_the_file_stays_readable(tmp_path):
    """真并发：八个线程各存一个档位，谁都不许抛，最后那份必须是能读回来的合法设置（uWSGI 多 worker）。"""
    barrier = threading.Barrier(8)
    errors = []

    def work(size):
        barrier.wait()
        try:
            save_prefs(tmp_path / "settings.json", Prefs("api", DEFAULT_COMFY_URL, size))
        except OSError as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(s,)) for s in ("1k", "2k") * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert load_prefs(tmp_path / "settings.json").size in ("1k", "2k")
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_defaults_are_overridden_per_field(tmp_path):
    """defaults（来自 .env）与文件各管一项：文件里有的以文件为准，没有的沿用 defaults。"""
    f = tmp_path / "settings.json"
    f.write_text('{"source": "api"}', encoding="utf-8")
    p = load_prefs(f, Prefs(source="comfyui", comfy_url="http://from-env:8188"))
    assert (p.source, p.comfy_url) == ("api", "http://from-env:8188")


def test_binary_garbage_falls_back(tmp_path):
    """不是坏 JSON 而是根本读不出字符时，同样不能把异常抛到请求里。"""
    f = tmp_path / "settings.json"
    f.write_bytes(b"\xff\xfe\x00\x00")
    assert load_prefs(f).source == "comfyui"


def test_save_creates_missing_parent_dir(tmp_path):
    f = tmp_path / "data" / "settings.json"
    save_prefs(f, Prefs("api", DEFAULT_COMFY_URL))
    assert f.is_file()


def test_saved_settings_are_owner_only_under_a_zero_umask(tmp_path, daemon_umask):
    """线上实测这个文件落成了 666（uWSGI 的 umask 是 0000）：设置里存的是生成来源和 ComfyUI 地址。"""
    f = tmp_path / "data" / "settings.json"
    save_prefs(f, Prefs("api", DEFAULT_COMFY_URL, "2k"))
    assert f.stat().st_mode & 0o077 == 0, oct(f.stat().st_mode & 0o777)
    assert f.parent.stat().st_mode & 0o077 == 0, oct(f.parent.stat().st_mode & 0o777)
