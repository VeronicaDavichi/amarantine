"""040：合并转发（viewMultiMsg）解析与不丢弃。"""

from __future__ import annotations

from memory.services.importer import normalize_message, parse_merged_forward


def test_parse_brief_only() -> None:
    xml = ('<?xml version=\'1.0\' encoding=\'UTF-8\' standalone="yes"?> '
           '<msg serviceID="35" templateID="1" action="viewMultiMsg" brief="[聊天记录]" '
           'm_fileName="8ca82bef-debd-4783-8aa2-dd8a1ee36e90" m_resid="SWhan7iJ" />')
    got = parse_merged_forward(xml)
    assert got == "[合并转发] [聊天记录]"


def test_parse_title_and_summary() -> None:
    xml = ('<?xml version="1.0"?><msg action="viewMultiMsg" brief="[聊天记录]">'
           "<title>大肥鱼乐园 的聊天记录</title>"
           "<summary>侯潮是鸭腿老大准备搭的城市</summary></msg>")
    got = parse_merged_forward(xml)
    assert got is not None
    assert "大肥鱼乐园" in got and "侯潮" in got


def test_normal_text_not_mistaken() -> None:
    assert parse_merged_forward("今天吃了火锅") is None
    assert parse_merged_forward("<msg>普通</msg>") is None
    assert parse_merged_forward("") is None


def test_normalize_keeps_merged_forward() -> None:
    """合并转发不能返回 None（以前会被当垃圾丢掉）。"""
    raw = {"contentType": 1,
           "text": '<?xml version="1.0"?><msg action="viewMultiMsg" brief="[聊天记录]"/>'}
    text, reason = normalize_message(raw)
    assert text is not None, reason
    assert text.startswith("[合并转发]")
