"""清理残留测试 namespace（spec 027）。

默认 dry-run，只报告。`--apply` 才真删。

用法：
    python ../harness/purge_namespaces.py                  # 只报告
    python ../harness/purge_namespaces.py --apply          # 真删
    python ../harness/purge_namespaces.py --apply --include-protected

为什么默认 dry-run：删的是真实数据库，删错不可逆。
为什么保护 `personal`：它只有 1 行 meta、没有任何实体/事件/向量，
但名字不像测试生成的（测试是 `markerfix611095` 这种）。
证据不足以判定是残留，就保护 —— 删错不可逆，留着只占一行。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import urllib.parse

import httpx
from neo4j import GraphDatabase

from memory.config import load_settings

#: 永不删除的 namespace。`--include-protected` 才能动。
PROTECTED = {"work", "personal"}

#: SQLite 里带 namespace 列、需要按 namespace 清理的表。
SQLITE_TABLES = (
    # spec 046/047：按 namespace 清理时必须连原文和 job 一起删 ——
    # 只删向量会留下 ingest_job.done，重灌直接被幂等挡掉（实测踩过）。
    "raw_quote",
    "ingest_job",
    "alias_reject",
    "surname_decl",
    "embedding_index_meta",
    "recall_trace",
    "recall_feedback",
    "directive_usage",
)


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, int]] = []  # (存储, namespace, 条数)
        self.errors: list[str] = []

    def add(self, store: str, ns: str, n: int) -> None:
        if n:
            self.rows.append((store, ns, n))

    def namespaces(self) -> set[str]:
        return {ns for _, ns, _ in self.rows}

    def summary(self) -> None:
        if not self.rows:
            print("  （没有要清理的数据）")
            return
        by_store: dict[str, dict[str, int]] = {}
        for store, ns, n in self.rows:
            by_store.setdefault(store, {})[ns] = n
        for store in sorted(by_store):
            total = sum(by_store[store].values())
            print(f"  {store:<22} {len(by_store[store])} 个 namespace / {total} 条")
            for ns in sorted(by_store[store]):
                print(f"    {ns:<28} {by_store[store][ns]:>5}")


# ---------------------------------------------------------------- Neo4j


def scan_neo4j(settings, report: Report) -> dict[str, int]:
    """返回 {namespace: 节点数}，并把非保护的写进 report。"""
    drv = GraphDatabase.driver(settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
    counts: dict[str, int] = {}
    try:
        with drv.session() as sess:
            rows = sess.run(
                "MATCH (n) WHERE n.namespace IS NOT NULL "
                "RETURN n.namespace AS ns, count(*) AS n ORDER BY n DESC"
            ).data()
            for r in rows:
                counts[r["ns"]] = r["n"]
    finally:
        drv.close()
    return counts


def purge_neo4j(settings, namespaces: list[str], report: Report) -> None:
    drv = GraphDatabase.driver(settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
    try:
        with drv.session() as sess:
            for ns in namespaces:
                try:
                    res = sess.run(
                        "MATCH (n) WHERE n.namespace = $ns "
                        "WITH n LIMIT 10000 "
                        "DETACH DELETE n "
                        "RETURN count(*) AS deleted",
                        ns=ns,
                    ).data()
                    deleted = res[0]["deleted"] if res else 0
                    print(f"    neo4j  {ns:<28} 删除 {deleted} 个节点")
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"neo4j {ns}: {exc}")
                    print(f"    neo4j  {ns:<28} 失败 {exc}")
    finally:
        drv.close()


# ---------------------------------------------------------------- Weaviate


def scan_weaviate(settings, report: Report) -> dict[str, dict[str, int]]:
    """返回 {collection: {namespace: count}}。"""
    base = settings.WEAVIATE_URL.rstrip("/")
    out: dict[str, dict[str, int]] = {}
    try:
        r = httpx.get(f"{base}/v1/schema", timeout=15)
        classes = [c["class"] for c in r.json().get("classes", [])]
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"weaviate schema: {exc}")
        return out

    for cls in classes:
        q = {
            "query": (
                f'{{Aggregate{{{cls}(groupBy:["namespace"])'
                f"{{groupedBy{{value}}meta{{count}}}}}}}}"
            )
        }
        try:
            rr = httpx.post(f"{base}/v1/graphql", json=q, timeout=30)
            groups = rr.json().get("data", {}).get("Aggregate", {}).get(cls, []) or []
            detail = {
                g["groupedBy"]["value"]: g["meta"]["count"]
                for g in groups
                if g.get("groupedBy") and g.get("meta")
            }
            if detail:
                out[cls] = detail
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"weaviate {cls}: {exc}")
    return out


def purge_weaviate(settings, namespaces: list[str], report: Report) -> None:
    base = settings.WEAVIATE_URL.rstrip("/")

    # Weaviate 的 batch delete 要求 body 里必须有 `match.class`：
    # 缺了会返回 500 {"error":[{"message":"validate: empty match.class clause"}]}。
    # 所以只能按 collection 逐个删，不能一次删「所有类里的这个 namespace」。
    try:
        r = httpx.get(f"{base}/v1/schema", timeout=15)
        classes = [c["class"] for c in r.json().get("classes", [])]
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"weaviate schema: {exc}")
        print(f"    weaviate 读取 schema 失败: {exc}")
        return

    for cls in classes:
        for ns in namespaces:
            try:
                # 注意 httpx.delete() 不支持 json=，必须用 httpx.request("DELETE", ...)
                body = {
                    "match": {
                        "class": cls,
                        "where": {
                            "path": ["namespace"],
                            "operator": "Equal",
                            "valueText": ns,
                        },
                    },
                    "output": "minimal",
                }
                r = httpx.request(
                    "DELETE", f"{base}/v1/batch/objects", json=body, timeout=60
                )
                if r.status_code >= 400:
                    report.errors.append(f"weaviate {cls}/{ns}: {r.status_code} {r.text[:100]}")
                    print(f"    weaviate {cls}/{ns:<22} 失败 {r.status_code}")
                    continue
                data = r.json()
                if data.get("results", {}).get("failed", 0):
                    report.errors.append(f"weaviate {cls}/{ns}: {data['results']}")
                deleted = data.get("results", {}).get("successful", 0)
                if deleted:
                    print(f"    weaviate {cls}/{ns:<22} 删除 {deleted} 条")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"weaviate {cls}/{ns}: {exc}")
                print(f"    weaviate {cls}/{ns:<22} 失败 {exc}")


# ---------------------------------------------------------------- SQLite


def scan_sqlite(settings, report: Report) -> dict[str, dict[str, int]]:
    db = settings.DATABASE_URL.replace("sqlite:///", "")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out: dict[str, dict[str, int]] = {}
    try:
        existing = {
            r["name"]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for t in SQLITE_TABLES:
            if t not in existing:
                continue
            cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
            if "namespace" not in cols:
                continue
            rows = con.execute(
                f"SELECT namespace, count(*) AS n FROM {t} "
                f"WHERE namespace IS NOT NULL GROUP BY namespace"
            ).fetchall()
            detail = {r["namespace"]: r["n"] for r in rows}
            if detail:
                out[t] = detail
    finally:
        con.close()
    return out


def purge_sqlite(settings, namespaces: list[str], report: Report) -> None:
    db = settings.DATABASE_URL.replace("sqlite:///", "")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        existing = {
            r["name"]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for t in SQLITE_TABLES:
            if t not in existing:
                continue
            cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
            if "namespace" not in cols:
                continue
            for ns in namespaces:
                try:
                    cur = con.execute(f"DELETE FROM {t} WHERE namespace = ?", (ns,))
                    if cur.rowcount:
                        print(f"    sqlite {t}.{ns:<20} 删除 {cur.rowcount} 行")
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"sqlite {t}.{ns}: {exc}")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- 主流程


def snapshot(settings) -> dict[str, int]:
    """清理前后的快照，用来证明 work 的数据一条没少。"""
    snap: dict[str, int] = {}
    drv = GraphDatabase.driver(settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
    try:
        with drv.session() as sess:
            for lb in ("Event", "Entity", "Person", "Time", "Action", "Alias"):
                r = sess.run(
                    f"MATCH (n:`{lb}`) WHERE n.namespace = 'work' RETURN count(n) AS n"
                ).data()
                snap[f"neo4j:{lb}"] = r[0]["n"] if r else 0
    finally:
        drv.close()

    wv = scan_weaviate(settings, Report())
    for cls, detail in wv.items():
        snap[f"weaviate:{cls}"] = detail.get("work", 0)

    db = settings.DATABASE_URL.replace("sqlite:///", "")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        existing = {
            r["name"]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for t in SQLITE_TABLES:
            if t not in existing:
                continue
            cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
            if "namespace" not in cols:
                continue
            r = con.execute(f"SELECT count(*) AS n FROM {t} WHERE namespace='work'").fetchone()
            snap[f"sqlite:{t}"] = r["n"]
    finally:
        con.close()
    return snap


def main() -> int:
    ap = argparse.ArgumentParser(description="清理残留测试 namespace")
    ap.add_argument("--apply", action="store_true", help="真删（默认只报告）")
    ap.add_argument(
        "--namespace",
        default="",
        help="只处理这个 namespace（逗号分隔可多个）；默认全部非保护域",
    )
    ap.add_argument(
        "--include-protected",
        action="store_true",
        help=f"连保护列表一起处理（{sorted(PROTECTED)}）—— 危险",
    )
    args = ap.parse_args()

    settings = load_settings()
    report = Report()

    print("=" * 68)
    print("扫描" if not args.apply else "清理")
    print("=" * 68)

    # ---- 扫描三处 ----
    neo = scan_neo4j(settings, report)
    wv = scan_weaviate(settings, report)
    lite = scan_sqlite(settings, report)

    # 注意：neo 的 key 是 namespace，而 lite/wv 的 key 是**表名/collection 名**，
    # namespace 在它们的 value 里。写成 `set(lite)` 会把表名当 namespace
    # （dry-run 时真的出现过：待清理列表里混进了 `alias_reject`、`recall_trace`）。
    all_ns: set[str] = set(neo)
    for detail in wv.values():
        all_ns |= set(detail)
    for detail in lite.values():
        all_ns |= set(detail)
    all_ns.discard(None)

    picked = {n.strip() for n in args.namespace.split(",") if n.strip()}
    if picked:
        unknown = sorted(picked - all_ns)
        if unknown:
            print(f"⚠ 指定的 namespace 里这些不存在/已清空: {unknown}")
        all_ns = sorted(picked & all_ns)
        targets = sorted(all_ns)
        skipped = []
    else:
        targets = sorted(all_ns - PROTECTED) if not args.include_protected else sorted(all_ns)
        skipped = sorted(all_ns & PROTECTED) if not args.include_protected else []

    print(f"\n共发现 {len(all_ns)} 个 namespace")
    if skipped:
        print(f"受保护跳过: {skipped}")
    print(f"待清理 {len(targets)} 个: {targets}")

    for ns in targets:
        if ns in neo:
            report.add("neo4j", ns, neo[ns])
        for cls, detail in wv.items():
            if ns in detail:
                report.add(f"weaviate:{cls}", ns, detail[ns])
        for t, detail in lite.items():
            if ns in detail:
                report.add(f"sqlite:{t}", ns, detail[ns])

    print()
    report.summary()

    if not args.apply:
        print()
        print("=" * 68)
        print("dry-run，什么都没删。确认无误后加 --apply")
        print("=" * 68)
        return 0 if not report.errors else 1

    # ---- 真删 ----
    before = snapshot(settings)
    print()
    print("=" * 68)
    print("执行删除（顺序：图 -> 向量 -> SQLite）")
    print("=" * 68)

    # 1. 图（可重建）
    purge_neo4j(settings, targets, report)
    # 2. 向量（可重建）
    purge_weaviate(settings, targets, report)
    # 3. SQLite（唯一数据，放最后）
    purge_sqlite(settings, targets, report)

    # ---- 验证 ----
    print()
    print("=" * 68)
    print("验证")
    print("=" * 68)

    after = snapshot(settings)
    print("\n  work 的数据（清理前后必须完全一致）：")
    changed = False
    for k in sorted(before):
        b, a = before[k], after.get(k, 0)
        flag = "" if b == a else "   <== 变了！"
        if b != a:
            changed = True
        print(f"    {k:<28} {b:>5} -> {a:>5}{flag}")

    # 垃圾是否归零
    neo_after = scan_neo4j(settings, report)
    wv_after = scan_weaviate(settings, report)
    lite_after = scan_sqlite(settings, report)
    left: set[str] = set(neo_after)
    for detail in wv_after.values():
        left |= set(detail)
    for detail in lite_after.values():
        left |= set(detail)
    leftover = sorted(left - PROTECTED) if not args.include_protected else sorted(left)

    print(f"\n  剩余非保护 namespace: {len(leftover)}")
    if leftover:
        print(f"    {leftover}")
    else:
        print("    0（干净了）")

    print()
    if changed:
        print("✗ work 的数据变了 —— 这不是预期行为，请检查")
        return 1
    if leftover:
        print("✗ 仍有残留 namespace")
        return 1
    if report.errors:
        print(f"✗ {len(report.errors)} 个错误：")
        for e in report.errors[:10]:
            print(f"    {e}")
        return 1

    print("✓ 垃圾清零，work 数据未变")
    return 0


if __name__ == "__main__":
    sys.exit(main())
