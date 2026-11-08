"""037：拉丁缩写抽取通道（`_ACRONYM` + 停用词 + hex 过滤）。

反例用例（jpg / hex / m_fileName / 名册人名）不是拍脑袋写的 ——
全部取自本机 500 张真实 QQ 摘要卡的实际词形，见 spec 037 的进展记录。
"""

from __future__ import annotations

import pytest

from memory.services.labeler import (
    extract_entities,
    register_roster,
    reset_roster,
)


@pytest.fixture(autouse=True)
def _clean_roster():
    reset_roster()
    yield
    reset_roster()


def names(text: str) -> list[str]:
    return [e.name for e in extract_entities(text)]


# ---------------- 正例：该进图的专有名词 ----------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ESB接口返回字段的数据字典", "ESB"),
        ("任泽安让王洁有空看一下群里ESB查询手机号的需求", "ESB"),
        ("用 PyCharm 调一下", "PyCharm"),
        ("Kafka 那边堆积了", "Kafka"),
        ("MQ 没配好", "MQ"),
        ("CIF 字段对不上", "CIF"),
    ],
)
def test_proper_nouns_become_topic(text: str, expected: str) -> None:
    entities = extract_entities(text)
    hit = [e for e in entities if e.name == expected]
    assert hit, f"{expected} 没被抽出，实际={[e.name for e in entities]}"
    assert hit[0].kind == "topic"


# ---------------- 反例：停用词 / 小写 / hex / 文件元数据 ----------------


@pytest.mark.parametrize(
    ("text", "banned"),
    [
        ("API网关限制了模型大小", "API"),
        ("SQL 报错", "SQL"),
        ("JSON 解析失败", "JSON"),
        ("HTTP 500", "HTTP"),
        ("and 然后呢", "and"),
        ("esb 查不到", "esb"),
        ("3217017791发送图片50550779A7B77451877B7562B4FFD918.jpg", "jpg"),
        ("423124659发了一张图片 5FDEB3AD54E7483E04A9F6F48389E8D3.png", "png"),
        ("逐风之旅发送图片FDA74A63E3E67CFA8489F9B7570ABEAE.jpeg", "jpeg"),
        ("505456846发送文件0ed9921f795c09f3aab10a4cc49f8a29.amr", "amr"),
        ("逐风之旅发送聊天记录（m_fileName=a4adda39-682b-4178-a4c8-db44d2d48145）", "m_fileName"),
        ("图片文件 {252FEE87-F439-2708-1940-B8BB6E6F5281}", "F439"),
        ("图片文件 {252FEE87-F439-2708-1940-B8BB6E6F5281}", "B8BB6E6F5281"),
        # 全小写的系统名也会被拒 —— 这是 spec §6 明确的取舍（收纯小写 → 噪声不可控）。
        # 真实语料里 `dsh-music-insight` 就属于这类「宁可漏」的情况。
        ("dsh-music-insight 有情绪", "dsh-music-insight"),
    ],
)
def test_noise_tokens_are_rejected(text: str, banned: str) -> None:
    got = names(text)
    assert banned not in got, f"{banned} 不该进图，实际={got}"


# ---------------- 去重：不与「项目+编号」重复 ----------------


def test_coded_pattern_wins_over_acronym() -> None:
    """`项目AB` 整条已收，不能再拆出 `AB`（区间重叠也要挡）。"""
    got = names("项目AB 的系统日志")
    assert "项目AB" in got
    assert "AB" not in got


def test_project_a_unchanged() -> None:
    """不变行为：`项目A` 仍是单条 topic，形态不变。"""
    got = names("项目A 的系统日志")
    assert "项目A" in got
    assert "A" not in got


# ---------------- 名册里的人名不能被建成 topic ----------------


def test_roster_person_is_not_turned_into_topic() -> None:
    """`Veronica Davichi` 是发言人（Person），不能额外冒出一个 topic `Veronica`。"""
    register_roster(["Veronica Davichi", "Kyoma"])
    entities = extract_entities("Veronica Davichi 说：有 token 的时候不需要")

    kinds = {e.name: e.kind for e in entities}
    assert kinds.get("Veronica Davichi") == "person"
    assert "Veronica" not in kinds
    assert "Davichi" not in kinds


def test_lowercase_name_from_roster_still_person() -> None:
    """名册里的小写英文名也要认，不因为「纯小写」被 037 的判据挡掉 —— 它走名册通道。"""
    register_roster(["kyoma"])
    entities = extract_entities("kyoma 说：100 块瞬间融化是吧")
    kinds = {e.name: e.kind for e in entities}
    assert kinds.get("kyoma") == "person"
