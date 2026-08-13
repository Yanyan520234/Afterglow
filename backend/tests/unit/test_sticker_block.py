"""sticker_store.render_sticker_block_for_prompt 单测。"""

from xuwen.chat_api.sticker_store import Sticker, render_sticker_block_for_prompt


def _sticker(name: str, desc: str) -> Sticker:
    return Sticker(
        name=name,
        description=desc,
        sha="0" * 64,
        extension="jpg",
        owner="shared",
        tags=["历史"],
        created_at_ms=0,
    )


def test_block_lists_names_and_descriptions():
    block = render_sticker_block_for_prompt([_sticker("嘿嘿", "得意"), _sticker("狗头", "嫌弃")])
    assert "[sticker:嘿嘿]：得意" in block
    assert "[sticker:狗头]：嫌弃" in block
    assert "全部**可用名字" in block


def test_block_encourages_proactive_use():
    block = render_sticker_block_for_prompt([_sticker("嘿嘿", "得意")])
    assert "偶尔主动" in block
    assert "文字回应优先" in block


def test_block_empty_forbids_stickers():
    block = render_sticker_block_for_prompt([])
    assert "当前没有可用的表情包" in block
    assert "绝对不要" in block
