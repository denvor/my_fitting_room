from tryon.prefs import DEFAULT_COMFY_URL, Prefs, load_prefs, save_prefs


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
