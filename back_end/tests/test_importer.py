"""spec 040：导入工具的纯逻辑（规范化 / 隐私 / 切窗 / 幂等键 / 事件时间）。"""

from __future__ import annotations

import pytest

from memory.services import importer as IM


# ------------------------------------------------------------ 规范化


def test_link_share_keeps_source_url() -> None:
    """坑 #1：`contentType=102` 只取标题会丢地址 —— GitLab 就这样丢过。"""
    raw = {
        "type": 102,
        "content": {
            "contentType": 102,
            "attachments": [{
                "extension": '{"title": "分享链接", "source_url": "http://git.example/x"}',
            }],
        },
    }
    text, reason = IM.normalize_message(raw)
    assert reason == ""
    assert "http://git.example/x" in text


def test_image_message_dropped() -> None:
    """图片没有正文，进不来。"""
    text, reason = IM.normalize_message({"type": 2, "content": {"contentType": 2}})
    assert text is None
    assert "type2" in reason


def test_quote_reply_takes_title() -> None:
    """引用回复取 `#### ` 后的正文（extension.title）。"""
    raw = {
        "type": 1200,
        "content": {
            "contentType": 1200,
            "attachments": [{"extension": '{"title": "不对"}'}],
        },
    }
    assert IM.normalize_message(raw)[0] == "不对"


def test_rich_text_takes_desc() -> None:
    raw = {
        "type": 3100,
        "content": {
            "contentType": 3100,
            "attachments": [{"extension": '{"desc": "这个接口要加个字段"}'}],
        },
    }
    assert IM.normalize_message(raw)[0] == "这个接口要加个字段"


# ------------------------------------------------------------ 隐私闸门


@pytest.mark.parametrize(
    "text,expect",
    [
        ("身份证 320102199001011234", "身份证号"),
        ("电话 13851265402", "手机号"),
        ("卡号 6222021234567890123", "银行卡号"),
        ("用户名 jk_aicj 密码：szxjk4.0", "口令"),
        ("验证码 213836", "2FA"),
    ],
)
def test_privacy_hits(text: str, expect: str) -> None:
    """敏感信息必须被拦下，且**说得出类别**。"""
    assert expect in IM.privacy_hits(text)


def test_id_card_not_double_reported_as_bank_card() -> None:
    """18 位身份证同时满足卡号规则 —— 只报更具体的那个。"""
    assert IM.privacy_hits("身份证 320102199001011234") == ["身份证号"]


def test_normal_text_has_no_hits() -> None:
    assert IM.privacy_hits("前端页面我改好了") == []


# ------------------------------------------------------------ 切窗与幂等键


def test_chunk_turns_splits_evenly() -> None:
    turns = [{"ts": f"2026-10-{i:02d}"} for i in range(1, 11)]
    chunks = IM.chunk_turns(turns, 3)
    assert [len(c) for c in chunks] == [3, 3, 3, 1]


def test_chunk_turns_zero_means_whole() -> None:
    turns = [{"ts": "2026-10-01"}] * 5
    assert len(IM.chunk_turns(turns, 0)) == 1


def test_dedupe_key_is_unique_per_chunk() -> None:
    """同一窗口切两片，键必须不同（否则第二片被幂等吞掉）。"""
    day = [{"ts": "2026-10-30 09:00:00"}]
    k1 = IM.dedupe_key_for("cid1", day, 1)
    k2 = IM.dedupe_key_for("cid1", day, 2)
    assert k1 != k2
    assert "2026-10-30" in k1
    assert k1.endswith("c01")


def test_dedupe_key_is_deterministic() -> None:
    """同样的输入必须给同样的键 —— 续跑靠它。"""
    day = [{"ts": "2026-10-30 09:00:00"}, {"ts": "2026-10-30 18:00:00"}]
    assert IM.dedupe_key_for("c", day, 3) == IM.dedupe_key_for("c", day, 3)


# ------------------------------------------------------------ 事件时间（A6/A7）


def test_event_date_inherited_from_source_turn() -> None:
    """A6：事件继承了源消息的日期。"""
    turns = [{"text": "任泽安：主表的人工报备情况没有展示", "ts": "2026-10-30 14:50:00"}]
    dates = IM.assign_event_dates(["任泽安指出主表的人工报备情况没有展示"], turns)
    assert dates == ["2026-10-30"]


def test_event_date_none_when_unmatched() -> None:
    """A7：判不出就是 None，**绝不回落到今天**。"""
    turns = [{"text": "任泽安：主表的人工报备情况没有展示", "ts": "2026-10-30 14:50:00"}]
    dates = IM.assign_event_dates(["欧盟和联合国哪个更强"], turns)
    assert dates == [None]


def test_event_dates_length_matches_parts() -> None:
    turns = [{"text": "甲方说接口要加字段", "ts": "2026-10-29 10:00:00"}]
    dates = IM.assign_event_dates(["甲方说接口要加字段", "另一件事"], turns)
    assert len(dates) == 2
