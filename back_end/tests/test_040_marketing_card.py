"""spec 039/040：营销不做关系判定（用户 2026-11-19）。

用户原话：「营销就不做了，营销我们不判定关系，
只判断自己以前关注的子项有没有产品」。
"""

from __future__ import annotations

import pytest

from memory.services.marketing import looks_like_marketing_card


@pytest.mark.parametrize("text", [
    "运营发布本日扭蛋上新及商品图片",
    "传讯员8号发布明日梦谷小屋定金截单提醒及图片",
    "kk发布新品速递并介绍新网球王子完结纪念周边预约上架",
    "运营发布9月20日截单提醒",
    "传讯员8号发布二店梦谷小部屋今日现货掉落及图片",
])
def test_marketing_cards_detected(text: str) -> None:
    ok, detail = looks_like_marketing_card(text)
    assert ok is True, (text, detail)


@pytest.mark.parametrize("text", [
    "任泽安询问老师们现在是否有空一起线上交流反洗钱智能体的方案",
    "远望打算本地部署小模型来做对话bot",
    "克劳德负责写门牌号并把尘霜记为第二号",
    "乌角鲨最终称没钱养服务器，劝大家别让他实现以免无法debug",
])
def test_normal_cards_not_marketing(text: str) -> None:
    ok, _detail = looks_like_marketing_card(text)
    assert ok is False, text


def test_empty_text() -> None:
    ok, detail = looks_like_marketing_card("")
    assert ok is False
    assert detail["reason"] == "empty"


def test_single_business_word_alone_not_enough() -> None:
    """只有一个业务词、也没有店铺角色 → 不判营销（避免误杀）。

    `上新` 在闲聊里也会出现（「我上新了头像」），单靠一个词会误杀。
    """
    ok, _ = looks_like_marketing_card("我把头像上新了")
    assert ok is False


def test_business_word_plus_seller_detected() -> None:
    ok, detail = looks_like_marketing_card("运营发布截单提醒")
    assert ok is True
    assert detail["hits"] and detail["seller"]
