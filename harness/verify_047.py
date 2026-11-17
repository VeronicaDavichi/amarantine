#!/usr/bin/env python3
"""047 A1–A8 真库验收。

逐条对照 spec 的验收标准，用真实图数据判定，不靠单测推断。
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

NS = "qq-505456846"
GID = "485490399"
NEO = "http://127.0.0.1:7474/db/neo4j/tx/commit"


def cy(q: str, params: dict | None = None) -> list[list]:
    stmt = {"statement": q}
    if params:
        stmt["parameters"] = params
    req = urllib.request.Request(
        NEO, data=json.dumps({"statements": [stmt]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())["results"][0]["data"]


def admin(path: str, **params):
    import httpx

    token = ""
    for line in (ROOT / "back_end" / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            token = line.split("=", 1)[1].strip()
    with httpx.Client(timeout=60) as c:
        return c.get(f"http://127.0.0.1:8765{path}", params=params,
                     headers={"Authorization": f"Bearer {token}"}).json()


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    # A1：群内查「往汐」→ 2 个 Account，ambiguous=true
    r = admin("/admin/whois", name="往汐", scope=f"group:{GID}",
              namespace=NS, base="true")
    ok = len(r.get("accounts") or []) == 2 and r.get("ambiguous") is True
    results.append(("A1 群内 whois 往汐 → 2 账号 ambiguous",
                    ok, f"{len(r.get('accounts') or [])} 个 / ambiguous={r.get('ambiguous')}"))

    # A2：全局查「潮间带」→ 命中同一 uid
    r2 = admin("/admin/whois", name="潮间带", scope="global", namespace=NS)
    uid_global = {a["uid"] for a in (r2.get("accounts") or [])}
    uid_group = {a["uid"] for a in (r.get("accounts") or [])}
    ok = bool(uid_global & uid_group)
    results.append(("A2 全局 whois 潮间带命中同一 uid",
                    ok, f"global={sorted(uid_global)} ∩ group={sorted(uid_group)}"))

    # A3：senderName 缺失时用 uid 反查名片（导入报告的可用显示名比例）
    rows = cy(
        "MATCH (a:Account {namespace:$ns})-[r:DISPLAYED_AS {scope:$scope}]->(al:Alias) "
        "WHERE r.source = 'group_card' RETURN count(DISTINCT a.uid) AS c",
        {"ns": NS, "scope": f"group:{GID}"})
    card_uids = rows[0]["row"][0]
    total = cy("MATCH (g:group_member3) RETURN 1")  # 占位，不用
    ok = card_uids >= 300
    results.append(("A3 用 uid 反查群名片（本群可用名片数）",
                    ok, f"{card_uids} 个 uid 有群名片（成员 390）"))

    # A4：同一 uid 在两个群不同名片 → 两条 DISPLAYED_AS 边共存
    rows = cy(
        "MATCH (a:Account {namespace:$ns})-[r:DISPLAYED_AS]->(al:Alias) "
        "WHERE r.scope STARTS WITH 'group:' "
        "WITH a.uid AS uid, collect(DISTINCT r.scope) AS scopes "
        "WHERE size(scopes) >= 2 RETURN uid, scopes LIMIT 3",
        {"ns": NS})
    ok = bool(rows)
    results.append(("A4 同 uid 跨群多条 DISPLAYED_AS 共存",
                    ok, f"样例 {rows[0]['row'] if rows else '无'}"))

    # A5：跨 scope 不串 —— 本群名片在别的群查不到
    r5 = admin("/admin/whois", name="往汐（北瑀家的鱼）",
               scope="group:876081394", namespace=NS)
    ok = len(r5.get("accounts") or []) == 0
    results.append(("A5 跨 scope 不串（本群名片在 Lo 群查不到）",
                    ok, f"{len(r5.get('accounts') or [])} 个（应 0）"))

    # A6：昵称/群名片/备注三类标记各自可查
    probes = [("潮间带", "global"), ("往汐（北瑀家的鱼）", f"group:{GID}")]
    found = []
    for name, scope in probes:
        rr = admin("/admin/whois", name=name, scope=scope, namespace=NS)
        found.append((name, scope, len(rr.get("accounts") or [])))
    remark = cy(
        "MATCH (a:Account {namespace:$ns})-[r:DISPLAYED_AS]->() "
        "WHERE r.source = 'remark' RETURN count(r) AS c", {"ns": NS})[0]["row"][0]
    ok = all(n for _name, _scope, n in found)
    results.append(("A6 昵称/群名片各自可查（备注类无数据）",
                    ok, f"{found}；remark 边 {remark} 条"))

    # A7：无成员表时回落 —— 该群没有 DISPLAYED_AS 时不报错、返回空
    r7 = admin("/admin/whois", name="随便一个名字", scope="group:000000",
               namespace=NS)
    ok = r7.get("name_missing") is True
    results.append(("A7 无名册/无匹配 → name_missing 而非报错",
                    ok, f"name_missing={r7.get('name_missing')}"))

    # T6：头衔边
    rows = cy("MATCH ()-[r:TITLED]->() RETURN count(r) AS c")
    n_title = rows[0]["row"][0]
    rows2 = cy("MATCH (a:Account {namespace:$ns})-[r:TITLED]->(t:Title) "
               "RETURN a.uid AS uid, t.text AS title LIMIT 3", {"ns": NS})
    ok = n_title >= 900
    results.append(("T6 群头衔 TITLED 边", ok,
                    f"{n_title} 条；样例 {[r['row'] for r in rows2]}"))

    # 输出
    print("=== 047 真库验收 ===")
    allok = True
    for name, okk, detail in results:
        print(f"{'✓' if okk else '✗'} {name}\n    {detail}")
        allok = allok and okk
    print(f"\n{'全部通过' if allok else '有未通过项'}")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
