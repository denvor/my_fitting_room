"""sample 图库：目录结构就是图库本身，没有清单文件要你维护。

约定是 data/samples/<大类>/<细分类>/<图>.jpg，细分类可以不要（发型那两类就直接躺着一层）。
"""

import json
import shutil
import threading
from io import BytesIO

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


def jpg_bytes(width: int = 400, height: int = 500, color=(9, 8, 7)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


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


# ==================== 写侧：加图、建类、改名、删 ====================

from tryon.samples import add_samples, make_room, move_path, remove_path  # noqa: E402


def read_tree(root):
    return {g["group"]: {s["name"]: [i["name"] for i in s["items"]] for s in g["subs"]} for g in list_samples(root)}


def test_adding_a_picture_lands_in_the_right_shelf(tmp_path):
    root = tmp_path / "samples"
    added = add_samples(root, "男装", "上衣", [("牛仔衬衫.jpg", jpg_bytes(500, 700))])
    assert added == ["男装/上衣/牛仔衬衫.jpg"]
    assert read_tree(root) == {"男装": {"上衣": ["牛仔衬衫"]}}
    assert (root / "男装/上衣/牛仔衬衫.jpg").is_file()


def test_a_new_category_is_created_by_the_upload(tmp_path):
    """上传表单里填一个新细分类：不该报错，也不该把人塞进一个已有分类里。"""
    root = tmp_path / "samples"
    add_samples(root, "女装", "大衣", [("a.jpg", jpg_bytes(400, 500))])
    add_samples(root, "女装", None, [("b.jpg", jpg_bytes(400, 500))])  # 不要细分类
    assert read_tree(root) == {"女装": {"大衣": ["a"], "女装": ["b"]}}


def test_a_name_that_collides_gets_a_suffix_instead_of_overwriting(tmp_path):
    """撞名就覆盖的话，人会把上一张不小心同名的好图丢掉。"""
    root = tmp_path / "samples"
    first, second = jpg_bytes(400, 500, (9, 9, 9)), jpg_bytes(400, 500, (200, 5, 5))
    assert add_samples(root, "男装", "上衣", [("重名.jpg", first)])[0] == "男装/上衣/重名.jpg"
    assert add_samples(root, "男装", "上衣", [("重名.jpg", second)]) == ["男装/上衣/重名-2.jpg"]
    assert read_tree(root)["男装"]["上衣"] == ["重名", "重名-2"]


def test_a_non_picture_is_refused(tmp_path):
    root = tmp_path / "samples"
    with pytest.raises(SampleRejected):
        add_samples(root, "男装", "上衣", [("假装是图.txt", "这根本不是图片".encode())])
    assert list(root.rglob("*.txt")) == []


def test_an_oversized_picture_is_refused(tmp_path):
    root = tmp_path / "samples"
    with pytest.raises(SampleRejected, match="10MB"):
        add_samples(root, "男装", "上衣", [("大.jpg", b"0" * (11 * 1024 * 1024))])


@pytest.mark.parametrize("bad_group", ["../etc", "男装/女装", ".thumbs", "", "  ", "a" * 80])
def test_a_category_name_that_would_escape_or_confuse_is_refused(tmp_path, bad_group):
    root = tmp_path / "samples"
    with pytest.raises(SampleRejected):
        add_samples(root, bad_group, None, [("x.jpg", jpg_bytes(400, 500))])


def test_the_filename_is_taken_from_the_upload_not_trusted_as_a_path(tmp_path):
    """浏览器给的 filename 可以带路径（IE 会给整条 Windows 路径），只认最后那一段。"""
    root = tmp_path / "samples"
    added = add_samples(root, "男装", "上衣", [("C:\\fakepath\\外套.jpg", jpg_bytes(400, 500))])
    assert added == ["男装/上衣/外套.jpg"]


def test_written_files_are_owner_only_under_a_zero_umask(tmp_path, daemon_umask):
    root = tmp_path / "samples"
    added = add_samples(root, "男装", "上衣", [("x.jpg", jpg_bytes(400, 500))])
    f = root / added[0]
    assert f.stat().st_mode & 0o077 == 0, oct(f.stat().st_mode & 0o777)
    assert f.parent.stat().st_mode & 0o077 == 0 and root.stat().st_mode & 0o077 == 0


def test_making_a_room_creates_both_levels(tmp_path):
    root = tmp_path / "samples"
    make_room(root, "男装", "裤子")
    assert (root / "男装/裤子").is_dir()
    make_room(root, "男士发型", None)
    assert (root / "男士发型").is_dir()


def test_an_empty_room_shows_up_as_an_empty_shelf(tmp_path):
    """建了类还没放图，页面上要看得见那一节（不然人会以为没建成又建一次）。"""
    root = tmp_path / "samples"
    make_room(root, "童装", "外套")
    assert (root / "童装/外套").is_dir()
    assert [d.name for d in (root / "童装").iterdir()] == ["外套"]


def test_renaming_a_subcategory_moves_the_pictures_with_it(tmp_path):
    root = tmp_path / "samples"
    add_samples(root, "男装", "上衣", [("a.jpg", jpg_bytes(400, 500)), ("b.jpg", jpg_bytes(400, 500))])
    move_path(root, "男装/上衣", "男装/T恤")
    assert read_tree(root) == {"男装": {"T恤": ["a", "b"]}}


def test_renaming_a_group_renames_everything_under_it(tmp_path):
    root = tmp_path / "samples"
    add_samples(root, "男装", "上衣", [("a.jpg", jpg_bytes(400, 500))])
    move_path(root, "男装", "男款")
    assert read_tree(root) == {"男款": {"上衣": ["a"]}}


def test_moving_a_picture_into_another_shelf(tmp_path):
    root = tmp_path / "samples"
    add_samples(root, "男装", "上衣", [("a.jpg", jpg_bytes(400, 500))])
    add_samples(root, "女装", "大衣", [])
    move_path(root, "男装/上衣/a.jpg", "女装/大衣/a.jpg")
    assert tree_paths(root) == {"女装/大衣/a.jpg"}  # 男装/上衣 那节还在（空架子也报），只是没图了


def test_renaming_a_shelf_drops_the_stale_thumbnail_cache(tmp_path):
    """缓存是按路径存的：改了类名，旧路径下那一整棵缓存都是孤儿，留着迟早和新的对不上。"""
    root = tmp_path / "samples"
    add_samples(root, "男装", "外套", [("a.jpg", jpg_bytes(400, 500))])
    sample_thumb(root, "男装/外套/a.jpg")
    move_path(root, "男装/外套", "男装/大衣")
    assert not (root / ".thumbs" / "男装" / "外套").exists(), "旧路径的缓存目录还在"
    assert not (root / ".thumbs" / "男装" / "外套.jpg").exists()


def test_a_move_onto_an_existing_thing_is_refused(tmp_path):
    root = tmp_path / "samples"
    add_samples(root, "男装", "上衣", [("a.jpg", jpg_bytes(400, 500))])
    add_samples(root, "男装", "裤子", [("a.jpg", jpg_bytes(400, 500))])
    with pytest.raises(SampleRejected):
        move_path(root, "男装/上衣/a.jpg", "男装/裤子/a.jpg")


def test_a_directory_cannot_be_moved_inside_itself(tmp_path):
    """`男装` → `男装/内层` 会把整棵树套娃，ComfyUI 扫到地老天荒。"""
    root = tmp_path / "samples"
    make_room(root, "男装", "上衣")
    with pytest.raises(SampleRejected):
        move_path(root, "男装", "男装/内层")


def test_deleting_a_picture_also_drops_its_cached_thumb(tmp_path):
    root = tmp_path / "samples"
    add_samples(root, "男装", "上衣", [("a.jpg", jpg_bytes(400, 500))])
    sample_thumb(root, "男装/上衣/a.jpg")
    assert sample_path(root, "男装/上衣/a.jpg").is_file()
    remove_path(root, "男装/上衣/a.jpg")
    assert not (root / "男装/上衣/a.jpg").exists()
    assert not (root / ".thumbs/男装/上衣/a.jpg").exists()
    assert tree_paths(root) == set()


def test_only_an_empty_shelf_can_be_deleted(tmp_path):
    """删一类要先把里面的图清空 —— 一句"删掉上衣"不该连带抹掉几十张商品图。"""
    root = tmp_path / "samples"
    add_samples(root, "男装", "上衣", [("a.jpg", jpg_bytes(400, 500))])
    with pytest.raises(SampleRejected, match="还有"):
        remove_path(root, "男装/上衣")
    make_room(root, "男装", "空的")
    remove_path(root, "男装/空的")
    assert not (root / "男装/空的").exists()


# ==================== 写侧的接口 ====================


@pytest.fixture
def wclient(tmp_path):
    """一个几乎空的图库：只有一个大类一节，写操作用它。"""
    root = tmp_path / "samples"
    jpg(root, "男装/上衣/已有.jpg")
    (tmp_path / "settings.json").write_text(
        json.dumps({"source": "comfyui", "comfy_url": DEFAULT_COMFY_URL}), encoding="utf-8"
    )
    app = create_app(
        SETTINGS,
        generate_fn=lambda *a, **k: pytest.fail("这条用例不该生成"),
        comfy_generate_fn=lambda *a, **k: pytest.fail("这条用例不该生成"),
        root=tmp_path / "records",
        prefs_file=tmp_path / "settings.json",
        samples_dir=root,
    )
    app.testing = True
    return app.test_client(), root


def tree_paths(root):
    return {i["path"] for g in list_samples(root) for s in g["subs"] for i in s["items"]}


def test_the_add_endpoint_writes_two_pictures_at_once(wclient):
    client, root = wclient
    r = client.post(
        "/api/samples/add",
        data={
            "group": "女装",
            "sub": "大衣",
            "image": [(BytesIO(jpg_bytes(400, 500)), "呢子.jpg"), (BytesIO(jpg_bytes(400, 500)), "羽绒.jpg")],
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["added"] == ["女装/大衣/呢子.jpg", "女装/大衣/羽绒.jpg"]
    assert "女装/大衣/呢子.jpg" in tree_paths(root)


def test_the_add_endpoint_creates_the_shelf_it_was_given(wclient):
    """表单里写一个新大类：库里本来没有，建出来。"""
    client, root = wclient
    r = client.post(
        "/api/samples/add",
        data={"group": "童装", "sub": "", "image": [(BytesIO(jpg_bytes(300, 400)), "a.jpg")]},
        content_type="multipart/form-data",
    )
    assert r.get_json()["added"] == ["童装/a.jpg"]
    assert (root / "童装/a.jpg").is_file()


def test_the_add_endpoint_reports_a_non_picture(wclient):
    client, _root = wclient
    r = client.post(
        "/api/samples/add",
        data={"group": "男装", "sub": "上衣", "image": [(BytesIO("这不是图".encode()), "假.jpg")]},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400 and "图片" in r.get_json()["message"]


def test_the_add_endpoint_needs_a_category(wclient):
    client, _root = wclient
    r = client.post(
        "/api/samples/add",
        data={"group": "", "image": [(BytesIO(jpg_bytes()), "a.jpg")]},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400 and "大类" in r.get_json()["message"]


def test_mkdir_makes_an_empty_shelf_visible(wclient):
    client, root = wclient
    assert client.post("/api/samples/mkdir", json={"group": "男装", "sub": "裤子"}).status_code == 200
    assert (root / "男装/裤子").is_dir()
    # 空架子在页面上要有，但 list_samples 报的是有图的节 —— 所以这里问磁盘，页面另有 sections 字段
    body = client.get("/api/samples").get_json()
    male = next(g for g in body["groups"] if g["group"] == "男装")
    assert "裤子" in [s["name"] for s in male["subs"]]


def test_move_renames_a_shelf_and_carries_the_pictures(wclient):
    client, root = wclient
    r = client.post("/api/samples/move", json={"from": "男装/上衣", "to": "男装/T恤"})
    assert r.status_code == 200 and r.get_json()["to"] == "男装/T恤"
    assert "男装/T恤/已有.jpg" in tree_paths(root)


def test_move_onto_something_existing_is_refused(wclient):
    client, root = wclient
    client.post("/api/samples/mkdir", json={"group": "男装", "sub": "裤子"})
    r = client.post("/api/samples/move", json={"from": "男装/上衣", "to": "男装/裤子"})
    assert r.status_code == 400 and (root / "男装/上衣/已有.jpg").is_file()


def test_remove_drops_a_picture(wclient):
    client, root = wclient
    sample_thumb(root, "男装/上衣/已有.jpg")
    assert client.post("/api/samples/remove", json={"path": "男装/上衣/已有.jpg"}).status_code == 200
    assert not (root / "男装/上衣/已有.jpg").exists() and not (root / ".thumbs/男装/上衣/已有.jpg").exists()


def test_remove_refuses_a_shelf_that_still_has_pictures(wclient):
    client, root = wclient
    r = client.post("/api/samples/remove", json={"path": "男装/上衣"})
    assert r.status_code == 400 and "还有" in r.get_json()["message"]
    assert (root / "男装/上衣/已有.jpg").is_file()


@pytest.mark.parametrize("endpoint,payload", [
    ("/api/samples/move", {"from": "../../etc", "to": "男装/x"}),
    ("/api/samples/move", {"from": "男装/上衣", "to": "../escape"}),
    ("/api/samples/remove", {"path": "../../etc/passwd"}),
    ("/api/samples/remove", {"path": ".thumbs"}),
    ("/api/samples/mkdir", {"group": "../外面"}),
])
def test_no_write_endpoint_will_step_outside_the_library(wclient, endpoint, payload):
    client, root = wclient
    r = client.post(endpoint, json=payload)
    assert r.status_code == 400, (endpoint, r.get_json())
    assert not (root.parent / "escape").exists()


def test_the_write_endpoints_need_the_passphrase(tmp_path, lib):
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
    client = app.test_client()
    for endpoint in ("/api/samples/add", "/api/samples/mkdir", "/api/samples/move", "/api/samples/remove"):
        assert client.post(endpoint, json={}).status_code == 401, endpoint


def test_the_management_page_is_a_page_not_a_picture(wclient):
    """页面 URL 只能是一段：static/… 和 api/… 这些相对地址靠它解析，写成 /samples/manage 就指到 /samples/static/… 去了。"""
    client, _root = wclient
    r = client.get("/samples-manage")
    assert r.status_code == 200 and b"sample" in r.data.lower()
