#!/usr/bin/env node
/**
 * 追踪一个关键词在导入链路的哪一层丢失（spec 047 排查工具）。
 *
 * ## 为什么需要它
 *
 * 「候潮」在 FTS 里 75 条，导入后原文表 0 条。要回答「怎么丢的」，
 * 必须逐层对照：FTS 索引 → 消息表 → 导出语料 → 原文表 → 卡片。
 * 手工查一次要 4 次不同工具，容易漏。
 *
 * ## 用法
 *
 *     node harness/trace_message.mjs --group 485490399 --keyword 候潮
 *     node harness/trace_message.mjs --group 485490399 --keyword 候潮 --corpus harness/corpus/qq-recent-bigfish/qq-485490399.json
 *
 * 输出会明确告诉你命中落在哪一层，以及**不在**哪一层。
 */

import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";

const PKG = process.env.QQ_MCP_PKG ?? "D:/nodejs/node_modules/@powercess/qq-mcp";
const DB_DIR = process.env.QQ_DB_DIR ?? "D:/TencentData/Chats/Tencent Files/505456846/nt_qq/nt_db";
const MSG_DB = `${DB_DIR}/nt_msg.db`;
const FTS_DB = `${DB_DIR}/group_msg_fts.db`;

const require = createRequire(import.meta.url);
const resolveSdk = (sub) =>
  pathToFileURL(require.resolve(`@modelcontextprotocol/sdk/${sub}`, { paths: [PKG] })).href;

function parseArgs(argv) {
  const out = { group: 0, keyword: "", corpus: "", limit: 2000 };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--group") out.group = Number(argv[++i]);
    else if (argv[i] === "--keyword") out.keyword = argv[++i];
    else if (argv[i] === "--corpus") out.corpus = argv[++i];
    else if (argv[i] === "--limit") out.limit = Number(argv[++i]);
  }
  if (!out.group || !out.keyword) throw new Error("需要 --group 和 --keyword");
  return out;
}

const args = parseArgs(process.argv.slice(2));

const { Client } = await import(resolveSdk("client/index.js"));
const { StdioClientTransport } = await import(resolveSdk("client/stdio.js"));
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [`${PKG}/dist/index.js`],
  env: Object.fromEntries(Object.entries(process.env).filter(([, v]) => typeof v === "string")),
});
const client = new Client({ name: "amarantine-trace", version: "0.1.0" });
await client.connect(transport);
const call = async (name, payload) => {
  const res = await client.callTool({ name, arguments: payload }, undefined, { timeout: 900000 });
  const text = res.content?.[0]?.text ?? "{}";
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
};

try {
  console.log(`\n追踪「${args.keyword}」@ 群 ${args.group}\n` + "=".repeat(60));

  // 第 1 层：FTS 索引
  const fts = await call("search_messages_fts", {
    dbPath: FTS_DB, target: "group", term: args.keyword, limit: 500,
  });
  const hits = (fts.hits ?? []).filter((h) => Number(h.conversationId) === args.group);
  console.log(`[1] FTS 索引命中: ${hits.length} 条`);
  const times = hits.map((h) => Number(h.timestamp));
  if (times.length) {
    console.log(`    时间范围: ${new Date(Math.min(...times) * 1000).toISOString()}`
      + ` → ${new Date(Math.max(...times) * 1000).toISOString()}`);
  }

  // 第 2 层：消息表（导出取数的来源）
  const pulled = await call("query_chat_messages", {
    dbPath: MSG_DB, chat: "group", groupQq: args.group, limit: args.limit, order: "desc",
  });
  const msgs = pulled.messages ?? [];
  const ids = new Set(msgs.map((m) => String(m.msgId)));
  const inTable = hits.filter((h) => ids.has(String(h.msgId)));
  console.log(`[2] 消息表最近 ${msgs.length} 条（scanned=${pulled.scanned}, truncated=${pulled.truncated}）`);
  console.log(`    其中含关键词(按 msgId 匹配): ${inTable.length} / ${hits.length}`);
  if (inTable.length) {
    for (const h of inTable.slice(0, 5)) {
      console.log(`    ✓ ${h.msgId} ${String(h.text).slice(0, 60)}`);
    }
  } else if (hits.length) {
    console.log("    ✗ FTS 命中在消息表里**一条都没有** → 丢在管线之前（源头）");
    // 时间最近邻，辅助判断是否为转发内容
    let near = 0;
    for (const h of hits) {
      const ht = Number(h.timestamp);
      const d = Math.min(...msgs.map((m) => Math.abs(Number(m.timestamp) - ht)));
      if (d <= 3) near += 1;
    }
    console.log(`    时间±3s 内有消息: ${near}/${hits.length}`);
  }

  // 第 3 层：导出语料（可选）
  if (args.corpus) {
    const corpus = JSON.parse(readFileSync(args.corpus, "utf8"));
    const hit = (corpus.messages ?? []).filter((m) => String(m.text || "").includes(args.keyword));
    console.log(`[3] 导出语料: ${(corpus.messages ?? []).length} 条 | 含关键词: ${hit.length}`);
  }
  console.log("\n[4] 原文表 / 卡片：用 SQLite 与 Weaviate 查询（见 trace_message 的 Python 侧）");
} catch (err) {
  console.log("追踪失败:", err.message);
  process.exitCode = 1;
} finally {
  await client.close();
}
