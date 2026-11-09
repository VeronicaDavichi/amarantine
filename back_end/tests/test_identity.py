"""spec 043：身份来源分层（备注解析 / 非汉族姓名 / Account 写入）。

覆盖验收 A1（备注解析）、A6（非汉族姓名不建 Person），以及 T3/T4 的
`roster` 双形态兼容、T5 的 Account 幂等写入。
"""

from __future__ import annotations

import pytest

from memory.services import identity as I


# ------------------------------------------------------------ A1 备注解析


@pytest.mark.parametrize(
    "remark,name,note",
    [
        ("王晓虹 - 加班问题人事", "王晓虹", "加班问题人事"),
        ("王晓虹（合规）", "王晓虹", "合规"),
        ("王晓虹/合规", "王晓虹", "合规"),
        ("王晓虹", "王晓虹", ""),
        ("", "", ""),
    ],
)
def test_parse_remark(remark: str, name: str, note: str) -> None:
    """备注是「姓名 + 说明」时，说明不能混进人名。"""
    assert I.parse_remark(remark) == (name, note)


def test_parse_remark_keeps_long_note() -> None:
    """说明很长也不截断 —— 截断与否是显示层的事。"""
    assert I.parse_remark("韩晓迪 - 反洗钱系统接口对接人（外包）") == (
        "韩晓迪",
        "反洗钱系统接口对接人（外包）",
    )


def test_clean_nick_strips_company_prefix() -> None:
    """`蓝网-韩晓迪` → `韩晓迪`；不带前缀的原样返回。"""
    assert I.clean_nick("蓝网-韩晓迪") == "韩晓迪"
    assert I.clean_nick("韩晓迪") == "韩晓迪"
    assert I.clean_nick("王泳") == "王泳"


def test_display_name_priority() -> None:
    """备注姓名 > 真名 > 昵称（去前缀）。"""
    assert I.Identity(uid="1", nick="某公司-王泳", remark="王泳 - 前端").display_name == "王泳"
    assert I.Identity(uid="1", nick="某公司-王泳", real_name="王泳").display_name == "王泳"
    assert I.Identity(uid="1", nick="某公司-王泳").display_name == "王泳"


def test_candidate_names_include_raw_and_clean_nick() -> None:
    """名册要同时收全称和简称，否则群里叫哪个都可能漏。"""
    names = I.Identity(uid="1", nick="蓝网-韩晓迪").candidate_names()
    assert names == ["韩晓迪", "蓝网-韩晓迪"]


# ------------------------------------------------------------ A6 非汉族姓名


@pytest.mark.parametrize("name", ["阿卜杜拉·买买提", "阿拉法特江", "买买提·阿不都"])
def test_ethnic_names_detected(name: str) -> None:
    """少数民族姓名（含 `·` 或 ≥5 字非百家姓）→ 不建 Person。"""
    assert I.is_non_han(name) is True
    assert I.classify_identity_name(name)[0] == I.KIND_ETHNIC


@pytest.mark.parametrize("name", ["Gavin", "BOB洪亚军", "王政博Yves", "Phoebe.Ding"])
def test_english_nick_is_not_ethnic(name: str) -> None:
    """**回归**：含英文的昵称不是「非汉族姓名」。

    旧实现把「含字母」直接当成非汉族姓名，于是 `BOB洪亚军`、`Gavin` 这些
    真人被扫出名册 —— 8 个真人因此失去了成为 `Person` 的资格。
    """
    assert I.is_non_han(name) is False
    assert I.classify_identity_name(name)[0] == I.KIND_PERSON


def test_short_non_han_is_known_blind_spot() -> None:
    """`买买提江` 是 4 字全汉字、首字非百家姓 —— 判不出来。

    这是**已知盲区**，不是 bug：汉族姓名最多 4 字（复姓 + 双名），
    长度门槛只能设在 5，否则复姓名会被误伤。宁可漏。
    """
    assert I.is_non_han("买买提江") is False


