"""输出档位（1K / 2K）的合同：面积上限、输入长边、credit 单价、档位怎么存、怎么传到两条来源。"""

from io import BytesIO

import pytest
from PIL import Image

from tryon.comfy_api import comfy_resolution
from tryon.image_prep import api_size, out_area, max_side, prepare_upload
from tryon.prefs import DEFAULT_SIZE, VALID_SIZES, Prefs, load_prefs, save_prefs
from tryon.quota import CREDITS_PER_1K, CREDITS_PER_2K, credits_for
from tryon.server import create_app
from tryon.settings import Settings, load_settings

ONE_K_AREA = 2_250_000
TWO_K_AREA = 4_157_440  # 1792×2320：实测接口收得下、扣 108 credit 的那一档
FOUR_80P_AREA = 307_200  # 480×640：2026-10-31 实测本地 26 秒那一档（同机 1K 是 95 秒）


def dimensions(size_str: str) -> tuple[int, int]:
    w, h = size_str.split("*")
    return int(w), int(h)


def jpeg(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (120, 90, 60)).save(buf, format="JPEG")
    return buf.getvalue()


# ---- 档位表 ----


def test_band_table():
    assert (out_area("1k"), out_area("2k"), out_area("480p")) == (ONE_K_AREA, TWO_K_AREA, FOUR_80P_AREA)
    assert (max_side("1k"), max_side("2k"), max_side("480p")) == (1536, 2048, 1536)


def test_credits_per_band_are_measured_not_doubled():
    # 2K 不是 1K 的两倍价：实测 108，不是 116
    assert (credits_for("1k"), credits_for("2k")) == (CREDITS_PER_1K, CREDITS_PER_2K) == (58, 108)


def test_480p_costs_the_same_as_1k_on_the_api():
    # 实测 2026-10-31：请求 0.31M 的图，入账正好 +58.000，接口按面积分档、没有低于 1K 的那一档
    assert credits_for("480p") == CREDITS_PER_1K


# ---- 480p 这一档只管输出，输入不动 ----


def test_prepare_upload_480p_does_not_shrink_the_input():
    # 实测输入 1.77M / 0.79M / 0.31M 三种都是 26 秒，所以缩输入只会丢掉喂给模型的条件，不会省时间
    raw = jpeg(3072, 4096)
    assert (prepare_upload(raw, "480p").width, prepare_upload(raw, "480p").height) == (1152, 1536)


def test_comfy_resolution_480p_targets_the_small_budget_whatever_the_input():
    assert comfy_resolution(896, 1152, "480p") == 544 == comfy_resolution(2048, 1536, "480p")
    assert 544 % 32 == 0 and 544 * 544 <= FOUR_80P_AREA


# ---- API 那条：输出尺寸 ----


def test_api_size_2k_goes_past_the_1k_ceiling_but_stops_at_2k():
    # 输出跟着人相的像素走、只被档位上限拦住，所以要比的是同一张输入在两个档下各给多少
    w1, h1 = dimensions(api_size(1536, 2048, "1k"))
    w, h = dimensions(api_size(1536, 2048, "2k"))
    assert w1 * h1 <= ONE_K_AREA < w * h, "2K 档还压在 1K 上限就是没生效"
    assert w * h <= TWO_K_AREA
    assert w % 8 == 0 and h % 8 == 0
    assert abs((w / h) - (1536 / 2048)) < 0.02  # 跟人相的比例不能因为换档而变形


def test_api_size_1k_unchanged_by_default():
    assert api_size(1536, 1152) == api_size(1536, 1152, "1k")
    w, h = dimensions(api_size(1536, 1152))
    assert w * h <= ONE_K_AREA


def test_api_size_2k_keeps_the_ratio_cap():
    w, h = dimensions(api_size(2048, 200, "2k"))
    assert max(w / h, h / w) <= 8.0


# ---- 两条来源共用的输入上限 ----


def test_api_size_2k_upscales_a_small_person():
    w, h = dimensions(api_size(896, 1152, "2k"))
    assert w * h > TWO_K_AREA * 0.9, "选 2K 却只出 1K 面积"
    assert w * h <= TWO_K_AREA


def test_prepare_upload_2k_keeps_more_pixels_than_1k():
    raw = jpeg(3072, 4096)
    small = prepare_upload(raw, "1k")
    big = prepare_upload(raw, "2k")
    assert (small.width, small.height) == (1152, 1536)
    assert big.width * big.height > small.width * small.height
    assert max(big.width, big.height) <= 2048
    assert big.width * big.height <= TWO_K_AREA


def test_prepare_upload_default_stays_1k():
    assert prepare_upload(jpeg(3072, 4096)).width == 1152


# ---- ComfyUI 那条：resolution 是像素预算 ----


