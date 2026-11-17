"""spec 039 全量落地：噪声过滤 + 端点解析（2026-11-17 实测出来的两条缺口）。"""

from __future__ import annotations

from memory.services import relations as R


def test_media_placeholder_tail_rejected() -> None:
    """`运营 -OWNS-> 图片` 是噪声：图片只是附件形式，不是维护对象。"""
    for tail in ("图片", "信息", "消息", "内容", "附件"):
        reason = R.is_junk_relation("运营", tail)
        assert reason, f"{tail} 应被拒"
        assert "媒体占位" in reason


def test_action_head_rejected() -> None:
    """`训练 -OWNS-> 音频文件`：主语是动作，不是行为主体。"""
    reason = R.is_junk_relation("训练", "音频文件")
    assert reason and "动作词" in reason


def test_action_word_as_tail_is_allowed() -> None:
    """**尾值**允许动作词（「部署＝那次部署」，spec 039 语义定义）。

    这条是反悔保护：有人会把 `_ACTION_HEAD_DENY` 顺手用到尾值上，
    那样 `大肥 -OWNS-> 部署` 会被杀掉 —— 而它是人工核对通过的。
    """
    assert R.is_junk_relation("大肥鱼", "部署") == ""
    assert R.is_junk_relation("远望", "训练") == ""


def test_valid_relation_not_junk() -> None:
    for head, tail in (("运营", "商品"), ("传讯员8号", "部屋"),
                       ("克劳德", "门牌号"), ("王婕", "Oracle")):
        assert R.is_junk_relation(head, tail) == "", (head, tail)


def test_validate_uses_junk_filter() -> None:
    ok, reason = R.validate_relation(
        "运营", "图片", 4,
        candidates=["运营", "图片"],
        text="运营发布商品图片",
        evidence="运营发布商品图片",
    )
    assert not ok
    assert "媒体占位" in reason
