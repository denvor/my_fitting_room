"""sample 图库：目录结构就是图库本身，没有清单文件要你维护。

约定是 data/samples/<大类>/<细分类>/<图>.jpg，细分类可以不要（发型那两类就直接躺着一层）。
"""

import json
import shutil
import threading

import pytest
from PIL import Image

from tryon.samples import (
    GROUP_ORDER,
    SampleRejected,
    list_samples,
    sample_path,
    sample_thumb,
)
from tryon.server import create_app
from tryon.settings import Settings
from dataclasses import replace
from tryon.prefs import DEFAULT_COMFY_URL

SETTINGS = Settings(endpoint="https://example.test/generation", api_key="test-key", model="qwen-image-3.0-pro")


def jpg(root, rel: str, size=(400, 600), color=(120, 130, 140)):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "samples"
    jpg(root, "男装/上衣/衬衫A.jpg")
    jpg(root, "男装/裤子/西裤B.jpg")
    jpg(root, "女装/连衣裙/裙子C.jpg")
    jpg(root, "男士发型/短发D.jpg")  # 这一类没有细分类
    jpg(root, "女士发型/长卷发E.jpg", size=(300, 300))
    return root


# --- 扫目录 ---


def test_the_tree_comes_from_the_directory(lib):
    groups = list_samples(lib)
    assert [g["group"] for g in groups] == list(GROUP_ORDER)
    assert groups[0]["subs"][0]["name"] == "上衣"
    assert groups[0]["subs"][0]["items"][0]["path"] == "男装/上衣/衬衫A.jpg"
    assert groups[0]["subs"][0]["items"][0]["name"] == "衬衫A"


def test_groups_are_ordered_as_the_page_shows_them(lib):
    """四个大类的顺序写死，别按拼音或 mtime 排 —— 每次刷新换个位置没法用。"""
    assert GROUP_ORDER == ("男装", "女装", "男士发型", "女士发型")
    jpg(lib, "童装/外套/童A.jpg")
    assert [g["group"] for g in list_samples(lib)] == [*GROUP_ORDER, "童装"]


def test_loose_files_under_a_group_are_their_own_section(lib):
    """男士发型下面直接躺图，没有细分类：这一批也得看得见，不然那两类永远进不了页面。"""
    entry = next(g for g in list_samples(lib) if g["group"] == "男士发型")
    assert [sub["name"] for sub in entry["subs"]] == ["男士发型"]  # 没有细分类时这一节就用大类自己的名字
    assert [i["path"] for s in entry["subs"] for i in s["items"]] == ["男士发型/短发D.jpg"]


def test_only_pictures_are_listed(lib):
    (lib / "男装/上衣").mkdir(parents=True, exist_ok=True)
    (lib / "男装/上衣/notes.txt").write_text("不是图", encoding="utf-8")
    (lib / "男装/上衣/.DS_Store").write_bytes(b"\x00")
    jpg(lib, "男装/上衣/夹克F.png")
    paths = [i["path"] for g in list_samples(lib) for s in g["subs"] for i in s["items"]]
    assert "男装/上衣/notes.txt" not in paths and "男装/上衣/.DS_Store" not in paths
    assert "男装/上衣/夹克F.png" in paths


def test_missing_library_is_empty_not_an_error(tmp_path):
    assert list_samples(tmp_path / "nope") == []


def test_hidden_directories_are_not_a_category(lib):
    """缩略图缓存就住在 .thumbs 里，它不该变成一个能点的分类。"""
    (lib / ".thumbs").mkdir()
    jpg(lib, ".thumbs/幽灵.jpg")
    assert [g["group"] for g in list_samples(lib)] == list(GROUP_ORDER)


# --- 取图：路径必须留在库里 ---


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "/etc/passwd", "男装/上衣/../../ secrets.txt", "../男装/上衣/衬衫A.jpg", "", "."],
)
def test_a_path_that_leaves_the_library_is_refused(lib, bad):
    with pytest.raises(SampleRejected):
        sample_path(lib, bad)


def test_a_real_path_resolves(lib):
    assert sample_path(lib, "男装/上衣/衬衫A.jpg").is_file()


