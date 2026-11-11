"""spec 040：营销内容识别与召回术语（用户定的「先查记忆」闸门）。

覆盖：链接轰炸/图片刷屏/营销词三种判据，以及「术语抽取不含模板词」。
"""

from __future__ import annotations

from memory.services import marketing as M


def test_url_bomb_is_marketing() -> None:
    text = "商品清单\n" + "\n".join(
        f"款{i}：https://item.taobao.com/item.htm?ft=t&id={1000+i}" for i in range(10)
    )
    hit, detail = M.looks_like_marketing(text)
    assert hit
    assert detail["url_ratio"] > 0.1


def test_image_flood_is_marketing() -> None:
    text = "【本日开启尾款】\n" + "\n".join(
        f"[图片] {i:032X}.png" for i in range(8)
    )
    hit, detail = M.looks_like_marketing(text)
    assert hit
    assert detail["image_lines"] >= 4


def test_keyword_marketing() -> None:
    text = "本日上新\n尾款链接如下\n拍下备注\n"
    hit, detail = M.looks_like_marketing(text)
    assert hit
    assert "上新" in detail["marketing_words"]


def test_normal_chat_not_marketing() -> None:
    for text in (
        "这个键帽配色挺好看的，链接发我一下",   # 只有「链接」一个词
        "明天有空吗，一起看看那个展",
        "尾款已经付了，别催了",              # 只有「尾款」一个词
    ):
        hit, _ = M.looks_like_marketing(text)
        assert not hit, text


def test_recall_terms_drops_templates_and_urls() -> None:
    text = (
        "【本日开启尾款】鬼灭之刃 无限城篇 cafe3期\n"
        "相卡抱盒：https://item.taobao.com/item.htm?ft=t&id=995049908691\n"
        "[图片] 926908EA82E9BCD336DAC1EE4DCB8706.png"
    )
    terms = M.recall_terms(text)
    assert "鬼灭之刃" in terms
    assert "无限城篇" in terms
    assert all("http" not in t for t in terms)
    assert "尾款" not in terms  # 模板词不参与查询


def test_recall_terms_empty_for_pure_ads() -> None:
    text = "https://a.com/x\n[图片] ABCDEF0123456789ABCDEF0123456789.png"
    assert M.recall_terms(text) == []


# ------------------------------------------------------------ 召回闸门


def _import_chat():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "import_chat_mod", "/mnt/e/PycharmProjects/amarantine/harness/import_chat.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _turn(text: str) -> dict:
    return {"text": text, "ts": "2026-05-01 10:00:00"}


def test_gate_keeps_marketing_related_to_memory() -> None:
    """广告里的品牌在已有记忆里出现过 → 保留（用户确实在关注）。"""
    mod = _import_chat()
    turns = [_turn("【本日开启尾款】鬼灭之刃 无限城篇 https://item.taobao.com/item.htm?id=1")]
    dropped: list[dict] = []
    kept, stats = mod.gate_marketing(turns, lexicon="以前聊过 鬼灭之刃 的剧场版", dropped=dropped)
    assert stats["marketing"] == 1
    assert stats["kept"] == 1 and stats["dropped"] == 0
    assert kept


def test_gate_drops_unrelated_marketing() -> None:
    """记忆里没有的品牌 → 丢掉。"""
    mod = _import_chat()
    turns = [_turn("【本日开启尾款】排球少年 和纸风徽章 https://item.taobao.com/item.htm?id=2")]
    dropped: list[dict] = []
    kept, stats = mod.gate_marketing(turns, lexicon="反洗钱系统接口字段", dropped=dropped)
    assert stats["dropped"] == 1
    assert not kept
    assert dropped[0]["reason"] == "marketing_unrelated"


def test_gate_drops_marketing_without_terms() -> None:
    """纯链接/图片哈希（抽不出术语）→ 丢。"""
    mod = _import_chat()
    turns = [_turn("https://a.com/x\n[图片] ABCDEF0123456789ABCDEF0123456789.png")]
    dropped: list[dict] = []
    kept, stats = mod.gate_marketing(turns, lexicon="随便什么记忆", dropped=dropped)
    assert not kept
    assert dropped[0]["reason"] == "marketing_no_terms"


def test_gate_keeps_non_marketing_untouched() -> None:
    """普通聊天不参与闸门，原样保留。"""
    mod = _import_chat()
    turns = [_turn("这个键帽配色挺好看的")]
    dropped: list[dict] = []
    kept, stats = mod.gate_marketing(turns, lexicon="", dropped=dropped)
    assert kept == turns
    assert stats["marketing"] == 0
