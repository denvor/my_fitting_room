"""三个分类：单参考图、双参考图（上衣+裤子）、换发型 —— 槽数和指令都在这儿定，前后端共用一份。"""

import pytest

from tryon.prompt import (
    MAX_INPUT_IMAGES,
    OUTFIT_PROMPT,
    REF_SLOTS,
    SLOT_LABELS,
    TRYON_PROMPT,
    VALID_MODES,
    HAIR_PROMPT,
    prompt_for,
)


def test_the_three_categories_in_order():
    """顺序就是首页 tab 的顺序：已经有的那档排第一。"""
    assert VALID_MODES == ("single", "outfit", "hair")


@pytest.mark.parametrize(("mode", "slots"), [("single", 1), ("outfit", 2), ("hair", 1)])
def test_each_mode_knows_how_many_reference_images_it_takes(mode, slots):
    assert REF_SLOTS[mode] == slots
    assert len(SLOT_LABELS[mode]) == slots


def test_no_mode_asks_for_more_images_than_the_api_takes():
    """千问图像编辑系列只吃 1–3 张输入图（官方文档原文），人像也占一张。"""
    for mode in VALID_MODES:
        assert 1 + REF_SLOTS[mode] <= MAX_INPUT_IMAGES == 3


def test_single_prompt_is_untouched():
    """单件这条是量出来的质量基线（README 耗时表、规格 §6.6），加分类不许顺手改它。"""
    assert prompt_for("single") is TRYON_PROMPT


@pytest.mark.parametrize("mode", ["outfit", "hair"])
def test_every_prompt_names_the_slots_it_has(mode):
    """指令里引用的图号必须对得上槽数：说"图像3"却只喂两张，模型就去编一张。"""
    text = prompt_for(mode)
    for n in range(1, 1 + REF_SLOTS[mode] + 1):  # 人像 + 参考图
        assert f"图像{n}" in text
    assert f"图像{1 + REF_SLOTS[mode] + 1}" not in text


def test_outfit_prompt_puts_both_pieces_on_and_keeps_the_person():
    assert "上衣" in OUTFIT_PROMPT and "裤子" in OUTFIT_PROMPT
    assert "脸部五官" in OUTFIT_PROMPT and "背景不变" in OUTFIT_PROMPT
    assert "衣架" in OUTFIT_PROMPT  # 平铺/挂拍图里那些东西要明说忽略


def test_hair_prompt_changes_only_the_hair():
    """换发型最容易连带把脸和衣服改了，所以这两句是这条指令的全部意义。"""
    assert HAIR_PROMPT is not OUTFIT_PROMPT
    assert "发型" in HAIR_PROMPT
    for keep in ("脸部五官", "服装", "背景"):
        assert keep in HAIR_PROMPT


def test_slot_labels_are_what_the_page_shows():
    assert SLOT_LABELS["single"] == ("衣服照片",)
    assert SLOT_LABELS["outfit"] == ("上衣照片", "裤子照片")
    assert SLOT_LABELS["hair"] == ("发型参考",)


def test_unknown_mode_is_said_not_guessed():
    """猜成 single 会让人以为图没生效：宁可直接说这个分类不存在。"""
    with pytest.raises(ValueError, match="分类"):
        prompt_for("hats")
