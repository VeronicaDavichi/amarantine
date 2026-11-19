#!/usr/bin/env python3
"""生成人工标注数据（spec 039 / 041），供 docs/labeling.html 使用。

三组：

- `statements`：含「负责/维护/对接/协作/依赖/一起」的句子，
  一半是规则抽出来的（测 precision），一半是规则漏掉的（测 recall）
- `behaviors`：人-动作-对象组（含 1~3 条证据），标「稳定关系 / 偶发 / 错误」
- `senses`：指称候选（term + 上下文 + 机器给的解释）

用法：

    python3 harness/make_labeling_data.py --out docs/labeling-data.js
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import random
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services.labeler import extract_entities  # noqa: E402
from memory.services.reference import (  # noqa: E402
    build_candidates,
    extract_signals,
    suggest_gloss_llm,
)
from memory.services.relations import extract_statements  # noqa: E402

NEO4J = "http://127.0.0.1:7474/db/neo4j/tx/commit"
NS_QQ = "qq-10000000001"
NS_DING = "njzq-dingtalk"
STMT_HINT = re.compile(r"(负责|维护|对接|协作|依赖|一起|分管|管着)")


def cy(statement: str) -> list[list]:
    req = urllib.request.Request(
        NEO4J,
        data=json.dumps({"statements": [{"statement": statement}]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + base64.b64encode(b"neo4j:password").decode()},
    )
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["results"][0]["data"]


def sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？\n]", text or "")
    return [p.strip() for p in parts if 20 <= len(p.strip()) <= 160]




IDENTITY_DIR = ROOT / "back_end" / "data" / "corpus" / "qq-identities"


def identity_names() -> list[str]:
    """从群成员表读昵称/群名片（去掉括号后缀），作为实体池的一部分。

    为什么必须加：`extract_entities` 不认名册，`克劳德` 这类群友名字
    在纯文本抽取里会漏掉 —— 实测「本句抽到的实体」只有一个 `头一号`。
    """
    names: set[str] = set()
    if not IDENTITY_DIR.exists():
        return []
    for f in IDENTITY_DIR.glob("group-*.json"):
        try:
            members = json.loads(f.read_text(encoding="utf-8")).get("members") or []
        except Exception:
            continue
        for m in members:
            for raw in (m.get("nickname"), m.get("card")):
                name = str(raw or "").strip()
                name = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
                if 2 <= len(name) <= 12 and not name.isdigit():
                    names.add(name)
    return sorted(names)


def entity_pool(limit: int = 600) -> list[str]:
    """全局实体池：图里出现过的 Entity/Person 名字，供下拉框兜底。"""
    rows = cy(
        "MATCH (n) WHERE n.namespace IN ['%s','%s'] AND (n:Entity OR n:Person) "
        "RETURN DISTINCT n.name LIMIT %d" % (NS_QQ, NS_DING, limit))
    names = []
    for r in rows:
        name = (r.get("row") or [None])[0]
        if name:
            names.append(str(name))
    merged = set(names) | set(identity_names())
    return sorted(merged)[:limit]


_ROSTER_CACHE: list[str] | None = None


#: 名册里这些「名字」其实是口头词，不能当实体（实测 `有没有`/`没有` 被当人）
_ROSTER_STOP = {"有没有", "没有", "什么", "这个", "那个", "可以", "不是", "就是",
                "一个", "自己", "现在", "时候", "问题", "东西", "事情"}


def _clean_roster_name(raw: str) -> str:
    """名册名去掉 emoji/符号/括号内容，只留 CJK/字母/数字。"""
    text = re.sub(r"[（(][^）)]*[）)]", "", str(raw or "")).strip()
    text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text).strip()
    return text


def _roster_hits(text: str) -> list[str]:
    """名册匹配：整名优先，其次「去掉 emoji 后的主干」（`便携式🦆🍗` → `便携式`）。"""
    hits: list[str] = []
    for raw in _ROSTER_CACHE or []:
        clean = _clean_roster_name(raw)
        for cand in (raw, clean):
            if not cand or cand in _ROSTER_STOP:
                continue
            if cand.isascii():
                # 拉丁名 ≥2 且**词边界**：`kk` 要能选，但 `al` 不能命中 `Valhalla`
                if len(cand) < 2:
                    continue
                if not re.search(rf"(?<![A-Za-z0-9]){re.escape(cand)}(?![A-Za-z0-9])", text):
                    continue
            else:
                if len(cand) < 2 or cand not in text:
                    continue
            if cand not in hits:
                hits.append(cand)
                break
            if cand == clean:
                break
    return hits


def entities_of(text: str) -> list[dict]:
    """抽出一条证据里可选的实体（name + kind），给页面做下拉选项。

    三个来源：

    1. `extract_entities`（人名/专名）
    2. **名册**（`克劳德`、`便携式` 这类 jieba 认不出的群友）
    3. **jieba 普通名词**（`服务器`、`产品`、`老师` —— 关系目标常常是普通名词，
       之前完全没进候选，导致用户只能手填）
    """
    global _ROSTER_CACHE
    out: list[dict] = []
    seen: set[str] = set()

    def add(name: str, kind: str) -> None:
        name = (name or "").strip()
        if not name or name in seen or len(name) > 12 or name.isdigit():
            return
        if name in ("的", "了", "是", "在", "和", "与", "这", "那", "我", "你", "他", "她"):
            return
        seen.add(name)
        out.append({"name": name, "kind": kind})

    try:
        for e in extract_entities(text):
            add(e.name, e.kind)
    except Exception:
        pass

    if _ROSTER_CACHE is None:
        _ROSTER_CACHE = identity_names()
    for name in _roster_hits(text):
        add(name, "roster")

    # 普通名词：jieba 的 n/nz/vn（关系目标多半是这类词）
    try:
        import jieba.posseg as pseg

        for w in pseg.cut(text):
            if w.flag in ("n", "nz", "vn", "nt") and 2 <= len(w.word) <= 10:
                add(w.word, "noun")
    except Exception:
        pass
    return out[:24]




# ============================================================ 039 v2：只标「句子明说」

#: 强关系词：出现即「这句话在说职责/协作/依赖」
_STRONG_CUES = (
    "负责", "牵头", "主管", "分管", "维护", "运维", "对接", "依赖", "依托",
    "协作", "合作", "配合", "协助", "搭建", "开发", "实现", "修复", "部署",
)

#: 弱关系词：常出现在「一起笑」「需要数据」这类非关系句里
_WEAK_CUES = ("一起", "共同", "需要", "使用", "提供", "更新", "发布", "新增")

#: 营销/公告模板：不是人与人/人与系统 的职责关系
_AD_PAT = re.compile(r"(上新|尾款|补款|链接|扭蛋|定金|包邮|拍下|上架|团购|优惠)")


def _roster_names_all() -> list[str]:
    global _ROSTER_CACHE
    if _ROSTER_CACHE is None:
        _ROSTER_CACHE = identity_names()
    return _ROSTER_CACHE








#: emoji / 控制字符（与 services/emoji_alias.py 同口径）
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3"
    "\u202a-\u202e\u200b-\u200f]")


def _has_boundary(alias: str, frag: str) -> bool:
    """薄壳：规则在 `memory.services.name_marker.has_name_boundary`（可测试）。"""
    from memory.services.name_marker import has_name_boundary

    return has_name_boundary(alias, frag)




def _uid_identity_index() -> dict[str, dict]:
    """`uid → {qq, groups, card, nickname}`，从本地成员表读。

    为什么不在页面上显示 uid：`u_435AByFRA4uBemFD8Xbj6Q` 对人没有意义，
    人认的是 **QQ 号** 和 **在哪个群**。所以碎片复核页只显示这两样。
    """
    idx: dict[str, dict] = {}
    for f in sorted(IDENTITY_DIR.glob("group-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        gid = str(data.get("groupQq") or f.stem.replace("group-", ""))
        for m in data.get("members") or []:
            uid = str(m.get("uid") or "").strip()
            if not uid:
                continue
            rec = idx.setdefault(uid, {"qq": m.get("qq"), "groups": [],
                                       "card": m.get("card"), "nickname": m.get("nickname")})
            if gid not in rec["groups"]:
                rec["groups"].append(gid)
    return idx


def build_fragments(limit: int = 80) -> list[dict]:
    """找「像某个账号名字的一部分」的 topic 实体，交给人判断要不要并回 Person。

    这些碎片是上游分词把带空格/emoji 的显示名切碎留下的
    （`Veronica Davichi` → `Veronica`），spec 044 想「降级」它们，
    但降级只会让噪声闭嘴；**并回 Person 才能让「谁说了什么」查得准**。

    只收「唯一账号」的（多账号同名跳过，否则并错人）。
    """
    name2uids: dict[str, set[str]] = {}
    for r in cy(f"MATCH (a:Account {{namespace:'{NS_QQ}'}})"
                f"-[:DISPLAYED_AS]->(al:Alias {{namespace:'{NS_QQ}'}}) "
                f"RETURN al.name AS n, a.uid AS uid"):
        name = (r.get("row") or [None])[0]
        uid = (r.get("row") or [None, None])[1]
        if name and uid:
            name2uids.setdefault(name, set()).add(uid)

    person_names = {r["row"][0] for r in cy(
        f"MATCH (p:Person {{namespace:'{NS_QQ}'}}) RETURN p.name AS n")}

    rows = cy(f"""
    MATCH (e:Entity {{namespace:'{NS_QQ}'}})
    WHERE coalesce(e.kind,'topic') = 'topic'
    OPTIONAL MATCH (ev:Event {{namespace:'{NS_QQ}'}})-[:MENTIONS]->(e)
    RETURN e.name AS name, count(DISTINCT ev) AS cards
    """)

    items: list[dict] = []
    for r in rows:
        name = (r.get("row") or [None])[0]
        cards = (r.get("row") or [None, 0])[1] or 0
        if not name or name in person_names:
            continue
        owners: set[str] = set()
        alias_hit = ""
        for alias, uids in name2uids.items():
            if alias != name and name in alias:
                owners |= uids
                if not alias_hit or len(alias) < len(alias_hit):
                    alias_hit = alias
        if len(owners) != 1 or not alias_hit:
            continue
        # 连续串内部切出来的不算（谐音/拼词会切错，见 _has_boundary）
        if not _has_boundary(alias_hit, name):
            continue
        rest = alias_hit.replace(name, "", 1)
        uid = next(iter(owners))
        ident = _uid_identity_index().get(uid) or {}
        items.append({
            "kind": "fragment",
            "id": f"frag-{len(items)+1}",
            "frag": name,
            "alias": alias_hit,
            "rest": rest,
            "uid": uid,
            # 给人看的身份：QQ 号 + 所在群（uid 只留给程序用）
            "qq": ident.get("qq"),
            "groups": ident.get("groups") or [],
            "card": ident.get("card") or "",
            "nickname": ident.get("nickname") or "",
            "cards": cards,
            "source": "unique_owner",
        })

    items.sort(key=lambda x: -x["cards"])
    return items[:limit]

def build_coverage(limit: int = 24, *, seed: int = 13, half: int | None = None) -> list[dict]:
    """生成关系覆盖评测集（039 的 recall 口径）。

    **种子不是全库随机**：实测全库随机会抽到 24 张营销卡（`kk发布…上新`），
    机器全判 0 条，测不出 recall（那是「该抽 0 条」，不是漏抽）。
    改用 v2 陈述句作种子（已过滤营销、含人名 + 强关系词），
    并保证一半样本是「机器抽到了边」的，一半是「机器抽 0 条」的。
    """
    from memory.config import load_settings
    from memory.services.relations import (
        PREDICATE_TO_INTENSITY,
        extract_statements_combined,
    )

    settings = load_settings()
    roster = _roster_names_all()
    seed_items = build_statements_v2(limit=90)
    texts = [it["evidence"] for it in seed_items]

    with_claims: list[dict] = []
    without: list[dict] = []
    for i, text in enumerate(texts):
        ctx = texts[max(0, i - 2): i]
        claims, _rejected = extract_statements_combined(
            text, settings, context=ctx, roster=roster)
        item = {
            "kind": "coverage",
            "id": "",  # 最后统一编号
            "evidence": text,
            "machine": [
                {"head": c.head, "predicate": c.predicate,
                 "intensity": PREDICATE_TO_INTENSITY.get(c.predicate), "tail": c.tail}
                for c in claims
            ],
            "source": "v2_seed",
        }
        (with_claims if claims else without).append(item)

    n_half = half if half is not None else limit // 2
    picked = with_claims[:n_half] + without[: max(0, limit - len(with_claims[:n_half]))]
    random.seed(seed)
    random.shuffle(picked)
    for i, it in enumerate(picked, 1):
        it["id"] = f"cov-{i}"
    return picked[:limit]


def build_claims_audit(limit: int = 20, *, scan: int = 120) -> list[dict]:
    """生成「机器边核对」评测集：每项是一条机器抽出的边，人判对/不对。

    这是 039 的 precision 口径：`precision = 判对 / 总核对数`。
    比「机器去匹配人工自由填的词」可靠 —— 后者会把两个都对的答案判成错。
    """
    from memory.config import load_settings
    from memory.services.relations import (
        PREDICATE_TO_INTENSITY,
        RELATION_CUES,
        extract_statements_combined,
    )

    settings = load_settings()
    roster = _roster_names_all()
    # 只在「v2 陈述句」上抽边：那些句子已过滤营销、含人名与强关系词。
    # 全库扫描会把 `运营发布商品` 之类营销句全抽成边（实测 20 条全是噪声）。
    seed = build_statements_v2(limit=60)
    texts = [it["evidence"] for it in seed]

    items: list[dict] = []
    for i, text in enumerate(texts):
        if len(items) >= limit:
            break
        if not text.strip():
            continue
        ctx = [t for t in texts[max(0, i - 2): i] if t]
        claims, _ = extract_statements_combined(
            text, settings, context=ctx, roster=roster)
        for c in claims:
            items.append({
                "kind": "claim",
                "id": f"claim-{len(items)+1}",
                "evidence": text[:200],
                "head": c.head,
                "predicate": c.predicate,
                "intensity": PREDICATE_TO_INTENSITY.get(c.predicate),
                "tail": c.tail,
                "source": c.source,
            })
            if len(items) >= limit:
                break
    return items

def build_statements_v2(limit: int = 20) -> list[dict]:
    """生成「句子明说关系」的评测集。

    与 v1 的区别：v1 混入了「现实里是什么关系」（世界知识），
    导致正确抽到句子字面关系的样本被判错。v2 只标句子本身说了什么。
    """
    roster = _roster_names_all()
    rows = cy(f"MATCH (e:Event) RETURN e.summary LIMIT 6000")
    sents: list[str] = []
    for row in rows:
        text = (row.get("row") or [""])[0] or ""
        for part in re.split(r"[。！？\n]", text):
            p = part.strip()
            if 12 <= len(p) <= 100:
                sents.append(p)

    pos: list[str] = []
    neg: list[str] = []
    seen: set[str] = set()
    for sent in sents:
        key = sent[:14]
        if key in seen or _AD_PAT.search(sent):
            continue
        if not any(name in sent for name in roster):
            continue
        if any(c in sent for c in _STRONG_CUES):
            pos.append(sent)
            seen.add(key)
        elif any(c in sent for c in _WEAK_CUES):
            neg.append(sent)
            seen.add(key)

    random.seed(7)
    random.shuffle(pos)
    random.shuffle(neg)

    items: list[dict] = []
    for sent in pos[: max(1, limit - limit // 3)]:
        items.append({
            "kind": "statement",
            "id": f"stmt2-{len(items)+1}",
            "evidence": sent,
            "entities": entities_of(sent),
            "machine": _machine_view(sent),
            "source": "explicit",
        })
    for sent in neg[: limit // 3]:
        items.append({
            "kind": "statement",
            "id": f"stmt2-{len(items)+1}",
            "evidence": sent,
            "entities": entities_of(sent),
            "machine": _machine_view(sent),
            "source": "weak_cue",
        })
    return items


def _machine_view(text: str) -> dict:
    """机器当前抽到的东西（页面展示用，同时也是评测输入）。"""
    from memory.services.relations import candidate_entities, extract_statements

    cands = candidate_entities(text, roster=_roster_names_all())
    claims = extract_statements(text, candidates=cands)
    if not claims:
        return {"extracted": False, "head": None, "predicate": None, "tail": None}
    c = claims[0]
    return {"extracted": True, "head": c.head, "predicate": c.predicate,
            "tail": c.tail, "count": len(claims)}

def build_statements(limit: int = 20) -> list[dict]:
    rows = cy(f"MATCH (e:Event {{namespace:'{NS_QQ}'}}) RETURN e.summary LIMIT 3000")
    rows += cy(f"MATCH (e:Event {{namespace:'{NS_DING}'}}) RETURN e.summary LIMIT 3000")
    pool: list[str] = []
    for r in rows:
        for s in sentences((r.get("row") or [""])[0] or ""):
            if STMT_HINT.search(s):
                pool.append(s)
    random.seed(42)
    random.shuffle(pool)

    items: list[dict] = []
    picked: set[str] = set()
    # 1) 规则抽得到的（测 precision）
    for s in pool:
        claims = extract_statements(s)
        if not claims:
            continue
        c = claims[0]
        key = f"{c.head}|{c.predicate}|{c.tail}"
        if key in picked:
            continue
        picked.add(key)
        items.append({
            "kind": "statement",
            "id": f"stmt-pos-{len(items)+1}",
            "evidence": s,
            "entities": entities_of(s),
            "machine": {"head": c.head, "predicate": c.predicate, "tail": c.tail,
                        "extracted": True},
            "source": "rule_hit",
        })
        if len([i for i in items if i["source"] == "rule_hit"]) >= limit // 2:
            break
    # 2) 规则漏掉的（测 recall）
    for s in pool:
        if extract_statements(s):
            continue
        items.append({
            "kind": "statement",
            "id": f"stmt-neg-{len(items)+1}",
            "evidence": s,
            "entities": entities_of(s),
            "machine": {"head": None, "predicate": None, "tail": None, "extracted": False},
            "source": "rule_miss",
        })
        if len([i for i in items if i["source"] == "rule_miss"]) >= limit // 2:
            break
    return items


def build_behaviors(limit: int = 10) -> list[dict]:
    data = cy(
        f"MATCH (p:Person {{namespace:'{NS_QQ}'}})<-[:INVOLVES]-(e:Event)-[:ACTION]->(a:Action) "
        f"MATCH (e)-[:MENTIONS]->(m) "
        f"RETURN p.name, a.verb, m.name, e.event_id, e.summary, e.date_start LIMIT 5000")

    # 按 (日期, 动词) 合并同类事件：
    # 实测 `kk 发布 A/B/C/D` 4 条、`阿强/阿珍 讨论 广东` 2 条，其实是同一批动作，
    # 不合并会让评测集里 20 条有一半是同源，指标失真。
    groups: dict[tuple, list[dict]] = {}
    for row in data:
        name, verb, target, eid, summary, day = (row.get("row") or [None] * 6)
        key = (str(day or "")[:10], verb or "")
        groups.setdefault(key, []).append({
            "person": name or "", "verb": verb or "", "target": target or "",
            "event_id": eid, "evidence": summary, "date": str(day or "")[:10],
        })
    items = []
    for (_day, _verb), evs in sorted(groups.items(), key=lambda x: -len(x[1]))[:limit]:
        rep = evs[0]
        items.append({
            "kind": "behavior",
            "id": f"beh-{len(items)+1}",
            "person": rep["person"], "verb": rep["verb"], "target": rep["target"],
            "date": rep["date"],
            "merged_count": len(evs),
            "merged_people": sorted({e["person"] for e in evs})[:5],
            "evidence_count": len(evs),
            "evidence": [e["evidence"] for e in evs[:3]],
            "entities": entities_of(" ".join(str(e["evidence"] or "") for e in evs[:3])),
            "source": "behavior",
        })
    return items


def build_senses_llm(limit: int = 20, *, max_terms: int = 40) -> list[dict]:
    """用 LLM 判断候选词在群里指什么（规则版 gloss 正确率只有 20%）。"""
    from memory.config import load_settings

    settings = load_settings()
    rows = cy(f"MATCH (e:Event {{namespace:'{NS_QQ}'}}) RETURN e.summary LIMIT 3000")
    summaries = [(r.get("row") or [""])[0] or "" for r in rows]
    # 只取**定义句**信号：@/称呼只说明「有人在叫这个名字」，不说明它指什么。
    # 实测不过滤时 10 条里 9 条是「群友昵称」这种废话。
    sigs = [s for text in summaries for s in extract_signals(text) if s.kind == "define"]
    cands = sorted(build_candidates(sigs, scope=NS_QQ),
                   key=lambda c: -getattr(c, "score", 0))

    items: list[dict] = []
    seen: set[str] = set()
    for c in cands[:max_terms]:
        term = str(getattr(c, "term", "") or "")
        if not term or term in seen:
            continue
        seen.add(term)
        contexts = [t for t in summaries if term in t][:5]
        if not contexts:
            continue
        gloss = suggest_gloss_llm(term, contexts, settings)
        print(f"    LLM gloss: {term} → {gloss}")
        if not gloss:
            continue
        items.append({
            "kind": "sense",
            "id": f"sense-{len(items)+1}",
            "term": term,
            "gloss": gloss,
            "score": getattr(c, "score", 0),
            "evidence": contexts[:2],
            "scope": NS_QQ,
            "source": "llm",
        })
        if len(items) >= limit:
            break
    return items


def build_senses(limit: int = 20) -> list[dict]:
    rows = cy(f"MATCH (e:Event {{namespace:'{NS_QQ}'}}) RETURN e.summary LIMIT 3000")
    summaries = [(r.get("row") or [""])[0] or "" for r in rows]
    sigs = [s for text in summaries for s in extract_signals(text)]
    cands = build_candidates(sigs, scope=NS_QQ)
    # 同一 term 只留一条（评测里 `人` 出现 4 次、`内容` 2 次，全是同源碎片）
    best: dict[str, object] = {}
    for c in cands:
        term = str(getattr(c, "term", "") or "")
        if not term:
            continue
        cur = best.get(term)
        if cur is None or getattr(c, "score", 0) > getattr(cur, "score", 0):
            best[term] = c
    items = []
    for c in best.values():
        gloss = getattr(c, "gloss", None)
        if not gloss or len(str(gloss)) < 2:
            continue
        ctx = [s for s in summaries if getattr(c, "term", "") in s][:2]
        items.append({
            "kind": "sense",
            "id": f"sense-{len(items)+1}",
            "term": getattr(c, "term", ""),
            "gloss": str(gloss)[:80],
            "score": getattr(c, "score", 0),
            "evidence": ctx,
            "scope": getattr(c, "scope", NS_QQ),
            "source": "reference",
        })
        if len(items) >= limit:
            break
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "labeling-data.js"))
    ap.add_argument(
        "--fragments", action="store_true",
        help="生成人名碎片复核集（044 前置：碎片并回 Person）",
    )
    ap.add_argument(
        "--coverage", action="store_true",
        help="生成关系覆盖评测集（039 recall 口径：这句该有几条）",
    )
    ap.add_argument(
        "--claims-audit", action="store_true",
        help="生成「机器边核对」评测集（039 precision 口径）",
    )
    ap.add_argument(
        "--statements-v2", action="store_true",
        help="生成「只标句子明说关系」的评测集（039 改造 A 用）",
    )
    ap.add_argument(
        "--senses-llm", action="store_true",
        help="用 LLM 抽 gloss（规则版正确率 20%%，不推荐）",
    )
    ap.add_argument(
        "--senses", type=int, default=0,
        help="义项条数；默认 0 —— 规则版 gloss 基本全是碎片（实测正确率 20%%），"
             "等改成 LLM 抽取再开",
    )
    args = ap.parse_args()

    data = {
        "generated_at": "2026-11-13",
        "instructions": {
            "statement": "判断这句话是否真的表达了「谁负责/维护/依赖什么」；"
                         "machine 是规则抽取结果，抽错也算错。",
            "behavior": "判断这个人对目标的动作是否构成**稳定关系**"
                        "（≥2 次且像长期职责），偶发的算「偶发」。",
            "sense": "判断这个词在群里是否真的指这个意思；"
                     "上下文不足以判断时选「不确定」。",
        },
        "entity_pool": entity_pool(),
        "statements": (
            build_statements_v2() if getattr(args, "statements_v2", False)
            else build_statements()
        ),
        "claims": build_claims_audit() if getattr(args, "claims_audit", False) else [],
        "coverage": build_coverage() if getattr(args, "coverage", False) else [],
        "fragments": build_fragments() if getattr(args, "fragments", False) else [],
        "behaviors": build_behaviors(),
        "senses": (
            build_senses_llm(args.senses) if getattr(args, "senses_llm", False)
            else (build_senses() if args.senses > 0 else [])
        ),
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.LABEL_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n",
                   encoding="utf-8")
    print(f"statements={len(data['statements'])} behaviors={len(data['behaviors'])} "
          f"senses={len(data['senses'])} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
