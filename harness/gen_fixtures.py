"""A3 合成夹具生成器。

产出 back_end/fixtures/events.json；每条都过 EventCard 校验，保证合法。

覆盖矩阵（对应 A2/A6/A10/A11 验收）：
  - date_end=null 的持续事件
  - 同实体冲突对（两版本，不合并）
  - 五类硬信号：pinned(manual) / pinned(user_directive) / unresolved / 不可逆词表 / confirmed_by_use
  - 新条目（保护期内）与老条目（可淘汰）
  - sensitive 保留级别

用法：
    cd back_end && uv run python ../harness/gen_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

from memory.models.event_card import EventCard

NS = "agent-demo"
INGESTED = "2026-10-30T10:21:14+08:00"


def evt(i: int, **kw: object) -> dict:
    """构造一条最小合法事件，字段按需覆盖。"""
    base: dict = {
        "namespace": NS,
        "event_id": f"evt_{i:016x}",
        "date_start": "2026-09-01",
        "types": ["daily"],
        "entities": [],
        "summary": f"合成事件 {i}",
        "retention": "normal",
        "ingested_at": INGESTED,
    }
    base.update(kw)
    return base


EVENTS: list[dict] = [
    # ---------- 1. 普通日常（10） ----------
    evt(1, summary="主人和老同学在街边小馆吃了顿晚饭，聊到各自近况",
        date_start="2026-08-12", types=["daily", "social"],
        entities=[{"name": "老同学", "kind": "person"}, {"name": "街边小馆", "kind": "place"}]),
    evt(2, summary="主人看完了电影《一一》，评价节奏慢但结尾喜欢",
        date_start="2026-07-20", types=["daily", "hobby"],
        entities=[{"name": "一一", "kind": "event"}], emotion="平静", emotion_intensity=0.3),
    evt(3, summary="主人自己修好了自行车后刹，换了刹车皮",
        date_start="2026-06-15", types=["daily"],
        entities=[{"name": "自行车", "kind": "topic"}]),
    evt(4, summary="主人开始学吉他，练了 C 大调和弦，手指疼",
        date_start="2026-05-03", types=["hobby"],
        entities=[{"name": "吉他", "kind": "topic"}], emotion="兴奋", emotion_intensity=0.5),
    evt(5, summary="主人为赶项目上线连续加班三天，最后一天到凌晨两点",
        date_start="2026-04-18", types=["work"], importance=0.6,
        importance_reason="高强度持续期，后续健康受影响",
        emotion="疲惫", emotion_intensity=0.7),
    evt(6, summary="主人周末去城郊爬山，走了 12 公里，天气很好",
        date_start="2026-09-20", types=["daily", "health"],
        entities=[{"name": "城郊", "kind": "place"}]),
    evt(7, summary="主人买了一把静音机械键盘，青轴太吵换成了茶轴",
        date_start="2026-03-11", types=["daily", "shopping"],
        entities=[{"name": "机械键盘", "kind": "topic"}]),
    evt(8, summary="主人读完《深度工作》，认为时间块方法可试",
        date_start="2026-02-08", types=["study", "hobby"],
        entities=[{"name": "深度工作", "kind": "event"}], conclusion="打算下周试时间块"),
    evt(9, summary="主人参加了一场线上技术分享，主题是向量检索的召回评估",
        date_start="2026-08-29", types=["work", "study"]),
    evt(10, summary="主人照着菜谱做了番茄牛腩，火候差点但味道可以",
         date_start="2026-07-05", types=["daily"],
         entities=[{"name": "番茄牛腩", "kind": "topic"}]),

    # ---------- 2. 持续事件（date_end=null，2） ----------
    evt(11, summary="主人有慢性胃炎，需要长期注意饮食，不能吃辣",
         date_start="2025-11-02", date_end=None, types=["health", "慢性病"],
         importance=0.8, importance_reason="长期健康约束，影响日常选择",
         entities=[{"name": "慢性胃炎", "kind": "topic"}]),
    evt(12, summary="主人在跟一门分布式系统课程，预计还要两个月",
         date_start="2026-09-01", date_end=None, types=["study"],
         unresolved=["课程结课项目", "期末考核"],
         entities=[{"name": "分布式系统课程", "kind": "event"}]),

    # ---------- 3. 冲突对：同实体两版本（2，A6 用） ----------
    evt(13, summary="小王当时在 A 公司做后端，负责订单系统",
         date_start="2025-06-10", date_end="2026-02-28", types=["work", "social"],
         entities=[{"name": "小王", "kind": "person"}, {"name": "A 公司", "kind": "org"}]),
    evt(14, summary="小王从 A 公司跳槽到 B 公司，转做基础架构",
         date_start="2026-03-01", types=["work", "social"],
         entities=[{"name": "小王", "kind": "person"}, {"name": "B 公司", "kind": "org"}]),

    # ---------- 4. 硬信号：pinned（2） ----------
    evt(15, summary="主人交代过：家里备用钥匙放在门口鞋柜第二层",
         date_start="2026-01-15", types=["life"],
         pinned=True, pinned_source="manual", importance=0.9,
         importance_reason="安全相关，人工标记",
         entities=[{"name": "备用钥匙", "kind": "topic"}]),
    evt(16, summary="主人明确要求记住：对花生过敏，外出吃饭要确认",
         date_start="2026-02-20", types=["health"],
         pinned=True, pinned_source="user_directive", user_directive="remember",
         importance=0.9, importance_reason="用户显式指令",
         entities=[{"name": "花生过敏", "kind": "topic"}]),

    # ---------- 5. 硬信号：unresolved（2） ----------
    evt(17, summary="主人还在犹豫要不要接那个外包项目，担心影响主业",
         date_start="2026-09-25", types=["work"],
         unresolved=["是否接外包项目"], importance=0.7,
         importance_reason="未闭环决策，预期会回查",
         entities=[{"name": "外包项目", "kind": "topic"}], emotion="纠结", emotion_intensity=0.5),
    evt(18, summary="主人在等房东回复能否续租一年，价格还没谈拢",
         date_start="2026-10-02", types=["life", "housing"],
         unresolved=["续租价格", "是否搬走"],
         entities=[{"name": "房东", "kind": "person"}]),

    # ---------- 6. 硬信号：不可逆词表（6，覆盖附录 A 六类） ----------
    evt(19, summary="主人做了阑尾炎手术，住院三天，术后恢复顺利",
         date_start="2026-04-02", types=["health", "手术"],
         importance=0.9, importance_reason="不可逆健康事件",
         entities=[{"name": "阑尾炎手术", "kind": "event"}]),
    evt(20, summary="主人从城东搬到了城西，通勤时间缩短半小时",
         date_start="2026-05-18", types=["life", "搬家"],
         importance=0.85, importance_reason="不可逆环境变化",
         entities=[{"name": "城西", "kind": "place"}]),
    evt(21, summary="主人从原公司离职，最后工作日是 9 月 30 日",
         date_start="2026-09-30", types=["work", "离职"],
         importance=0.9, importance_reason="不可逆职业事件",
         entities=[{"name": "原公司", "kind": "org"}]),
    evt(22, summary="主人签了购房合同，首付已付，明年交房",
         date_start="2026-06-22", types=["finance", "买房"],
         importance=0.95, importance_reason="不可逆财务事件",
         entities=[{"name": "新房", "kind": "place"}]),
    evt(23, summary="主人研究生毕业，论文答辩通过",
         date_start="2026-06-30", types=["study", "毕业"],
         importance=0.9, importance_reason="不可逆教育节点",
         entities=[{"name": "研究生", "kind": "event"}]),
    evt(24, summary="主人的工作签证获批，有效期三年",
         date_start="2026-08-05", types=["legal", "签证"],
         importance=0.9, importance_reason="不可逆法律状态",
         entities=[{"name": "工作签证", "kind": "topic"}]),

    # ---------- 7. 硬信号：confirmed_by_use（2） ----------
    evt(25, summary="主人偏好用 Vim 编辑代码，讨厌鼠标操作",
         date_start="2025-12-01", types=["preference"],
         confirmed_by_use=2, importance=0.7,
         importance_reason="多次被引用",
         entities=[{"name": "Vim", "kind": "topic"}]),
    evt(26, summary="主人对咖啡因敏感，下午三点后不喝咖啡",
         date_start="2026-01-08", types=["health", "preference"],
         confirmed_by_use=3, importance=0.7,
         importance_reason="多次被引用"),
    evt(27, summary="主人的常用开发语言是 Python，其次 Go",
         date_start="2025-10-20", types=["work", "preference"],
         confirmed_by_use=4, importance=0.75,
         importance_reason="多次被引用"),

    # ---------- 8. 老条目（可淘汰候选，3） ----------
    evt(28, summary="主人看过一部纪录片讲深海生物，觉得画面不错",
         date_start="2025-01-14", types=["daily"],
         recall_count=0),
    evt(29, summary="主人买过一盆绿萝放在窗台，后来忘了浇水",
         date_start="2025-03-22", types=["daily"],
         recall_count=0),
    evt(30, summary="主人试过一家新开的咖啡馆，拿铁一般",
         date_start="2025-05-09", types=["daily"],
         recall_count=0),

    # ---------- 9. 新条目（保护期内，2） ----------
    evt(31, summary="主人今天提到下周要去看牙医，做常规洗牙",
         date_start="2026-10-25", types=["health"],
         recall_count=0),
    evt(32, summary="主人刚把书桌重新布置了一遍，显示器升高了",
         date_start="2026-10-28", types=["daily"],
         recall_count=0),

    # ---------- 10. 敏感保留（1） ----------
    evt(33, summary="主人提到自己有焦虑倾向，在考虑是否看心理医生",
         date_start="2026-03-05", types=["health", "emotion"],
         retention="sensitive", importance=0.8,
         importance_reason="敏感信息，需谨慎处理",
         emotion="焦虑", emotion_intensity=0.8),
]


def main() -> None:
    cards = [EventCard.model_validate(e) for e in EVENTS]
    for c in cards:
        c.check_invariants()

    out = Path(__file__).resolve().parents[1] / "back_end" / "fixtures" / "events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = [c.model_dump(mode="json") for c in cards]
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"OK: {len(cards)} 条 → {out}")


if __name__ == "__main__":
    main()
