"""chat 路由 _frame_media 媒体框注入单测。"""

from xuwen.chat_api.routes.chat import _frame_media


def test_frame_empty_text():
    assert _frame_media("", "（对方发来一段语音：你好）") == "（对方发来一段语音：你好）"


def test_frame_whitespace_text():
    assert _frame_media("   ", "（对方发来一段语音：你好）") == "（对方发来一段语音：你好）"


def test_frame_replaces_non_text_placeholder():
    assert _frame_media("[非文本消息]", "（对方发来一段语音：你好）") == "（对方发来一段语音：你好）"


def test_frame_replaces_non_text_placeholder_with_space():
    assert (
        _frame_media("[非文本消息]  ", "（对方发来一段语音：你好）")
        == "（对方发来一段语音：你好）"
    )


def test_frame_appends_to_real_text():
    assert (
        _frame_media("在的", "（对方发来一段语音：你好）")
        == "在的\n（对方发来一段语音：你好）"
    )


def test_frame_appends_after_image_desc():
    desc = "（对方发来一张图：一只猫）"
    voice = "（对方发来一段语音：看看）"
    assert (
        _frame_media(_frame_media("[非文本消息]", desc), voice)
        == "（对方发来一张图：一只猫）\n（对方发来一段语音：看看）"
    )