def test_comfy_resolution_targets_the_band_rather_than_capping_it():
    # 2K 是目标不是上限：小参考图也给满预算，否则"选 2K 出 1K"没人看得出来为什么
    assert comfy_resolution(896, 1152, "2k") == 2016 == comfy_resolution(2048, 1536, "2k")
    # 1K 档维持基线：预算内原样出，超了才压回 1472
    assert comfy_resolution(896, 1152, "1k") == 0
    assert comfy_resolution(2048, 1536, "1k") == 1472


def test_comfy_resolution_2k_budget_is_a_multiple_of_32():
    budget = comfy_resolution(4096, 4096, "2k")
    assert budget % 32 == 0
    assert budget**2 <= TWO_K_AREA
    assert budget > 1472


# ---- 档位怎么存 ----


def test_default_size_is_1k():
    assert DEFAULT_SIZE == "1k" and Prefs().size == "1k"
    assert Settings(endpoint="e", api_key="k", model="m").size == "1k"


def test_prefs_size_round_trips(tmp_path):
    f = tmp_path / "settings.json"
    # 存的是非默认值，才证明这字段真的往返了，而不是回退到默认刚好撞上
    save_prefs(f, Prefs("comfyui", "http://127.0.0.1:8188", "2k"))
    assert load_prefs(f, Prefs()).size == "2k"


def test_prefs_without_size_field_falls_back_to_default(tmp_path):
    # 老设置文件里根本没有这一项，不能把用户填好的来源和地址一起清掉
    f = tmp_path / "settings.json"
    f.write_text('{"source": "api", "comfy_url": "http://10.0.0.9:8188"}', encoding="utf-8")
    prefs = load_prefs(f, Prefs())
    assert (prefs.source, prefs.comfy_url, prefs.size) == (
        "api",
        "http://10.0.0.9:8188",
        DEFAULT_SIZE,
    )


def test_bad_size_value_only_costs_that_field(tmp_path):
    f = tmp_path / "settings.json"
    save_prefs(f, Prefs("api", "http://127.0.0.1:8188", "4k"))
    reloaded = load_prefs(f, Prefs("comfyui", "http://x", "2k"))
    assert reloaded.size == "2k" and reloaded.source == "api"


def test_size_env_key_parsed(tmp_path):
    p = tmp_path / ".env"
    p.write_text("OPENAI_URL=https://x/compatible-mode/v1\nAPI_KEY=k\nMODEL=m\nTRYON_SIZE=1k\n", encoding="utf-8")
    assert load_settings(p).size == "1k"


@pytest.mark.parametrize("raw,expected", [("", DEFAULT_SIZE), ("4k", DEFAULT_SIZE)])
def test_missing_or_illegal_size_env_falls_back_to_default(tmp_path, raw, expected):
    p = tmp_path / ".env"
    line = f"TRYON_SIZE={raw}\n" if raw else ""
    p.write_text(f"OPENAI_URL=https://x/compatible-mode/v1\nAPI_KEY=k\nMODEL=m\n{line}", encoding="utf-8")
    assert load_settings(p).size == expected


def test_valid_sizes_are_the_three_bands_smallest_first():
    assert VALID_SIZES == ("480p", "1k", "2k")


