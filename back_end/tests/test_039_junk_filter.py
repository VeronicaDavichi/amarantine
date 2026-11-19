"""spec 039 全量落地：噪声过滤 + 端点解析（2026-11-17 实测出来的两条缺口）。"""

from __future__ import annotations

from memory.services import relations as R


def test_action_head_rejected() -> None:
    """`训练 -OWNS-> 音频文件`：主语是动作，不是行为主体。"""
    reason = R.is_junk_relation("训练", "音频文件")
    assert reason and "动作词" in reason


def test_media_tail_is_allowed() -> None:
    """**媒体名词做尾值是允许的** —— 人工标注判定为对。

    反悔保护：`kk -OWNS-> 图片`、`传讯员8号 -MAINTAINS-> 信息`、
    `安澜 -OWNS-> 视频` 在 2026-11-17 的 20 条机器边核对里全部标为 ok
    （用户语义：发布某物的人负责维护该物的信息）。
    代码单方面把它们过滤掉就会与标注冲突。
    """
    for head, tail in (("kk", "图片"), ("传讯员8号", "信息"),
                       ("传讯员8号", "内容"), ("安澜", "视频")):
        assert R.is_junk_relation(head, tail) == "", (head, tail)


def test_action_word_as_tail_is_allowed() -> None:
    """**尾值**允许动作词（「部署＝那次部署」，spec 039 语义定义）。"""
    assert R.is_junk_relation("大肥鱼", "部署") == ""
    assert R.is_junk_relation("远望", "训练") == ""


def test_empty_tail_rejected() -> None:
    assert R.is_junk_relation("kk", "") != ""


def test_valid_relation_not_junk() -> None:
    for head, tail in (("运营", "商品"), ("传讯员8号", "部屋"),
                       ("克劳德", "门牌号"), ("王洁", "Oracle")):
        assert R.is_junk_relation(head, tail) == "", (head, tail)


def test_validate_rejects_action_head() -> None:
    ok, reason = R.validate_relation(
        "训练", "音频文件", 4,
        candidates=["训练", "音频文件"],
        text="训练维护音频文件",
        evidence="训练维护音频文件",
    )
    assert not ok
    assert "动作词" in reason
