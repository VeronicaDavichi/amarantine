#!/usr/bin/env python3
"""046 A3/A5 真库验证。

A3：构造一次 ingest，让 LLM 返回越界 source_indices → 该事件不建卡、计数 >0
A5：构造「与已有卡同一件事」的消息 → 新卡 resolves 指旧卡，旧卡 summary 不变

做法：直接调 run_ingest（不经过 HTTP），用 monkeypatch 替换 split_with_sources，
这样能精确控制模型输出而不依赖真实 LLM 判断。
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services import llm_split as LS  # noqa: E402
from memory.services import raw_quote as RQ  # noqa: E402
from memory.services.weaviate_store import WeaviateStore  # noqa: E402

NS = "verify046b"
SEQ = f"verify046b-session"




def wipe(settings) -> int:
    """清空验证 namespace（Weaviate + SQLite），返回删除条数。"""
    import sqlite3

    import httpx

    store = WeaviateStore(settings)
    try:
        rows = store.list_all(NS, limit=1000)
    finally:
        store.close()
    n = 0
    for r in rows:
        oid = r.get("id") or r.get("_id")
        if not oid:
            continue
        try:
            httpx.delete(f"http://127.0.0.1:8081/v1/objects/MemoryEvent/{oid}", timeout=20)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    db = pathlib.Path(RQ.db_path_from_settings(settings))
    con = sqlite3.connect(db)
    for t in ("raw_quote", "ingest_job"):
        try:
            con.execute(f"DELETE FROM {t} WHERE namespace=?", (NS,))
        except Exception:  # noqa: BLE001
            pass
    con.commit()
    con.close()
    return n


def cards_of(store, tag: str) -> list[dict]:
    rows = store.list_all(NS, limit=500)
    return [r for r in rows if tag in str(r.get("summary") or "")]


def main() -> int:
    settings = load_settings()
    print(f"清空 {NS}: 删 {wipe(settings)} 张旧卡")
    store = WeaviateStore(settings)
    try:
        existing = cards_of(store, "【046基准】")
        if not existing:
            print("先用 run_ingest 建一张基准卡…")
            job = RQ.IngestJobStore(pathlib.Path(RQ.db_path_from_settings(settings)))
            js = job.create(dedupe_key="046-base", namespace=NS, source="test",
                            session_id=SEQ, turns_total=1, turns_skipped=0,
                            status=RQ.JOB_QUEUED)
            rs = RQ.RawQuoteStore(pathlib.Path(RQ.db_path_from_settings(settings)))
            qid = f"q-base-{js.job_id[:8]}"
            rs.put(namespace=NS, quote_id=qid, text="【046基准】候潮城一期方案已定",
                   role="user", ts="2026-05-01 10:00:00", source="test", session_id=SEQ)
            orig = LS.split_with_sources

            def one(win_ctx, window, st, **kw):
                return [LS.SourcedEvent(summary="【046基准】候潮城一期方案已定",
                                        source_indices=[1])]

            LS.split_with_sources = one
            try:
                RQ.run_ingest(job_id=js.job_id, namespace=NS, session_id=SEQ,
                              turns=[{"text": "候潮城一期方案已定", "role": "user",
                                      "ts": "2026-05-01 10:00:00"}],
                              quote_ids=[qid], settings=settings)
            finally:
                LS.split_with_sources = orig
            existing = cards_of(store, "【046基准】")
            store2 = WeaviateStore(settings)
            existing = [r for r in store2.list_all(NS, limit=500)
                        if "【046基准】" in str(r.get("summary") or "")]
            store2.close()
        if not existing:
            print("✗ 基准卡没建出来")
            return 1
        base = existing[0]
        print(f"基准卡: {base.get('event_id')} | {base.get('summary')}")

        # ---------- A5：第二片与已有卡同一件事 → merge_into ----------
        job = RQ.IngestJobStore(pathlib.Path(RQ.db_path_from_settings(settings)))
        js = job.create(dedupe_key="046-merge", namespace=NS, source="test",
                        session_id=SEQ, turns_total=1, turns_skipped=0,
                        status=RQ.JOB_QUEUED)
        rs = RQ.RawQuoteStore(pathlib.Path(RQ.db_path_from_settings(settings)))
        qid = f"q-merge-{js.job_id[:8]}"
        rs.put(namespace=NS, quote_id=qid, text="方案又补充了一期细节",
               role="user", ts="2026-05-02 10:00:00", source="test", session_id=SEQ)

        captured = {}

        def with_merge(win_ctx, window, st, **kw):
            captured["existing"] = kw.get("existing") or []
            target = captured["existing"][0]["event_id"] if captured["existing"] else None
            return [LS.SourcedEvent(summary="【046补充】候潮城一期方案细节确认",
                                    source_indices=[1], merge_into=target)]

        orig = LS.split_with_sources
        LS.split_with_sources = with_merge
        try:
            RQ.run_ingest(job_id=js.job_id, namespace=NS, session_id=SEQ,
                          turns=[{"text": "方案又补充了一期细节", "role": "user",
                                  "ts": "2026-05-02 10:00:00"}],
                          quote_ids=[qid], settings=settings)
        finally:
            LS.split_with_sources = orig

        print(f"注入的已有卡候选: {[e['event_id'] for e in captured.get('existing') or []]}")
        store3 = WeaviateStore(settings)
        rows = store3.list_all(NS, limit=500)
        store3.close()
        new = [r for r in rows if "【046补充】" in str(r.get("summary") or "")]
        base_after = [r for r in rows if r.get("event_id") == base.get("event_id")]
        print(f"新卡: {[(r.get('event_id'), (r.get('resolves') or [])) for r in new]}")
        ok5 = bool(new) and base.get("event_id") in (new[0].get("resolves") or [])
        ok5 = ok5 and bool(base_after) and base_after[0].get("summary") == base.get("summary")
        print(f"A5 merge_into → resolves 指旧卡、旧卡 summary 不变: {'✓' if ok5 else '✗'}")

        # ---------- A3：越界索引 → 不建卡 + 计数 ----------
        job2 = RQ.IngestJobStore(pathlib.Path(RQ.db_path_from_settings(settings)))
        js3 = job2.create(dedupe_key="046-oob", namespace=NS, source="test",
                          session_id=SEQ, turns_total=1, turns_skipped=0,
                          status=RQ.JOB_QUEUED)
        rs3 = RQ.RawQuoteStore(pathlib.Path(RQ.db_path_from_settings(settings)))
        qid3 = f"q-oob-{js3.job_id[:8]}"
        rs3.put(namespace=NS, quote_id=qid3, text="【046越界】这句话的索引会越界",
               role="user", ts="2026-05-03 10:00:00", source="test", session_id=SEQ)

        def oob(win_ctx, window, st, **kw):
            return [
                LS.SourcedEvent(summary="【046越界】索引越界的事件", source_indices=[99],
                                invalid_source=True),
                LS.SourcedEvent(summary="【046正常】同片正常事件", source_indices=[1]),
            ]

        orig = LS.split_with_sources
        LS.split_with_sources = oob
        try:
            outcome = RQ.run_ingest(job_id=js3.job_id, namespace=NS, session_id=SEQ,
                                    turns=[{"text": "【046越界】这句话的索引会越界",
                                            "role": "user", "ts": "2026-05-03 10:00:00"}],
                                    quote_ids=[qid3], settings=settings)
        finally:
            LS.split_with_sources = orig

        store4 = WeaviateStore(settings)
        rows4 = store4.list_all(NS, limit=500)
        store4.close()
        oob_cards = [r for r in rows4 if "【046越界】" in str(r.get("summary") or "")]
        ok_cards = [r for r in rows4 if "【046正常】" in str(r.get("summary") or "")]
        print(f"越界事件建卡数: {len(oob_cards)}（应为 0）")
        print(f"正常事件建卡数: {len(ok_cards)}（应为 1）")
        print(f"out_of_range 计数: {getattr(outcome, 'out_of_range', None)}（应为 1）")
        ok3 = (not oob_cards) and len(ok_cards) == 1 and getattr(outcome, "out_of_range", 0) == 1
        print(f"A3 越界不建卡 + 计数: {'✓' if ok3 else '✗'}")

        return 0 if (ok5 and ok3) else 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