def test_local_source_keeps_480p(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text('{"source": "comfyui", "comfy_url": "http://x", "size": "480p"}', encoding="utf-8")
    assert load_prefs(f, Prefs()).size == "480p"


def test_api_source_never_runs_480p(tmp_path):
    # 接口那边没有低于 1K 的档：存成 api + 480p 也得出 1K，不然页面写着低档、账上按 1K 扣
    f = tmp_path / "settings.json"
    f.write_text('{"source": "api", "comfy_url": "http://x", "size": "480p"}', encoding="utf-8")
    assert load_prefs(f, Prefs()).size == "1k"


def test_conflicting_env_defaults_get_clamped_too(tmp_path):
    # .env 里 TRYON_SOURCE=api 配 TRYON_SIZE=480p 是自相矛盾的组合，默认值这条路径也得收敛到 1K
    assert load_prefs(tmp_path / "没有这个文件.json", Prefs("api", "http://x", "480p")).size == "1k"


def test_every_band_has_its_own_local_timeout():
    # comfy_api.generate 按档位查超时上限，缺一项就是选到那一档时 KeyError
    from tryon.comfy_api import TIMEOUT_BY_SIZE

    assert set(TIMEOUT_BY_SIZE) >= set(VALID_SIZES)


# ---- 档位真的传到了两条来源 ----


class Recording:
    def __init__(self):
        self.api_settings = []
        self.comfy_size = []
        self.upload_sizes = []

    def api_fn(self, settings, person, garment, timeout=600):
        self.api_settings.append(settings)
        raise RuntimeError("验到请求就够了")

    def comfy_fn(self, comfy_url, person, garment, timeout=300, size="1k"):
        self.comfy_size.append(size)
        from tryon.comfy_api import ComfyUnavailable

        raise ComfyUnavailable(comfy_url, "记账用")

    def quota_fn(self):
        from tryon.quota import Quota

        return Quota(total=25500, used=0.0, times_1k=440, remaining_days=12)


SAVED_2K_API = '{"source": "api", "comfy_url": "http://x", "size": "2k"}'
SAVED_2K_COMFY = '{"source": "comfyui", "comfy_url": "http://x", "size": "2k"}'
SAVED_480P_COMFY = '{"source": "comfyui", "comfy_url": "http://x", "size": "480p"}'
SAVED_480P_API = '{"source": "api", "comfy_url": "http://x", "size": "480p"}'


def make(tmp_path, rec, prefs_text=None, **over):
    settings = Settings(
        endpoint="https://example.test/generation", api_key="k", model="qwen-image-3.0-pro", password="", **over
    )
    prefs_file = tmp_path / "settings.json"
    if prefs_text:
        prefs_file.write_text(prefs_text, encoding="utf-8")
    app = create_app(
        settings,
        root=tmp_path / "records",
        prefs_file=prefs_file,
        secret_file=tmp_path / "secret.key",
        generate_fn=rec.api_fn,
        comfy_generate_fn=rec.comfy_fn,
        comfy_check_fn=lambda url: None,
        quota_fn=rec.quota_fn,
    )
    return app


def post_tryon(client):
    return client.post(
        "/api/tryon",
        data={
            "person": (BytesIO(jpeg(3072, 4096)), "p.jpg"),
            "garment": (BytesIO(jpeg(800, 600)), "g.jpg"),
        },
        content_type="multipart/form-data",
    )


def test_api_path_defaults_to_1k(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, source="api").test_client()
    post_tryon(client)
    assert rec.api_settings and rec.api_settings[0].size == "1k"  # .env 和设置文件都没写 → 默认 1K


def test_api_path_honours_a_saved_2k_choice(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, prefs_text=SAVED_2K_API).test_client()
    post_tryon(client)
    assert rec.api_settings[0].size == "2k"


def test_local_path_gets_the_selected_band(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, prefs_text=SAVED_2K_COMFY).test_client()
    post_tryon(client)
    assert rec.comfy_size == ["2k"]


def test_local_path_gets_the_480p_band(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, prefs_text=SAVED_480P_COMFY).test_client()
    post_tryon(client)
    assert rec.comfy_size == ["480p"]


def test_api_path_falls_back_to_1k_when_the_saved_band_is_480p(tmp_path):
    # 设置文件是死的、来源可能被别的入口改过：真发请求那一刻也得按 1K 走
    rec = Recording()
    client = make(tmp_path, rec, prefs_text=SAVED_480P_API).test_client()
    post_tryon(client)
    assert rec.api_settings[0].size == "1k"


def test_saving_480p_on_the_api_source_falls_back_and_says_so(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, source="api").test_client()
    r = client.post("/api/settings", json={"source": "api", "comfy_url": "http://x", "size": "480p"})
    body = r.get_json()
    assert r.status_code == 200 and body["size"] == "1k"
    assert "480p" in body["size_note"], "回落要说明，不能静默改掉用户刚选的那一项"


def test_quota_tells_the_page_which_band_it_is_running(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, prefs_text=SAVED_480P_COMFY).test_client()
    assert client.get("/api/quota").get_json()["size"] == "480p"


def test_quota_line_prices_the_selected_band(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, prefs_text=SAVED_2K_API).test_client()
    body = client.get("/api/quota").get_json()
    assert body["size"] == "2k" and body["credits_per_generation"] == CREDITS_PER_2K
    assert body["quota"]["times"] == int(25500 // CREDITS_PER_2K)  # 按 2K 单价折算，不是 1K


def test_quota_line_prices_1k_by_default(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, source="api").test_client()
    body = client.get("/api/quota").get_json()
    assert body["size"] == "1k" and body["credits_per_generation"] == 58
    assert body["quota"]["times"] == 439


def test_settings_api_accepts_and_validates_size(tmp_path):
    rec = Recording()
    client = make(tmp_path, rec, source="api").test_client()
    assert client.get("/api/settings").get_json()["size"] == "1k"
    ok = client.post("/api/settings", json={"source": "api", "comfy_url": "http://x", "size": "2k"})
    assert ok.status_code == 200 and ok.get_json()["size"] == "2k"
    bad = client.post("/api/settings", json={"source": "api", "comfy_url": "http://x", "size": "8k"})
    assert bad.status_code == 400 and "1K" in bad.get_json()["message"]


def test_failure_message_names_the_band_price(tmp_path):
    # 走 API 失败时那句"可能已经扣掉 N credit"必须是本档的价，不是写死的 58
    rec = Recording()
    client = make(tmp_path, rec, prefs_text=SAVED_2K_API).test_client()
    body = post_tryon(client).get_json()
    assert str(CREDITS_PER_2K) in body["message"] and "58 credit" not in body["message"]