@pytest.mark.parametrize("name", ["王泳", "韩晓迪", "陈柏林", "智小安", "欧阳锋", "蓝网-韩晓迪"])
def test_han_names_pass(name: str) -> None:
    """常见汉族姓名（含复姓、含公司前缀的昵称）不能被误挡。"""
    assert I.is_non_han(name) is False


# ------------------------------------------------------------ T3 roster 双形态


def test_split_roster_accepts_plain_names() -> None:
    """老格式（纯字符串）保持可用。"""
    names, idents, rejected = I.split_roster(["王泳", "韩晓迪"])
    assert names == ["王泳", "韩晓迪"]
    assert idents == []
    assert rejected == []


def test_split_roster_accepts_identity_dicts() -> None:
    """新格式带上 uid / 备注，名字从备注里来。"""
    names, idents, rejected = I.split_roster(
        [{"uid": "53660559", "nick": "蓝网-刘佳", "remark": "刘佳 - 广州合规"}]
    )
    assert "刘佳" in names
    assert "蓝网-刘佳" in names          # 原文也收
    assert idents[0].uid == "53660559"
    assert idents[0].display_name == "刘佳"
    assert rejected == []


def test_split_roster_rejects_non_han() -> None:
    """非汉族姓名不进名册，但要被报告出来（REQ-6）。"""
    names, _, rejected = I.split_roster(
        [{"uid": "1", "nick": "阿卜杜拉·买买提"}, "王泳"]
    )
    assert names == ["王泳"]
    assert rejected == [{"name": "阿卜杜拉·买买提", "kind": I.KIND_ETHNIC}]


def test_split_roster_drops_single_char() -> None:
    """单字不进名册（035 的既有约束）。"""
    names, _, _ = I.split_roster(["王", "泳"])
    assert names == []


def test_split_roster_dedupes() -> None:
    """同一个名字出现多次只收一次。"""
    names, _, _ = I.split_roster(["王泳", {"uid": "1", "remark": "王泳"}, "王泳"])
    assert names == ["王泳"]


# ------------------------------------------------------------ T5 Account 写入


class _Result:
    def consume(self) -> None:
        return None


class _Session:
    def __init__(self, driver: "_Driver") -> None:
        self.driver = driver

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, cypher: str, **params: object) -> _Result:
        self.driver.calls.append((cypher, params))
        return _Result()


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def session(self) -> _Session:
        return _Session(self)

    def close(self) -> None:
        return None


def test_ensure_identities_writes_account_and_edge() -> None:
    """一条身份 → 一条 Account + 一条 OPERATED_BY，参数齐全可回溯。"""
    drv = _Driver()
    stats = I.ensure_identities(
        namespace="ns1",
        identities=[I.Identity(uid="53660559", nick="蓝网-刘佳", remark="刘佳 - 广州合规")],
        settings=object(),
        driver=drv,
    )
    assert stats == {"accounts": 1, "linked": 1, "failed": 0}
    cypher, params = drv.calls[0]
    assert "MERGE (a:Account" in cypher
    assert "OPERATED_BY" in cypher
    assert params["uid"] == "53660559"
    assert params["display_name"] == "刘佳"
    assert params["key"]  # key_for(name) 非空


def test_ensure_identities_is_idempotent_by_uid() -> None:
    """两次调用发的是同一条 MERGE（幂等靠 cypher 语义，不靠调用方）。"""
    drv = _Driver()
    for _ in range(2):
        I.ensure_identities(
            namespace="ns1",
            identities=[I.Identity(uid="1", nick="王泳")],
            settings=object(),
            driver=drv,
        )
    assert len(drv.calls) == 2
    assert all("MERGE (a:Account" in c for c, _ in drv.calls)


def test_ensure_identities_skips_empty_uid() -> None:
    """没有 uid 的记录不写 —— 账号是锚，锚不能空。"""
    drv = _Driver()
    stats = I.ensure_identities(
        namespace="ns1",
        identities=[I.Identity(uid="", nick="王泳")],
        settings=object(),
        driver=drv,
    )
    assert stats["accounts"] == 0
    assert drv.calls == []


