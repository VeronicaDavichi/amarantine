"""从 DingTalk 聊天记录导入真实工作记忆（spec 034）。

用法：
    python ../harness/seed_dingtalk.py --corpus data/corpus/dingtalk_smoke.json
    python ../harness/seed_dingtalk.py --corpus ... --namespace njzq-dingtalk
    python ../harness/seed_dingtalk.py --corpus ... --dry-run

## 为什么走 /admin/ingest 而不是 remember_event

`summary` 要的是「事件摘要」，把一段问答压成一句话是**理解**任务。
规则只能拼「谁说了什么」，那样产出的卡在召回时不会有好的表现，
而且 `summary` 参与 embedding，质量差会污染整个向量空间。

ingest 的 LLM 切分就是干这个的，现成的。它还顺带处理了
「原文先落库」「dedupe_key 幂等」「do_not_store 拦截」。

## 过滤策略（宁可多留，不要过滤太狠）

- `content_type != 1` 的纯图片消息丢掉（没有文本可切）
- 但 3100 / 1200 是**富文本/引用回复**，含真实内容，**必须保留**
  （一开始我以为非 1 都该丢，那样会丢掉大部分技术问答）
- 去掉 `[图片]`、`[捂脸哭]` 这类占位符
- 纯寒暄（`好的 谢谢` / `收到` / `OK`）丢掉
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import httpx

from memory.config import load_settings

#: 没有文本内容的类型（纯图片消息）
SKIP_TYPES = {2}
#: 含真实文本的类型：1 纯文本、1200 引用回复、3100 富文本
KEEP_TYPES = {1, 1200, 3100}

#: 占位符（图片、表情）—— 清掉，否则会进摘要
PLACEHOLDERS = ("[图片]", "[捂脸哭]", "[表情]", "[微笑]", "[OK]")

#: 纯寒暄：整条就这些词，没有信息量
GREETINGS = ("好的", "谢谢", "收到", "好滴", "嗯嗯", "ok", "OK", "好的 谢谢", "好的 谢谢!")


def clean(text: str) -> str:
    """去掉占位符和前后空白。"""
    out = text
    for ph in PLACEHOLDERS:
        out = out.replace(ph, "")
    return out.strip()


def is_noise(text: str) -> bool:
    """纯寒暄 / 太短 / 只有标点。"""
    t = text.strip()
    if len(t) < 4:
        return True
    if t in GREETINGS:
        return True
    # 去掉标点后没剩什么
    stripped = "".join(c for c in t if c.isalnum())
    return len(stripped) < 3


def collect_roster(data: dict) -> list[str]:
    """从语料里抽权威名册（spec 035）。

    三个来源，全部是**平台给的**，不需要用分词去猜：

    1. 群标题里的成员名（`陈柏林,王泳,任泽安,王洁` 逗号分隔）
    2. 每条消息的 `sender`（发言人显示名）
    3. 语料里显式给的 `_roster`（可选）

    **为什么要传给服务端**：`extract_entities` 跑在服务进程里，
    客户端注册名册没用（那是另一个进程的模块级状态）。
    """
    names: list[str] = []

    title = str(data.get("title") or "")
    for part in title.replace("，", ",").split(","):
        w = part.strip()
        # 群标题里可能有「工作通知:xxx」这种，只留像人名的
        if 2 <= len(w) <= 4 and not any(c in w for c in ":：()（）[]【】"):
            names.append(w)

    for m in data.get("messages") or []:
        sender = str(m.get("sender") or "").strip()
        if 2 <= len(sender) <= 4:
            names.append(sender)

    for n in data.get("_roster") or []:
        if isinstance(n, str) and n.strip():
            names.append(n.strip())

    # 去重保序
    seen: dict[str, None] = {}
    for n in names:
        seen.setdefault(n, None)
    return list(seen)


def merge_identity_roster(roster: list[str], data: dict, identity_path: str) -> list:
    """把身份记录并进名册（spec 043 T3）。

    只并入**本批出现的**人：语料里的 sender / 群标题里出现的名字。
    全量 474 条身份每次都带上会让每批注册几百个名字、写几百个 Account —— 没必要。

    返回的列表是混合形态：老格式的纯字符串 + 新格式的身份 dict。
    """
    from memory.services.identity import from_json_file

    idents = from_json_file(identity_path)
    if not idents:
        return roster

    appeared: set[str] = set()
    for m in data.get("messages") or []:
        sender = str(m.get("sender") or "").strip()
        if sender:
            appeared.add(sender)
    for part in str(data.get("title") or "").replace("，", ",").split(","):
        name = part.strip()
        if name:
            appeared.add(name)

    matched = [i for i in idents if set(i.candidate_names()) & appeared]
    if not matched:
        return roster

    covered: set[str] = set()
    for i in matched:
        covered |= set(i.candidate_names())
    plain = [n for n in roster if n not in covered]
    payload = [
        {
            "uid": i.uid,
            "nick": i.nick,
            "remark": i.remark,
            "real_name": i.real_name,
            "platform": i.platform,
            "source": "identity_file",
        }
        for i in matched
    ]
    print(f"  身份匹配: 本批出现 {len(appeared)} 人 → 命中身份记录 {len(matched)} 条")
    return plain + payload


def load_turns(path: pathlib.Path) -> tuple[str, str, list[dict]]:
    data = json.loads(path.read_text())
    cid = data["cid"]
    title = data.get("title") or cid

    turns = []
    for m in data["messages"]:
        ctype = m.get("type")
        raw = str(m.get("text") or "")
        if ctype in SKIP_TYPES:
            continue
        # 未知类型也保留 —— 宁可多留，别把有效信息当噪音丢掉
        text = clean(raw)
        if is_noise(text):
            continue
        turns.append(
            {
                "text": text,
                "role": "user",  # DingTalk 里每条都是人发的，没有 assistant
                "ts": m.get("ts"),
                # sender 拼进文本：ingest 的 turn 结构没有 sender 字段，
                # 但「谁说的」是重要的实体信号（真人名会进 entity_names）
                "_sender": m.get("sender"),
            }
        )

    # 把发言人拼进 text —— 这是唯一能保留「谁说的」的位置
    for t in turns:
        sender = t.pop("_sender", None)
        if sender:
            t["text"] = f"{sender}：{t['text']}"
    return cid, title, turns


def post_ingest(
    base_url: str,
    token: str,
    namespace: str,
    cid: str,
    turns: list[dict],
    suffix: str = "",
    roster: list | None = None,
    *,
    source: str = "dingtalk",
    key_prefix: str = "dt",
) -> dict:
    ts_values = [t["ts"] for t in turns if t.get("ts")]
    window = f"{min(ts_values)[:10]}:{max(ts_values)[:10]}" if ts_values else "now"
    # dedupe_key 带 cid 和窗口：同窗口重复提交返回已有 job。
    # **不能靠「不重复跑」**：ingest 里的 LLM 切分不确定，重跑会产出
    # 重复但 id 不同的卡（types 参与 event_id），等于污染。
    #
    # `suffix` 是给「上一个 job 失败了，想重试」用的。
    # 这暴露了一个设计问题：**IngestJobStore 对 failed job 也做幂等**，
    # 于是失败了必须换 key 才能重跑。理想行为应该是「failed 允许覆盖」，
    # 但那要改 core 逻辑，超出本次范围 —— 先记在 spec 的 Notes 里。
    dedupe_key = f"{key_prefix}:{cid}:{window}"
    if suffix:
        dedupe_key += f":{suffix}"

    body = {
        "source": source,
        "session_id": cid,
        "dedupe_key": dedupe_key,
        "turns": turns,
        "namespace": namespace,
        # 名册随请求走：服务端 `extract_entities` 才知道这些人名是完整的
        # （否则「陈柏林」会被切成「柏林」）
        "roster": roster or [],
    }
    r = httpx.post(
        f"{base_url}/admin/ingest",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    return {"status": r.status_code, "body": r.json() if r.content else {}}


def get_job(base_url: str, token: str, job_id: str) -> dict:
    r = httpx.get(
        f"{base_url}/admin/ingest/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    return r.json() if r.content else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="从 DingTalk 语料导入记忆")
    ap.add_argument("--corpus", required=True, help="语料 JSON 路径")
    ap.add_argument("--namespace", default="njzq-dingtalk")
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--dry-run", action="store_true", help="只打印不提交")
    ap.add_argument(
        "--wait",
        type=int,
        default=180,
        help="等 job 完成的最长秒数（默认 180）",
    )
    ap.add_argument(
        "--suffix",
        default="",
        help="dedupe_key 后缀。上一个 job 失败时用它重试（failed 也做幂等）",
    )
    ap.add_argument(
        "--identity",
        default="",
        help="身份记录 JSON（harness/export_identity.py 产出）。给了就带 uid 写 Account（spec 043）",
    )
    args = ap.parse_args()

    settings = load_settings()
    token = getattr(settings, "ADMIN_TOKEN", "") or ""
    if not token:
        print("✗ 没配 ADMIN_TOKEN，无法调管理接口")
        return 1

    path = pathlib.Path(args.corpus)
    if not path.exists():
        print(f"✗ 语料文件不存在: {path}")
        return 1

    data = json.loads(path.read_text())
    cid, title, turns = load_turns(path)
    roster = collect_roster(data)
    if args.identity:
        if not pathlib.Path(args.identity).exists():
            print(f"✗ 身份文件不存在: {args.identity}")
            return 1
        roster = merge_identity_roster(roster, data, args.identity)
    print("=" * 68)
    print(f"语料: {path.name}")
    print(f"  会话: {title} ({cid})")
    print(f"  保留 {len(turns)} / 原始若干 条消息")
    print(f"  namespace: {args.namespace}")
    _sample = [r if isinstance(r, str) else r.get("nick", "") for r in roster[:8]]
    print(f"  名册 {len(roster)} 个: {_sample}{'…' if len(roster) > 8 else ''}")
    print("=" * 68)
    for i, t in enumerate(turns[:3], 1):
        print(f"  [{i}] {t['ts']}  {t['text'][:56]}…")
    if len(turns) > 3:
        print(f"  ……还有 {len(turns) - 3} 条")

    if args.dry_run:
        print("\ndry-run，没有提交")
        return 0

    print("\n提交 /admin/ingest …")
    resp = post_ingest(
        args.base_url,
        token,
        args.namespace,
        cid,
        turns,
        suffix=args.suffix,
        roster=roster,
    )
    status = resp["status"]
    body = resp["body"]
    print(f"  HTTP {status}")
    print(f"  {json.dumps(body, ensure_ascii=False)[:200]}")

    if status >= 400:
        print("✗ 提交失败")
        return 1

    job_id = body.get("job_id")
    if not job_id:
        print(f"  没有 job_id（status={body.get('status')}），可能被 do_not_store 拦下")
        return 0

    print(f"\n等 job {job_id} 完成（最多 {args.wait}s）…")
    deadline = time.time() + args.wait
    last = None
    while time.time() < deadline:
        job = get_job(args.base_url, token, job_id)
        st = job.get("status")
        if st != last:
            print(f"  status={st}")
            last = st
        if st in ("succeeded", "failed", "done", "error"):
            print(f"\n最终状态: {st}")
            print(json.dumps(job, ensure_ascii=False)[:400])
            return 0 if st in ("succeeded", "done") else 1
        time.sleep(5)

    print(f"✗ 等了 {args.wait}s 还没完成，job 可能还在跑")
    return 1


if __name__ == "__main__":
    sys.exit(main())
