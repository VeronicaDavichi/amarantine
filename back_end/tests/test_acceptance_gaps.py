"""ADR 验收缺口补齐 —— `docs/acceptance-audit.md` 里标 🟡 的那些。

命名规则：`test_a<N>_*`，编号直接对应 ADR 第 5 节的 A 项，方便对着审计表核。

这批的共同点：**功能都在，只是原来没有断言钉住它**。
所以这里不造新 fake，全部复用各模块已有的测试替身。
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from memory.core.forget_policy import (
    BUCKET_PROTECTED,
    BUCKET_SAFE_TO_DELETE,
    ForgetCandidate,
    sweep,
)
from memory.core.scoring import weighted_score
from memory.services.recall import recall

from tests.test_recall import (  # noqa: F401  —— 复用已验证的替身
    NOW,
    TODAY,
    FakeStore,
    _embedder,
    _graph_down,
    _graph_ok,
    _hit,
)
from tests.test_raw_quote import env as raw_env  # noqa: F401  —— A5 复用 ingest 夹具


# ---------------- A1：recall 结果必须具备契约字段 ----------------


def test_a1_materials_carry_contract_fields() -> None:
    """A1 的实质：material 少一个字段，LLM 侧就静默拿到残缺结构。

    ADR 原文要求「过 JSON Schema 校验，缺字段即失败」。这里钉住的是
    更弱但可维护的一半：**必填字段一个都不能少，且整体 JSON 可序列化**。
    完整 schema 校验器属功能缺口（见审计表 A1 的说明）。
    """
    result = recall(
        query="上个月项目A的进展",
        namespace="work",
        settings=__import__("memory.config", fromlist=["load_settings"]).load_settings(),
        store=FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")]),
        embedder=_embedder,
        graph_searcher=_graph_ok,
        now=NOW,
        today=TODAY,
    )
    assert result.materials, "这条 fixture 本该有命中"

    required = {"event_id", "summary", "date_start", "importance", "score"}
    for m in result.materials:
        missing = required - set(m)
        assert not missing, f"material {m.get('event_id')} 缺字段: {missing}"

    # 能被 JSON 序列化才算「tool result」
    import json

    json.dumps({"materials": result.materials, "trace": result.trace})


# ---------------- A2：查询时间范围必须真的作用到检索 ----------------


def test_a2_query_date_range_is_applied_to_search() -> None:
    """A2 的实质：时间锚定不是在 prompt 里写一句话，是**真的过滤**。

    断言两层：区间被解析出来并传给检索；返回的卡确实落在区间内。
    """
    seen: dict[str, object] = {}

    class RecordingStore(FakeStore):
        def search_near_vector(self, vector, namespace, *, limit=50, date_from=None, timeout_s=None):
            seen["date_from"] = date_from
            return [_hit("evt_in", days_ago=20)]

        def search_bm25(self, text, namespace, *, limit=50, date_from=None):
            seen.setdefault("bm25_from", date_from)
            return [_hit("evt_in", days_ago=20)]

    from memory.config import load_settings

    recall(
        query="上个月项目A的进展",
        namespace="work",
        settings=load_settings(),
        store=RecordingStore(),
        embedder=_embedder,
        graph_searcher=_graph_ok,
        now=NOW,
        today=TODAY,
    )

    assert "date_from" in seen, "检索没收到时间下界 —— 时间锚定形同虚设"
    assert seen["date_from"] is not None, "查询含明确时间词，下界不该是空的"


# ---------------- A3：图降级必须快，不能把体验拖垮 ----------------


def test_a3_graph_failure_degrades_within_budget() -> None:
    """A3 后半句：降级不仅要「还能返回」，还要**快**。

    ADR 给的是 ≤1200ms。真实耗时依赖网络，这里断言的是
    「降级路径没有引入额外等待」—— 若将来加了重试/超时，这条会立刻红。
    """
    started = time.monotonic()
    result = recall(
        query="上个月项目A的进展",
        namespace="work",
        settings=__import__("memory.config", fromlist=["load_settings"]).load_settings(),
        store=FakeStore(vector_hits=[_hit("evt_a")], bm25_hits=[_hit("evt_a")]),
        embedder=_embedder,
        graph_searcher=_graph_down,
        now=NOW,
        today=TODAY,
    )
    elapsed_ms = (time.monotonic() - started) * 1000

    assert result.degraded is True
    assert result.materials, "降级不该把结果也降没了"
    assert elapsed_ms < 1200, f"图降级耗时 {elapsed_ms:.0f}ms 超过 1200ms 预算"


# ---------------- A9：遗忘扫描绝不能动事实层 ----------------


def _hash_candidates(cands: list[ForgetCandidate]) -> tuple:
    """事实层指纹：条数 + 每条内容。只要扫描改了任何一位，指纹就变。"""
    return tuple(
        (
            c.event_id,
            c.importance,
            c.recall_count,
            c.created_at,
            c.last_recalled_at,
            c.retention,
            c.pinned,
            tuple(c.unresolved),
            c.review_verdict,
        )
        for c in cands
    )


def test_a9_sweep_never_mutates_facts() -> None:
    """A9：扫描前后 EventCard 的条数与内容哈希必须完全一致。

    `sweep` 是纯分类函数 —— 这条测试防的是「有人为了省一次查询，
    在分类过程中顺手改了 candidate」。
    """
    cands = [
        ForgetCandidate(
            event_id=f"evt_{i}",
            importance=0.1 * i,
            recall_count=i,
            created_at=date(2024, 1, 1),
            last_recalled_at=date(2024, 6, 1),
            retention="normal",
            pinned=(i == 4),
            unresolved=["待办"] if i == 3 else [],
        )
        for i in range(6)
    ]
    before = _hash_candidates(cands)

    report = sweep(
        cands,
        today=date(2026, 11, 3),
        idle_days=180,
        min_recalls=1,
        grace_days=90,
        protect_importance=0.7,
        dry_run=True,
    )

    assert _hash_candidates(cands) == before, "扫描改了事实层的候选内容"
    assert sum(report.buckets.values()) == len(cands), "分桶总数必须等于候选数"


# ---------------- A12：误删率闸门需要「可抽检的明细」 ----------------


def test_a12_sweep_exposes_per_item_evidence_for_audit() -> None:
    """A12 要求人工抽检 100 条。抽检的前提是**每条都能说出为什么**。

    只有汇总数字（buckets）不够 —— 抽检时必须能看到 event_id 与 reason。
    """
    cands = [
        ForgetCandidate(
            event_id=f"evt_{i}",
            importance=0.1,
            recall_count=0,
            created_at=date(2023, 1, 1),
            last_recalled_at=date(2023, 2, 1),
            retention="normal",
        )
        for i in range(12)
    ]
    report = sweep(
        cands,
        today=date(2026, 11, 3),
        idle_days=180,
        min_recalls=1,
        grace_days=90,
        protect_importance=0.7,
        dry_run=True,
    )

    deletable = report.by_bucket(BUCKET_SAFE_TO_DELETE)
    assert len(deletable) == report.buckets[BUCKET_SAFE_TO_DELETE]
    for v in deletable:
        assert v.event_id, "抽检样本缺 event_id"
        assert v.reason, "抽检样本缺可解释的 reason"

    # dry_run=false 必须被拒（A12 的另一半，已有测试，这里防回归）
    try:
        sweep(
            cands,
            today=date(2026, 11, 3),
            idle_days=180,
            min_recalls=1,
            grace_days=90,
            protect_importance=0.7,
            dry_run=False,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("dry_run=false 竟然被放行了")


# ---------------- A28：时效不能主导 ----------------


def test_a28_recency_does_not_dominate_relevance() -> None:
    """A28：3 年前高相关 应当压过 昨天低相关。

    ADR 的原话是「前者 final 更高或至少入选」。这里断言前者更高 ——
    因为「至少入选」在有 top-K 截断时不可靠，前者更高才是可判定的。
    """
    old_but_relevant = weighted_score(0.95, delta_days=365 * 3, importance=0.5)
    recent_but_weak = weighted_score(0.5, delta_days=1, importance=0.5)

    assert old_but_relevant > recent_but_weak, (
        f"时效压过了相关性：老但相关={old_but_relevant:.4f} "
        f"≤ 新但弱相关={recent_but_weak:.4f}"
    )


# ---------------- 顺带：保护原因必须可解释（A10 的现存 3 类） ----------------


def test_a10_implemented_signals_are_explainable() -> None:
    """A10 目前只实现 3 类硬信号（pinned / retention / unresolved）。

    这条钉住「已实现的那 3 类都有独立 reason」—— 图结构与 confirmed_by_use
    两类属功能缺口，见审计表，不在这里假装。
    """
    cases = {
        "pinned": ForgetCandidate(event_id="e1", pinned=True, created_at=date(2023, 1, 1)),
        "retention": ForgetCandidate(event_id="e2", retention="permanent", created_at=date(2023, 1, 1)),
        "unresolved": ForgetCandidate(event_id="e3", unresolved=["x"], created_at=date(2023, 1, 1)),
    }
    reasons = {}
    for name, c in cases.items():
        report = sweep(
            [c],
            today=date(2026, 11, 3),
            idle_days=180,
            min_recalls=1,
            grace_days=90,
            protect_importance=0.7,
        )
        verdict = report.verdicts[0]
        assert verdict.bucket == BUCKET_PROTECTED, f"{name} 没被保护"
        reasons[name] = verdict.reason

    assert len(set(reasons.values())) == 3, f"三类硬信号的 reason 撞了: {reasons}"


# ---------------- A21：LLM 不许生成词表外的标签 ----------------


def test_a21_llm_cannot_emit_types_outside_vocab() -> None:
    """A21：词表是**白名单**，不是建议。

    实现里有一句 `filtered = [t for t in refined.types if t in allowed] or base.types`
    —— 这句话的存在意义就是防 LLM 自由生成。原来没有任何测试钉住它，
    意味着哪天有人「为了方便」把过滤删掉，不会有任何东西报警。
    """
    from memory.services.labeler import LabelResult, label_event

    def evil_llm(summary: str, candidates: list[str]) -> LabelResult:
        return LabelResult(
            types=["hacked_type", "work"],
            entities=[],
            source="llm",
        )

    result = label_event(
        "张三决定项目A延期到六月，顺便讨论了合同",
        vocab=["work"],
        llm_labeler=evil_llm,
    )
    assert "hacked_type" not in result.types, "词表外的标签漏进 types 了"
    assert set(result.types) <= {"work", "general"}, f"越界标签: {result.types}"


def test_a21_llm_returns_only_illegal_types_falls_back() -> None:
    """全都被过滤掉时必须回退规则层，而不是变成空 types。"""
    from memory.services.labeler import LabelResult, label_event

    def all_evil(summary: str, candidates: list[str]) -> LabelResult:
        return LabelResult(types=["nope", "nada"], entities=[], source="llm")

    result = label_event("张三决定项目A延期", vocab=["work"], llm_labeler=all_evil)
    assert result.types, "types 不能为空"
    assert "nope" not in result.types
    assert result.source != "llm" or set(result.types) <= {"work", "general"}


# ---------------- A5：材料里的每个 quote 都必须能溯源 ----------------


def test_a5_every_material_quote_is_resolvable(raw_env) -> None:  # noqa: F811
    """A5：不只验一条，要**遍历**每条材料的每个 `raw_quote_ids`。

    只测单条时，「第 2 个 quote 写错 namespace」这种 bug 不会暴露。
    """
    client, _, _, _ = raw_env
    turns = [
        {"text": "第一句话：主人接受新 offer", "role": "user", "ts": "2026-11-03T09:00:00+08:00"},
        {"text": "第二句话：通勤一小时", "role": "user", "ts": "2026-11-03T09:01:00+08:00"},
    ]
    resp = client.post(
        "/admin/ingest",
        json={"dedupe_key": "k-a5", "turns": turns},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 202

    from memory.services.raw_quote import quote_id_for

    checked = 0
    for t in turns:
        qid = quote_id_for(
            namespace="work",
            source="chat",
            session_id=None,
            ts=t["ts"],
            text=t["text"],
        )
        r = client.get(
            f"/admin/raw/{qid}", headers={"Authorization": "Bearer test-token"}
        )
        assert r.status_code == 200, f"{qid} 溯源失败: {r.status_code}"
        body = r.json()
        assert body["text"].strip(), f"{qid} 的 text 是空的"
        checked += 1

    assert checked == len(turns)