def test_same_nick_different_uid_creates_two_accounts() -> None:
    """同名不同 uid（3 个「智小安业务助手」）必须落成两个 Account，不能合并。

    Account 的 MERGE 键是 `(namespace, uid)`，昵称不参与身份判定 ——
    这正是 REQ-4 与「昵称会重名」的和解方式。
    """
    drv = _Driver()
    stats = I.ensure_identities(
        namespace="ns1",
        identities=[
            I.Identity(uid="111", nick="智小安业务助手"),
            I.Identity(uid="222", nick="智小安业务助手"),
        ],
        settings=object(),
        driver=drv,
    )
    assert stats["accounts"] == 2
    uids = [params["uid"] for _, params in drv.calls]
    assert uids == ["111", "222"]
    # spec 045：同名不同 uid 现在是**两个 Person**（键按 uid 派生），
    # Account 也是两个 —— 这才是「同名不同人」的正确形态
    keys = {params["key"] for _, params in drv.calls}
    assert keys == {"uid:111", "uid:222"}


def test_ensure_identities_swallows_driver_errors() -> None:
    """图写失败只告警（ADR D2），不能把 ingest 拖死。"""

    class Boom(_Driver):
        def session(self) -> _Session:
            raise RuntimeError("neo4j down")

    stats = I.ensure_identities(
        namespace="ns1",
        identities=[I.Identity(uid="1", nick="王泳")],
        settings=object(),
        driver=Boom(),
    )
    assert stats["failed"] == 1


# ------------------------------------------------------------ A6 返工：四分类


@pytest.mark.parametrize(
    "nick",
    ["钉钉小秘书", "AI助理", "工资条管家", "报表推送机器人", "OpenClaw业务助手"],
)
def test_service_accounts_are_not_person(nick: str) -> None:
    """服务号/机器人不是人 —— 按标记词判，不看字符构成。"""
    assert I.classify_identity_name(nick)[0] == I.KIND_SERVICE


@pytest.mark.parametrize(
    "nick,core",
    [
        ("BOB洪亚军", "洪亚军"),
        ("王政博Yves", "王政博"),
        ("22传32马雯卓", "马雯卓"),
    ],
)
def test_decorated_real_names_are_unwrapped(nick: str, core: str) -> None:
    """真人带英文/数字修饰 → 剥出中文姓名后照常入名册。"""
    kind, name = I.classify_identity_name(nick)
    assert kind == I.KIND_PERSON
    assert name == core


@pytest.mark.parametrize("nick", ["Gavin", "Phoebe.Ding", "nice"])
def test_plain_english_nick_counts_as_person(nick: str) -> None:
    """纯英文昵称默认是真人（可能是同事），不能因为「不像汉族姓名」被扫掉。"""
    assert I.classify_identity_name(nick)[0] == I.KIND_PERSON


@pytest.mark.parametrize("nick", ["aaa", "王", "李"])
def test_placeholder_is_dropped(nick: str) -> None:
    """无信息量的昵称（重复小写、单字）不入名册。"""
    assert I.classify_identity_name(nick)[0] == I.KIND_PLACEHOLDER


def test_roster_keeps_both_raw_and_core_name() -> None:
    """带修饰的真人：剥出来的中文名和原文都进名册（群里两种叫法都有）。"""
    names, _, _ = I.split_roster([{"uid": "1", "nick": "BOB洪亚军"}])
    assert "洪亚军" in names
    assert "BOB洪亚军" in names


def test_roster_rejection_carries_reason() -> None:
    """被挡的必须带原因，报告才可复核。"""
    _, _, rejected = I.split_roster(
        [{"uid": "1", "nick": "AI助理"}, {"uid": "2", "nick": "aaa"}]
    )
    assert {r["kind"] for r in rejected} == {I.KIND_SERVICE, I.KIND_PLACEHOLDER}
