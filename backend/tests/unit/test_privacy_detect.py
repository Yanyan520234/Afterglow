"""隐私类请求检测（规则层强制静默 + need_owner 提醒）的单元测试。"""

from __future__ import annotations

from xuwen.chat_api.chat_pipeline import detect_privacy_request


def test_voice_requests():
    assert detect_privacy_request("发个语音给我呗") == ["对方想听本人的声音（语音）"]
    assert detect_privacy_request("想听你声音") == ["对方想听本人的声音"]
    assert "对方想和本人语音通话/打电话" in detect_privacy_request("打个电话？")
    assert "对方想听本人说话" in detect_privacy_request("想听你说话")


def test_photo_requests():
    assert detect_privacy_request("发张你的照片看看") == ["对方想要本人照片"]
    assert detect_privacy_request("你本人的照片还有吗") == ["对方想要本人照片"]
    assert detect_privacy_request("发你自拍") == ["对方想要本人自拍"]


def test_privacy_info():
    assert detect_privacy_request("身份证号发我") == ["对方索取本人隐私信息"]


def test_no_false_positive_on_life_photos():
    assert detect_privacy_request("四月的照片发我看看") == []
    assert detect_privacy_request("看看你昨天吃的啥") == []
    assert detect_privacy_request("周末出来吃饭吧") == []
    assert detect_privacy_request("") == []
