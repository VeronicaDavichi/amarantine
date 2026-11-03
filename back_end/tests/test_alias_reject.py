"""别名声明的「裁决 + 留痕」链路。

覆盖三件事，每件都对应一个曾经真实发生过的静默失败：

1. **裁决带理由** —— 判据不再只回答「过/不过」，被拒时留下 `reason`
   （`印度人`/`科学家` 两次误判都是因为「不过」不留痕才藏了两个会话）。
2. **两侧都留痕** —— 左值被拒（连 claim 都不生成）和右值被拒
   （claim 在卡上、建边时跳过）都要落 `alias_reject`。
3. **罕见姓豁免** —— jieba 的 `nr` 词条 + 用户声明的 `姓X`，
   让 `种师道` 这类名字不再被量词判据误杀。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from memory.config import Settings
from memory.services import graph_outbox as GO
from memory.services.alias_reject import AliasRejectQueue, queue_from_settings
from memory.services.graph_outbox import OutboxResult
from memory.services.labeler import (
    NAME_ENDING,
    NAME_LEADING,
    NAME_MARKER,
    NAME_OK,
    NAME_TOO_LONG,
    _is_clean_name,
    _judge_name,
    declared_surnames,
    extract_alias_claims_verbose,
    label_by_rules,
    parse_surname_declarations,
    register_surnames,
    reset_surnames,
)
from memory.services.remember import remember_event
from memory.services.surname_decl import SurnameRegistry, load_into_labeler
from memory.services.weaviate_store import to_properties

NOW = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 3)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.MEMORY_NAMESPACE = "work"
    s.DATABASE_URL = f"sqlite:///{(tmp_path / 'q.sqlite3').as_posix()}"
    s.EMBEDDING_DIM = 8
    return s


# ---------------- 1. 裁决带理由 ----------------


@pytest.mark.parametrize(
    "name,reason,detail",
    [
        ("", "empty", ""),
        ("这个特别长的东西名称", NAME_TOO_LONG, "10"),
        ("那个东西", NAME_MARKER, "那个"),
        ("这种人", NAME_MARKER, "这种"),
        ("最近", NAME_LEADING, "最"),
        ("个老实人", NAME_LEADING, "个"),
        ("三哥主打的", NAME_ENDING, "的"),
        ("豆包", NAME_OK, ""),
    ],
)
def test_judge_name_explains_why(name: str, reason: str, detail: str) -> None:
    """每一种拒绝都要说得出「被什么拒的」。"""
    verdict = _judge_name(name)
    assert verdict.reason == reason, f"{name} 的理由该是 {reason}"
    assert verdict.detail == detail, f"{name} 的证据该是 {detail}"
    assert verdict.ok is (reason == NAME_OK)


def test_judge_name_agrees_with_clean_name() -> None:
    """`_is_clean_name` 只是壳，两者绝不能不一致。"""
    for name in ["豆包", "三哥", "印度人", "那个东西", "最近", "", "个老实人"]:
        assert _is_clean_name(name) is _judge_name(name).ok, name


# ---------------- 2. 两侧都留痕 ----------------


def test_left_side_reject_is_reported_not_dropped() -> None:
    """左值被拒时原来直接 `continue` —— 连 claim 都不生成，最隐形。

    `三哥主打的就是有声有色有味` 左侧以「的」结尾，是句子片段。
    """
    claims, rejects = extract_alias_claims_verbose("三哥主打的就是有声有色有味")
    assert claims == []
    assert len(rejects) == 1
    r = rejects[0]
    assert (r.alias, r.side) == ("三哥主打的", "alias")
    assert r.reason == NAME_ENDING
    assert r.detail == "的"


def test_left_side_pronoun_reject_is_reported() -> None:
    _claims, rejects = extract_alias_claims_verbose(
        "我刚才说的那个东西就是我之前提的那个"
    )
    sides = {r.side for r in rejects}
    assert sides == {"alias"}
    assert all(r.reason != NAME_OK for r in rejects)


def test_verbose_matches_plain_for_good_input() -> None:
    """正常句子：verbose 和普通版给一样的 claim，不额外产噪声。"""
    text = "糖包就是豆包"
    claims, rejects = extract_alias_claims_verbose(text)
    assert [(c.alias, c.canonical) for c in claims] == [("糖包", "豆包")]
    assert rejects == []


def test_partition_skips_and_explains() -> None:
    """`_partition_alias_claims` 是两条建边路径共用的唯一切分点。"""
    from memory.models.event_card import AliasClaim

    claims = [
        AliasClaim(alias="老王", canonical="豆包", label="exact"),
        AliasClaim(alias="糖包", canonical="那个东西", label="uncertain"),
    ]
    items, skipped = GO._partition_alias_claims(claims)
    assert [i["canonical"] for i in items] == ["豆包"]
    assert len(skipped) == 1
    assert skipped[0].canonical == "那个东西"
    assert skipped[0].reason == NAME_MARKER
    assert skipped[0].detail == "那个"
    # `side` 写进留痕表时要明确是右值
    assert skipped[0].to_dict()["side"] == "canonical"


# ---------------- 3. 罕见姓豁免 ----------------


def test_rare_surname_from_jieba_dictionary() -> None:
    """`种师道` 在 jieba 词典里是 `nr` —— 免费救回来。"""
    assert _is_clean_name("种师道"), "jieba 认得的人名不该被量词判据拦掉"


def test_measure_word_must_itself_be_a_surname() -> None:
    """豁免的第一道闸：量词字本身得是姓氏。

    实测 jieba 的 HMM 会把 `位小明` 也标成 `nr`（切分器对新词很激进），
    没有这层过滤，豁免就变成「量词后面跟个像名字的就行」，
    `位小明` 这类半截短语会全部放进来。
    """
    assert not _is_clean_name("位小明"), "`位` 不是姓氏，不该豁免"
    assert _judge_name("位小明").reason == NAME_LEADING
    assert _is_clean_name("种师道"), "`种` 是姓氏，该豁免"


@pytest.mark.parametrize("junk", ["种人", "种老师", "个老实人", "位小明"])
def test_measure_word_fragments_still_rejected(junk: str) -> None:
    """豁免要窄：只有 jieba 认得的整串人名才放过。"""
    verdict = _judge_name(junk)
    assert not verdict.ok
    assert verdict.reason == NAME_LEADING


@pytest.mark.parametrize(
    "text,expected",
    [
        ("这人姓种", ["种"]),
        ("姓种，叫种师道", ["种"]),
        ("姓名", []),            # 「名」是汉字，不是姓氏声明
        ("姓氏", []),
        ("老百姓", []),
        ("百家姓", []),
        ("我叫张伟", []),
    ],
)
def test_parse_surname_declarations(text: str, expected: list[str]) -> None:
    """`姓` 是高频构词字，只认句末/标点前的形式。"""
    assert parse_surname_declarations(text) == expected


def test_declared_surname_exempts_measure_word() -> None:
    """声明过「个」是姓之后，jieba 不认识的 `个小红` 也能过。

    用 `个` 而不是 `种` 是因为 jieba 的 HMM 本来就把 `种小明` 标成 `nr`
    —— 那样测不到「用户声明」这条路径。`个小红` 实测是三个词
    （`个/m`+`小/a`+`红/a`），只能靠声明救。
    """
    assert not _is_clean_name("个小红"), "没声明前该被拦（jieba 不认识）"
    register_surnames(["个"])
    assert _is_clean_name("个小红"), "声明后该放行"
    reset_surnames()
    assert not _is_clean_name("个小红")


def test_declared_surname_needs_three_chars() -> None:
    """声明过姓氏，但只有 2 个字时仍然不豁免。

    `个明` 是 `个/m`+`明/t`，不是人名整串。2 个字判不准
    （也可能是「量词+名词」），所以要求「姓 + 名≥2」。
    不豁免不等于丢 —— 它会进 `alias_reject`，看得见、能补。
    """
    register_surnames(["个"])
    assert not _is_clean_name("个明")
    assert _judge_name("个明").reason == NAME_LEADING


def test_surname_declaration_takes_effect_within_one_sentence() -> None:
    """同一句里先声明后使用，要立刻生效。

    `label_by_rules` 的顺序是 parse → register → extract，
    所以 `种小明` 在抽取别名时已经知道「种」是姓。
    """
    result = label_by_rules("这人姓种，种小明就是老种")
    assert result.surnames == ["种"]
    pairs = [(c.alias, c.canonical) for c in result.alias_claims]
    assert ("种小明", "老种") in pairs, f"声明没在同一句里生效: {pairs}"


# ---------------- 4. 留痕表 ----------------


def test_alias_reject_store_roundtrip(settings: Settings) -> None:
    q = queue_from_settings(settings)
    n = q.record_many(
        "work",
        "evt_1",
        [
            {"alias": "三哥主打的", "canonical": "有声有色有味",
             "side": "alias", "reason": NAME_ENDING, "detail": "的"},
            {"alias": "糖包", "canonical": "那个ai中最弱智的",
             "side": "canonical", "reason": NAME_MARKER, "detail": "那个"},
        ],
        source_text="原文",
    )
    assert n == 2
    assert q.count_pending("work") == 2
    assert q.stats("work") == {NAME_ENDING: 1, NAME_MARKER: 1}

    items = q.list_pending("work", limit=10)
    assert {i.side for i in items} == {"alias", "canonical"}
    assert items[0].source_text == "原文"

    only_marker = q.list_pending("work", reason=NAME_MARKER)
    assert [i.alias for i in only_marker] == ["糖包"]

    assert q.resolve(
        "work", event_id="evt_1", alias="糖包",
        canonical="那个ai中最弱智的", side="canonical",
    )
    assert q.count_pending("work") == 1
    assert q.stats("work") == {NAME_ENDING: 1}


def test_alias_reject_repeat_clears_resolution(settings: Settings) -> None:
    """判据可能已经改了 —— 重复上报要清掉旧的「已处理」。"""
    q = queue_from_settings(settings)
    row = {"alias": "a", "canonical": "b", "side": "alias",
           "reason": NAME_ENDING, "detail": "的"}
    q.record_many("work", "evt_1", [row])
    q.resolve("work", event_id="evt_1", alias="a", canonical="b", side="alias")
    assert q.count_pending("work") == 0
    q.record_many("work", "evt_1", [row])
    assert q.count_pending("work") == 1


def test_alias_reject_store_is_isolated_per_namespace(settings: Settings) -> None:
    q = queue_from_settings(settings)
    q.record_many("work", "evt_1", [
        {"alias": "a", "canonical": "b", "side": "alias",
         "reason": NAME_ENDING, "detail": "的"}
    ])
    assert q.count_pending("home") == 0


def test_record_safe_swallows_errors(settings: Settings) -> None:
    """留痕是旁路：它挂了绝不能让记忆写不进去。"""
    from memory.services.alias_reject import record_safe

    class Boom:
        def record_many(self, *a, **kw):
            raise RuntimeError("disk full")

    assert record_safe(
        settings, namespace="work", event_id="evt_1",
        rejects=[{"alias": "a", "canonical": "b", "side": "alias",
                  "reason": NAME_ENDING, "detail": "的"}],
        queue=Boom(),  # type: ignore[arg-type]
    ) == 0


# ---------------- 5. 姓氏表 ----------------


def test_surname_registry_roundtrip(settings: Settings) -> None:
    from memory.services.surname_decl import registry_from_settings

    reg = registry_from_settings(settings)
    assert reg.declare("work", ["种"], evidence="这人姓种", event_id="evt_1") == 1
    assert reg.declare("work", ["种"], evidence="又说了一遍") == 0
    active = reg.list_active("work")
    assert [d.char for d in active] == ["种"]
    assert active[0].evidence == "又说了一遍"

    assert reg.revoke("work", "种")
    assert reg.list_active("work") == []
    assert reg.revoke("work", "种") is False


def test_surname_loads_into_labeler(settings: Settings) -> None:
    from memory.services.surname_decl import registry_from_settings

    reg = registry_from_settings(settings)
    reg.declare("work", ["种"])
    reset_surnames()
    assert declared_surnames() == frozenset()
    assert load_into_labeler(settings, registry=reg) == 1
    assert "种" in declared_surnames()
    assert _is_clean_name("种小明")


def test_surname_registry_uses_its_own_db_path(tmp_path) -> None:
    """表要落在 DATABASE_URL 指的地方，不能写到开发库。"""
    reg = SurnameRegistry(tmp_path / "sub" / "x.sqlite3")
    reg.declare("work", ["种"])
    assert (tmp_path / "sub" / "x.sqlite3").exists()
    assert AliasRejectQueue(tmp_path / "sub" / "y.sqlite3") is not None


# ---------------- 6. remember 集成 ----------------


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict = {}

    def ensure_collection(self) -> None:
        pass

    def get_by_event_id(self, namespace, event_id):
        return self.rows.get((namespace, event_id))

    def insert_event(self, card, vector, *, anomaly=None) -> str:
        self.rows[(card.namespace, card.event_id)] = to_properties(
            card, anomaly=anomaly
        )
        return f"uuid-{card.event_id}"

    def close(self) -> None:
        pass


class FakeCounter:
    def count_pinned(self, namespace, *, now):
        return 0

    def record_pinned(self, namespace, *, now):
        pass


def _embedder(text, settings):
    return [0.01] * settings.EMBEDDING_DIM


def _graph(card, *, settings, weaviate_id, context=None):
    """真实的切分逻辑，假的 driver —— 只为了拿到 `alias_skipped`。"""
    items, skipped = GO._partition_alias_claims(list(card.alias_claims))
    return OutboxResult(
        ok=True,
        aliases=len(items),
        alias_skipped=tuple(s.to_dict() for s in skipped),
    )


def _remember(settings: Settings, summary: str):
    return remember_event(
        namespace="work",
        summary=summary,
        settings=settings,
        store=FakeStore(),
        counter=FakeCounter(),
        embedder=_embedder,
        graph_writer=_graph,
        now=NOW,
        today=TODAY,
    )


def test_remember_records_left_side_reject(settings: Settings) -> None:
    """左侧被拒的声明要真的落到表里，否则 LLM 复核层永远看不到。"""
    _remember(settings, "三哥主打的就是有声有色有味")
    q = queue_from_settings(settings)
    items = q.list_pending("work")
    assert items, "左值被拒却没有任何留痕"
    assert items[0].side == "alias"
    assert items[0].reason == NAME_ENDING
    assert items[0].source_text is not None


def test_remember_records_right_side_skip(settings: Settings) -> None:
    """右侧被拒（claim 在卡上、边没建）也要留痕。"""
    _remember(settings, "糖包就是这种人")
    q = queue_from_settings(settings)
    items = q.list_pending("work")
    assert items, "右值被跳过却没有任何留痕"
    assert items[0].side == "canonical"
    assert items[0].canonical == "这种人"
    assert items[0].reason == NAME_MARKER
    assert items[0].detail == "这种"


def test_remember_records_overlong_canonical(settings: Settings) -> None:
    """描述性长句按长度拒 —— 顺序是长度→标记→开头→结尾。

    注意 `_strip_prefix` 会先削掉 `那个`/`这个`/`我说的`，
    所以 `那个ai中最弱智的` 到这里已经是 `ai中最弱智的`（7 字）。
    """
    _remember(settings, "糖包就是那个ai中最弱智的")
    items = queue_from_settings(settings).list_pending("work")
    assert items
    assert items[0].canonical == "ai中最弱智的"
    assert items[0].reason == NAME_TOO_LONG
    assert items[0].detail == "7"


def test_remember_records_surname_declaration(settings: Settings) -> None:
    _remember(settings, "这人姓种")
    from memory.services.surname_decl import registry_from_settings

    active = registry_from_settings(settings).list_active("work")
    assert [d.char for d in active] == ["种"]


def test_remember_clean_alias_records_nothing(settings: Settings) -> None:
    """正常声明不该在留痕表里留噪声 —— 否则这张表会被淹掉。"""
    _remember(settings, "糖包就是豆包")
    assert queue_from_settings(settings).count_pending("work") == 0
