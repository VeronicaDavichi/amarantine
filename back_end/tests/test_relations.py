"""spec 039：关系谓词（陈述 + 行为双通道）。

覆盖 A1（陈述）、A2（行为聚合）、A3（阈值）、A4（合并不降级）、A5（冲突不合并）。
"""

from __future__ import annotations

from datetime import date

import pytest

from memory.services import relations as R


# ------------------------------------------------------------ 谓词表


def test_reports_to_removed() -> None:
    """`REPORTS_TO` 已删：语料里 0 证据。"""
    assert "REPORTS_TO" not in R.PREDICATE_SPECS


def test_predicate_specs_have_types() -> None:
    for predicate, (head, tail) in R.PREDICATE_SPECS.items():
        assert head in ("Person", "Entity"), predicate
        assert tail in ("Person", "Entity"), predicate


@pytest.mark.parametrize(
    "verb,predicate",
    [("部署", "MAINTAINS"), ("发布", "MAINTAINS"), ("修改", "OWNS"), ("修复", "OWNS")],
)
def test_verb_mapping(verb: str, predicate: str) -> None:
    assert R.predicate_for_verb(verb) == predicate


def test_unknown_verb_not_mapped() -> None:
    """`讨论`/`确认` 说明参与过，不说明负责 —— 不硬凑谓词。"""
    assert R.predicate_for_verb("讨论") is None
    assert R.predicate_for_verb("确认") is None


# ------------------------------------------------------------ A2 行为聚合


def _row(person, verb, target, eid, day="2026-11-01"):
    return {"person": person, "verb": verb, "target": target,
            "event_id": eid, "date": day}


def test_behavior_aggregation_builds_edge() -> None:
    """A2：王泳 3 次部署前端 → MAINTAINS，evidence=3。"""
    rows = [_row("王泳", "部署", "前端", f"e{i}") for i in range(3)]
    claims = R.aggregate_behaviors(rows, threshold=2, window_days=0, today=date(2026, 11, 9))
    assert len(claims) == 1
    claim = claims[0]
    assert (claim.head, claim.predicate, claim.tail) == ("王泳", "MAINTAINS", "前端")
    assert claim.label == "probable"
    assert len(claim.evidence) == 3


def test_behavior_dedupes_same_event() -> None:
    """同一事件重复出现只算一次证据 —— 重放不该把数字刷大。"""
    rows = [_row("王泳", "部署", "前端", "e1") for _ in range(5)]
    claims = R.aggregate_behaviors(rows, threshold=2, window_days=0)
    assert claims == []


def test_behavior_below_threshold_no_edge() -> None:
    """A3：只部署 1 次 → 不建边（临时帮忙不等于职责）。"""
    rows = [_row("王泳", "部署", "前端", "e1")]
    assert R.aggregate_behaviors(rows, threshold=2, window_days=0) == []


def test_behavior_window_filters_old_events() -> None:
    """A6：超出窗口的旧证据不计入。"""
    rows = [
        _row("王泳", "部署", "前端", "old1", "2026-01-01"),
        _row("王泳", "部署", "前端", "old2", "2026-01-02"),
        _row("王泳", "部署", "前端", "new1", "2026-11-01"),
    ]
    claims = R.aggregate_behaviors(
        rows, threshold=2, window_days=90, today=date(2026, 11, 9)
    )
    assert claims == [], "只有 1 条在窗口内，不该建边"


def test_behavior_ignores_self_maintenance() -> None:
    """自己维护自己没意义。"""
    rows = [_row("前端", "部署", "前端", f"e{i}") for i in range(3)]
    assert R.aggregate_behaviors(rows, threshold=2, window_days=0) == []


# ------------------------------------------------------------ A1 陈述


def test_statement_owns() -> None:
    claims = R.extract_statements("王泳负责前端页面")
    assert [(c.predicate, c.head, c.tail, c.label) for c in claims] == [
        ("OWNS", "王泳", "前端页面", "exact")
    ]


def test_statement_maintains() -> None:
    claims = R.extract_statements("成善维护 ESB 接口")
    assert claims[0].predicate == "MAINTAINS"
    assert claims[0].head == "成善"


def test_statement_collaborates() -> None:
    claims = R.extract_statements("刘佳和刘岳一起处理这个客户")
    assert claims[0].predicate == "COLLABORATES_WITH"
    assert {claims[0].head, claims[0].tail} == {"刘佳", "刘岳"}


def test_statement_depends_on() -> None:
    claims = R.extract_statements("aml-scripts 依赖 中登接口")
    assert claims[0].predicate == "DEPENDS_ON"


def test_statement_ignores_plain_prose() -> None:
    """普通叙述不该被抽成关系。"""
    assert R.extract_statements("接口字段对不上，明天再看") == []


# ------------------------------------------------------------ A4/A5 合并


def test_merge_exact_wins_over_behavior() -> None:
    """A4：先有陈述 exact，行为命中不降级，证据累加。"""
    statement = R.extract_statements("王泳负责前端")[0]
    behavior = R.RelationClaim(head="王泳", predicate="OWNS", tail="前端",
                               label="probable", evidence=("e1", "e2"))
    merged = R.merge_claims([statement, behavior])
    assert len(merged) == 1
    claim = merged[0]
    assert claim.label == "exact"
    assert claim.confidence == 1.0
    assert set(claim.evidence) == {"e1", "e2"}


def test_merge_keeps_conflicting_targets_separate() -> None:
    """A5：陈述指 A、行为指 B → 两条边，不合并。"""
    a = R.RelationClaim(head="王泳", predicate="OWNS", tail="A", label="exact")
    b = R.RelationClaim(head="王泳", predicate="OWNS", tail="B", label="probable")
    merged = R.merge_claims([a, b])
    assert {c.tail for c in merged} == {"A", "B"}


def test_statement_rejects_prose_with_function_words() -> None:
    """实测假阳性：`第一个接口是` 与 `的接口需要前端轮询来` 不该成边。"""
    text = "第一个接口是中登查询，依赖它的接口需要前端轮询来获取结果"
    claims = R.extract_statements(text)
    heads = {c.head for c in claims}
    tails = {c.tail for c in claims}
    assert "第一个接口是" not in heads | tails
    assert "的接口需要前端轮询来" not in heads | tails


def test_statement_rejects_long_phrase_names() -> None:
    """超过 8 个字的一定是短语，不是实体名。"""
    assert R.extract_statements("这个特别长的东西依赖那个也很长的东西") == []


def test_depends_on_requires_dependency_verb() -> None:
    """`调用` 不算依赖：`调用了接口` 是动作。"""
    assert R.extract_statements("前端调用了中登查询") == []


def test_query_predicates_match_registry() -> None:
    """A7：查询只走 039 定义的四个谓词，不碰 RELATED 挂载边。

    查询脚本在 `harness/query_relations.py`，这里对齐两边的谓词表，
    避免有人加谓词只改了一边。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "query_relations",
        "/mnt/e/PycharmProjects/amarantine/harness/query_relations.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert set(module.PREDICATES) == set(R.PREDICATE_SPECS)
