"""spec 023：`FORGET_SWEEP_DAYS` 的接线。

核心不是「定时器能跑」，而是**这个配置真的决定了行为**。
一条假配置（配了没人读）能通过任何「代码存在」的检查，
只有「改配置 → 行为变」的测试才抓得住。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import memory.services.forget_scan as fscan
import memory.services.maintenance as m
from memory.config import Settings
from memory.core.forget_policy import SweepReport
from memory.services.forget_scan import ScanResult
from memory.services.maintenance import MaintenanceWorker, SWEEP_EVERY_FALLBACK_S
from memory.services.pending_task import PendingTaskStore, db_path_from_settings

NS = "work"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path) -> PendingTaskStore:
    settings = Settings()
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'sweep.db').as_posix()}"
    return PendingTaskStore(db_path_from_settings(settings))


def _fake_scan_factory(counter: list[ScanResult], *, boom: bool = False):
    def _fake(settings, namespace, **kw):
        if boom:
            raise RuntimeError("weaviate down")
        result = ScanResult(
            report=SweepReport(buckets={}, verdicts=[], dry_run=True),
            total=len(counter),
            graph_degraded=False,
        )
        counter.append(result)
        return result

    return _fake


# ---------------- D1：配置真的决定间隔 ----------------

def test_forget_sweep_days_drives_interval() -> None:
    """`FORGET_SWEEP_DAYS` 改了，扫描间隔必须跟着变。

    这是「不是假配置」的唯一证据。如果实现里写死 86400，
    这条会失败 —— 而任何「代码存在性」的检查都发现不了。
    """
    s = Settings()
    s.FORGET_SWEEP_DAYS = 7
    w = MaintenanceWorker(NS, s, sweep_on_start=False)
    assert w._sweep_every_s == 7 * 86400.0


def test_invalid_sweep_days_falls_back_instead_of_disabling() -> None:
    """`FORGET_SWEEP_DAYS <= 0` 用兜底间隔，不是「永不扫描」。

    一个配错的周期不该让遗忘扫描永久不跑 —— 那比配错更危险。
    """
    for bad in (0, -5):
        s = Settings()
        s.FORGET_SWEEP_DAYS = bad
        w = MaintenanceWorker(NS, s, sweep_on_start=False)
        assert w._sweep_every_s == SWEEP_EVERY_FALLBACK_S


def test_explicit_interval_overrides_config() -> None:
    s = Settings()
    s.FORGET_SWEEP_DAYS = 30
    w = MaintenanceWorker(NS, s, sweep_every_s=60.0, sweep_on_start=False)
    assert w._sweep_every_s == 60.0


# ---------------- D2 / D3：到点才扫 ----------------

def test_sweep_runs_on_start_then_waits(store, monkeypatch) -> None:
    """`sweep_on_start=True`：第一次 tick 扫，之后要等满周期。"""
    calls: list[ScanResult] = []
    monkeypatch.setattr(fscan, "scan", _fake_scan_factory(calls))

    s = Settings()
    w = MaintenanceWorker(
        NS,
        s,
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_every_s=86400.0,
        sweep_on_start=True,
    )

    w.tick(now=NOW)
    assert len(calls) == 1, "第一次 tick 应该扫描"

    w.tick(now=NOW + timedelta(minutes=1))
    assert len(calls) == 1, "没到周期不该重复扫（全量拉取是重操作）"

    w.tick(now=NOW + timedelta(days=1))
    assert len(calls) == 2, "满周期后应该扫"


def test_sweep_on_start_false_skips_first_tick(store, monkeypatch) -> None:
    """`sweep_on_start=False`：第一次 tick 不扫。测试默认用它。"""
    calls: list[ScanResult] = []
    monkeypatch.setattr(fscan, "scan", _fake_scan_factory(calls))

    s = Settings()
    w = MaintenanceWorker(
        NS,
        s,
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_every_s=86400.0,
        sweep_on_start=False,
    )

    w.tick(now=NOW)
    assert calls == [], "sweep_on_start=False 却扫了"

    w.tick(now=NOW + timedelta(days=1))
    assert len(calls) == 1


# ---------------- D6：扫描失败不拖死循环 ----------------

def test_sweep_failure_does_not_raise(store, monkeypatch) -> None:
    """Weaviate 挂了，tick 不能抛 —— 抛了就没人在补课了。"""
    monkeypatch.setattr(fscan, "scan", _fake_scan_factory([], boom=True))

    s = Settings()
    w = MaintenanceWorker(
        NS,
        s,
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_every_s=86400.0,
        sweep_on_start=True,
    )
    w.tick(now=NOW)  # 不该抛


def test_sweep_failure_still_reschedules(store, monkeypatch) -> None:
    """失败也要推进下次时间 —— 否则每个 tick 都会重试一次重操作。"""
    monkeypatch.setattr(fscan, "scan", _fake_scan_factory([], boom=True))

    s = Settings()
    w = MaintenanceWorker(
        NS,
        s,
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_every_s=86400.0,
        sweep_on_start=True,
    )
    w.tick(now=NOW)
    assert w._next_sweep_at == NOW + timedelta(seconds=86400.0)


# ---------------- D7：报告是汇总，不是全量 ----------------

def test_tick_report_sweep_is_summary_only() -> None:
    """`TickReport.sweep` 只装汇总 —— 装全量 verdicts 会让日志和内存爆。"""
    from memory.services.maintenance import ForgetSweepSummary

    s = ForgetSweepSummary(total=1000, protected=900, need_review=50, safe_to_delete=50)
    assert not hasattr(s, "verdicts")
    assert s.safe_to_delete == 50


def test_summary_did_anything_ignores_all_protected() -> None:
    """全是 PROTECTED 的扫描不值得打日志（每天一条「0 个可删」是噪音）。"""
    from memory.services.maintenance import ForgetSweepSummary

    assert ForgetSweepSummary(total=500, protected=500).did_anything is False
    assert ForgetSweepSummary(total=500, protected=450, need_review=50).did_anything is True
    assert ForgetSweepSummary(total=500, safe_to_delete=1).did_anything is True


def test_tick_report_includes_sweep_in_did_anything(store, monkeypatch) -> None:
    """扫描有可删候选时，`did_anything` 必须为真（否则日志被吞掉）。"""
    def _fake(settings, namespace, **kw):
        from memory.core.forget_policy import BUCKET_SAFE_TO_DELETE, ForgetVerdict

        return ScanResult(
            report=SweepReport(
                buckets={BUCKET_SAFE_TO_DELETE: 1},
                verdicts=[
                    ForgetVerdict(
                        event_id="e1",
                        bucket=BUCKET_SAFE_TO_DELETE,
                        reason="idle_and_unrecalled",
                        idle_days=200,
                        age_days=300,
                    )
                ],
                dry_run=True,
            ),
            total=1,
        )

    monkeypatch.setattr(fscan, "scan", _fake)
    s = Settings()
    w = MaintenanceWorker(
        NS,
        s,
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_every_s=86400.0,
        sweep_on_start=True,
    )
    report = w.tick(now=NOW)
    assert report.sweep is not None
    assert report.sweep.safe_to_delete == 1
    assert report.sweep.sample_ids == ["e1"]
    assert report.did_anything is True


# ---------------- D9：idle_days=0 的语义钉住 ----------------

def test_idle_days_zero_means_immediate_not_disabled() -> None:
    """`idle_days=0` 是「立即淘汰」，**不是**「关闭淘汰」。

    ADR 之前没定义这个语义，容易被误当成关闭开关
    （`GRAPH_HUB_DEGREE=0` 确实是关闭，两者惯例不同）。
    这条测试把现状钉住，避免将来有人「顺手改成关闭」。
    """
    from datetime import date

    from memory.core.forget_policy import BUCKET_SAFE_TO_DELETE, ForgetCandidate, classify

    today = date(2026, 11, 5)
    card = ForgetCandidate(
        event_id="e1",
        created_at=today - timedelta(days=200),
        last_recalled_at=today,          # idle = 0
        recall_count=0,
        retention="normal",
    )
    got = classify(
        card,
        today=today,
        idle_days=0,
        min_recalls=2,
        grace_days=7,
        protect_importance=0.9,
    )
    assert got.bucket == BUCKET_SAFE_TO_DELETE, (
        "idle_days=0 若变成「关闭」，这条会失败 —— 语义变了必须改 ADR 和这里"
    )


def test_sweep_schedule_does_not_depend_on_wall_clock(store, monkeypatch) -> None:
    """**回归测试**：`sweep_on_start=False` 的调度不能用 `datetime.now()` 起算。

    原实现里 `__init__` 写的是 `datetime.now(utc) + sweep_every_s`，
    但 `tick(now=...)` 支持注入时间。两者混用 = 测试结果取决于**今天是几号**。

    这个 bug 真的发生过：`test_sweep_on_start_false_skips_first_tick` 在写的时候
    通过，过了几天自己挂了（注入的 NOW 落在 `_next_sweep_at` 之前）。

    用 1970 年的时间点能把这类问题一次钉死 —— 任何依赖墙钟的实现都会立刻失败。
    """
    calls: list[ScanResult] = []
    monkeypatch.setattr(fscan, "scan", _fake_scan_factory(calls))

    s = Settings()
    w = MaintenanceWorker(
        NS,
        s,
        store=store,
        reconcile_every_s=1e9,
        reconcile_on_start=False,
        sweep_every_s=86400.0,
        sweep_on_start=False,
    )

    epoch = datetime(1970, 1, 2, tzinfo=timezone.utc)
    w.tick(now=epoch)
    assert calls == [], "第一次 tick 不该扫（sweep_on_start=False）"

    w.tick(now=epoch + timedelta(days=1))
    assert len(calls) == 1, (
        "满周期没扫 —— 调度仍然依赖墙钟（__init__ 里用了 datetime.now）"
    )
