# -*- coding: utf-8 -*-
"""「我是谁」：把登录账号锚定成记忆里的「本人」（spec 050 延伸）。

## 为什么需要

导入的语料是**某个账号**看到的聊天记录。同一个 QQ 号在不同群有不同群名片，
所以「我说了什么」在图上是散在多个名字下的。没有「本人」这个概念时：

- 召回「我上次说的方案」只能靠猜名字
- 关系层抽出的主语可能是本人的某个群名片，但没人知道那是「我」

## 做法

配置 `SELF_QQ`（登录用的 QQ 号）→ 从权威成员表反查 `uid`（spec 043 的账号锚定），
再拿到这个账号在各群的显示名。**不做猜测**：查不到就是查不到，明确报「没配/没找到」。

## 为什么从成员表反查，不查图

成员表是 qq-mcp 导出的**权威数据**（`uid ↔ qq ↔ 名片`）；
图是派生层，可能被清理/重建。用权威源反查，结果才稳定。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("memory.self_identity")

#: 成员表目录（`harness/export_qq_identities.py` 的产物）
IDENTITY_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "corpus" / "qq-identities"
)


def _identity_dir(settings: Any = None) -> Path:
    """成员表目录；可用 `IDENTITY_DIR` 配置覆盖（测试用）。"""
    raw = getattr(settings, "IDENTITY_DIR", "") if settings is not None else ""
    return Path(str(raw)).expanduser() if raw else IDENTITY_DIR


def resolve_self(settings: Any = None, *, namespace: str | None = None) -> dict:
    """解析「本人」：QQ 号 → uid → 各群显示名。

    Args:
        settings: 配置（要 `SELF_QQ`，可选 `IDENTITY_DIR`）。
        namespace: 记忆域（只回填到结果里，便于调用方串起来）。

    Returns:
        `{"configured", "qq", "uid", "names", "namespaces_seen", "reason"}`。

        - `configured=False`：没配 `SELF_QQ`
        - `uid` 为空：配了但成员表里查不到这个 QQ 号
    """
    qq = str(getattr(settings, "SELF_QQ", "") or "").strip()
    out: dict[str, Any] = {
        "configured": bool(qq),
        "qq": qq,
        "uid": "",
        "names": [],
        "nicknames": [],
        "canonical": "",
        "namespaces_seen": [],
        "namespace": namespace or "",
        "reason": "",
    }
    if not qq:
        out["reason"] = "没有配置 SELF_QQ（登录用的 QQ 号）"
        return out

    idir = _identity_dir(settings)
    if not idir.is_dir():
        out["reason"] = f"成员表目录不存在：{idir}"
        return out

    uids: set[str] = set()
    names: set[str] = set()
    nicks: set[str] = set()
    groups: set[str] = set()
    for f in sorted(idir.glob("group-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 单个文件坏了跳过
            continue
        gid = str(data.get("groupQq") or f.stem.replace("group-", ""))
        for m in data.get("members") or []:
            if str(m.get("qq") or "") != qq:
                continue
            uid = str(m.get("uid") or "").strip()
            if uid:
                uids.add(uid)
            nick = str(m.get("nickname") or "").strip()
            if nick:
                nicks.add(nick)
            card = str(m.get("card") or "").strip()
            if card:
                names.add(card)
            groups.add(gid)

    if not uids:
        out["reason"] = f"成员表里没有 qq={qq} 的记录（先跑 export_qq_identities.py）"
        return out
    if len(uids) > 1:
        # 同一个 QQ 号出现多个 uid：可能是多开/换号，先取第一个并说明
        out["reason"] = f"该 QQ 号对应 {len(uids)} 个 uid，取第一个；请人工确认"
        logger.warning("SELF_QQ=%s 对应多个 uid：%s", qq, sorted(uids))

    out["uid"] = sorted(uids)[0]
    out["uids"] = sorted(uids)
    out["names"] = sorted(names)
    out["nicknames"] = sorted(nicks)
    out["namespaces_seen"] = sorted(groups)
    # **canonical 取账号昵称**（全局名），不是某个群名片 ——
    # 实测同一个号有 20 个群名片（含「海嗣」「苏苏大可爱」这类），
    # 按群名片挑会把 Person 名字设成随手改的那个。
    out["canonical"] = (preferred_name(sorted(nicks))
                        or preferred_name(sorted(names)) or qq)
    return out


def self_uids(settings: Any = None) -> set[str]:
    """本人可能对应的 uid 集合（给关系/身份层过滤用）。"""
    got = resolve_self(settings)
    return set(got.get("uids") or ([got["uid"]] if got.get("uid") else []))

#: 名字里允许的字符（汉字/假名/拉丁/数字）。其余算「装饰」。
import re as _re

_NAME_OK = _re.compile(r"[\u4e00-\u9fa5\u3040-\u30ffA-Za-z0-9]")


def preferred_name(names: list[str], *, fallback: str = "") -> str:
    """从一堆群名片里挑一个最像「名字」的。

    打分规则（低分优先）：
    1. 装饰字符（空格/点/符号）越少越好
    2. 长度 2–16 优先（太长多半是整活）
    3. 同分时取较短者

    **为什么需要**：同一个人 20 个群名片里常有几个是整活名
    （`.........大........群......呼....唤..........我........`），
    按字典序取第一个会把 Person 的名字设成那串。
    """
    best = ""
    best_score = None
    for raw in names or []:
        name = (raw or "").strip()
        if not name:
            continue
        junk = sum(1 for ch in name if not _NAME_OK.match(ch))
        too_long = 1 if not (2 <= len(name) <= 16) else 0
        score = (junk, too_long, len(name))
        if best_score is None or score < best_score:
            best, best_score = name, score
    return best or fallback