def test_a_name_that_is_not_a_picture_is_refused(lib):
    (lib / "男装/上衣/说明.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SampleRejected):
        sample_path(lib, "男装/上衣/说明.txt")


def test_a_missing_picture_is_refused(lib):
    with pytest.raises(SampleRejected):
        sample_path(lib, "男装/上衣/没这张.jpg")


# --- 缩略图 ---


def test_thumbnails_are_small_and_cached(lib):
    first = sample_thumb(lib, "女士发型/长卷发E.jpg")
    # 缓存按 rel 镜像目录：镜像源文件自己的名字，再往上一层是 .thumbs（长卷发在发型大类下没有细分类）
    assert first.parent.parent.name == ".thumbs" and first.parent.name == "女士发型"
    w, h = Image.open(first).size
    assert max(w, h) <= 240
    again = sample_thumb(lib, "女士发型/长卷发E.jpg")
    assert again == first and first.is_file()


def test_a_bigger_source_gets_a_newer_thumb(lib):
    """换了同名图不能还看旧缩略图：按源文件的 mtime 判缓存过没过。"""
    assert Image.open(sample_thumb(lib, "男装/上衣/衬衫A.jpg")).size == (160, 240)
    jpg(lib, "男装/上衣/衬衫A.jpg", size=(900, 600), color=(10, 20, 30))  # 换一张同名的横图
    assert Image.open(sample_thumb(lib, "男装/上衣/衬衫A.jpg")).size == (240, 160)


def test_parallel_thumbs_of_one_image_never_raise(lib):
    """图库网格一次就要几十张缩略图，线上是 4 进程 × 2 线程：同一个缓存文件会有人同时建目录、同时写。

    2026-11-01 浏览器实测就是这样 404 的 —— `mkdir` 没给 exist_ok，第二个人撞上 FileExistsError，
    路由把它和"库里没这张"一起当成 404 报了出去。
    """
    barrier = threading.Barrier(8)
    errors = []

    def work():
        barrier.wait()
        try:
            sample_thumb(lib, "男装/上衣/衬衫A.jpg")
        except OSError as e:
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    cached = list((lib / ".thumbs" / "男装" / "上衣").iterdir())
    assert [p.name for p in cached] == ["衬衫A.jpg"], "缓存目录里留了半截临时文件"


def test_thumbnail_files_are_owner_only(lib, daemon_umask):
    """和记录、设置同一套理由：uWSGI 的 umask 是 0000，缓存也得只有跑服务的账号读得动。"""
    p = sample_thumb(lib, "男装/上衣/衬衫A.jpg")
    assert p.stat().st_mode & 0o077 == 0, oct(p.stat().st_mode & 0o777)
    assert p.parent.stat().st_mode & 0o077 == 0, oct(p.parent.stat().st_mode & 0o777)


# --- 接口 ---


@pytest.fixture
def client(tmp_path, lib):
    root = tmp_path / "records"
    (tmp_path / "settings.json").write_text(
        json.dumps({"source": "comfyui", "comfy_url": DEFAULT_COMFY_URL, "size": "1k"}), encoding="utf-8"
    )
    app = create_app(
        SETTINGS,
        generate_fn=lambda *a, **k: pytest.fail("这条用例不该生成"),
        comfy_generate_fn=lambda *a, **k: pytest.fail("这条用例不该生成"),
        root=root,
        prefs_file=tmp_path / "settings.json",
        samples_dir=lib,
    )
    app.testing = True
    return app.test_client()


def test_the_tree_endpoint_answers_with_paths_and_thumbs(client):
    body = client.get("/api/samples").get_json()
    assert body["ok"] is True
    item = body["groups"][0]["subs"][0]["items"][0]
    assert item["url"].startswith("samples/") and item["thumb"].startswith("samples/thumb/")
    assert body["groups"][0]["group"] == "男装"


def test_a_sample_and_its_thumb_are_served(client):
    from urllib.parse import quote

    rel = quote("男装/上衣/衬衫A.jpg")
    assert client.get(f"/samples/{rel}").status_code == 200
    assert client.get(f"/samples/thumb/{rel}").status_code == 200


def test_a_traversal_on_the_url_is_a_404_not_a_leak(client):
    r = client.get("/samples/..%2F..%2Fsettings.json")
    assert r.status_code == 404
    assert b"comfy_url" not in r.data


def test_a_missing_sample_is_404(client):
    assert client.get("/samples/男装/上衣/没这张.jpg").status_code == 404


def test_the_library_needs_the_passphrase(tmp_path, lib):
    """图库里是人像和衣服照片，和记录同级敏感：网关必须照样拦住。"""
    (tmp_path / "settings.json").write_text(
        json.dumps({"source": "comfyui", "comfy_url": DEFAULT_COMFY_URL}), encoding="utf-8"
    )
    app = create_app(
        replace(SETTINGS, password="let-me-in"),
        generate_fn=lambda *a, **k: None,
        comfy_generate_fn=lambda *a, **k: None,
        root=tmp_path / "records",
        prefs_file=tmp_path / "settings.json",
        secret_file=tmp_path / "secret.key",
        samples_dir=lib,
    )
    app.testing = True
    c = app.test_client()
    assert c.get("/api/samples").status_code == 401
    assert c.get("/samples/男装/上衣/衬衫A.jpg").status_code in (302, 401)


def test_no_library_directory_at_all_still_answers(client, lib):
    shutil.rmtree(lib)
    assert client.get("/api/samples").get_json()["groups"] == []
